"""L0-骨架 锚点生成 (v0.4)

从 session 的 messages 提炼 L0-骨架：锚点句（≤ 20 字）、关键词列表、
时间范围、消息数，并支持落库到 ``segments`` 表（L0-骨架，不可变）。

设计要点：
- 锚点句取首条 role='user' 消息的内容，截断到 ANCHOR_MAX_LEN；
  优先在标点 / 空格处断开，否则硬切。无 user 时回退到首条任意角色。
- 关键词提取对中英文分别处理：含 CJK 走 jieba，纯 ASCII 走空格
  切分；过滤内置停用词 + 纯标点 token。
- ``build_skeleton`` 仅聚合不写库；``save_skeleton`` 负责落库。
- L0-骨架不可变：``save_skeleton`` 对已存在 segment 的 session 不再
  二次写入（由 ``no_update_skeleton`` 触发器保证）。

公共 API：
- ``generate_anchor(messages) -> str``
- ``extract_keywords(messages, top_k=5) -> list[str]``
- ``build_skeleton(session_id, path=None) -> dict``
- ``save_skeleton(session_id, skeleton, path=None) -> str``
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Optional, Union

import jieba

from src.l0.session_writer import get_session_messages
from src.store.sqlite import get_connection, query

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 锚点句最大长度（V0.4_PIVOT.md §3.1：锚点句 ≤ 20 字）
ANCHOR_MAX_LEN: int = 20

# 内置停用词表（中文常见虚词 + 英文冠词代词；M1 够用，T7/T10 可换外部词典）
STOPWORDS: frozenset[str] = frozenset({
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没",
    "看", "好", "自己", "这", "那", "把", "它", "么", "啊", "吧", "吗", "呢",
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "为什么",
    "因为", "所以", "但是", "不过", "还是", "或者", "以及", "可以", "应该",
    "需要", "想", "让", "给", "从", "向", "为", "为了", "今天", "昨天", "明天",
    "现在", "以前", "以后", "刚才", "之前", "之后", "一下", "一些", "一点",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "and", "or", "but", "if", "this", "that", "it", "its", "i", "you",
})

# 锚点截断优先点
_ANCHOR_CUT_CHARS: frozenset[str] = frozenset("。！？，、；：,.!?;: \n\t")
_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _has_cjk(text: str) -> bool:
    """判断文本是否含 CJK 统一表意文字（基本区）。"""
    return any(0x4E00 <= ord(c) <= 0x9FFF for c in text)


def _is_meaningful_token(token: str) -> bool:
    """判断 token 是否为有意义的词（非停用词 / 非纯标点 / 非空）。"""
    if not token or token in STOPWORDS:
        return False
    if _PUNCT_RE.match(token):
        return False
    return True


def _truncate_anchor(text: str, max_len: int = ANCHOR_MAX_LEN) -> str:
    """截断文本到 max_len 字符，优先在标点 / 空格处断开。"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    # 在 [max_len // 2, max_len] 区间内由大到小找切点
    lower = max(max_len // 2, 1)
    for i in range(max_len, lower - 1, -1):
        if i < len(text) and text[i] in _ANCHOR_CUT_CHARS:
            return text[:i].rstrip()
    return text[:max_len]


def _first_content(messages: list[dict], role: Optional[str] = None) -> Optional[str]:
    """返回首条匹配 role（None=任意）且 content 非空的原文。"""
    for msg in messages:
        if role is not None and msg.get("role") != role:
            continue
        content = msg.get("content")
        if content:
            text = str(content).strip()
            if text:
                return text
    return None


# ---------------------------------------------------------------------------
# 锚点句生成
# ---------------------------------------------------------------------------


def generate_anchor(messages: list[dict]) -> str:
    """从 messages 提取锚点句（≤ ANCHOR_MAX_LEN 字）。

    优先取首条 role='user' 消息；无 user 时回退首条任意角色；全空返回 ""。
    """
    if not messages:
        return ""
    text = _first_content(messages, role="user") or _first_content(messages)
    return _truncate_anchor(text, ANCHOR_MAX_LEN) if text else ""


# ---------------------------------------------------------------------------
# 关键词提取
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """用 jieba / 空格切词；返回去首尾空白后的 token 列表。"""
    text = (text or "").strip()
    if not text:
        return []
    tokens = jieba.cut(text) if _has_cjk(text) else text.split()
    return [t.strip() for t in tokens if t.strip()]


def extract_keywords(messages: list[dict], top_k: int = 5) -> list[str]:
    """从 messages 提取 top_k 个高频关键词（停用词已过滤）。"""
    if top_k <= 0 or not messages:
        return []
    counter: Counter = Counter()
    for msg in messages:
        for token in _tokenize(msg.get("content")):
            if _is_meaningful_token(token):
                counter[token] += 1
    # 词频降序；同频字典序
    items = sorted(counter.items(), key=lambda x: (-x[1], x[0]))
    return [token for token, _ in items[:top_k]]


# ---------------------------------------------------------------------------
# 骨架构建与落库
# ---------------------------------------------------------------------------


def build_skeleton(
    session_id: str,
    path: Optional[Union[Path, str]] = None,
) -> dict:
    """为指定 session 构建 L0-骨架数据（不写库）。

    返回 dict：``anchor`` / ``keywords`` / ``message_count`` /
    ``time_range: (start_at, end_at)`` / ``topic_label``。
    """
    messages = get_session_messages(session_id, path=path)
    if messages:
        start_at, end_at = int(messages[0]["created_at"]), int(messages[-1]["created_at"])
    else:
        start_at = end_at = 0
    anchor = generate_anchor(messages)
    return {
        "anchor": anchor,
        "keywords": extract_keywords(messages, top_k=5),
        "message_count": len(messages),
        "time_range": (start_at, end_at),
        "topic_label": anchor,
    }


def save_skeleton(
    session_id: str,
    skeleton: dict,
    path: Optional[Union[Path, str]] = None,
) -> str:
    """将 skeleton 落库到 ``segments`` 表（L0-骨架）。

    行为：
    - 若该 session 已有 segment → 返回已有 segment_id（骨架不可变）
    - 否则 INSERT 新记录：start_msg_seq=1, end_msg_seq=message_count
      （最小 1），start_at/end_at=time_range，topic_label=topic_label
      （或 anchor），fog_anchor=anchor。

    返回：
        ``segment_id`` 字符串。

    异常：
        ValueError：skeleton 字段缺失或 time_range 非法。
        RuntimeError：底层数据库写入失败。
    """
    # ---- 参数校验 ----
    if not isinstance(skeleton, dict):
        raise ValueError("skeleton 必须是 dict")
    if "anchor" not in skeleton or "time_range" not in skeleton:
        raise ValueError("skeleton 必须包含 'anchor' 与 'time_range' 字段")
    time_range = skeleton["time_range"]
    if not (isinstance(time_range, (tuple, list)) and len(time_range) == 2):
        raise ValueError("time_range 必须是 (start_at, end_at) 二元组")
    anchor = str(skeleton["anchor"])
    start_at, end_at = int(time_range[0]), int(time_range[1])
    topic_label = str(skeleton.get("topic_label") or anchor) or anchor
    msg_count = max(1, int(skeleton.get("message_count", 0) or 0))

    # ---- 查重：L0-骨架不可变 ----
    existing = query(
        "SELECT segment_id FROM segments WHERE session_id = ? LIMIT 1",
        (session_id,), path=path,
    )
    if existing:
        return str(existing[0]["segment_id"])

    # ---- INSERT 新段 ----
    segment_id = str(uuid.uuid4())
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO segments "
            "(segment_id, session_id, start_msg_seq, end_msg_seq, "
            "start_at, end_at, topic_label, fog_anchor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (segment_id, session_id, 1, msg_count,
             start_at, end_at, topic_label, anchor),
        )
    return segment_id


__all__ = [
    "generate_anchor",
    "extract_keywords",
    "build_skeleton",
    "save_skeleton",
    "ANCHOR_MAX_LEN",
    "STOPWORDS",
]
