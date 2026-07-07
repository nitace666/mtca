# -*- coding: utf-8 -*-
"""tests/test_recall_vector_fallback.py -- Step 7 bugfix：B3 + B4 回归测试（红）

设计：
- 全 mock：不调真 Ollama / ChromaDB
- mock search_sessions / search_segments / _search_facts_channel 全返 []
- mock VectorIndex.search 返 ≥ 1 个 hit
- B3 修复前：baseline 空 → recall() 早 return [] → 测试断言失败（红）
- B3 修复后：向量层"救命" → recall() 返 vector hit → 测试通过（绿）

B4 测试：
- vector=1 第一次 recall() 写入 cache
- 改 vector=0 第二次 recall()：当前实现 cache 命中错位 → 测试红
- B4 修复后：cache key 含 vector_state → 第二次不命中 cache → 真跑 → 返不同结果
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# === 共享 helper ===

def _set_enabled(mtca_db: Path, enabled: bool) -> None:
    """写 settings.vector_recall_enabled = '1' or '0'"""
    conn = sqlite3.connect(mtca_db)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
        ("vector_recall_enabled", "1" if enabled else "0", 0),
    )
    conn.commit()
    conn.close()


def _ensure_tables(mtca_db: Path) -> None:
    """建空 segments + messages 表（recall() 需要 db_query / _messages_in_segment）"""
    conn = sqlite3.connect(mtca_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS segments ("
        "segment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "start_msg_seq INTEGER NOT NULL, end_msg_seq INTEGER NOT NULL, "
        "start_at INTEGER NOT NULL, end_at INTEGER NOT NULL, "
        "current_score REAL DEFAULT 100, current_tier TEXT DEFAULT 'L0'"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "seq INTEGER NOT NULL, role TEXT NOT NULL, "
        "content TEXT, tool_calls TEXT, tool_results TEXT, "
        "token_count INTEGER, created_at INTEGER NOT NULL"
        ")"
    )
    conn.commit()
    conn.close()


def _seed_segment(mtca_db: Path, seg_id: str, sess_id: str) -> None:
    """灌 1 段 + 1 message 让 recall() 加载 messages 时不返 []"""
    conn = sqlite3.connect(mtca_db)
    conn.execute(
        "INSERT OR REPLACE INTO segments(segment_id, session_id, start_msg_seq, "
        "end_msg_seq, start_at, end_at, current_tier, current_score) "
        "VALUES (?, ?, 1, 1, 0, 0, 'L0', 100)",
        (seg_id, sess_id),
    )
    conn.execute(
        "INSERT OR REPLACE INTO messages(message_id, session_id, seq, role, "
        "content, created_at) VALUES (?, ?, 1, 'user', 'mock', 0)",
        (f"m-{seg_id}", sess_id),
    )
    conn.commit()
    conn.close()


# === B3 测试 ===

def test_向量层在baseline空时救命(mtca_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3：FTS5 + facts baseline 都空时，向量层应当救命返回结果。

    当前实现：recall() 在 L1240-1245 `if not merged: return []` 早 return，
    根本不到 L1302+ 的向量块 → 期望 fail（红）。
    修后：删 `return []`，让流程继续到向量块 → 期望通过（绿）。
    """
    from src.recall.recall_engine import recall

    _ensure_tables(mtca_db)
    _set_enabled(mtca_db, True)

    from src.recall import recall_engine as _re
    _re._RECALL_CACHE.clear()

    # mock: baseline（FTS5 + facts）全空
    monkeypatch.setattr(
        "src.recall.recall_engine.search_sessions", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "src.recall.recall_engine.search_segments", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        "src.recall.recall_engine._search_facts_channel", lambda *a, **kw: []
    )

    # mock: VectorIndex 构造返 mock 实例（_ensure_client 不会被调，因为我们 mock 了 search）
    vector_hit = {
        "segment_id": "vec-rescue-hit",
        "score": 0.95,
        "chunk_idx": -1,
    }

    with patch("src.recall.vector_index.VectorIndex") as MockVI:
        mock_inst = MagicMock()
        mock_inst.search.return_value = [vector_hit]
        MockVI.return_value = mock_inst

        # mock 加载 messages 也得返非空，否则 db_query 找不到 rows -> skip -> 空结果
        monkeypatch.setattr(
            "src.recall.recall_engine._messages_in_segment",
            lambda *a, **kw: [{"message_id": "m1", "session_id": "s1",
                               "seq": 1, "role": "user", "content": "mock",
                               "tool_calls": None, "tool_results": None,
                               "token_count": None, "created_at": 0}],
        )
        # mock db_query 让 SELECT segment_id/start_at (expand_neighbors) 和
        # SELECT start_msg_seq/end_msg_seq (段加载) 都返非空
        def _db_mock_b3(*a, **kw):
            return [{"segment_id": "vec-rescue-hit", "session_id": "s1",
                     "start_at": 0, "start_msg_seq": 1, "end_msg_seq": 1}]
        monkeypatch.setattr(
            "src.recall.recall_engine.db_query", _db_mock_b3
        )

        results = recall("test query", top_k=5, path=str(mtca_db))

    assert len(results) >= 1, (
        f"baseline 空 + vector 启用应返 ≥1 hits（B3 修复），实际 {len(results)} 个结果"
    )
    seg_ids = [r.get("segment_id") for r in results]
    assert "vec-rescue-hit" in seg_ids, (
        f"应包含向量救命 hit vec-rescue-hit，实际 seg_ids={seg_ids}"
    )


