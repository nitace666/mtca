"""MTCA 召回引擎（v0.4 / T8）

按 DEVELOPER_PLAN.md §4.4 + ARCHITECTURE.md §2 实现双通道召回：
- 主通道 1（FTS5）：``messages_fts MATCH`` 全文搜索 L0-细节
- 主通道 2（topic）：``segments.topic_label LIKE`` 段落话题搜索
- 合并 / 去重 / 加载段内 L0 原文 / 邻居展开 / Jaccard rerank
- 失败回退：``recall_with_fallback`` 在召回为空时返回 ``get_recent_sessions``

设计要点：
- 所有 SQL 用 ``?`` 占位符；FTS5 查询前用 ``_sanitize_fts_query`` 过滤
  引号等特殊字符，避免 MATCH 语法错误。
- 段内消息加载复用 ``session_writer.get_session_messages``，按
  ``start_msg_seq`` / ``end_msg_seq`` 截取；不重新实现消息读取。
- 时间窗口参数 ``time_window`` 形如 ``(start_ms, end_ms)``，对
  ``segments.start_at`` / ``sessions.started_at`` 双向生效。
- ``topics`` 为字符串列表，按 ``OR`` 关系逐项 ``LIKE`` 匹配。
- 邻居展开以 ``session_id`` 分组，按 ``start_at`` 排序后取前后
  ``window`` 段（默认 1 段）。

公共 API：
- ``recall(query, time_window=None, topics=None, top_k=5, path=None)``
- ``search_sessions(query, time_window, topics, limit, path=None)``
- ``search_segments(query, time_window, topics, limit, path=None)``
- ``expand_neighbors(segment_ids, window=1, path=None)``
- ``rerank(query, candidates, path=None)``
- ``recall_with_fallback(query, time_window=None, topics=None,
  top_k=5, path=None)``
- ``get_recent_sessions(limit=10, path=None)``
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Optional, Union

from src.l0.skeleton import extract_keywords
from src.l0.session_writer import get_session_messages
from src.store.sqlite import query as db_query

# T28 防御性 LRU 缓存 (OrderedDict hashlib json)
from collections import OrderedDict
import hashlib
import json

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# rerank 权重（与 scoring.py 的 JACCARD_THRESHOLD 解耦；rerank 内部用更严）
_JACCARD_RERANK_THRESHOLD: float = 0.0   # rerank 不过滤，>=0 即参与排序

# 默认邻居窗口（前后各 N 段）
_DEFAULT_NEIGHBOR_WINDOW: int = 1

# 默认回退会话数
_FALLBACK_LIMIT: int = 10

# T28 防御性 LRU 缓存
_RECALL_CACHE_MAX = 128

_RECALL_CACHE: "OrderedDict[str, list[dict]]" = OrderedDict()


def _recall_cache_key(query, time_window, topics, top_k, path):
    payload = json.dumps(
        {"p": str(path) if path else "", "q": query, "tw": time_window, "to": topics or [], "k": top_k},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _cache_get(key):
    if key in _RECALL_CACHE:
        _RECALL_CACHE.move_to_end(key)
        return _RECALL_CACHE[key]
    return None


def _cache_put(key, value):
    if key in _RECALL_CACHE:
        _RECALL_CACHE.move_to_end(key)
    _RECALL_CACHE[key] = value
    while len(_RECALL_CACHE) > _RECALL_CACHE_MAX:
        _RECALL_CACHE.popitem(last=False)

# L0 不可雾化字段（即便是 fogged_once 段，骨架字段仍可读）
_FOGGED_OK_STATES: frozenset[str] = frozenset({"clear", "fogged_once", "archived"})

# 时间窗口字段映射（segments 表用 start_at 过滤；sessions 表用 started_at）
_TIME_COL_SEGMENT: str = "start_at"
_TIME_COL_SESSION: str = "started_at"

# LIKE 通配符默认行为：contains
_LIKE_WILDCARD: str = "%"

# FTS5 关键词提取（用于 rerank 的 query 关键词集合）
_QUERY_KEYWORDS_TOPK: int = 10

# FTS5 关键词最小长度（短于此长度的高频虚词会被过滤，避免噪声）
_MIN_TOKEN_LEN: int = 2

# rerank 时：Jaccard 系数 + 段原始分数的混合权重（和为 1.0）
_WEIGHT_JACCARD: float = 0.7
_WEIGHT_SCORE: float = 0.3
_SCORE_NORMALIZE: float = 100.0   # 将 current_score 除以该值映射到 [0,1]

# 段内文本字段（rerank 计算相关性用，与 scoring._SEGMENT_TEXT_KEYS 对齐）
_RERANK_TEXT_KEYS: tuple[str, ...] = ("topic_label", "fog_anchor")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """返回当前毫秒时间戳（便于回退会话排序）。"""
    return int(time.time() * 1000)


def _sanitize_fts_query(query_text: str) -> str:
    """将原始 query 规整为 FTS5 MATCH 接受的关键词串。

    策略：
    - 去掉 FTS5 特殊字符 ``"`` ``'`` ``(`` ``)`` ``*`` ``:`` ``^``；
    - 保留中英文 / 数字 / 下划线 / 空白；
    - 多余空白压缩为单空格。
    """
    if not query_text:
        return ""
    # 仅保留字母、数字、下划线、空白、CJK
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", str(query_text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _coerce_limit(limit: Any, default: int = 20) -> int:
    """规整 limit 为合法正整数。"""
    if not isinstance(limit, int) or limit <= 0:
        return default
    return int(limit)


def _coerce_window(window: Any) -> int:
    """规整邻居窗口为非负整数。"""
    if not isinstance(window, int) or window < 0:
        return _DEFAULT_NEIGHBOR_WINDOW
    return int(window)


def _validate_time_window(time_window: Any) -> Optional[tuple[int, int]]:
    """校验 time_window 为 (start_ms, end_ms) 或 None。"""
    if time_window is None:
        return None
    if not isinstance(time_window, (tuple, list)) or len(time_window) != 2:
        raise ValueError("time_window 必须是 (start_ms, end_ms) 二元组或 None")
    start, end = int(time_window[0]), int(time_window[1])
    if start < 0 or end < start:
        raise ValueError("time_window 非法：start >= 0 且 end >= start")
    return start, end


def _query_keyword_set(query_text: str) -> set[str]:
    """提取 query 的关键词集合（用于 Jaccard rerank）。

    复用 skeleton.extract_keywords 走 jieba/空格切词路径。
    """
    msgs = [{"role": "user", "content": query_text or "", "seq": 1}]
    kws = extract_keywords(msgs, top_k=_QUERY_KEYWORDS_TOPK)
    return {k for k in kws if len(k) >= _MIN_TOKEN_LEN}


def _segment_text(seg: dict) -> str:
    """把段落骨架字段拼成单字符串（rerank 用）。"""
    parts: list[str] = []
    for key in _RERANK_TEXT_KEYS:
        val = seg.get(key)
        if val:
            parts.append(str(val))
    return " ".join(parts)


def _segment_keyword_set(seg: dict) -> set[str]:
    """提取段落骨架的关键词集合。"""
    text = _segment_text(seg)
    if not text.strip():
        return set()
    msgs = [{"role": "user", "content": text, "seq": 1}]
    kws = extract_keywords(msgs, top_k=_QUERY_KEYWORDS_TOPK)
    return {k for k in kws if len(k) >= _MIN_TOKEN_LEN}


def _build_topics_filter(topics: Optional[list[str]]) -> tuple[str, list[Any]]:
    """构造 topics 过滤的 SQL 片段与参数。

    返回 ``(sql_clause, params)``：
    - topics 为 None / 空列表 → ``("", [])`` 表示不加过滤。
    - 否则 → ``AND topic_label LIKE ? OR topic_label LIKE ? ...`` 的
      参数化片段（每项自动加前后 %）。
    """
    if not topics:
        return "", []
    parts: list[str] = []
    params: list[Any] = []
    for t in topics:
        if not isinstance(t, str) or not t.strip():
            continue
        parts.append("topic_label LIKE ?")
        params.append(f"{_LIKE_WILDCARD}{t.strip()}{_LIKE_WILDCARD}")
    if not parts:
        return "", []
    return " AND (" + " OR ".join(parts) + ")", params


def _build_time_filter(
    time_window: Optional[tuple[int, int]],
    column: str,
) -> tuple[str, list[Any]]:
    """构造 time_window 过滤的 SQL 片段与参数。"""
    if not time_window:
        return "", []
    start, end = time_window
    return f" AND {column} >= ? AND {column} <= ?", [start, end]


# ---------------------------------------------------------------------------
# 主通道 2：segments.topic_label 搜索
# ---------------------------------------------------------------------------


def search_segments(
    query: Optional[str] = None,
    time_window: Optional[tuple[int, int]] = None,
    topics: Optional[list[str]] = None,
    limit: int = 20,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """按 topic_label LIKE 搜索段落（主通道 2）。

    参数：
        query: 关键词；可空（空时退化为按 topics / time_window 过滤）。
        time_window: ``(start_ms, end_ms)`` 或 ``None``。
        topics: 话题白名单字符串列表；任一命中即返回（OR 关系）。
        limit: 返回上限；默认 20。
        path: 数据库路径。

    返回：
        list[dict]，每条包含 ``segment_id`` / ``session_id`` /
        ``current_tier`` / ``current_score`` / ``topic_label`` /
        ``fog_anchor`` / ``start_at``。
    """
    tw = _validate_time_window(time_window)
    safe_limit = _coerce_limit(limit, default=20)
    topics_clause, topics_params = _build_topics_filter(topics)
    time_clause, time_params = _build_time_filter(tw, _TIME_COL_SEGMENT)

    like_params: list[Any] = []
    if query and str(query).strip():
        like_params.append(f"{_LIKE_WILDCARD}{str(query).strip()}{_LIKE_WILDCARD}")

    where_parts: list[str] = []
    if like_params:
        where_parts.append("topic_label LIKE ?")
    if topics_clause:
        where_parts.append(topics_clause.replace(" AND ", "", 1))
    if time_clause:
        where_parts.append(time_clause.replace(" AND ", "", 1))

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = (
        "SELECT segment_id, session_id, current_tier, current_score, "
        "topic_label, fog_anchor, start_at, end_at, fog_state, silence_state "
        "FROM segments"
        f"{where_sql} "
        "ORDER BY current_score DESC, start_at DESC LIMIT ?"
    )

    params: list[Any] = like_params + topics_params + time_params + [safe_limit]
    return db_query(sql, tuple(params), path=path)


# ---------------------------------------------------------------------------
# 主通道 1：FTS5 全文搜索（L0-细节）
# ---------------------------------------------------------------------------


def search_sessions(
    query: Optional[str] = None,
    time_window: Optional[tuple[int, int]] = None,
    topics: Optional[list[str]] = None,
    limit: int = 20,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """FTS5 搜索 messages_fts，关联到 segments（主通道 1）。

    算法：
        1. FTS5 MATCH 找出命中 messages；
        2. JOIN sessions 取元信息；
        3. JOIN segments 按 ``message.seq IN [start_msg_seq, end_msg_seq]``
           找到所属段落；
        4. 额外应用 ``topics`` / ``time_window`` 过滤；
        5. 去重后按 ``current_score DESC`` 返回。

    返回：list[dict]，字段同 ``search_segments``，多带 ``hit_count``。
    """
    tw = _validate_time_window(time_window)
    safe_limit = _coerce_limit(limit, default=20)

    # query 为空时直接走 segments.topic_label LIKE 路径（覆盖退路）
    fts_q = _sanitize_fts_query(query or "")
    if not fts_q:
        # 复用 segments 搜索，避免重复实现 LIKE 过滤
        return search_segments(
            query=query, time_window=time_window,
            topics=topics, limit=safe_limit, path=path,
        )

    topics_clause, topics_params = _build_topics_filter(topics)
    time_clause, time_params = _build_time_filter(tw, _TIME_COL_SESSION)

    sql = (
        "SELECT s.segment_id, s.session_id, s.current_tier, s.current_score, "
        "s.topic_label, s.fog_anchor, s.start_at, s.end_at, "
        "s.fog_state, s.silence_state, "
        "COUNT(m.message_id) AS hit_count "
        "FROM messages_fts fts "
        "JOIN messages m ON m.rowid = fts.rowid "
        "JOIN sessions sess ON sess.session_id = m.session_id "
        "JOIN segments s ON s.session_id = m.session_id "
        "AND m.seq BETWEEN s.start_msg_seq AND s.end_msg_seq "
        "WHERE messages_fts MATCH ?"
        f"{topics_clause}"
        f"{time_clause}"
        " GROUP BY s.segment_id, s.session_id, s.current_tier, s.current_score, "
        "s.topic_label, s.fog_anchor, s.start_at, s.end_at, s.fog_state, s.silence_state "
        "ORDER BY s.current_score DESC, hit_count DESC LIMIT ?"
    )

    params: list[Any] = [fts_q] + topics_params + time_params + [safe_limit]
    return db_query(sql, tuple(params), path=path)


# ---------------------------------------------------------------------------
# 邻居展开
# ---------------------------------------------------------------------------


def expand_neighbors(
    segment_ids: list[str],
    window: int = _DEFAULT_NEIGHBOR_WINDOW,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """按 session_id + start_at 排序，扩展每个段落的邻居。

    行为：
        - 输入空 / ``window==0`` → 返回空列表。
        - 对每个 segment，找到同 session 内按 ``start_at`` 排序的相邻
          段（前后各 ``window`` 个），去重后追加到结果。

    返回：list[dict]（包含 segment_id 的所有段）。
    """
    if not segment_ids or window <= 0:
        return []

    # 1) 取输入段的 (session_id, start_at)
    placeholders = ",".join("?" for _ in segment_ids)
    seeds = db_query(
        f"SELECT segment_id, session_id, start_at FROM segments "
        f"WHERE segment_id IN ({placeholders})",
        tuple(segment_ids),
        path=path,
    )
    if not seeds:
        return []

    # 2) 按 session 分组
    by_session: dict[str, list[dict]] = {}
    for s in seeds:
        by_session.setdefault(s["session_id"], []).append(s)

    # 3) 对每个 session：拉出该 session 全部段并按 start_at 排序
    neighbor_ids: list[str] = []
    seen: set[str] = set()
    for session_id, group in by_session.items():
        all_segs = db_query(
            "SELECT segment_id, start_at FROM segments "
            "WHERE session_id = ? ORDER BY start_at ASC",
            (session_id,),
            path=path,
        )
        if not all_segs:
            continue
        # 本 session 内 seed 段在 all_segs 中的位置
        seed_positions: set[int] = set()
        for g in group:
            for idx, row in enumerate(all_segs):
                if row["segment_id"] == g["segment_id"]:
                    seed_positions.add(idx)
                    break
        for pos in seed_positions:
            for delta in range(-window, window + 1):
                if delta == 0:
                    continue
                npos = pos + delta
                if 0 <= npos < len(all_segs):
                    nid = all_segs[npos]["segment_id"]
                    if nid not in seen:
                        seen.add(nid)
                        neighbor_ids.append(nid)

    if not neighbor_ids:
        return []

    placeholders2 = ",".join("?" for _ in neighbor_ids)
    return db_query(
        "SELECT segment_id, session_id, current_tier, current_score, "
        "topic_label, fog_anchor, start_at, end_at, fog_state, silence_state "
        f"FROM segments WHERE segment_id IN ({placeholders2})",
        tuple(neighbor_ids),
        path=path,
    )


# ---------------------------------------------------------------------------
# Rerank（Jaccard + 段分数混合）
# ---------------------------------------------------------------------------


def rerank(
    query: str,
    candidates: list[dict],
    path: Optional[Union[Path, str]] = None,  # noqa: ARG001
) -> list[dict]:
    """对候选段按 ``Jaccard + score`` 混合得分重排。

    评分公式：
        ``final = W_J * jaccard + W_S * normalize(current_score)``
    - ``W_J = 0.7`` / ``W_S = 0.3``（常量）。
    - ``normalize`` 把 ``current_score`` 映射到 ``[0,1]``（除以 100）。
    - ``path`` 参数预留（M3 接入嵌入相似度时使用）。

    返回：list[dict]，每个 dict 多了 ``score`` 字段（即最终得分）。
    排序按 ``score DESC``。
    """
    if not candidates:
        return []

    q_kws = _query_keyword_set(query)
    # query 没有关键词时退化为按 current_score 排序
    if not q_kws:
        scored = []
        for c in candidates:
            scored.append({
                **c,
                "score": _WEIGHT_SCORE
                * (_coerce_score_value(c.get("current_score")) / _SCORE_NORMALIZE),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored

    scored = []
    for c in candidates:
        seg_kws = _segment_keyword_set(c)
        if seg_kws:
            inter = len(q_kws & seg_kws)
            union = len(q_kws | seg_kws)
            jaccard = (inter / union) if union else 0.0
        else:
            jaccard = 0.0
        norm = _coerce_score_value(c.get("current_score")) / _SCORE_NORMALIZE
        final = _WEIGHT_JACCARD * jaccard + _WEIGHT_SCORE * max(0.0, min(1.0, norm))
        scored.append({**c, "score": float(final)})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def _coerce_score_value(value: Any) -> float:
    """规整段 current_score 为 float；非法值回落 0。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 段内消息加载（L0-细节）
