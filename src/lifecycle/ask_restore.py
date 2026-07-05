"""AI 询问升级（src/lifecycle/ask_restore.py - T21）

实现 V0.4_PIVOT.md §3.6「AI 询问机制」：
当 AI 召回引用了 silent 段，触发「是否启用？」询问；用户响应
'yes' / 'no' / 'skip_7d' 决定 ref_count / silence_state /
last_ask_at / long_silent 更新策略。

5 条铁律：
1. 升级 = 重新计时（'yes' 把 promoted_at 置为 now）
2. 防骚扰 = 7 天冷却（'no' 更新 last_ask_at）
3. 用户主权 = 'skip_7d' 提供更长冷却通道
4. 阈值 = PROMOTE_THRESHOLD (3)：ref_count >= 3 自动 active
5. 只问 silent 段（active / dormant 不进入询问路径）

公共 API：
- ``PROMOTE_THRESHOLD`` 升级阈值
- ``ASK_COOLDOWN_DAYS`` 7 天防骚扰窗口
- ``should_ask(segment_id, path=None, now_ms=None) -> bool``
- ``make_ask_prompt(segment_id, path=None, now_ms=None) -> str``
- ``handle_user_response(segment_id, response, path=None, now_ms=None) -> dict``
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional, Union

from src.l0.segment_writer import get_segment, update_segment
from src.store.sqlite import execute

# ---------------------------------------------------------------------------
# 常量（V0.4_PIVOT.md §3.6）
# ---------------------------------------------------------------------------

PROMOTE_THRESHOLD: int = 3
ASK_COOLDOWN_DAYS: int = 7
ASK_COOLDOWN_MS: int = ASK_COOLDOWN_DAYS * 24 * 60 * 60 * 1000
DAYS_PER_MONTH: int = 30
VALID_RESPONSES: tuple[str, ...] = ("yes", "no", "skip_7d")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _days_to_ms(days: int) -> int:
    """天 → 毫秒。"""
    return int(days) * 24 * 60 * 60 * 1000


def _coerce_int(value: Any, default: int = 0) -> int:
    """规整为 int；非法回落到 default。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_seg(segment_id: str, path: Optional[Union[Path, str]]) -> dict:
    """取段落，不存在抛 ValueError。"""
    if not segment_id or not isinstance(segment_id, str):
        raise ValueError("segment_id 必须是非空字符串")
    seg = get_segment(segment_id, path=path)
    if not seg:
        raise ValueError(f"段落不存在：{segment_id}")
    return seg