# === B4 测试 ===

def test_cache_key含vector状态(mtca_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B4：同一 query 在 vector 开/关切换后，cache 不应命中错位。"""
    from src.recall.recall_engine import recall

    _ensure_tables(mtca_db)
    _set_enabled(mtca_db, False)

    from src.recall import recall_engine as _re
    _re._RECALL_CACHE.clear()

    # mock db_query 让 expand_neighbors 和段加载都返非空 row
    def _db_mock_b4(*a, **kw):
        # 返含 session_id + start_at + start_msg_seq/end_msg_seq
        # 用空 list（不模拟邻居扩展避免 KeyError，但段加载部分要返 1 row）
        # expand_neighbors 看到空 seeds 返 []，安全
        return [{"segment_id": "x", "session_id": "s",
                 "start_at": 0, "start_msg_seq": 1, "end_msg_seq": 1}]
    monkeypatch.setattr(
        "src.recall.recall_engine.db_query", _db_mock_b4
    )
    monkeypatch.setattr(
        "src.recall.recall_engine._messages_in_segment",
        lambda *a, **kw: [{"message_id": "m1", "session_id": "s1", "seq": 1,
                            "role": "user", "content": "mock",
                            "tool_calls": None, "tool_results": None,
                            "token_count": None, "created_at": 0}],
    )

    with patch("src.recall.vector_index.VectorIndex") as MockVI:
        mock_inst = MagicMock()
        mock_inst.search.return_value = []
        MockVI.return_value = mock_inst

        # 第一次：vector=1, mock search_sessions 返 vec-only-hit，灌段
        _set_enabled(mtca_db, True)
        _seed_segment(mtca_db, "vec-only-hit", "s1")
        monkeypatch.setattr(
            "src.recall.recall_engine.search_sessions",
            lambda *a, **kw: [{"segment_id": "vec-only-hit", "session_id": "s1",
                                "current_score": 80.0, "current_tier": "L0",
                                "start_at": 0, "end_at": 0,
                                "topic_label": "", "fog_anchor": ""}],
        )
        monkeypatch.setattr(
            "src.recall.recall_engine.search_segments", lambda *a, **kw: []
        )
        monkeypatch.setattr(
            "src.recall.recall_engine._search_facts_channel", lambda *a, **kw: []
        )

        r1 = recall("same query", top_k=5, path=str(mtca_db))
        cache_size_after_r1 = len(_re._RECALL_CACHE)
        assert cache_size_after_r1 >= 1, (
            f"第一次 recall 后 cache 应非空，实际 {cache_size_after_r1}"
        )
        seg_ids_r1 = [r.get("segment_id") for r in r1]
        assert "vec-only-hit" in seg_ids_r1, (
            f"第一次应返 vec-only-hit，实际 {seg_ids_r1}"
        )

        # 第二次：vector=0, mock 让 search_sessions 返 fts-only-hit，灌段
        _set_enabled(mtca_db, False)
        _seed_segment(mtca_db, "fts-only-hit", "s2")
        monkeypatch.setattr(
            "src.recall.recall_engine.search_sessions",
            lambda *a, **kw: [{"segment_id": "fts-only-hit", "session_id": "s2",
                                "current_score": 50.0, "current_tier": "L0",
                                "start_at": 0, "end_at": 0,
                                "topic_label": "", "fog_anchor": ""}],
        )

        r2 = recall("same query", top_k=5, path=str(mtca_db))

        seg_ids_r2 = [r.get("segment_id") for r in r2]
        assert "vec-only-hit" not in seg_ids_r2, (
            f"vector=off 应不返 vec-only-hit（cache 错位），实际 seg_ids_r2={seg_ids_r2}。"
            f" 期望 cache key 含 vector_enabled 状态。"
        )
        assert "fts-only-hit" in seg_ids_r2, (
            f"vector=off 应返 fts-only-hit（真跑 mock），实际 seg_ids_r2={seg_ids_r2}"
        )
