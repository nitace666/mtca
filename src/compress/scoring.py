"""动态打分引擎（src/compress/scoring.py — T7）

实现 MTCA 长期记忆架构中的动态打分机制：
- 每轮 tick 衰减 silence_state='active' 段落 1 分
- /循环 段落跳过衰减（cycle_tag 来自 sessions 表）
- 引用检测（Jaccard >= 0.3）加分
- 阈值触发降级（L1 -> L2 -> L3 -> L3_hidden）
- 用户控制接口：/重要 /循环 /归档
- 写 score_events 审计

设计要点：
- 所有 SQL 用 ? 占位符
- 分数与档位同时落库到 segments 表
- 审计事件独立写 score_events
- 复用 src.l0.skeleton.extract_keywords 做关键词抽取

公共 API：
- ``tick(path=None, session_id=None, recent_msgs=None) -> int``
- ``has_been_referenced(segment, recent_msgs=None, path=None) -> bool``
- ``determine_tier(score) -> str``
- ``mark_important(segment_id, path=None) -> int``
- ``mark_cycle(segment_id, cycle_tag, path=None) -> int``
- ``archive(segment_id, path=None) -> int``
- ``get_recent_messages(window=10, path=None) -> list[dict]``
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Union

from src.compress.multi_factor_score import (
    calculate_score,
    get_quadrant_from_seg,
)
from src.l0.skeleton import extract_keywords
from src.l0.segment_writer import get_segment, update_segment
from src.store.sqlite import execute, query

# ---------------------------------------------------------------------------
# 常量（DEVELOPER_PLAN.md §4.2 + 用户 prompt）
# ---------------------------------------------------------------------------

INITIAL_SCORE: float = 100.0
PER_TICK_DECAY: float = 1.0
REFERENCE_BOOST: float = 10.0
L1_THRESHOLD: float = 70.0
L2_THRESHOLD: float = 50.0
L3_THRESHOLD: float = 30.0
IMPORTANT_SCORE: float = 10000.0

# 引用检测 Jaccard 阈值（用户 prompt：>= 0.3）
JACCARD_THRESHOLD: float = 0.3

# 段内文本字段（用于关键词提取）
_SEGMENT_TEXT_KEYS: tuple[str, ...] = ("topic_label", "fog_anchor")

# 默认最近消息窗口
DEFAULT_RECENT_WINDOW: int = 10

# 提取关键词上限
_SEGMENT_KEYWORDS_TOPK: int = 10
_RECENT_KEYWORDS_TOPK: int = 20


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """返回当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _segment_text(segment: dict) -> str:
    """把段落的骨架文本字段（topic_label + fog_anchor）拼成单字符串。"""
    parts: list[str] = []
    for key in _SEGMENT_TEXT_KEYS:
        val = segment.get(key)
        if val:
            parts.append(str(val))
    return " ".join(parts)