# ---------------------------------------------------------------------------


def _messages_in_segment(
    session_id: str,
    start_seq: int,
    end_seq: int,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """取段内全部消息（按 seq 升序）。

    复用 ``get_session_messages`` 后按 seq 区间截取；雾化消息
    （``content IS NULL``）保留字段但 ``content`` 显式为 ``None``。
    """
    if not session_id:
        return []
    all_msgs = get_session_messages(session_id, path=path)
    return [m for m in all_msgs if start_seq <= int(m["seq"]) <= end_seq]


# ---------------------------------------------------------------------------
# 主入口：recall
# ---------------------------------------------------------------------------


def recall(
    query: str,
    time_window: Optional[tuple[int, int]] = None,
    topics: Optional[list[str]] = None,
    top_k: int = 5,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """召回主入口：双通道召回 + 合并 + 邻居展开 + 重排 + 加载段内 L0。

    返回 list[dict]，每条形如：
        ``{segment_id, session_id, tier, score, messages}``
    - ``messages`` 是段内消息列表（含 content / seq / role / ...）。
    - ``score`` 是 rerank 最终得分。
    - 数量上限 ``top_k``（默认 5）。

    行为：
        - 空 query / 仅含停用词 → 走 segments.topic_label LIKE 路径。
        - 两通道结果按 ``segment_id`` 去重。
        - 邻居展开后再 rerank；ranking 前缀 ``top_k`` 截取。
        - 不修改数据库（只读）。
    """
    tw = _validate_time_window(time_window)
    safe_top_k = _coerce_limit(top_k, default=5)
    # 多召回一些邻居再 rerank；上限 ×4 防止邻居爆炸
    fetch_limit = safe_top_k * 4

    # T28 LRU cache check
    cache_key = _recall_cache_key(query, tw, topics, safe_top_k, path)
    hit = _cache_get(cache_key)
    if hit is not None:
        return list(hit)

    cands_a = search_sessions(
        query=query, time_window=tw, topics=topics,
        limit=fetch_limit, path=path,
    )
    cands_b = search_segments(
        query=query, time_window=tw, topics=topics,
        limit=fetch_limit, path=path,
    )

    # 去重（按 segment_id；保留先出现的）
    seen: set[str] = set()
    merged: list[dict] = []
    for c in cands_a + cands_b:
        sid = c.get("segment_id")
        if sid and sid not in seen:
            seen.add(sid)
            merged.append(c)

    if not merged:
        return []

    # 邻居展开（以合并后的段为中心）
    expanded = expand_neighbors(
        [c["segment_id"] for c in merged if c.get("segment_id")],
        window=_DEFAULT_NEIGHBOR_WINDOW, path=path,
    )
    for e in expanded:
        eid = e.get("segment_id")
        if eid and eid not in seen:
            seen.add(eid)
            merged.append(e)

    # rerank + 截取
    ranked = rerank(query, merged, path=path)
    picked = ranked[:safe_top_k]

    # 加载段内 L0-细节
    results: list[dict] = []
    for seg in picked:
        sid = seg.get("session_id")
        # 段内消息 seq 范围：start_msg_seq/end_msg_seq 在原 DB 列里
        # search_* 已 SELECT 了 start_at/end_at；这里再拉一次拿 seq
        rows = db_query(
            "SELECT start_msg_seq, end_msg_seq FROM segments "
            "WHERE segment_id = ?",
            (seg["segment_id"],),
            path=path,
        )
        if not rows:
            continue
        start_seq = int(rows[0]["start_msg_seq"])
        end_seq = int(rows[0]["end_msg_seq"])
        msgs = _messages_in_segment(sid, start_seq, end_seq, path=path)
        results.append({
            "segment_id": seg["segment_id"],
            "session_id": sid,
            "tier": seg.get("current_tier") or "L0",
            "score": float(seg.get("score", 0.0)),
            "messages": msgs,
        })
    _cache_put(cache_key, results)
    return results


# ---------------------------------------------------------------------------
# 兜底：get_recent_sessions + recall_with_fallback
# ---------------------------------------------------------------------------


def get_recent_sessions(
    limit: int = _FALLBACK_LIMIT,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """获取最近的会话列表（按 ``started_at DESC`` 排序）。

    返回字段：
        ``session_id``, ``started_at``, ``ended_at``, ``topic_label``,
        ``message_count``, ``current_score``, ``agent_source``,
        ``is_important``, ``is_archived``。

    用于 ``recall_with_fallback`` 召回为空时的兜底展示。
    """
    safe_limit = _coerce_limit(limit, default=_FALLBACK_LIMIT)
    return db_query(
        "SELECT session_id, started_at, ended_at, topic_label, "
        "message_count, current_score, agent_source, is_important, "
        "is_archived, cycle_tag "
        "FROM sessions ORDER BY started_at DESC LIMIT ?",
        (safe_limit,),
        path=path,
    )


def recall_with_fallback(
    query: str,
    time_window: Optional[tuple[int, int]] = None,
    topics: Optional[list[str]] = None,
    top_k: int = 5,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """召回失败兜底：recall 为空时返回 ``get_recent_sessions``。

    返回元素统一形如：
        ``{session_id, topic_label, started_at, message_count,
           current_score, agent_source, is_important, is_archived,
           is_fallback: True}``

    若 recall 有结果，原样返回。
    """
    results = recall(
        query=query, time_window=time_window,
        topics=topics, top_k=top_k, path=path,
    )
    if results:
        return results

    recent = get_recent_sessions(limit=_FALLBACK_LIMIT, path=path)
    fallback: list[dict] = []
    for r in recent:
        fallback.append({
            "session_id": r.get("session_id"),
            "topic_label": r.get("topic_label"),
            "started_at": r.get("started_at"),
            "message_count": r.get("message_count"),
            "current_score": r.get("current_score"),
            "agent_source": r.get("agent_source"),
            "is_important": r.get("is_important"),
            "is_archived": r.get("is_archived"),
            "is_fallback": True,
        })
    return fallback


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


__all__ = [
    "recall",
    "search_sessions",
    "search_segments",
    "expand_neighbors",
    "rerank",
    "recall_with_fallback",
    "get_recent_sessions",
]
