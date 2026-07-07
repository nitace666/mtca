"""方案 C：简化 CRDT（字段级 timestamp merge）。

设计：
- sidecar 表 sync_field_ts：(segment_id, field_name) 复合主键
  每行存 (ts, value, device_id, schema_version)
- push：把 payload 的每个字段拆成一行 sync_field_ts
- pull：从 peer 拉所有行，与本地逐 (seg, field) 比较 ts
    - peer.ts > local.ts -> 用 peer.value 覆盖
    - peer.ts < local.ts -> 保留本地
    - ts 平局 -> device_id 字典序大者赢
- schema_version 不匹配 -> 字段级 merge（warnings 累加，前向兼容）

优缺点：
    [+] 字段级冲突解决（最细粒度）
    [+] schema 演进友好（前向兼容）
    [+] 扩展性高
    [-] 每个字段一次 SQL（写放大 ~22x）

仅 spike 用，不进生产。
"""

from __future__ import annotations
import sqlite3, time
from pathlib import Path
from typing import Optional, Union

SCHEMA_VERSION: str = "v0.4"

_SEGMENTS_DDL = "CREATE TABLE IF NOT EXISTS segments (segment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, start_msg_seq INTEGER NOT NULL, end_msg_seq INTEGER NOT NULL, start_at INTEGER NOT NULL, end_at INTEGER NOT NULL, topic_label TEXT, current_tier TEXT DEFAULT 'L0', current_score REAL DEFAULT 100);"

_SYNC_FIELD_TS_DDL = """
CREATE TABLE IF NOT EXISTS sync_field_ts (
    segment_id TEXT NOT NULL, field_name TEXT NOT NULL, value TEXT,
    ts INTEGER NOT NULL, device_id TEXT NOT NULL, schema_version TEXT NOT NULL,
    PRIMARY KEY (segment_id, field_name));
CREATE INDEX IF NOT EXISTS idx_sync_field_ts_seg ON sync_field_ts(segment_id);
"""

# segments 表可写列名（与 _SEGMENTS_DDL 对齐；不可变字段在 _IMMUTABLE 中）
_SEG_COLS: tuple[str, ...] = (
    "session_id", "start_msg_seq", "end_msg_seq", "start_at", "end_at",
    "topic_label", "current_tier", "current_score",
)
_INT_FIELDS: frozenset[str] = frozenset(
    {"start_msg_seq", "end_msg_seq", "start_at", "end_at"})
_FLOAT_FIELDS: frozenset[str] = frozenset({"current_score"})


def init_sync_field_ts(conn: sqlite3.Connection) -> None:
    conn.executescript(_SYNC_FIELD_TS_DDL)
    conn.commit()


