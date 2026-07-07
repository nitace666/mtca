"""方案 B：Litestream-like（WAL frame 思路，不装本体）。

设计：
- sidecar 表 sync_wal：每条 segment 变更 = 一行 frame（保留全部历史）
- sidecar 表 sync_wal_consumed：标记已 replay 的 frame_id
- push：写一行 frame 到 local.sync_wal
- pull：从 peer.sync_wal 拉未消费 frame，按 frame_ts DESC 倒序 replay
  （后写后赢，等价 LWW 语义但 frame 都被保留）
- schema_version 不匹配 -> 强 replay（warnings 累加；新字段缺失用 COALESCE 保留旧值）

优缺点：
    [+] 保留所有历史变更（可审计）
    [-] sync_wal 增长无界（生产需裁剪任务 / 外部 daemon）
    [-] 部署复杂度高

仅 spike 用，不进生产。
"""

from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from typing import Optional, Union

SCHEMA_VERSION: str = "v0.4"

_SEGMENTS_DDL = "CREATE TABLE IF NOT EXISTS segments (segment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, start_msg_seq INTEGER NOT NULL, end_msg_seq INTEGER NOT NULL, start_at INTEGER NOT NULL, end_at INTEGER NOT NULL, topic_label TEXT, current_tier TEXT DEFAULT 'L0', current_score REAL DEFAULT 100);"

_SYNC_WAL_DDL = """
CREATE TABLE IF NOT EXISTS sync_wal (frame_id INTEGER PRIMARY KEY AUTOINCREMENT, segment_id TEXT NOT NULL, frame_ts INTEGER NOT NULL, device_id TEXT NOT NULL, schema_version TEXT NOT NULL, op TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sync_wal_ts ON sync_wal(frame_ts);
CREATE TABLE IF NOT EXISTS sync_wal_consumed (frame_id INTEGER NOT NULL, device_id TEXT NOT NULL, PRIMARY KEY (frame_id, device_id));
"""


def init_sync_wal(conn: sqlite3.Connection) -> None:
    conn.executescript(_SYNC_WAL_DDL)
    conn.commit()


class LitestreamLikeAdapter:
    """Litestream 思路同步适配器（spike 原型）。"""

    def __init__(self, local_db, peer_db=None, device_id="A",
                 schema_version=SCHEMA_VERSION, network_delay_ms=0):
        self.local_db = str(local_db)
        self.peer_db = str(peer_db) if peer_db is not None else self.local_db
        self.device_id = device_id
        self.schema_version = schema_version
        self.network_delay_ms = network_delay_ms
        self._last_sync_ms = 0
        self._pending = 0
        self._errors: list[str] = []
        self._warnings: list[str] = ["Litestream-like 提示：sync_wal 无界增长，生产需裁剪任务"]

    def _connect(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def _sleep(self) -> None:
        if self.network_delay_ms > 0:
            time.sleep(self.network_delay_ms / 1000.0)

    def init(self) -> None:
        conn = self._connect(self.local_db)
        try:
            conn.executescript(_SEGMENTS_DDL)
            init_sync_wal(conn)
        finally:
            conn.close()

    def push(self, data: dict) -> str:
        self._sleep()
        seg_id = data["id"]
        payload = data.get("payload") or {}
        ts = int(data.get("ts") or int(time.time() * 1000))
        conn = self._connect(self.local_db)
        try:
            # 1. 写 sync_wal frame（事件日志）
            cur = conn.execute(
                "INSERT INTO sync_wal (segment_id, frame_ts, device_id, schema_version, op, payload_json) "
                "VALUES (?, ?, ?, ?, 'upsert', ?)",
                (seg_id, ts, self.device_id, self.schema_version,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
            # 2. 同步 upsert segments（让 local 能 SELECT 到；partial payload 走 UPDATE）
            cols = [c for c in payload.keys() if c != "segment_id"]
            if cols:
                existing = conn.execute("SELECT 1 FROM segments WHERE segment_id = ?", (seg_id,)).fetchone()
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
                    conn.execute(f"UPDATE segments SET {set_clause} WHERE segment_id = ?",
                                 [payload[c] for c in cols] + [seg_id])
            conn.commit()
            self._last_sync_ms = max(self._last_sync_ms, ts)
            return f"frame-{cur.lastrowid}"
        finally:
            conn.close()

    def pull(self, since_ms: int = 0) -> list[dict]:
        self._sleep()
        peer = self._connect(self.peer_db)
        local = self._connect(self.local_db)
        result: list[str] = []
        try:
            rows = peer.execute(
                "SELECT w.frame_id, w.segment_id, w.frame_ts, w.device_id, w.schema_version, w.payload_json "
                "FROM sync_wal w LEFT JOIN sync_wal_consumed c "
                "  ON w.frame_id = c.frame_id AND w.device_id = c.device_id "
                "WHERE w.frame_ts > ? AND c.frame_id IS NULL "
                "ORDER BY w.frame_ts DESC, w.frame_id DESC",
                (since_ms,),
            ).fetchall()
            for r in rows:
                seg_id, frame_ts, peer_dev, peer_sv = r["segment_id"], int(r["frame_ts"]), r["device_id"], r["schema_version"]
                if peer_sv != self.schema_version:
                    self._warnings.append(f"schema 不匹配 seg={seg_id} peer={peer_sv} local={self.schema_version} 强 replay")
                payload = json.loads(r["payload_json"])
                cols = [c for c in payload.keys() if c != "segment_id"]
                if cols:
                    placeholders = ",".join("?" for _ in cols)
                    update_set = ",".join(f"{c}=COALESCE(excluded.{c}, segments.{c})" for c in cols)
                    local.execute(
                        f"INSERT INTO segments (segment_id,{','.join(cols)}) "
                        f"VALUES (?,{placeholders}) ON CONFLICT(segment_id) DO UPDATE SET {update_set}",
                        [seg_id] + [payload[c] for c in cols],
                    )
                local.execute("INSERT OR IGNORE INTO sync_wal_consumed (frame_id, device_id) VALUES (?, ?)", (r["frame_id"], r["device_id"]))
                local.execute(
                    "INSERT OR IGNORE INTO sync_wal (frame_id, segment_id, frame_ts, device_id, schema_version, op, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, 'upsert', ?)",
                    (r["frame_id"], seg_id, frame_ts, peer_dev, peer_sv,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True)),
                )
                result.append({"id": seg_id, "ts": frame_ts, "device_id": peer_dev, "frame_id": r["frame_id"]})
                self._last_sync_ms = max(self._last_sync_ms, frame_ts)
            local.commit()
            return result
        finally:
            peer.close()
            local.close()

    def status(self) -> dict:
        return {"mode": "litestream_like", "last_sync_ms": self._last_sync_ms,
                "pending": self._pending, "errors": list(self._errors), "warnings": list(self._warnings)}


__all__ = ["LitestreamLikeAdapter", "init_sync_wal", "SCHEMA_VERSION"]
