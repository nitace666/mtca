"""L0-B 时间分段器 (v0.4)

按 DEVELOPER_PLAN.md §4.1 的 3 规则 + 弱合并，将 L0-细节 messages
切分为段落，并落库到 segments 表（L0-B）。

设计要点：
- 3 规则：
  1. ``MAX_GAP_MINUTES`` 强制切（默认 60 分钟）
  2. ``MIN_SEGMENT_MSGS`` 短段过滤（默认 3 条）
  3. ``NIGHT_MERGE_WINDOW`` 跨天夜段合并（23:00–07:00）
- 弱合并：Jaccard 关键词重叠 ``>= WEAK_MERGE_JACCARD`` 且 gap 在
  ``WEAK_MERGE_MAX_GAP`` 分钟内（避免跨长 gap 误合）。
- 时间戳统一毫秒，与 ``messages.created_at`` 对齐。
- ``save_segments`` 写入 ``segments`` 表（L0-B），与 L0-骨架 共用
  同一张表但按 ``start_msg_seq`` 区分；本函数不触碰 ``start_msg_seq=1``
  的 L0-骨架行。

公共 API：
- ``segment_session(session_id, path=None) -> list[dict]``
- ``weak_merge_check(current_msgs, new_msg, gap_min) -> bool``
- ``is_night_merge(ts_a, ts_b) -> bool``
- ``save_segments(session_id, segments, path=None) -> list[str]``
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import jieba

from src.l0.session_writer import get_session_messages
from src.store.sqlite import get_connection


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# DEVELOPER_PLAN.md §4.1 强制阈值
MAX_GAP_MINUTES: int = 60
MIN_SEGMENT_MSGS: int = 3
NIGHT_MERGE_WINDOW: tuple[int, int] = (23, 7)  # [start_h, end_h)

# 弱合并：Jaccard 阈值 + 最大允许 gap（分钟）
WEAK_MERGE_JACCARD: float = 0.3
WEAK_MERGE_MAX_GAP: int = MAX_GAP_MINUTES * 2  # 120 min

# 夜段合并：最大允许间隔（小时），超过则视为非夜段
NIGHT_MERGE_MAX_GAP_HOURS: int = 8

# 弱关键词过滤器（与 skeleton.py 子集对齐）
_STOPWORDS: frozenset[str] = frozenset({
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都",
    "一", "个", "上", "也", "很", "到", "说", "要", "去", "你", "会",
    "着", "没", "看", "好", "自己", "这", "那", "把", "它", "吗", "呢",
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "为什么",
    "因为", "所以", "但是", "可以", "应该", "需要", "想", "让", "给",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "and", "or", "but", "if", "this", "that", "it", "its", "i", "you",
})


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _has_cjk(text: str) -> bool:
    """判断文本是否含 CJK 统一表意文字（基本区）。"""
    return any(0x4E00 <= ord(c) <= 0x9FFF for c in text)


def _tokenize(text: str) -> list[str]:
    """用 jieba / 空格切词，返回去空白后的 token 列表。"""
    text = (text or "").strip()
    if not text:
        return []
    tokens = jieba.cut(text) if _has_cjk(text) else text.split()
    return [t.strip() for t in tokens if t.strip()]


def _meaningful_tokens(text: str) -> set[str]:
    """返回去停用词 / 去单字符 / 去纯数字的 token 集合。"""
    out: set[str] = set()
    for tok in _tokenize(text):
        if tok in _STOPWORDS:
            continue
        if len(tok) < 2:
            continue
        if tok.isdigit():
            continue
        out.add(tok)
    return out


def _msg_keywords(msg: dict) -> set[str]:
    """单条消息的关键词集合（基于 content）。"""
    return _meaningful_tokens(msg.get("content") or "")


def _segment_keywords(messages: list[dict]) -> set[str]:
    """聚合段内全部消息的关键词集合（并集）。"""
    out: set[str] = set()
    for m in messages:
        out |= _msg_keywords(m)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    """两集合的 Jaccard 相似度；任一为空返回 0.0。"""
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# ---------------------------------------------------------------------------
# 夜段合并判定
# ---------------------------------------------------------------------------


def _hour_of(ts_ms: int) -> int:
    """返回时间戳的本地小时（0-23）。"""
    return datetime.fromtimestamp(ts_ms / 1000).hour


def _in_night_window(hour: int) -> bool:
    """判断小时是否落在夜段窗口 [start_h, 24) ∪ [0, end_h)。"""
    start_h, end_h = NIGHT_MERGE_WINDOW
    return hour >= start_h or hour < end_h


def is_night_merge(ts_a: int, ts_b: int) -> bool:
    """判定两个毫秒时间戳是否属于同一夜段（应合并）。

    规则：
    - 任一非正 / 相等 → False
    - 间隔 ``> NIGHT_MERGE_MAX_GAP_HOURS`` → False
    - 两端本地小时都落在夜段窗口 → True
    """
    if ts_a <= 0 or ts_b <= 0:
        return False
    if ts_a == ts_b:
        return False
    gap_hours = abs(int(ts_b) - int(ts_a)) / 3_600_000
    if gap_hours > NIGHT_MERGE_MAX_GAP_HOURS:
        return False
    return _in_night_window(_hour_of(ts_a)) and _in_night_window(_hour_of(ts_b))


# ---------------------------------------------------------------------------
# 弱合并判定（Jaccard 关键词重叠）
# ---------------------------------------------------------------------------


def weak_merge_check(
    current_msgs: list[dict],
    new_msg: dict,
    gap_min: float,
) -> bool:
    """判定是否弱合并跨 gap 段。

    返回 ``True`` 当且仅当：
    - ``current_msgs`` 非空且 ``gap_min >= 0``
    - ``gap_min <= WEAK_MERGE_MAX_GAP`` 分钟
    - 当前段与新消息的关键词 Jaccard ``>= WEAK_MERGE_JACCARD``

    行为说明：
    - 任一侧关键词为空 → 直接 False（无可比）
    - 完全无重叠 → 0.0 < 0.3 → False
    """
    if not current_msgs or gap_min < 0:
        return False
    if gap_min > WEAK_MERGE_MAX_GAP:
        return False
    cur_kw = _segment_keywords(current_msgs)
    new_kw = _msg_keywords(new_msg)
    if not cur_kw or not new_kw:
        return False
    return _jaccard(cur_kw, new_kw) >= WEAK_MERGE_JACCARD


# ---------------------------------------------------------------------------
# 段落发射
# ---------------------------------------------------------------------------


def _emit_segment(
    session_id: str,
    msgs: list[dict],
    weak_merged: bool,
) -> dict:
    """根据累积消息列表生成段落 dict（不含 gap_to_next，由 segment_session 填充）。"""
    return {
        "session_id": session_id,
        "start_msg_seq": int(msgs[0]["seq"]),
        "end_msg_seq": int(msgs[-1]["seq"]),
        "start_at": int(msgs[0]["created_at"]),
        "end_at": int(msgs[-1]["created_at"]),
        "message_count": len(msgs),
        "weak_merged": bool(weak_merged),
        "gap_to_next": 0,
    }


# ---------------------------------------------------------------------------
# 分段主入口
# ---------------------------------------------------------------------------


def segment_session(
    session_id: str,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """按 3 规则 + 弱合并将指定 session 的消息切分为段落 dict 列表。

    返回的每个 dict 字段：
    - ``session_id``
    - ``start_msg_seq`` / ``end_msg_seq``
    - ``start_at`` / ``end_at``（毫秒）
    - ``message_count``
    - ``weak_merged``（是否因弱合并跨 gap）
    - ``gap_to_next``（到下一段的分钟数；末段为 0）

    算法（按消息 seq 顺序）：
    1. 夜段合并（最高优先级，跨午夜也合并）
    2. ``gap_min > MAX_GAP_MINUTES`` → 强制切
    3. 弱合并（Jaccard ``>= WEAK_MERGE_JACCARD`` 且 gap ``<= WEAK_MERGE_MAX_GAP``）
    4. 其余 → 切

    最后过滤掉 ``message_count < MIN_SEGMENT_MSGS`` 的段。
    """
    msgs = get_session_messages(session_id, path=path)
    if not msgs:
        return []

    raw_segments: list[dict] = []
    current: list[dict] = [msgs[0]]
    current_weak = False

    for m in msgs[1:]:
        last = current[-1]
        gap_min = (int(m["created_at"]) - int(last["created_at"])) / 60000.0

        if is_night_merge(int(last["created_at"]), int(m["created_at"])):
            # 规则 1：夜段合并（最高优先级）
            current.append(m)
            current_weak = True
        elif gap_min > MAX_GAP_MINUTES:
            # 规则 2：强制切
            raw_segments.append(
                _emit_segment(session_id, current, current_weak)
            )
            current = [m]
            current_weak = False
        elif weak_merge_check(current, m, gap_min):
            # 规则 3：弱合并
            current.append(m)
            current_weak = True
        else:
            # 默认切
            raw_segments.append(
                _emit_segment(session_id, current, current_weak)
            )
            current = [m]
            current_weak = False

    # 收尾
    raw_segments.append(_emit_segment(session_id, current, current_weak))

    # 过滤短段
    filtered = [
        s for s in raw_segments if s["message_count"] >= MIN_SEGMENT_MSGS
    ]

    # 计算 gap_to_next（基于过滤后的相邻段，端到端）
    for i in range(len(filtered) - 1):
        gap_ms = int(filtered[i + 1]["start_at"]) - int(filtered[i]["end_at"])
        filtered[i]["gap_to_next"] = max(0, int(gap_ms // 60000))
    if filtered:
        filtered[-1]["gap_to_next"] = 0

    return filtered


# ---------------------------------------------------------------------------
# 段落落库
# ---------------------------------------------------------------------------


_REQUIRED_SEGMENT_KEYS: frozenset[str] = frozenset({
    "start_msg_seq", "end_msg_seq", "start_at", "end_at",
})


def save_segments(
    session_id: str,
    segments: list[dict],
    path: Optional[Union[Path, str]] = None,
) -> list[str]:
    """将 ``segment_session`` 返回的段落列表写入 ``segments`` 表（L0-B）。

    行为：
    - 每个 segment 生成新的 ``segment_id``（UUID4）后 INSERT 一行
    - 不触碰 ``start_msg_seq = 1`` 的 L0-骨架行（与 L0-骨架 共存）
    - 写入 ``gap_to_next`` 与 ``weak_merged`` 字段；其他列用 schema 默认值

    异常：
        ValueError：任一 segment 缺少必需字段。
        RuntimeError：底层数据库写入失败。
    """
    if not segments:
        return []

    # ---- 参数校验 ----
    for idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise ValueError(f"segments[{idx}] 必须是 dict")
        missing = _REQUIRED_SEGMENT_KEYS - set(seg.keys())
        if missing:
            raise ValueError(
                f"segments[{idx}] 缺少字段：{sorted(missing)}"
            )

    inserted_ids: list[str] = []
    try:
        with get_connection(path) as conn:
            for seg in segments:
                seg_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO segments "
                    "(segment_id, session_id, start_msg_seq, end_msg_seq, "
                    "start_at, end_at, gap_to_next, weak_merged) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        seg_id,
                        session_id,
                        int(seg["start_msg_seq"]),
                        int(seg["end_msg_seq"]),
                        int(seg["start_at"]),
                        int(seg["end_at"]),
                        int(seg.get("gap_to_next", 0) or 0),
                        1 if seg.get("weak_merged") else 0,
                    ),
                )
                inserted_ids.append(seg_id)
    except RuntimeError:
        raise

    return inserted_ids


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = [
    "MAX_GAP_MINUTES",
    "MIN_SEGMENT_MSGS",
    "NIGHT_MERGE_WINDOW",
    "WEAK_MERGE_JACCARD",
    "WEAK_MERGE_MAX_GAP",
    "segment_session",
    "weak_merge_check",
    "is_night_merge",
    "save_segments",
]