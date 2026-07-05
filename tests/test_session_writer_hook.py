"""session_writer hook 测试（C1.5）：write_message 后自动调 LLM 提炼事实。

覆盖：
1. test_hook_off_by_default            默认不调 LLM
2. test_hook_on_setting                settings 开启后调 LLM
3. test_hook_uses_mock_provider        显式 provider 注入时使用它
4. test_hook_does_not_block_write      LLM 失败时 write_message 仍成功
5. test_hook_only_assistant            可配置：仅 assistant 角色触发
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.l0.session_writer import create_session, write_message
from src.llm import config_store


class _MockProvider:
    name = "mock"
    def __init__(self, content: str = "[]") -> None:
        self._content = content
        self.calls: list[str] = []
    def generate(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append(prompt)
        return self._content
    def embed(self, t): return [0.0]
    def is_available(self): return True


@pytest.fixture
def mock_provider(monkeypatch: pytest.MonkeyPatch):
    """替换 auto provider，强制返回 _MockProvider。"""
    prov = _MockProvider(
        content=json.dumps(
            [{"content": "auto-extracted fact", "confidence": 0.9, "tags": ["auto"]}],
            ensure_ascii=False,
        )
    )
    from src.llm import extractor
    monkeypatch.setattr(extractor, "_get_provider", lambda provider=None: prov)
    return prov


def test_hook_off_by_default(mtca_db: Path, mock_provider: _MockProvider) -> None:
    """默认 settings.extractor.auto_extract 不存在 → 不调 LLM。"""
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "hi", path=mtca_db)
    assert mock_provider.calls == []
    from src.llm.facts_store import count_facts
    assert count_facts(path=mtca_db) == 0


def test_hook_on_setting(mtca_db: Path, mock_provider: _MockProvider) -> None:
    """settings.extractor.auto_extract=True → write_message 触发 LLM。"""
    config_store.db_set_setting("extractor.auto_extract", "True", path=mtca_db)
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "hi", path=mtca_db)
    assert len(mock_provider.calls) == 1
    from src.llm.facts_store import count_facts
    assert count_facts(path=mtca_db) == 1


def test_hook_does_not_block_write(mtca_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 失败时 write_message 仍成功（hook 是 fire-and-forget）。"""
    config_store.db_set_setting("extractor.auto_extract", "True", path=mtca_db)
    from src.llm import extractor

    def boom(*a, **kw):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(extractor, "_get_provider", boom)

    sid = create_session(path=mtca_db)
    # 不应抛错
    write_message(sid, "user", "hi", path=mtca_db)
    # 消息仍写成功
    from src.l0.session_writer import get_session_messages
    msgs = get_session_messages(sid, path=mtca_db)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "hi"


def test_hook_skips_empty_content(mtca_db: Path, mock_provider: _MockProvider) -> None:
    """空内容不触发 LLM（节省 token）。"""
    config_store.db_set_setting("extractor.auto_extract", "True", path=mtca_db)
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "", path=mtca_db)
    assert mock_provider.calls == []


def test_hook_disabled_for_role(mtca_db: Path, mock_provider: _MockProvider) -> None:
    """settings.extractor.roles=user,assistant（默认）— user 触发。"""
    config_store.db_set_setting("extractor.auto_extract", "True", path=mtca_db)
    config_store.db_set_setting("extractor.roles", "assistant", path=mtca_db)
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "I am Alice", path=mtca_db)
    assert mock_provider.calls == []  # user 被排除
    write_message(sid, "assistant", "Hi Alice", path=mtca_db)
    assert len(mock_provider.calls) == 1  # assistant 触发