class CrdtSimpleAdapter:
    """简化 CRDT 字段级 merge 适配器（spike 原型）。"""

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
        self._warnings: list[str] = []

    def _connect(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def _sleep(self) -> None:
        if self.network_delay_ms > 0:
            time.sleep(self.network_delay_ms / 1000.0)

    def _coerce(self, field: str, value) -> object:
        if value is None:
            return None
        if field in _INT_FIELDS:
            return int(value)
        if field in _FLOAT_FIELDS:
            return float(value)
        return str(value)

    def init(self) -> None:
        conn = self._connect(self.local_db)
        try:
            conn.executescript(_SEGMENTS_DDL)
            init_sync_field_ts(conn)
        finally:
            conn.close()

    def _write_field(self, conn, seg_id, field, value, ts, device_id, schema_version) -> None:
        conn.execute(
            "INSERT INTO sync_field_ts (segment_id, field_name, value, ts, device_id, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(segment_id, field_name) DO UPDATE SET "
            "value=excluded.value, ts=excluded.ts, device_id=excluded.device_id, "
            "schema_version=excluded.schema_version "
            "WHERE excluded.ts > sync_field_ts.ts "
            "OR (excluded.ts = sync_field_ts.ts AND excluded.device_id > sync_field_ts.device_id)",
            (seg_id, field, self._coerce(field, value), ts, device_id, schema_version),
        )

    def _read_field(self, conn, seg_id, field):
        return conn.execute(
            "SELECT value, ts, device_id, schema_version FROM sync_field_ts "
            "WHERE segment_id = ? AND field_name = ?",
            (seg_id, field),
        ).fetchone()

    def push(self, data: dict) -> str:
        self._sleep()
        seg_id = data["id"]
        payload = data.get("payload") or {}
        ts = int(data.get("ts") or int(time.time() * 1000))
        conn = self._connect(self.local_db)
        try:
            for field, value in payload.items():
                if field == "segment_id":
                    continue
                self._write_field(conn, seg_id, field, value, ts, self.device_id, self.schema_version)
            # 写完字段后重建 segments 行（让 local 能 SELECT 到）
            vals = {}
            for col in _SEG_COLS:
                row = self._read_field(conn, seg_id, col)
                if row is not None:
                    vals[col] = row["value"]
            if vals:
                placeholders = ",".join("?" for _ in vals)
                update_set = ",".join(f"{c}=excluded.{c}" for c in vals)
                conn.execute(
                    f"INSERT INTO segments (segment_id,{','.join(vals.keys())}) "
                    f"VALUES (?,{placeholders}) "
                    f"ON CONFLICT(segment_id) DO UPDATE SET {update_set}",
                    [seg_id] + [vals[c] for c in vals],
                )
            conn.commit()
            self._last_sync_ms = max(self._last_sync_ms, ts)
            return seg_id
        finally:
            conn.close()

    def _merge_field(self, local, seg_id, field, peer_value, peer_ts, peer_dev, peer_sv) -> str:
        if peer_sv != self.schema_version:
            self._warnings.append(
                f"schema 不匹配 seg={seg_id} field={field} peer={peer_sv} local={self.schema_version} 字段级 merge"
            )
        local_row = self._read_field(local, seg_id, field)
        if local_row is None:
            self._write_field(local, seg_id, field, peer_value, peer_ts, peer_dev, peer_sv)
            return "insert"
        local_ts, local_dev = int(local_row["ts"]), local_row["device_id"]
        if peer_ts > local_ts:
            self._write_field(local, seg_id, field, peer_value, peer_ts, peer_dev, peer_sv)
            return "overwrite"
        if peer_ts < local_ts:
            return "skip_local_newer"
        if peer_dev > local_dev:
            self._write_field(local, seg_id, field, peer_value, peer_ts, peer_dev, peer_sv)
            return "overwrite_tie"
        return "skip_dev_tie"

    def pull(self, since_ms: int = 0) -> list[dict]:
        self._sleep()
        peer = self._connect(self.peer_db)
        local = self._connect(self.local_db)
        result: list[dict] = []
        try:
            rows = peer.execute(
                "SELECT segment_id, field_name, value, ts, device_id, schema_version "
                "FROM sync_field_ts WHERE ts > ? ORDER BY ts ASC",
                (since_ms,),
            ).fetchall()
            per_seg: dict[str, list] = {}
            for r in rows:
                per_seg.setdefault(r["segment_id"], []).append(r)
            for seg_id, fields in per_seg.items():
                actions = []
                for f in fields:
                    act = self._merge_field(
                        local, seg_id, f["field_name"], f["value"],
                        int(f["ts"]), f["device_id"], f["schema_version"],
                    )
                    actions.append(f"{f['field_name']}:{act}")
                # 重建 segments 行
                vals: dict[str, object] = {}
                for col in _SEG_COLS:
                    row = self._read_field(local, seg_id, col)
                    if row is not None:
                        vals[col] = row["value"]
                if vals:
                    placeholders = ",".join("?" for _ in vals)
                    update_set = ",".join(f"{c}=excluded.{c}" for c in vals)
                    local.execute(
                        f"INSERT INTO segments (segment_id,{','.join(vals.keys())}) "
                        f"VALUES (?,{placeholders}) "
                        f"ON CONFLICT(segment_id) DO UPDATE SET {update_set}",
                        [seg_id] + [vals[c] for c in vals],
                    )
                max_ts = max(int(f["ts"]) for f in fields)
                self._last_sync_ms = max(self._last_sync_ms, max_ts)
                result.append({"id": seg_id, "actions": actions, "ts": max_ts})
            local.commit()
            return result
        finally:
            peer.close()
            local.close()

    def status(self) -> dict:
        return {"mode": "crdt_simple", "last_sync_ms": self._last_sync_ms,
                "pending": self._pending, "errors": list(self._errors),
                "warnings": list(self._warnings)}


__all__ = ["CrdtSimpleAdapter", "init_sync_field_ts", "SCHEMA_VERSION"]
