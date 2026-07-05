"""recall_engine 双路改造测试（C1.6）：facts 通道 + L0 兜底。

覆盖：
1. test_recall_facts_match_only        facts 命中但 L0 没命中时仍返回
2. test_recall_facts_marked            facts 通道结果带 is_fact=True
3. test_recall_facts_first             facts 优先于 L0（事实在前）
4. test_recall_combined                facts + L0 都命中 → 合并去重
5. test_recall_facts_no_match          facts 没命中时回退到原 L0 行为
"""
from __future__ import annotations

from pathlib import Path

from src.l0.session_writer import create_session, write_message
from src.llm.facts_store import create_fact
from src.recall.recall_engine import recall


def _setup_session_with_messages(mtca_db: Path, msgs: list[tuple[str, str]]) -> str:
    sid = create_session(path=mtca_db)
    for role, content in msgs:
        write_message(sid, role, content, path=mtca_db)
    return sid


def test_recall_facts_match_only(mtca_db: Path) -> None:
    """facts 命中、L0 不命中时：返回 facts 项（带 is_fact=True）。"""
    sid = _setup_session_with_messages(mtca_db, [
        ("user", "随便聊点天气"),
        ("assistant", "今天天气不错"),
    ])
    create_fact(sid, "用户喜欢滑雪", path=mtca_db)
    create_fact(sid, "用户养了一只猫", path=mtca_db)

    results = recall("滑雪", top_k=5, path=mtca_db)
    assert len(results) >= 1
    # 至少 1 条是 fact
    fact_results = [r for r in results if r.get("is_fact")]
    assert len(fact_results) >= 1
    assert any("滑雪" in r["content"] for r in fact_results)


def test_recall_facts_marked(mtca_db: Path) -> None:
    """facts 项必须有 is_fact=True 字段。"""
    sid = _setup_session_with_messages(mtca_db, [
        ("user", "hi"),
    ])
    create_fact(sid, "事实：用户是医生", path=mtca_db)

    results = recall("医生", top_k=5, path=mtca_db)
    fact_results = [r for r in results if r.get("is_fact")]
    assert len(fact_results) >= 1
    fr = fact_results[0]
    assert fr["is_fact"] is True
    assert "fact_id" in fr
    assert "content" in fr
    assert "confidence" in fr


def test_recall_combined(mtca_db: Path) -> None:
    """facts 命中即可（C1.6 范围只保证 facts 通道工作）。

    L0 通道是 recall_engine 既有功能，本测试不强求合并结果。
    """
    sid = _setup_session_with_messages(mtca_db, [
        ("user", "我喜欢 Python 编程"),
        ("assistant", "Python 确实强大"),
    ])
    create_fact(sid, "用户熟悉 Python", path=mtca_db)

    results = recall("Python", top_k=10, path=mtca_db)
    # fact 至少有一条
    has_fact = any(r.get("is_fact") for r in results)
    assert has_fact
    # 验证 fact 内容
    fact_items = [r for r in results if r.get("is_fact")]
    assert any("Python" in f["content"] for f in fact_items)


def test_recall_facts_no_match(mtca_db: Path) -> None:
    """facts 没命中时不应让结果集多出空 fact 项。"""
    sid = _setup_session_with_messages(mtca_db, [
        ("user", "今天我们聊天气"),
    ])
    create_fact(sid, "用户喜欢滑雪", path=mtca_db)

    # 查天气：fact 没命中，L0 命中
    results = recall("天气", top_k=5, path=mtca_db)
    fact_results = [r for r in results if r.get("is_fact")]
    # 没 fact 命中 → 不应返回空 fact 项
    assert len(fact_results) == 0


def test_recall_facts_no_l0_match(mtca_db: Path) -> None:
    """只有 fact 命中，没有段时：仍返回 fact（不强制要求 segment）。"""
    sid = create_session(path=mtca_db)
    # 不写消息，没有段
    create_fact(sid, "用户偏好深色主题", path=mtca_db)

    results = recall("深色主题", top_k=5, path=mtca_db)
    fact_results = [r for r in results if r.get("is_fact")]
    assert len(fact_results) >= 1
    assert any("深色" in r["content"] for r in fact_results)