def _log_event(
    segment_id: str,
    event_type: str,
    reason: str,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> int:
    """写一条 ask 审计到 score_events 表。"""
    ts = int(now_ms) if now_ms is not None else _now_ms()
    return execute(
        "INSERT INTO score_events "
        "(segment_id, event_type, delta, old_score, new_score, reason, created_at) "
        "VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
        (segment_id, event_type, reason, ts),
        path=path,
    )


def _age_phrase(now_ms: int, start_at_ms: int) -> str:
    """生成「X 月前」/「N 天前」/「不久前」时间短语。"""
    if not start_at_ms or start_at_ms <= 0:
        return "曾经"
    age_ms = max(0, now_ms - start_at_ms)
    if age_ms <= 0:
        return "不久前"
    months = int(age_ms // _days_to_ms(DAYS_PER_MONTH))
    if months <= 0:
        days = int(age_ms // _days_to_ms(1))
        return "不久前" if days <= 0 else f"{days} 天前"
    return "1 个月前" if months == 1 else f"{months} 个月前"


def _state(seg: dict) -> str:
    """安全取 silence_state；空值回落到 active。"""
    s = seg.get("silence_state")
    return s if s in ("active", "dormant", "silent") else "active"


def _normalize_response(response: Any) -> str:
    """校验 + 归一 response；非法抛 ValueError。"""
    if not isinstance(response, str):
        raise ValueError(f"response 必须是 {VALID_RESPONSES} 之一，收到：{response!r}")
    norm = response.strip().lower()
    if norm not in VALID_RESPONSES:
        raise ValueError(f"response 必须是 {VALID_RESPONSES} 之一，收到：{response!r}")
    return norm


# ---------------------------------------------------------------------------
# 询问判定
# ---------------------------------------------------------------------------


def should_ask(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> bool:
    """判断是否应发起 AI 询问。

    4 个条件全部满足才返回 True：
        1. silence_state == 'silent'
        2. long_silent != 1
        3. ref_count < PROMOTE_THRESHOLD
        4. last_ask_at 为空 / 超 7 天
    """
    seg = _get_seg(segment_id, path)
    if _state(seg) != "silent":
        return False
    if _coerce_int(seg.get("long_silent"), 0) != 0:
        return False
    if _coerce_int(seg.get("ref_count"), 0) >= PROMOTE_THRESHOLD:
        return False
    now = int(now_ms) if now_ms is not None else _now_ms()
    last = _coerce_int(seg.get("last_ask_at"), 0)
    if last > 0 and (now - last) <= ASK_COOLDOWN_MS:
        return False
    return True


# ---------------------------------------------------------------------------
# 询问 prompt
# ---------------------------------------------------------------------------


def make_ask_prompt(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> str:
    """生成中文 prompt：「你 X 月前做过 [topic_label]（[fog_anchor]），当前话题相关吗？」"""
    seg = _get_seg(segment_id, path)
    now = int(now_ms) if now_ms is not None else _now_ms()

    topic = (seg.get("topic_label") or "").strip() or "某个话题"
    anchor = (seg.get("fog_anchor") or "").strip() or "已雾化"
    phrase = _age_phrase(now, _coerce_int(seg.get("start_at"), 0))

    return f"你 {phrase} 做过 {topic}（{anchor}），当前话题相关吗？"


# ---------------------------------------------------------------------------
# 用户响应处理
# ---------------------------------------------------------------------------


def handle_user_response(
    segment_id: str,
    response: str,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> dict:
    """处理用户对询问的响应（yes / no / skip_7d）。

    'yes'    → ref_count++，按阈值升 dormant 或 active，重置 promoted_at。
    'no'     → last_ask_at = now（7 天防骚扰）。
    'skip_7d' → long_silent = 1，last_ask_at = now。

    返回 dict：含 response / segment_id / now_ms / silence_state /
    ref_count / long_silent；'yes' 分支额外含 promoted_to_active。
    """
    norm = _normalize_response(response)
    seg = _get_seg(segment_id, path)
    now = int(now_ms) if now_ms is not None else _now_ms()
    sid = seg["segment_id"]

    result: dict[str, Any] = {
        "response": norm,
        "segment_id": sid,
        "now_ms": now,
        "silence_state": _state(seg),
        "ref_count": _coerce_int(seg.get("ref_count"), 0),
        "long_silent": _coerce_int(seg.get("long_silent"), 0),
    }

    if norm == "yes":
        old = result["ref_count"]
        new_ref = old + 1
        target = "active" if new_ref >= PROMOTE_THRESHOLD else "dormant"
        update_segment(sid, path=path, ref_count=new_ref,
                       silence_state=target, promoted_at=now)
        _log_event(sid, "ask_yes",
                   f"ref_count {old}->{new_ref}, silence->{target}",
                   path=path, now_ms=now)
        result["ref_count"] = new_ref
        result["silence_state"] = target
        result["promoted_to_active"] = (target == "active")
        result["promoted_at"] = now
        return result

    if norm == "no":
        update_segment(sid, path=path, last_ask_at=now)
        _log_event(sid, "ask_no", "user declined (7d cooldown)",
                   path=path, now_ms=now)
        result["last_ask_at"] = now
        return result

    # skip_7d
    update_segment(sid, path=path, long_silent=1, last_ask_at=now)
    _log_event(sid, "ask_skip",
               "user skipped (long_silent=1, 7d cooldown)",
               path=path, now_ms=now)
    result["long_silent"] = 1
    result["last_ask_at"] = now
    return result


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


__all__ = [
    "PROMOTE_THRESHOLD",
    "ASK_COOLDOWN_DAYS",
    "ASK_COOLDOWN_MS",
    "VALID_RESPONSES",
    "should_ask",
    "make_ask_prompt",
    "handle_user_response",
]