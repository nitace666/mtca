"""src/llm/extractor.py 三个 bug 修复测试（M2.5.8 C）。

覆盖：
1. test_llm_generate_passes_system_to_provider  Bug#1：system prompt 必须传给 LLM
2. test_extract_facts_retries_on_empty           Bug#2：空结果触发 retry
3. test_extract_facts_retries_max_2              Bug#2：retry 上限 2 次（total 3 次 LLM）
4. test_extract_facts_retries_on_parse_error     Bug#2：parse 失败也 retry
5. test_fence_greedy_matches_nested             Bug#3：fence 正则贪婪匹配嵌套 markdown

设计要点：
- 用 stateful mock provider：按调用次数返回不同 content
- Bug#1 验证：_llm_generate 把 system 内容拼到 prompt 末尾（修法 2）
- Bug#2 验证：extract_facts 加 max_retries=2 循环，空结果 / parse 失败自动重试
- Bug#3 验证：_JSON_FENCE_RE 改贪婪后能正确提取含嵌套反引号的 JSON
"""
from __future__ import annotations

import json
import re
from typing import Any

import pytest

from src.llm.extractor import (
    _JSON_FENCE_RE,
    _llm_generate,
    build_extraction_prompt,
    extract_facts,
)


class _StatefulMockProvider:
    """可按调用次数返回不同 content 的 mock provider，用于 retry 测试。

    第 0 次返回 contents[0]，第 1 次返回 contents[1]... 超出范围返回最后一项。
    记录每次调用的 prompt 文本，便于断言 system 是否被传。
    """

    name = "mock"

    def __init__(self, contents: list) -> None:
        assert contents, "contents 不能为空"
        self._contents = list(contents)
        self.calls = 0
        self.prompts = []
        self.kwargs_history = []

    def generate(self, prompt, **kwargs):
        idx = min(self.calls, len(self._contents) - 1)
        self.prompts.append(prompt)
        self.kwargs_history.append(dict(kwargs))
        self.calls += 1
        return self._contents[idx]

    def embed(self, text):
        return [0.0] * 4

    def is_available(self):
        return True


# ---------------------------------------------------------------------------
# Bug#1：_llm_generate 必须把 system prompt 传给 provider
# ---------------------------------------------------------------------------
def test_llm_generate_passes_system_to_provider() -> None:
    """_llm_generate 调用 provider.generate 时，prompt 必须含 system 内容。

    旧代码只传 prompt["user"]，system 被丢弃 -> LLM 看不到 role/schema 约束。
    修法 2：手动拼接 system + 两换行 + user 传给 provider.generate。
    """
    prov = _StatefulMockProvider(contents=["[]"])
    _llm_generate(prov, "用户说明早 8 点要去机场。")
    assert prov.calls == 1
    prompt = prov.prompts[0]
    # system prompt 关键词必须在传给 provider 的 prompt 里
    assert "事实提炼助手" in prompt, "system 没传给 provider，prompt 前 200 字：" + repr(prompt[:200])
    assert "JSON" in prompt or "json" in prompt, "system 必须含 JSON schema 约束"
    assert "confidence" in prompt, "system 必须含 confidence 字段定义"
    # user 内容也必须在（不能丢）
    assert "用户说明早 8 点要去机场" in prompt, "user 内容不能丢"


# ---------------------------------------------------------------------------
# Bug#2：extract_facts 加 retry 机制（max_retries=2 -> total 3 次 LLM）
# ---------------------------------------------------------------------------
def test_extract_facts_retries_on_empty() -> None:
    """第一次 LLM 返回 []，第二次返回有效 -> 最终返回第二次的 facts。

    旧代码无 retry：第一次空就立即返回 [] -> 漏抽。
    新代码：空结果 / parse 失败 -> 自动 retry 最多 2 次。
    """
    valid = json.dumps(
        [{"content": "用户明早 8 点要去机场", "confidence": 0.9, "tags": ["travel"]}],
        ensure_ascii=False,
    )
    prov = _StatefulMockProvider(contents=["[]", valid])
    facts = extract_facts("用户说明早 8 点要去机场。", provider=prov)
    assert len(facts) == 1, "retry 后应拿到 1 fact，实际：" + repr(facts)
    assert facts[0]["content"] == "用户明早 8 点要去机场"
    assert prov.calls == 2, "应调 2 次 LLM（1 次 retry），实际 " + str(prov.calls)


def test_extract_facts_retries_max_2() -> None:
    """连续 3 次空 -> max_retries=2 触发上限 -> 最终返回 []。

    旧代码：1 次空就返回 []，calls=1。
    新代码：max_retries=2 -> total 3 次 LLM，3 次都空 -> 返回 []。
    """
    prov = _StatefulMockProvider(contents=["[]", "[]", "[]"])
    facts = extract_facts("莫名其妙的对话", provider=prov)
    assert facts == [], "3 次空后应返回 []，实际：" + repr(facts)
    assert prov.calls == 3, "max_retries=2 应触发 3 次 LLM，实际 " + str(prov.calls)


def test_extract_facts_retries_on_parse_error() -> None:
    """第一次返回非 JSON（parse 失败），第二次返回有效 -> retry 修复 parse 失败。

    旧代码：第一次 parse 失败就返回 []。
    新代码：parse 失败也视为空结果，触发 retry。
    """
    valid = json.dumps(
        [{"content": "用户叫张三", "confidence": 0.95, "tags": ["name"]}],
        ensure_ascii=False,
    )
    prov = _StatefulMockProvider(contents=["这是无效的回复，不知道怎么答", valid])
    facts = extract_facts("我说我叫张三", provider=prov)
    assert len(facts) == 1, "retry 后应拿到 1 fact，实际：" + repr(facts)
    assert facts[0]["content"] == "用户叫张三"
    assert prov.calls == 2, "parse 失败后应 retry，实际 " + str(prov.calls)


# ---------------------------------------------------------------------------
# Bug#3：_JSON_FENCE_RE 改贪婪匹配，正确处理嵌套反引号
# ---------------------------------------------------------------------------
def test_fence_greedy_matches_nested() -> None:
    """fence 贪婪匹配：嵌套反引号不能切断外层 fence 提取。

    旧非贪婪 .*? 会从外层 json fence 后匹配到内层第一个反引号（"代码 python" 那里），
    截短成残片或空。贪婪 .* 一直匹配到外层最后一个反引号，拿到完整 JSON。
    """
    nested_json = '[{"content":"代码 ```python print(1) ``` 示例","confidence":0.8,"tags":["code"]}]'
    llm_text = "前置说明 ```json\n" + nested_json + "\n``` 后置说明"

    # 新正则（修复后）应匹配完整 JSON
    m = _JSON_FENCE_RE.search(llm_text)
    assert m is not None, "新 fence 正则应能匹配"
    extracted = m.group(1).strip()
    assert nested_json in extracted, "贪婪匹配应拿到完整 JSON，实际：" + repr(extracted)

    # 反向断言：旧非贪婪会拿到残片（不含完整 JSON）
    old_re = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
    m_old = old_re.search(llm_text)
    assert m_old is not None, "旧正则也应能匹配（用于对比）"
    old_extracted = m_old.group(1).strip()
    assert old_extracted != nested_json, (
        "测试设计错误：旧非贪婪意外拿到完整 JSON：" + repr(old_extracted)
    )
    assert len(old_extracted) < len(nested_json), (
        "旧非贪婪应拿到残片（短于完整 JSON），old_len="
        + str(len(old_extracted)) + " full_len=" + str(len(nested_json))
    )