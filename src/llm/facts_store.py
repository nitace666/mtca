"""facts 表 CRUD（C1.3：LLM 提炼的事实存储）。

事实（fact）是 LLM 从对话中提炼的"可被记住的命题"，
不属于 L0-不可变；用户 / AI 都可增删改，便于纠错。

公共 API：
- ``create_fact(session_id, content, segment_id=None,
  source="extracted", confidence=1.0, tags=None, path=None) -> str``
- ``get_fact(fact_id, path=None) -> dict | None``
- ``list_facts_by_session(session_id, path=None) -> list[dict]``
- ``list_facts_by_segment(segment_id, path=None) -> list[dict]``
- ``search_facts(query, top_k=10, path=None) -> list[dict]``
- ``delete_fact(fact_id, path=None) -> bool``
- ``count_facts(path=None) -> int``
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Union

from src.store.sqlite import get_connection, query


# 有效 source 取值
_VALID_SOURCES: frozenset[str] = frozenset({"user", "assistant", "extracted", "imported"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sanitize_fts_query(q: str) -> str:
    """FTS5 关键词提取：去掉特殊字符，保留字母/数字/下划线/空白/CJK。

    与 recall_engine._sanitize_fts_query 策略一致。
    """
    if not q:
        return ""
    # 去掉引号 / 括号 / 星号 / 冒号 / 脱字符
    q = re.sub(chr(34) + chr(39) + chr(43), " ", q)
    q = re.sub(r"[*^():]+", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def create_fact(
    session_id: str,
    content: str,
    segment_id: Optional[str] = None,
    source: str = "extracted",
    confidence: float = 1.0,
    tags: Optional[str] = None,
    path: Optional[Union[Path, str]] = None,
) -> str:
    """插入一条 fact，返回 fact_id (UUID4)。

    参数校验：
    - ``content`` 必须是非空字符串
    - ``source`` 必须在 _VALID_SOURCES 内
    - ``confidence`` ∈ [0.0, 1.0]
    - ``tags`` 应为 JSON 字符串（None 表示无标签）
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content 必须是非空字符串")
    if source not in _VALID_SOURCES:
        raise ValueError(f"source 非法：{source!r}；允许：{sorted(_VALID_SOURCES)}")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError("confidence 必须在 [0.0, 1.0]")

    fid = str(uuid.uuid4())
    now = _now_ms()
    try:
        with get_connection(path) as conn:
            conn.execute(
                "INSERT INTO facts "
                "(fact_id, session_id, segment_id, content, source, confidence, tags, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fid, session_id, segment_id, content, source, confidence, tags, now),
            )
    except RuntimeError:
        raise
    return fid


def get_fact(
    fact_id: str,
    path: Optional[Union[Path, str]] = None,
) -> Optional[dict]:
    """按 fact_id 查询；不存在返回 None。"""
    rows = query(
        "SELECT * FROM facts WHERE fact_id = ?",
        (fact_id,),
        path=path,
    )
    return rows[0] if rows else None


def list_facts_by_session(
    session_id: str,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """按 session_id 列出全部 facts，按 created_at ASC。"""
    return query(
        "SELECT * FROM facts WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
        path=path,
    )


def list_facts_by_segment(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """按 segment_id 列出 facts。"""
    return query(
        "SELECT * FROM facts WHERE segment_id = ? ORDER BY created_at ASC",
        (segment_id,),
        path=path,
    )


def search_facts(
    query_text: str,
    top_k: int = 10,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """FTS5 全文搜索 facts_fts，失败时回退 LIKE。

    返回按 bm25 / 匹配位置排序的 top_k 条；query_text 为空时返回空列表。

    设计取舍：
    - FTS5 unicode61 不分中文（中文是连续字符），先用 FTS5 试
    - 如果 FTS5 返回空，回退到 ``content LIKE '%kw%'``（每个空格分隔的关键词）
    - 这样同时支持英文 FTS5 排序 + 中文 LIKE 兜底
    """
    if not query_text or not query_text.strip():
        return []
    safe_q = _sanitize_fts_query(query_text)
    if not safe_q:
        return []
    safe_top_k = max(1, int(top_k))

    # 1) FTS5 路径
    try:
        fts_rows = query(
            "SELECT f.* FROM facts f "
            "JOIN facts_fts fts ON f.rowid = fts.rowid "
            "WHERE facts_fts MATCH ? "
            "ORDER BY bm25(facts_fts) LIMIT ?",
            (safe_q, safe_top_k),
            path=path,
        )
        if fts_rows:
            return fts_rows
    except Exception:
        pass

    # 2) LIKE 兜底：把关键词拆出来 OR 匹配
    keywords = [w for w in safe_q.split() if len(w) >= 1]
    if not keywords:
        return []
    # 每个关键词 LIKE %kw%；rowid 取并集
    where_parts = " OR ".join(["f.content LIKE ?"] * len(keywords))
    like_params = tuple(f"%{kw}%" for kw in keywords)
    rows = query(
        f"SELECT DISTINCT f.* FROM facts f WHERE {where_parts} "
        f"ORDER BY f.created_at DESC LIMIT ?",
        like_params + (safe_top_k,),
        path=path,
    )
    return rows


def delete_fact(
    fact_id: str,
    path: Optional[Union[Path, str]] = None,
) -> bool:
    """删除一条 fact；存在并删除返回 True，否则 False。"""
    with get_connection(path) as conn:
        cur = conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
        return int(cur.rowcount or 0) > 0


def count_facts(path: Optional[Union[Path, str]] = None) -> int:
    """facts 总条数。"""
    rows = query("SELECT COUNT(*) AS n FROM facts", path=path)
    return int(rows[0]["n"]) if rows else 0


__all__ = [
    "create_fact",
    "get_fact",
    "list_facts_by_session",
    "list_facts_by_segment",
    "search_facts",
    "delete_fact",
    "count_facts",
]
