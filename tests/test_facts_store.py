"""src/llm/facts_store.py 测试：C1.3 facts 表 CRUD + FTS5 搜索。

覆盖：
1. test_create_fact_basic           create_fact 返回 UUID，存到 DB
2. test_get_fact_by_id              get_fact 返回完整 dict
3. test_list_facts_by_session       按 session_id 过滤
4. test_list_facts_by_segment       按 segment_id 过滤
5. test_facts_search_fts5           FTS5 全文搜索命中
6. test_delete_fact                 删除后 get_fact 返回 None
7. test_count_facts                 count_facts 准确
"""
from __future__ import annotations

from pathlib import Path

from src.l0.session_writer import create_session, write_message


def test_create_fact_basic(mtca_db: Path) -> None:
    from src.llm.facts_store import create_fact, get_fact

    sid = create_session(path=mtca_db)
    fid = create_fact(
        session_id=sid,
        content="Qwen3 默认是 reasoning 模型",
        path=mtca_db,
    )
    assert isinstance(fid, str) and len(fid) == 36
    row = get_fact(fid, path=mtca_db)
    assert row is not None
    assert row["content"] == "Qwen3 默认是 reasoning 模型"
    assert row["session_id"] == sid
    assert row["source"] == "extracted"


def test_get_fact_by_id(mtca_db: Path) -> None:
    from src.llm.facts_store import create_fact, get_fact

    sid = create_session(path=mtca_db)
    fid = create_fact(
        session_id=sid,
        content="测试 fact",
        confidence=0.85,
        tags="['ai','model']",
        path=mtca_db,
    )
    row = get_fact(fid, path=mtca_db)
    assert row["confidence"] == 0.85
    assert row["tags"] == "['ai','model']"

    assert get_fact("nonexistent-uuid", path=mtca_db) is None


def test_list_facts_by_session(mtca_db: Path) -> None:
    from src.llm.facts_store import create_fact, list_facts_by_session

    s1 = create_session(path=mtca_db)
    s2 = create_session(path=mtca_db)
    create_fact(s1, "事实 A", path=mtca_db)
    create_fact(s1, "事实 B", path=mtca_db)
    create_fact(s2, "事实 C", path=mtca_db)

    rows1 = list_facts_by_session(s1, path=mtca_db)
    rows2 = list_facts_by_session(s2, path=mtca_db)
    assert len(rows1) == 2
    assert len(rows2) == 1
    assert {r["content"] for r in rows1} == {"事实 A", "事实 B"}


def test_list_facts_by_segment(mtca_db: Path) -> None:
    import uuid
    from src.llm.facts_store import create_fact, list_facts_by_segment
    from src.store.sqlite import execute

    sid = create_session(path=mtca_db)
    write_message(sid, "user", "hi", path=mtca_db)
    write_message(sid, "assistant", "hello", path=mtca_db)
    now = 1000
    seg_a = str(uuid.uuid4())
    seg_b = str(uuid.uuid4())
    for seg_id in (seg_a, seg_b):
        execute(
            "INSERT INTO segments (segment_id, session_id, start_msg_seq, end_msg_seq, start_at, end_at) VALUES (?, ?, 1, 2, ?, ?)",
            (seg_id, sid, now, now),
            path=mtca_db,
        )

    create_fact(sid, "segment-A fact 1", segment_id=seg_a, path=mtca_db)
    create_fact(sid, "segment-A fact 2", segment_id=seg_a, path=mtca_db)
    create_fact(sid, "segment-B fact 1", segment_id=seg_b, path=mtca_db)

    a = list_facts_by_segment(seg_a, path=mtca_db)
    b = list_facts_by_segment(seg_b, path=mtca_db)
    assert len(a) == 2
    assert len(b) == 1


def test_facts_search_fts5(mtca_db: Path) -> None:
    from src.llm.facts_store import create_fact, search_facts

    sid = create_session(path=mtca_db)
    create_fact(sid, "Qwen3 是 reasoning 模型", path=mtca_db)
    create_fact(sid, "Llama2 是早期 Meta 模型", path=mtca_db)
    create_fact(sid, "MTCA 用 SQLite 做存储", path=mtca_db)

    hits = search_facts("Qwen3", top_k=5, path=mtca_db)
    assert len(hits) >= 1
    assert any("Qwen3" in h["content"] for h in hits)

    # 多关键词（OR）
    hits2 = search_facts("SQLite OR reasoning", top_k=10, path=mtca_db)
    assert len(hits2) >= 2


def test_delete_fact(mtca_db: Path) -> None:
    from src.llm.facts_store import create_fact, delete_fact, get_fact

    sid = create_session(path=mtca_db)
    fid = create_fact(sid, "要删除的 fact", path=mtca_db)
    assert delete_fact(fid, path=mtca_db) is True
    assert get_fact(fid, path=mtca_db) is None
    # 重复删除返回 False
    assert delete_fact(fid, path=mtca_db) is False


def test_count_facts(mtca_db: Path) -> None:
    from src.llm.facts_store import count_facts, create_fact

    assert count_facts(path=mtca_db) == 0
    sid = create_session(path=mtca_db)
    create_fact(sid, "f1", path=mtca_db)
    create_fact(sid, "f2", path=mtca_db)
    create_fact(sid, "f3", path=mtca_db)
    assert count_facts(path=mtca_db) == 3