def _coerce_score(value) -> float:
    """规整 score 字段为 float；None / 非法值回落为 0.0。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 档位判定
# ---------------------------------------------------------------------------


def determine_tier(score: float) -> str:
    """根据分数返回档位。

    规则：
        score >= L1_THRESHOLD (70) -> 'L1'
        L2_THRESHOLD (50) <= score < 70 -> 'L2'
        L3_THRESHOLD (30) <= score < 50 -> 'L3'
        score < L3_THRESHOLD -> 'L3_hidden'
    """
    s = _coerce_score(score)
    if s >= L1_THRESHOLD:
        return "L1"
    if s >= L2_THRESHOLD:
        return "L2"
    if s >= L3_THRESHOLD:
        return "L3"
    return "L3_hidden"


# ---------------------------------------------------------------------------
# 最近消息
# ---------------------------------------------------------------------------


def get_recent_messages(
    window: int = DEFAULT_RECENT_WINDOW,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """获取最近 ``window`` 条非空消息（按 created_at DESC, seq DESC 排序）。

    排除已雾化的消息（content IS NULL）。
    """
    if not isinstance(window, int) or window <= 0:
        window = DEFAULT_RECENT_WINDOW
    return query(
        "SELECT message_id, session_id, seq, role, content, created_at "
        "FROM messages WHERE content IS NOT NULL "
        "ORDER BY created_at DESC, seq DESC LIMIT ?",
        (int(window),),
        path=path,
    )


# ---------------------------------------------------------------------------
# 引用检测（Jaccard）
# ---------------------------------------------------------------------------


def has_been_referenced(
    segment: dict,
    recent_msgs: Optional[list[dict]] = None,
    path: Optional[Union[Path, str]] = None,
) -> bool:
    """检测段落是否在最近消息中被引用（Jaccard >= JACCARD_THRESHOLD）。

    算法：
        1. 提取段落的关键词（来自 topic_label + fog_anchor）；
        2. 提取最近消息的关键词；
        3. 计算 |A ∩ B| / |A ∪ B|；
        4. Jaccard >= 0.3 返回 True。

    参数：
        segment: 段落 dict（包含 topic_label / fog_anchor）。
        recent_msgs: 最近消息列表；None 时自动从 DB 取 DEFAULT_RECENT_WINDOW。
        path: 数据库路径。

    返回：
        True=被引用 / False=未引用。
    """
    if not segment:
        return False

    seg_text = _segment_text(segment)
    if not seg_text.strip():
        return False

    seg_kws = set(extract_keywords(
        [{"role": "user", "content": seg_text, "seq": 1}],
        top_k=_SEGMENT_KEYWORDS_TOPK,
    ))
    if not seg_kws:
        return False

    if recent_msgs is None:
        recent_msgs = get_recent_messages(path=path)
    if not recent_msgs:
        return False

    recent_kws = set(extract_keywords(recent_msgs, top_k=_RECENT_KEYWORDS_TOPK))
    if not recent_kws:
        return False

    intersection = seg_kws & recent_kws
    union = seg_kws | recent_kws
    if not union:
        return False

    jaccard = len(intersection) / len(union)
    return jaccard >= JACCARD_THRESHOLD


# ---------------------------------------------------------------------------
# 内部：审计 + 数据访问
# ---------------------------------------------------------------------------


def _log_score_event(
    segment_id: str,
    event_type: str,
    delta: Optional[float],
    old_score: Optional[float],
    new_score: Optional[float],
    reason: Optional[str],
    path: Optional[Union[Path, str]] = None,
) -> int:
    """写一行 score_events 审计记录。"""
    return execute(
        "INSERT INTO score_events "
        "(segment_id, event_type, delta, old_score, new_score, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            segment_id,
            event_type,
            delta,
            old_score,
            new_score,
            reason,
            _now_ms(),
        ),
        path=path,
    )


def _list_active_segments(
    path: Optional[Union[Path, str]] = None,
    session_id: Optional[str] = None,
) -> list[dict]:
    """列出 silence_state='active' 的段落（JOIN sessions 取 cycle_tag）。"""
    if session_id:
        return query(
            "SELECT s.segment_id, s.session_id, s.current_score, "
            "s.current_tier, s.silence_state, s.topic_label, s.fog_anchor, "
            "s.urgency_level, s.importance_level, s.emotion_tag, "
            "s.urgent_state, s.promoted_at, s.ref_count, "
            "sess.cycle_tag "
            "FROM segments s "
            "LEFT JOIN sessions sess ON sess.session_id = s.session_id "
            "WHERE s.silence_state = 'active' AND s.session_id = ?",
            (session_id,),
            path=path,
        )
    return query(
        "SELECT s.segment_id, s.session_id, s.current_score, "
        "s.current_tier, s.silence_state, s.topic_label, s.fog_anchor, "
        "s.urgency_level, s.importance_level, s.emotion_tag, "
        "s.urgent_state, s.promoted_at, s.ref_count, "
        "sess.cycle_tag "
        "FROM segments s "
        "LEFT JOIN sessions sess ON sess.session_id = s.session_id "
        "WHERE s.silence_state = 'active'",
        path=path,
    )


# ---------------------------------------------------------------------------
# tick() 主入口
# ---------------------------------------------------------------------------


def tick(
    path: Optional[Union[Path, str]] = None,
    session_id: Optional[str] = None,
    recent_msgs: Optional[list[dict]] = None,
) -> int:
    """每轮对话后异步调用：遍历 silence_state='active' 的段落。

    逻辑（M2.5 多因子打分 — 替换线性 -1/tick 衰减）：
        1. cycle_tag 非空 -> 跳过打分（写 cycle_skip 审计）；
        2. 引用检测 -> 递增 ref_count（让 calculate_score 中的 f_reference 反映）；
        3. calculate_score(seg, now) -> (new_score, new_tier)；
        4. 写 decay / boost 审计（保留旧 event_type 兼容）；
        5. 落库 current_score / current_tier；tier 变化写 threshold 审计。

    参数：
        path: 数据库路径。
        session_id: 若指定，仅处理该会话的段落；None=全局。
        recent_msgs: 最近消息；None=自动从 DB 取。

    返回：
        实际写库的段落数（不含被 cycle 跳过的）。
    """
    segments = _list_active_segments(path=path, session_id=session_id)
    if not segments:
        return 0

    # 取一次最近消息供所有段落复用
    if recent_msgs is None:
        recent_msgs = get_recent_messages(path=path)

    now = _now_ms()
    n_updated = 0
    for seg in segments:
        seg_id = seg["segment_id"]
        old_score = _coerce_score(seg.get("current_score"))
        old_tier = seg.get("current_tier") or "L0"
        cycle_tag = seg.get("cycle_tag")

        # ---- 1. /循环 跳过打分 ----
        if cycle_tag:
            _log_score_event(
                seg_id, "user_cycle_skip", None,
                old_score, old_score,
                f"cycle_tag={cycle_tag}",
                path=path,
            )
            continue

        # ---- 2. 引用检测：递增 ref_count ----
        #    把实时 Jaccard 命中转成持久化 ref_count（喂给 f_reference）。
        was_referenced = has_been_referenced(
            seg, recent_msgs=recent_msgs, path=path,
        )
        if was_referenced:
            current_ref = int(seg.get("ref_count", 0) or 0)
            new_ref = current_ref + 1
            update_segment(seg_id, path=path, ref_count=new_ref)
            seg["ref_count"] = new_ref

        # ---- 3. 多因子打分（M2.5）----
        new_score, new_tier = calculate_score(seg, now)

        # ---- 4. 审计（保留 decay / boost event_type）----
        delta = new_score - old_score
        _log_score_event(
            seg_id, "decay", delta,
            old_score, new_score, "multi_factor_tick",
            path=path,
        )
        if was_referenced:
            _log_score_event(
                seg_id, "boost", REFERENCE_BOOST,
                old_score, new_score, "reference_detected",
                path=path,
            )

        # ---- 5. 落库 ----
        update_segment(
            seg_id, path=path,
            current_score=new_score,
            current_tier=new_tier,
        )

        if new_tier != old_tier:
            _log_score_event(
                seg_id, "threshold", None,
                new_score, new_score,
                f"{old_tier}->{new_tier}",
                path=path,
            )

        n_updated += 1

    return n_updated


# ---------------------------------------------------------------------------
# 用户控制：/重要 /循环 /归档
# ---------------------------------------------------------------------------


def mark_important(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """把段落标记为 /重要：score=IMPORTANT_SCORE，tier=determine_tier(IMPORTANT_SCORE)。

    异常：
        ValueError：segment_id 非法或段落不存在。
    """
    if not segment_id or not isinstance(segment_id, str):
        raise ValueError("segment_id 必须是非空字符串")

    seg = get_segment(segment_id, path=path)
    if not seg:
        raise ValueError(f"段落不存在：{segment_id}")

    old_score = _coerce_score(seg.get("current_score"))
    new_tier = determine_tier(IMPORTANT_SCORE)
    delta = IMPORTANT_SCORE - old_score

    rows = update_segment(
        segment_id, path=path,
        current_score=IMPORTANT_SCORE,
        current_tier=new_tier,
    )
    _log_score_event(
        segment_id, "user_important", delta,
        old_score, IMPORTANT_SCORE, "/重要 锁定",
        path=path,
    )
    return rows


def mark_cycle(
    segment_id: str,
    cycle_tag: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """把段落标记为 /循环：在 sessions 表写入 cycle_tag，tick() 会跳过衰减。

    异常：
        ValueError：segment_id / cycle_tag 非法或段落不存在。
    """
    if not segment_id or not isinstance(segment_id, str):
        raise ValueError("segment_id 必须是非空字符串")
    if not isinstance(cycle_tag, str) or not cycle_tag.strip():
        raise ValueError("cycle_tag 必须是非空字符串")

    seg = get_segment(segment_id, path=path)
    if not seg:
        raise ValueError(f"段落不存在：{segment_id}")

    session_id = seg.get("session_id")
    tag = cycle_tag.strip()

    # 写 sessions.cycle_tag
    rows = execute(
        "UPDATE sessions SET cycle_tag = ? WHERE session_id = ?",
        (tag, session_id),
        path=path,
    )

    current_score = _coerce_score(seg.get("current_score"))
    _log_score_event(
        segment_id, "user_cycle", None,
        current_score, current_score,
        f"/循环 {tag}",
        path=path,
    )
    return rows


def archive(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """归档段落：tier='L3_hidden'，不删 L0。

    异常：
        ValueError：segment_id 非法或段落不存在。
    """
    if not segment_id or not isinstance(segment_id, str):
        raise ValueError("segment_id 必须是非空字符串")

    seg = get_segment(segment_id, path=path)
    if not seg:
        raise ValueError(f"段落不存在：{segment_id}")

    current_score = _coerce_score(seg.get("current_score"))
    old_tier = seg.get("current_tier") or "L0"

    rows = update_segment(
        segment_id, path=path,
        current_tier="L3_hidden",
    )
    _log_score_event(
        segment_id, "user_archive", None,
        current_score, current_score,
        f"/归档 {old_tier}->L3_hidden",
        path=path,
    )
    return rows


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


__all__ = [
    "INITIAL_SCORE", "PER_TICK_DECAY", "REFERENCE_BOOST",
    "L1_THRESHOLD", "L2_THRESHOLD", "L3_THRESHOLD", "IMPORTANT_SCORE",
    "JACCARD_THRESHOLD", "DEFAULT_RECENT_WINDOW",
    "determine_tier", "get_recent_messages", "has_been_referenced",
    "tick", "mark_important", "mark_cycle", "archive",
]