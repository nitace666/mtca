"""src/lifecycle/urgent_tracker.py — M2.5.3 "永不遗忘"紧急追踪核心

3 状态机：tracking（追踪中）→ expired（已过期，等响应）→ {completed, postponed, important}

满足 MTCA 第 9 铁律（"重要记忆永不遗忘"）：用户标记紧急的段落，到期后强制询问
"完成/延期/重要"，不是悄悄把遗忘。

公共 API：
- ``track_urgent(segment_id, expires_at_ms, anchor=None, path=None)``
- ``tick_urgent(path=None, now_ms=None) -> list[str]``
- ``handle_urgent_response(segment_id, response, path=None, now_ms=None)``
- ``list_expired(path=None) -> list[dict]``
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional, Union

from src.compress.regen_engine import schedule_regen
from src.l0.segment_writer import get_segment, update_segment
from src.store.sqlite import MTCA_DB_PATH, query


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 延期默认延后天数
POSTPONE_DAYS: int = 7
# 用户紧急标记的 urgency_level（Q1 入口）
URGENT_URGENCY_LEVEL: float = 1.0

# 合法响应集（小写归一化后）
_VALID_RESPONSES: frozenset[str] = frozenset({"completed", "postponed", "important"})


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """返回当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _resolve_path(path: Optional[Union[Path, str]]) -> Union[Path, str]:
    """None → 默认 DB 路径。"""
    return Path(path) if path else MTCA_DB_PATH


def _coerce_ms(value: Any) -> int:
    """把传入时间戳规整为 int 毫秒。"""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def track_urgent(
    segment_id: str,
    expires_at_ms: int,
    anchor: Optional[str] = None,
    path: Optional[Union[Path, str]] = None,
) -> dict[str, Any]:
    """标记段为用户追踪（urgent_state='tracking'）。

    自动设置：
    - urgent_state='tracking'
    - expires_at_ms=用户给的过期时间
    - urgency_level=1.0（最高，强制进 Q1）
    - anchor：被 update_segment 静默丢弃（fog_anchor 字段由 L0-骨架 触发器保护，
      不可写；保留入参以兼容上层调用，原意义上 urgent 的"锚点"由 urgency_level/expires_at_ms 已表达）

    返回 dict：{segment_id, urgent_state, expires_at_ms, urgency_level}
    """
    db_path = _resolve_path(path)
    update_segment(
        segment_id,
        urgent_state="tracking",
        expires_at_ms=_coerce_ms(expires_at_ms),
        urgency_level=URGENT_URGENCY_LEVEL,
        # fog_anchor=anchor,   # 见上方 docstring：触发器 + 白名单拦截，保留入参签名
        path=db_path,
    )
    return {
        "segment_id": segment_id,
        "urgent_state": "tracking",
        "expires_at_ms": _coerce_ms(expires_at_ms),
        "urgency_level": URGENT_URGENCY_LEVEL,
    }


def tick_urgent(
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> list[str]:
    """每轮（被 scoring.tick 调用）扫所有 tracking 段。

    已过期的 → urgent_state='expired'，返回新过期 segment_id 列表。
    未到期或 urgent_state 非 tracking 的段不动。
    """
    db_path = _resolve_path(path)
    now = _coerce_ms(now_ms) if now_ms else _now_ms()

    rows = query(
        "SELECT segment_id FROM segments "
        "WHERE urgent_state = 'tracking' "
        "AND expires_at_ms IS NOT NULL AND expires_at_ms <= ?",
        (now,),
        path=db_path,
    )
    ids: list[str] = []
    for row in rows:
        sid = row["segment_id"]
        update_segment(sid, urgent_state="expired", path=db_path)
        ids.append(sid)
    return ids


def list_expired(
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """返回当前所有 urgent_state='expired' 的段落。

    返回 dict 列表，每行包含 segment_id / topic_label / fog_anchor / expires_at_ms。
    上层（GUI/CLI）展示给用户响应。
    """
    db_path = _resolve_path(path)
    rows = query(
        "SELECT segment_id, topic_label, fog_anchor, expires_at_ms "
        "FROM segments WHERE urgent_state = 'expired' "
        "ORDER BY expires_at_ms ASC",
        path=db_path,
    )
    return [dict(r) for r in rows]


def handle_urgent_response(
    segment_id: str,
    response: str,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> dict[str, Any]:
    """用户对 expired/tracking 段的响应。

    response: 'completed' | 'postponed' | 'important'（大小写不敏感）

    - completed: 标 urgent_state='completed', current_tier='L2'，触发 L2 摘要重写
    - postponed: 重置 expires_at_ms = now + 7d，urgent_state 回 tracking
    - important: 标 urgent_state='important' + 调 mark_important()（score=10000，冻结）

    返回 dict：{segment_id, new_state, tier, expires_at_ms?, score?}
    """
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("segment_id 必须是非空字符串")
    if not isinstance(response, str):
        raise ValueError("response 必须是字符串")

    db_path = _resolve_path(path)
    now = _coerce_ms(now_ms) if now_ms else _now_ms()
    seg = get_segment(segment_id, path=db_path)
    if not seg:
        raise ValueError(f"segment 不存在：{segment_id}")

    state = seg.get("urgent_state")
    if state not in ("expired", "tracking"):
        raise ValueError(
            f"segment urgent_state={state!r} 不是 expired/tracking，无法响应"
        )

    norm = response.strip().lower()
    current_tier = seg.get("current_tier") or "L0"

    if norm == "completed":
        update_segment(
            segment_id,
            urgent_state="completed",
            current_tier="L2",
            path=db_path,
        )
        try:
            schedule_regen(segment_id, reason="fog", path=db_path)
        except Exception:
            pass  # regen 失败不阻塞响应
        return {
            "segment_id": segment_id,
            "new_state": "completed",
            "tier": "L2",
            "current_tier": "L2",
        }
    elif norm == "postponed":
        new_expires = now + POSTPONE_DAYS * 86400 * 1000
        update_segment(
            segment_id,
            urgent_state="tracking",
            expires_at_ms=new_expires,
            path=db_path,
        )
        return {
            "segment_id": segment_id,
            "new_state": "tracking",
            "tier": current_tier,
            "current_tier": current_tier,
            "expires_at_ms": new_expires,
        }
    elif norm == "important":
        # 延迟导入避免与 scoring.tick 的循环 import
        from src.compress.scoring import mark_important
        update_segment(segment_id, urgent_state="important", path=db_path)
        mark_important(segment_id, path=db_path)
        return {
            "segment_id": segment_id,
            "new_state": "important",
            "tier": "L1",
            "current_tier": "L1",
            "score": 10000.0,
        }
    else:
        raise ValueError(
            f"response 必须是 completed/postponed/important，收到：{response!r}"
        )


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = [
    "POSTPONE_DAYS", "URGENT_URGENCY_LEVEL",
    "track_urgent", "tick_urgent", "handle_urgent_response", "list_expired",
]
