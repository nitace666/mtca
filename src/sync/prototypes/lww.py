"""方案 A：Last-Write-Wins（LWW）+ 共享 sidecar 表 sync_meta。

设计：
- 每条 segment 同步时，sidecar 表 sync_meta 维护 updated_at_ms
- push：写本地 segments + sync_meta（upsert）
- pull：从 peer.sync_meta 拉 ts > since_ms 的段，按 segment_id merge 到 local
    - 同段 local.ts > peer.ts -> 保留 local（本地优先）
    - local.ts < peer.ts -> 用 peer 覆盖
    - ts 平局 -> device_id 字典序大者赢（确定性）
- schema_version 不匹配 -> 拒绝（status errors 累加）

优缺点：
    [+] 实现最简，无外部依赖
    [+] push 是单 SQL 事务，快
    [-] 同时改同字段时一边丢失（粗粒度）
    [-] schema 演进需要强制同步

仅 spike 用，不进生产。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional, Union

# Schema 版本：M3-0 spike 假定 M2.5=v0.4（spec gap，见 spike 报告）
SCHEMA_VERSION: str = "v0.4"

_SEGMENTS_DDL: str = """
CREATE TABLE IF NOT EXISTS segments (
    segment_id    TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    start_msg_seq INTEGER NOT NULL,
    end_msg_seq   INTEGER NOT NULL,
    start_at      INTEGER NOT NULL,
    end_at        INTEGER NOT NULL,
    topic_label   TEXT,
    current_tier  TEXT DEFAULT 'L0',
    current_score REAL DEFAULT 100
);
"""

_SYNC_META_DDL: str = """
CREATE TABLE IF NOT EXISTS sync_meta (
    segment_id     TEXT PRIMARY KEY,
    updated_at_ms  INTEGER NOT NULL,
    device_id      TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    op             TEXT NOT NULL DEFAULT 'upsert'
);
CREATE INDEX IF NOT EXISTS idx_sync_meta_ts ON sync_meta(updated_at_ms);
"""


def init_sync_meta(conn: sqlite3.Connection) -> None:
    """初始化 sidecar sync_meta 表。"""
    conn.executescript(_SYNC_META_DDL)
    conn.commit()


class LwwAdapter:
    """LWW 同步适配器（spike 原型）。"""

    def __init__(
        self,
        local_db: Union[Path, str],
        peer_db: Optional[Union[Path, str]] = None,
        device_id: str = "A",
        schema_version: str = SCHEMA_VERSION,
        network_delay_ms: int = 0,
    ) -> None:
        self.local_db = str(local_db)
        self.peer_db = str(peer_db) if peer_db is not None else self.local_db
        self.device_id = device_id
        self.schema_version = schema_version
        self.network_delay_ms = network_delay_ms
        self._last_sync_ms: int = 0
        self._pending: int = 0
        self._errors: list[str] = []

    def _connect(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def _sleep(self) -> None:
        if self.network_delay_ms > 0:
            time.sleep(self.network_delay_ms / 1000.0)

    def init(self) -> None:
        """建 segments + sync_meta on local_db。"""
        conn = self._connect(self.local_db)
        try:
            conn.executescript(_SEGMENTS_DDL)
            init_sync_meta(conn)
        finally:
            conn.close()

    def _upsert(self, conn, seg_id, payload, ts, device_id, schema_version):
        # partial payload: 已有 -> UPDATE 局部; 新增 -> INSERT (要求 payload 含 NOT NULL 字段)
        cols = [c for c in payload.keys() if c != "segment_id"]
        if cols:
            existing = conn.execute(
                "SELECT 1 FROM segments WHERE segment_id = ?", (seg_id,)
            ).fetchone()
            if existing is None:
                placeholders = ",".join("?" for _ in cols)
                update_set = ",".join(f"{c}=excluded.{c}" for c in cols)
                conn.execute(
                    f"INSERT INTO segments (segment_id,{','.join(cols)}) "
                    f"VALUES (?,{placeholders}) "
                    f"ON CONFLICT(segment_id) DO UPDATE SET {update_set}",
                    [seg_id] + [payload[c] for c in cols],
                )
            else:
                set_clause = ",".join(f"{c}=?" for c in cols)
                conn.execute(
                    f"UPDATE segments SET {set_clause} WHERE segment_id = ?",
                    [payload[c] for c in cols] + [seg_id],
                )
        conn.execute(
            "INSERT INTO sync_meta (segment_id, updated_at_ms, device_id, schema_version, op) "
            "VALUES (?, ?, ?, ?, 'upsert') "
            "ON CONFLICT(segment_id) DO UPDATE SET "
            "updated_at_ms=excluded.updated_at_ms, "
            "device_id=excluded.device_id, "
            "schema_version=excluded.schema_version, "
            "op='upsert'",
            (seg_id, ts, device_id, schema_version),
        )

    def push(self, data: dict) -> str:
        """写一条 segment 到 local_db。data={id, payload, ts}。"""
        self._sleep()
        seg_id = data["id"]
        payload = data.get("payload") or {}
        ts = int(data.get("ts") or int(time.time() * 1000))
        conn = self._connect(self.local_db)
        try:
            self._upsert(conn, seg_id, payload, ts, self.device_id, self.schema_version)
            conn.commit()
            self._last_sync_ms = max(self._last_sync_ms, ts)
            return seg_id
        except sqlite3.Error as exc:
            self._errors.append(f"push 失败 {seg_id}: {exc}")
            raise
        finally:
            conn.close()

    def pull(self, since_ms: int = 0) -> list[dict]:
        """从 peer_db 拉 ts > since_ms 的段，merge 到 local_db。"""
        self._sleep()
        peer = self._connect(self.peer_db)
        local = self._connect(self.local_db)
        result: list[dict] = []
        try:
            rows = peer.execute(
                "SELECT segment_id, updated_at_ms, device_id, schema_version "
                "FROM sync_meta WHERE updated_at_ms > ? ORDER BY updated_at_ms ASC",
                (since_ms,),
            ).fetchall()
            for r in rows:
                seg_id, peer_ts, peer_dev, peer_sv = r["segment_id"], int(r["updated_at_ms"]), r["device_id"], r["schema_version"]
                if peer_sv != self.schema_version:
                    self._errors.append(f"schema 不匹配 seg={seg_id} peer={peer_sv} local={self.schema_version}")
                    continue
                seg = peer.execute("SELECT * FROM segments WHERE segment_id = ?", (seg_id,)).fetchone()
                if seg is None:
                    continue
                local_meta = local.execute(
                    "SELECT updated_at_ms, device_id FROM sync_meta WHERE segment_id = ?",
                    (seg_id,),
                ).fetchone()
                if local_meta is None:
                    action = "insert"
                    self._upsert(local, seg_id, dict(seg), peer_ts, peer_dev, peer_sv)
                else:
                    local_ts, local_dev = int(local_meta["updated_at_ms"]), local_meta["device_id"]
                    if local_ts > peer_ts:
                        action = "skip_local_newer"
                    elif local_ts < peer_ts:
                        action = "overwrite"
                        self._upsert(local, seg_id, dict(seg), peer_ts, peer_dev, peer_sv)
                    elif local_dev > peer_dev:
                        action = "skip_dev_tie"
                    else:
                        action = "overwrite_tie"
                        self._upsert(local, seg_id, dict(seg), peer_ts, peer_dev, peer_sv)
                result.append({"id": seg_id, "ts": peer_ts, "device_id": peer_dev, "action": action})
                self._last_sync_ms = max(self._last_sync_ms, peer_ts)
            local.commit()
            return result
        finally:
            peer.close()
            local.close()

    def status(self) -> dict:
        return {
            "mode": "lww",
            "last_sync_ms": self._last_sync_ms,
            "pending": self._pending,
            "errors": list(self._errors),
        }


__all__ = ["LwwAdapter", "init_sync_meta", "SCHEMA_VERSION"]
