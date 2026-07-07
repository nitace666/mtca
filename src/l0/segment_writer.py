"""L0-B 段落写入器（v0.4）

为指定 session 提供段落级 CRUD 入口，封装 ``skeleton`` + ``time_segmenter``
的写入逻辑，并提供灵活的查询 / 局部更新能力。

设计要点：
- ``write_segments`` 一次性写入 L0-骨架 + L0-B 段落：先 build+save_skeleton
  （幂等），再 segment_session + save_segments，返回全部 segment_id。
- ``get_segment`` / ``list_segments`` 提供只读查询入口，遵循 stores 单文件
  读写分离约定。
- ``update_segment`` 仅允许在 ``_UPDATABLE_FIELDS`` 白名单内的字段被
  修改，自动跳过不可变字段（topic_label / start_at / fog_anchor 由
  ``no_update_skeleton`` 触发器保护；segment_id / session_id /
  start_msg_seq 由主键 / 设计语义保护）。
- 所有 SQL 使用 ``?`` 占位符，列名走白名单，杜绝拼接注入风险。

公共 API：
- ``write_segments(session_id, path=None) -> list[str]``
- ``get_segment(segment_id, path=None) -> dict | None``
- ``list_segments(session_id=None, limit=20, path=None) -> list[dict]``
- ``update_segment(segment_id, path=None, **fields) -> int``
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, Union

from src.l0.skeleton import build_skeleton, save_skeleton
from src.l0.time_segmenter import save_segments, segment_session
from src.store.sqlite import get_connection, query

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 不可变字段白名单（更新时直接跳过；部分字段由触发器保护，部分由设计语义）
_IMMUTABLE_FIELDS: frozenset[str] = frozenset({
    "segment_id", "session_id", "start_msg_seq",
    "topic_label", "start_at", "fog_anchor",
})

# 允许更新的字段（与 sqlite.py segments 表 + no_update_skeleton 触发器对齐）
_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "end_msg_seq", "end_at", "gap_to_next", "weak_merged",
    "current_tier", "current_score",
    "fog_state", "fog_at",
    "silence_state", "promoted_at", "ref_count",
    "last_ask_at", "user_retention_days", "long_silent",
    "superseded_by", "supersedes_count", "contradiction_level",
    # M2.5.1 新加：4 象限评分 + 紧急跟踪
    "urgency_level", "importance_level", "emotion_tag",
    "expires_at_ms", "urgent_state",
})

# 合法 SQL 标识符（防 SQL 拼接注入的额外保险）
_SAFE_COL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# 默认与硬上限
_DEFAULT_LIMIT: int = 20
_MAX_LIMIT: int = 1000


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _coerce_limit(limit: Any) -> int:
    """将 limit 规整为合法正整数。"""
    if not isinstance(limit, int) or limit <= 0:
        return _DEFAULT_LIMIT
    return min(int(limit), _MAX_LIMIT)


def _coerce_value(col: str, value: Any) -> Any:
    """按列名将 Python 值规整为 SQLite 兼容类型。"""
    if value is None:
        return None
    if col in ("end_msg_seq", "gap_to_next", "ref_count",
               "supersedes_count", "user_retention_days",
               "long_silent", "expires_at_ms"):
        return int(value)
    if col in ("end_at", "fog_at", "last_ask_at", "promoted_at"):
        return int(value)
    if col in ("current_score", "urgency_level", "importance_level"):
        return float(value)
    if col in ("weak_merged",):
        return 1 if value else 0
    return str(value)


def _filter_update_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """筛选合法可更新字段；空值与不可变字段直接丢弃。"""
    out: dict[str, Any] = {}
    for col, val in fields.items():
        if col in _IMMUTABLE_FIELDS:
            continue
        if col not in _UPDATABLE_FIELDS:
            continue
        if not _SAFE_COL_RE.match(col):
            continue
        if val is None:
            continue
        out[col] = _coerce_value(col, val)
    return out


# ---------------------------------------------------------------------------
# 写入：L0-骨架 + L0-B 段落
# ---------------------------------------------------------------------------


def write_segments(
    session_id: str,
    path: Optional[Union[Path, str]] = None,
) -> list[str]:
    """为指定 session 一次性写入 L0-骨架 + L0-B 段落。

    复用：
        - ``skeleton.build_skeleton`` + ``save_skeleton``：写入 L0-骨架
        - ``time_segmenter.segment_session`` + ``save_segments``：写入 L0-B 段落

    返回：
        全部 segment_id 列表：先骨架 ID，再段落 ID（按时间顺序）。
        若该 session 无消息则只返回骨架 ID（骨架写入在消息数为 0 时
        也会写一条记录，end_msg_seq=1）。

    异常：
        RuntimeError：底层数据库写入失败。
    """
    # ---- 1. 骨架（幂等：同一 session 多次调用不会重复写入）----
    skeleton = build_skeleton(session_id, path=path)
    skel_id = save_skeleton(session_id, skeleton, path=path)

    # ---- 2. L0-B 段落 ----
    paragraphs = segment_session(session_id, path=path)
    para_ids = save_segments(session_id, paragraphs, path=path)

    # 骨架在前，段落按时间序
    return [skel_id] + para_ids


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def get_segment(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> Optional[dict]:
    """按 segment_id 查询单段落；不存在时返回 ``None``。

    返回的 dict 包含 segments 表全部列。
    """
    rows = query(
        "SELECT * FROM segments WHERE segment_id = ?",
        (segment_id,),
        path=path,
    )
    return rows[0] if rows else None


def list_segments(
    session_id: Optional[str] = None,
    limit: int = 20,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """列出段落。

    参数：
        session_id：若指定，则按 session 过滤；否则查全表。
        limit：返回最大条数，默认 20，硬上限 1000；非法值回落到 20。
        path：数据库路径。

    返回：
        list[dict]，按 ``start_at ASC, start_msg_seq ASC`` 排序。
    """
    safe_limit = _coerce_limit(limit)
    if session_id is not None:
        return query(
            "SELECT * FROM segments WHERE session_id = ? "
            "ORDER BY start_at ASC, start_msg_seq ASC LIMIT ?",
            (session_id, safe_limit),
            path=path,
        )
    return query(
        "SELECT * FROM segments "
        "ORDER BY start_at ASC, start_msg_seq ASC LIMIT ?",
        (safe_limit,),
        path=path,
    )


# ---------------------------------------------------------------------------
# 更新
# ---------------------------------------------------------------------------


def update_segment(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
    **fields: Any,
) -> int:
    """按 segment_id 局部更新可写字段，返回受影响行数（0 或 1）。

    参数：
        segment_id：目标段落 UUID。
        path：数据库路径。
        **fields：键为列名、值为新值的字段集合。

    行为：
        - 自动丢弃不可变字段（segment_id / session_id / start_msg_seq /
          topic_label / start_at / fog_anchor）。
        - 自动丢弃未知字段（非 ``_UPDATABLE_FIELDS`` 白名单）。
        - 自动丢弃 None 值的字段。
        - 数值 / 布尔字段按列名约定自动转换。
        - ``fields`` 为空时直接返回 0，不发 SQL。

    返回：
        受影响行数（0=未找到或无有效字段，1=更新成功）。

    异常：
        RuntimeError：底层数据库写入失败。
    """
    valid = _filter_update_fields(fields)
    if not valid:
        return 0

    set_clause = ", ".join(f"{col} = ?" for col in valid)
    params: list[Any] = list(valid.values()) + [segment_id]
    sql = f"UPDATE segments SET {set_clause} WHERE segment_id = ?"

    try:
        with get_connection(path) as conn:
            cur = conn.execute(sql, params)
            return int(cur.rowcount or 0)
    except RuntimeError:
        raise


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# M2.5.8 B：同步接口预留（云同步插座）
# ---------------------------------------------------------------------------

import logging  # noqa: E402  追加在文件末段
import time  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from src.sync.sync_adapter import SyncAdapter

log = logging.getLogger(__name__)


def sync_segment_to_adapter(
    segment_id: str,
    adapter=None,
    path=None,
) -> str:
    '''把指定 segment 同步到适配器（默认 LocalOnlySync no-op）。

    失败不抛异常（不阻塞本地操作），仅 warning log + 返回 ""。
    适配器未指定时使用 src.sync.get_adapter() 获取的全局实例。

    返回值：成功时为 adapter.push(data) 的返回值（通常是远端 id）；
    失败 / 段不存在时返回 ""。
    '''
    if adapter is None:
        from src.sync import get_adapter
        adapter = get_adapter()
    try:
        seg = get_segment(segment_id, path=path)
        if seg is None:
            log.warning("sync_segment_to_adapter: segment 不存在 id=%s", segment_id)
            return ""
        data = {
            "type": "segment",
            "id": segment_id,
            "payload": dict(seg),
            "ts": int(time.time() * 1000),
        }
        return adapter.push(data)
    except Exception as exc:
        log.warning("sync_segment_to_adapter 失败 id=%s err=%s", segment_id, exc)
        return ""

__all__ = [
    "write_segments",
    "get_segment",
    "list_segments",
    "update_segment",
    "sync_segment_to_adapter",
]
