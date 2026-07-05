"""src/llm/extractor.py 测试：C1.4 LLM 事实提炼。

覆盖：
1. test_build_prompt_basic              单条 prompt 模板生成
2. test_build_prompt_empty_content     空内容抛出
3. test_extract_one_parses_json         LLM 返回 JSON 列表 → facts
4. test_extract_one_handles_invalid_json  LLM 返回非 JSON 时返回空
5. test_extract_one_handles_plain_text  LLM 返回纯文本时按单事实包装
6. test_extract_from_messages           批量提炼（multi-message）
7. test_extract_and_store              自动写库 + 返回 fact_ids
8. test_extract_and_store_empty         空消息列表返回空 list，不抛错

通过传入 mock provider 避免依赖外部 LLM server。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.l0.session_writer import create_session


class _MockProvider:
    """最小 LLMProvider 实现，generate 返回预设 content。"""

    name = "mock"

    def __init__(self, content: str = "[]") -> None:
        self._content = content
        self.calls = 0

    def generate(self, prompt: str, **kwargs: Any) -> str:
        self.calls += 1
        return self._content

    def embed(self, text: str) -> list[float]:
        return [0.0] * 4

    def is_available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# 测试 1：prompt 模板
# ---------------------------------------------------------------------------
def test_build_prompt_basic() -> None:
    from src.llm.extractor import build_extraction_prompt
    p = build_extraction_prompt("我说我叫张三，喜欢吃面。")
    assert isinstance(p, dict)
    assert "system" in p
    assert "user" in p
    assert "我叫张三" in p["user"]
    assert "JSON" in p["system"] or "json" in p["system"]


def test_build_prompt_empty_content() -> None:
    from src.llm.extractor import build_extraction_prompt
    with pytest.raises(ValueError):
        build_extraction_prompt("")
    with pytest.raises(ValueError):
        build_extraction_prompt("   \n\t  ")


# ---------------------------------------------------------------------------
# 测试 3：parse JSON 列表
# ---------------------------------------------------------------------------
def test_extract_one_parses_json() -> None:
    llm_text = json.dumps([
        {"content": "用户叫张三", "confidence": 0.95, "tags": ["name"]},
        {"content": "用户喜欢吃面", "confidence": 0.85, "tags": ["food"]},
    ], ensure_ascii=False)
    prov = _MockProvider(content=llm_text)

    from src.llm.extractor import extract_facts
    facts = extract_facts("我说我叫张三，喜欢吃面。", provider=prov)
    assert prov.calls == 1
    assert len(facts) == 2
    assert facts[0]["content"] == "用户叫张三"
    assert facts[0]["confidence"] == 0.95
    assert "name" in facts[0]["tags"]


# ---------------------------------------------------------------------------
# 测试 4：非 JSON 返回 → 空 list（不抛错）
# ---------------------------------------------------------------------------
def test_extract_one_handles_invalid_json() -> None:
    prov = _MockProvider(content="我不知道怎么回答")

    from src.llm.extractor import extract_facts
    facts = extract_facts("莫名其妙的话", provider=prov)
    assert facts == []


# ---------------------------------------------------------------------------
# 测试 5：单事实 plain text 包装
# ---------------------------------------------------------------------------
def test_extract_one_handles_plain_text() -> None:
    """非 JSON 输出（包括事实陈述）→ []（保守：不存垃圾）。

    设计取舍：宁可漏存，不可错存。LLM 应被 prompt 强约束输出 JSON，
    非 JSON 视为违反契约。
    """
    prov = _MockProvider(content="用户叫李四")

    from src.llm.extractor import extract_facts
    facts = extract_facts("我叫李四", provider=prov)
    assert facts == []


# ---------------------------------------------------------------------------
# 测试 6：批量提炼（多 message）
# ---------------------------------------------------------------------------
def test_extract_from_messages() -> None:
    llm_text = json.dumps([
        {"content": "用户喜欢 Python", "confidence": 0.9, "tags": ["lang"]},
    ], ensure_ascii=False)
    prov = _MockProvider(content=llm_text)

    from src.llm.extractor import extract_facts_from_messages
    msgs = [
        {"role": "user", "content": "I like Python"},
        {"role": "assistant", "content": "OK noted."},
    ]
    facts = extract_facts_from_messages(msgs, provider=prov)
    assert len(facts) == 1
    assert "Python" in facts[0]["content"]


# ---------------------------------------------------------------------------
# 测试 7：extract_and_store（写库）
# ---------------------------------------------------------------------------
def test_extract_and_store(mtca_db: Path) -> None:
    llm_text = json.dumps([
        {"content": "测试 fact 1", "confidence": 0.8, "tags": ["t1"]},
        {"content": "测试 fact 2", "confidence": 0.7, "tags": ["t2"]},
    ], ensure_ascii=False)
    prov = _MockProvider(content=llm_text)

    from src.llm.extractor import extract_and_store
    sid = create_session(path=mtca_db)
    fids = extract_and_store(
        session_id=sid,
        text="随便说点什么让模型提炼",
        provider=prov,
        path=mtca_db,
    )
    assert len(fids) == 2
    from src.llm.facts_store import count_facts
    assert count_facts(path=mtca_db) == 2


# ---------------------------------------------------------------------------
# 测试 8：空消息不抛错（不调 LLM）
# ---------------------------------------------------------------------------
def test_extract_and_store_empty(mtca_db: Path) -> None:
    prov = _MockProvider(content="should not be used")

    from src.llm.extractor import extract_and_store
    sid = create_session(path=mtca_db)
    fids = extract_and_store(session_id=sid, text="", provider=prov, path=mtca_db)
    assert fids == []
    assert prov.calls == 0
    from src.llm.facts_store import count_facts
    assert count_facts(path=mtca_db) == 0