"""CLI ``mtca-server llm-set`` 命令测试（C1.2）。

通过 click CliRunner + monkeypatch 重定向 DB 路径，验证：
1. test_llm_set_basic                 基本四参数写入
2. test_llm_set_updates_db            DB settings 表实际被写
3. test_llm_set_updates_toml          TOML 备份被同步
4. test_llm_set_no_thinking_default   不传 --enable-thinking 默认 False
5. test_llm_set_thinking_true_opt_in  --enable-thinking 开启
6. test_llm_set_minimal_args          只传 provider 也行（其它沿用 DEFAULT）
7. test_llm_set_invalid_provider      非法 provider 名拒绝
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli.server import main
from src.llm import config_store
from src.store.sqlite import init_db


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离 DB：monkeypatch MTCA_DB_PATH + settings/config 路径。"""
    db_path = tmp_path / "cli_test.db"
    init_db(db_path).close()
    monkeypatch.setattr(config_store, "MTCA_CONFIG_PATH", tmp_path / "fake.toml")
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", db_path)
    return db_path


def _db_path(monkeypatch, tmp_path) -> Path:
    return tmp_path / "cli_test.db"


def test_llm_set_basic(isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", isolated_db)
    result = runner.invoke(
        main,
        [
            "llm-set",
            "--provider", "lmstudio",
            "--base-url", "http://localhost:8083",
            "--model", "qwen36-heretic-q8",
        ],
    )
    assert result.exit_code == 0, result.output


def test_llm_set_updates_db(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", isolated_db)
    runner.invoke(
        main,
        [
            "llm-set",
            "--provider", "lmstudio",
            "--base-url", "http://localhost:8083",
            "--model", "qwen36-heretic-q8",
        ],
    )
    from src.llm.config_store import db_get_setting
    assert db_get_setting("llm.default", path=isolated_db) == "lmstudio"
    assert db_get_setting("lmstudio.base_url", path=isolated_db) == "http://localhost:8083"
    assert db_get_setting("lmstudio.model", path=isolated_db) == "qwen36-heretic-q8"


def test_llm_set_updates_toml(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    toml_path = tmp_path / "expected.toml"
    monkeypatch.setattr(config_store, "MTCA_CONFIG_PATH", toml_path)
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", isolated_db)
    runner.invoke(
        main,
        [
            "llm-set",
            "--provider", "ollama",
            "--base-url", "http://localhost:11434",
            "--model", "qwen3.6:35b-a3b",
        ],
    )
    assert toml_path.exists()
    text = toml_path.read_text(encoding="utf-8")
    assert "ollama" in text
    assert "http://localhost:11434" in text


def test_llm_set_no_thinking_default(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", isolated_db)
    runner.invoke(
        main,
        [
            "llm-set",
            "--provider", "lmstudio",
            "--base-url", "http://x",
            "--model", "y",
        ],
    )
    from src.llm.config_store import db_get_setting
    assert db_get_setting("lmstudio.enable_thinking", path=isolated_db) == "False"


def test_llm_set_thinking_true_opt_in(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", isolated_db)
    runner.invoke(
        main,
        [
            "llm-set",
            "--provider", "lmstudio",
            "--base-url", "http://x",
            "--model", "y",
            "--enable-thinking",
        ],
    )
    from src.llm.config_store import db_get_setting
    assert db_get_setting("lmstudio.enable_thinking", path=isolated_db) == "True"


def test_llm_set_minimal_args(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只传 provider 也可，其他字段允许后续通过补充命令设置。"""
    runner = CliRunner()
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", isolated_db)
    result = runner.invoke(main, ["llm-set", "--provider", "cloud"])
    assert result.exit_code == 0
    from src.llm.config_store import db_get_setting
    assert db_get_setting("llm.default", path=isolated_db) == "cloud"


def test_llm_set_invalid_provider(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr("src.cli.server.MTCA_DB_PATH", isolated_db)
    result = runner.invoke(
        main,
        [
            "llm-set",
            "--provider", "bogus_provider_xxx",
        ],
    )
    assert result.exit_code != 0
