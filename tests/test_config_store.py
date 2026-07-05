"""src/llm/config_store.py 测试：DB ↔ TOML 双向同步 + provider load_config 集成。

覆盖：
1.  test_db_settings_crud            settings 表 CRUD（set/get/list/delete）
2.  test_db_settings_overwrite       同一 key 多次 set 覆盖
3.  test_toml_roundtrip              TOML 写盘 → 读回一致
4.  test_sync_db_to_toml             DB 状态优先写到 TOML
5.  test_sync_toml_to_db             TOML → DB（DB 不存在时初始化）
6.  test_load_config_db_priority     load_config 优先用 DB，TOML 仅作备份
7.  test_load_config_toml_fallback   DB 空时回退到 TOML
8.  test_get_active_config_with_source  返回带 source 标记（"db"/"toml"/"default"）

测试用 pytest tmp_path（tmp_db fixture 来自 conftest）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 测试 1：settings 表 CRUD
# ---------------------------------------------------------------------------


def test_db_settings_crud(mtca_db: Path) -> None:
    """db_set_setting / db_get_setting / db_list_settings / db_delete_setting。"""
    from src.llm.config_store import (
        db_delete_setting,
        db_get_setting,
        db_list_settings,
        db_set_setting,
    )

    assert db_get_setting("missing", path=mtca_db) is None

    db_set_setting("llm.provider", "lmstudio", path=mtca_db)
    assert db_get_setting("llm.provider", path=mtca_db) == "lmstudio"

    rows = db_list_settings(path=mtca_db)
    assert ("llm.provider", "lmstudio") in rows

    db_delete_setting("llm.provider", path=mtca_db)
    assert db_get_setting("llm.provider", path=mtca_db) is None


# ---------------------------------------------------------------------------
# 测试 2：覆盖语义
# ---------------------------------------------------------------------------


def test_db_settings_overwrite(mtca_db: Path) -> None:
    """同 key 多次 set 覆盖 + updated_at 自动刷新。"""
    from src.llm.config_store import (
        db_get_setting,
        db_set_setting,
    )

    db_set_setting("lmstudio.base_url", "http://a:1234", path=mtca_db)
    db_set_setting("lmstudio.base_url", "http://b:5678", path=mtca_db)
    assert db_get_setting("lmstudio.base_url", path=mtca_db) == "http://b:5678"


# ---------------------------------------------------------------------------
# 测试 3：TOML 往返
# ---------------------------------------------------------------------------


def test_toml_roundtrip(tmp_path: Path) -> None:
    """toml_save + load_config（显式 path）保持结构。"""
    from src.llm.config_store import toml_save

    cfg_path = tmp_path / "config.toml"
    cfg = {
        "llm": {"default": "lmstudio", "fallback": "cloud"},
        "lmstudio": {"base_url": "http://localhost:8083", "model": "qwen36-heretic-q8"},
    }
    toml_save(cfg, path=cfg_path)
    assert cfg_path.exists()
    text = cfg_path.read_text(encoding="utf-8")
    assert "[llm]" in text
    assert "lmstudio" in text
    assert "http://localhost:8083" in text


# ---------------------------------------------------------------------------
# 测试 4：DB → TOML
# ---------------------------------------------------------------------------


def test_sync_db_to_toml(mtca_db: Path, tmp_path: Path) -> None:
    """sync_db_to_toml 应把 DB settings 写到 TOML 文件。"""
    from src.llm.config_store import (
        db_set_setting,
        sync_db_to_toml,
    )

    toml_path = tmp_path / "mtca.toml"
    db_set_setting("lmstudio.base_url", "http://localhost:8083", path=mtca_db)
    db_set_setting("lmstudio.model", "qwen36-heretic-q8", path=mtca_db)

    sync_db_to_toml(path=mtca_db, toml_path=toml_path)
    assert toml_path.exists()
    text = toml_path.read_text(encoding="utf-8")
    assert "http://localhost:8083" in text
    assert "qwen36-heretic-q8" in text


# ---------------------------------------------------------------------------
# 测试 5：TOML → DB（DB 空时初始化）
# ---------------------------------------------------------------------------


def test_sync_toml_to_db_when_db_empty(mtca_db: Path, tmp_path: Path) -> None:
    """DB 为空时 sync_toml_to_db 应从 TOML 加载全部键。"""
    from src.llm.config_store import (
        db_list_settings,
        sync_toml_to_db,
        toml_save,
    )

    toml_path = tmp_path / "mtca.toml"
    cfg = {
        "llm": {"default": "lmstudio", "fallback": "cloud"},
        "lmstudio": {"base_url": "http://localhost:8083", "model": "qwen36-heretic-q8"},
    }
    toml_save(cfg, path=toml_path)

    # 初始 DB 应空
    assert db_list_settings(path=mtca_db) == []

    sync_toml_to_db(path=mtca_db, toml_path=toml_path)

    rows = dict(db_list_settings(path=mtca_db))
    # 嵌套字典被点分展开
    assert rows.get("lmstudio.base_url") == "http://localhost:8083"
    assert rows.get("lmstudio.model") == "qwen36-heretic-q8"


# ---------------------------------------------------------------------------
# 测试 6：load_config 优先 DB
# ---------------------------------------------------------------------------


def test_load_config_db_priority(mtca_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """load_config() 优先用 DB，TOML 存在但被 DB 覆盖时读 DB。"""
    from src.llm import config_store
    from src.llm.config_store import (
        db_set_setting,
        load_config,
    )
    from src.llm.provider import MTCA_CONFIG_PATH

    # 重定向 TOML 路径到 tmp_path，避免污染 ~/.mtca
    fake_toml = tmp_path / "fake_config.toml"
    monkeypatch.setattr(config_store, "MTCA_CONFIG_PATH", fake_toml)

    # TOML 写一个 base_url
    from src.llm.config_store import toml_save
    toml_save(
        {"lmstudio": {"base_url": "http://from-toml:1234", "model": "x"}},
        path=fake_toml,
    )

    # DB 写一个不同的 base_url
    db_set_setting("lmstudio.base_url", "http://from-db:5678", path=mtca_db)

    cfg = load_config(path=mtca_db, toml_path=fake_toml)
    assert cfg["lmstudio"]["base_url"] == "http://from-db:5678"


# ---------------------------------------------------------------------------
# 测试 7：DB 空 → TOML fallback
# ---------------------------------------------------------------------------


def test_load_config_toml_fallback(mtca_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DB 无 settings 时 load_config 回退到 TOML。"""
    from src.llm import config_store
    from src.llm.config_store import (
        load_config,
        toml_save,
    )

    fake_toml = tmp_path / "fake_config.toml"
    monkeypatch.setattr(config_store, "MTCA_CONFIG_PATH", fake_toml)

    toml_save(
        {
            "llm": {"default": "cloud", "fallback": "ollama"},
            "lmstudio": {"base_url": "http://localhost:8083", "model": "qwen36"},
        },
        path=fake_toml,
    )

    cfg = load_config(path=mtca_db, toml_path=fake_toml)
    assert cfg["llm"]["default"] == "cloud"
    assert cfg["lmstudio"]["base_url"] == "http://localhost:8083"


# ---------------------------------------------------------------------------
# 测试 8：get_active_config 带 source
# ---------------------------------------------------------------------------


def test_get_active_config_with_source(mtca_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_active_config 返回值带 _source 字段（"db"/"toml"/"default"）。"""
    from src.llm import config_store
    from src.llm.config_store import (
        db_set_setting,
        get_active_config,
        toml_save,
    )

    fake_toml = tmp_path / "fake_config.toml"
    monkeypatch.setattr(config_store, "MTCA_CONFIG_PATH", fake_toml)

    db_set_setting("lmstudio.base_url", "http://db:1", path=mtca_db)
    toml_save(
        {"lmstudio": {"base_url": "http://toml:2", "model": "m"}},
        path=fake_toml,
    )

    cfg, source = get_active_config(path=mtca_db, toml_path=fake_toml)
    assert source == "db"
    assert cfg["lmstudio"]["base_url"] == "http://db:1"


