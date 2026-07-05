"""C1 端到端集成测试（C1.8）：跨模块协作。

覆盖：
1. test_llm_set_then_extract         CLI llm-set → extract_facts 用真实 provider
2. test_write_message_then_recall    session_writer hook → facts → recall
3. test_config_sync_roundtrip        DB → TOML → DB 内容一致
4. test_facts_extraction_pipeline    多 message → facts → search → recall
5. test_extractor_fallback_chain     provider 失败 → 优雅返回空
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from src.cli.server import main
from src.l0.session_writer import create_session, write_message
from src.llm import config_store
from src.llm.facts_store import count_facts, list_facts_by_session, search_facts
from src.recall.recall_engine import recall
from src.store.sqlite import init_db


class _MockProvider:
    name = "mock"

    def __init__(self, content: str = "[]") -> None:
        self._content = content

    def generate(self, prompt: str, **kwargs: Any) -> str:
        return self._content

    def embed(self, t: str):
        return [0.0]

    def is_available(self) -> bool:
        return True


def test_config_sync_roundtrip(tmp_path: Path) -> None:
    """DB → TOML → DB 一致。"""
    db = tmp_path / "rt.db"
    init_db(db).close()
    config_store.db_set_setting("llm.default", "lmstudio", path=db)
    config_store.db_set_setting("lmstudio.base_url", "http://localhost:8083", path=db)
    config_store.db_set_setting("lmstudio.model", "qwen36-heretic-q8", path=db)

    toml_path = tmp_path / "rt.toml"
    config_store.sync_db_to_toml(path=db, toml_path=toml_path)
    assert toml_path.exists()

    # 新 DB：sync_toml_to_db 应该恢复全部
    db2 = tmp_path / "rt2.db"
    init_db(db2).close()
    n = config_store.sync_toml_to_db(path=db2, toml_path=toml_path)
    assert n >= 3
    assert config_store.db_get_setting("lmstudio.model", path=db2) == "qwen36-heretic-q8"


def test_write_message_then_recall(mtca_db: Path, monkeypatch) -> None:
    """session_writer hook → 写 facts → recall 能找到。"""
    config_store.db_set_setting("extractor.auto_extract", "True", path=mtca_db)
    from src.llm import extractor
    monkeypatch.setattr(
        extractor,
        "_get_provider",
        lambda provider=None: _MockProvider(content=json.dumps(
            [{"content": "用户喜欢滑雪", "confidence": 0.9, "tags": ["sport"]}],
            ensure_ascii=False,
        )),
    )
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "我喜欢滑雪", path=mtca_db)

    assert count_facts(path=mtca_db) == 1
    # recall 应能找到
    results = recall("滑雪", top_k=5, path=mtca_db)
    assert any(r.get("is_fact") for r in results)


def test_facts_extraction_pipeline(mtca_db: Path, monkeypatch) -> None:
    """多消息 → extract_facts_from_messages → 写库 → search。"""
    from src.llm import extractor
    monkeypatch.setattr(
        extractor,
        "_get_provider",
        lambda provider=None: _MockProvider(content=json.dumps(
            [
                {"content": "用户叫张三", "confidence": 0.95, "tags": ["name"]},
                {"content": "用户是工程师", "confidence": 0.85, "tags": ["job"]},
            ],
            ensure_ascii=False,
        )),
    )
    sid = create_session(path=mtca_db)
    msgs = [
        {"role": "user", "content": "我叫张三，是工程师"},
        {"role": "assistant", "content": "好的，记住了"},
    ]
    facts = extractor.extract_facts_from_messages(msgs)
    assert len(facts) == 2
    for f in facts:
        config_store  # unused, just for type
        from src.llm.facts_store import create_fact
        create_fact(sid, f["content"], confidence=f["confidence"],
                    tags=json.dumps(f["tags"], ensure_ascii=False), path=mtca_db)

    rows = list_facts_by_session(sid, path=mtca_db)
    assert len(rows) == 2
    # search 应该能找到
    hits = search_facts("工程师", path=mtca_db)
    assert any("工程师" in h["content"] for h in hits)


def test_extractor_fallback_chain(mtca_db: Path, monkeypatch) -> None:
    """provider 抛错时 extract_facts 返回 []，不传播异常。"""
    from src.llm import extractor

    class _Boom:
        def generate(self, *a, **kw):
            raise RuntimeError("LLM down")
        def embed(self, t):
            return []
        def is_available(self):
            return True

    monkeypatch.setattr(extractor, "_get_provider", lambda provider=None: _Boom())
    facts = extractor.extract_facts("随便说点什么")
    assert facts == []


def test_cli_llm_set_then_get(tmp_path: Path, monkeypatch) -> None:
    """CLI llm-set 写 DB 后，get_active_config 应能识别 source=db。"""
    db = tmp_path / "cli_int.db"
    init_db(db).close()
    monkeypatch.setattr(config_store, "MTCA_CONFIG_PATH", tmp_path / "cli_int.toml")
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", db)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "llm-set",
            "--provider", "lmstudio",
            "--base-url", "http://localhost:8083",
            "--model", "qwen36-heretic-q8",
            "--no-enable-thinking",
        ],
    )
    assert result.exit_code == 0, result.output

    cfg, source = config_store.get_active_config(path=db, toml_path=tmp_path / "cli_int.toml")
    assert source == "db"
    assert cfg["lmstudio"]["base_url"] == "http://localhost:8083"
    assert cfg["lmstudio"]["model"] == "qwen36-heretic-q8"