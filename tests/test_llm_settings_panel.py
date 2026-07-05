"""gui/llm_settings_panel.py 测试（C1.7）：4 radio + 最小字段。

覆盖：
1. test_panel_has_4_radios              4 个 provider radio
2. test_panel_loads_from_settings       从 DB settings 加载初始值
3. test_panel_save_persists_to_db       save() 写入 settings 表
4. test_panel_save_syncs_toml           save() 同步到 TOML
5. test_panel_default_provider          默认 provider = 当前 [llm].default
6. test_panel_enable_thinking_toggle    enable_thinking checkbox 可切换
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from src.llm import config_store
from src.store.sqlite import init_db


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def isolated_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp):
    db = tmp_path / "panel_test.db"
    init_db(db).close()
    monkeypatch.setattr(config_store, "MTCA_CONFIG_PATH", tmp_path / "fake_panel.toml")
    monkeypatch.setattr("gui.llm_settings_panel.MTCA_DB_PATH", db)
    from gui.llm_settings_panel import LLMSettingsPanel
    panel = LLMSettingsPanel()
    yield panel, db


def test_panel_has_4_radios(isolated_panel, qapp) -> None:
    from PySide6.QtWidgets import QRadioButton
    panel, _ = isolated_panel
    radios = [r for r in panel.findChildren(QRadioButton)]
    labels = sorted(r.text() for r in radios)
    assert labels == sorted(["ollama", "lmstudio", "llamacpp", "cloud"])


def test_panel_loads_from_settings(isolated_panel, qapp) -> None:
    panel, db = isolated_panel
    # DB 预设 lmstudio 是默认 provider
    config_store.db_set_setting("llm.default", "lmstudio", path=db)
    config_store.db_set_setting("lmstudio.base_url", "http://localhost:8083", path=db)
    config_store.db_set_setting("lmstudio.model", "qwen36-heretic-q8", path=db)
    panel.reload_from_db()
    assert panel.base_url_edit.text() == "http://localhost:8083"
    assert panel.model_edit.text() == "qwen36-heretic-q8"


def test_panel_save_persists_to_db(isolated_panel, qapp) -> None:
    panel, db = isolated_panel
    # 选中 lmstudio radio
    panel._radio_for("lmstudio").setChecked(True)
    panel.base_url_edit.setText("http://localhost:8083")
    panel.model_edit.setText("qwen36-heretic-q8")
    panel.thinking_checkbox.setChecked(False)
    panel.save()
    assert config_store.db_get_setting("llm.default", path=db) == "lmstudio"
    assert config_store.db_get_setting("lmstudio.base_url", path=db) == "http://localhost:8083"
    assert config_store.db_get_setting("lmstudio.model", path=db) == "qwen36-heretic-q8"
    assert config_store.db_get_setting("lmstudio.enable_thinking", path=db) == "False"


def test_panel_save_syncs_toml(isolated_panel, tmp_path, qapp) -> None:
    panel, db = isolated_panel
    panel._radio_for("ollama").setChecked(True)
    panel.base_url_edit.setText("http://localhost:11434")
    panel.model_edit.setText("qwen3.5:9b")
    panel.save()
    toml_path = tmp_path / "fake_panel.toml"
    assert toml_path.exists()
    text = toml_path.read_text(encoding="utf-8")
    assert "ollama" in text
    assert "http://localhost:11434" in text


def test_panel_default_provider(isolated_panel, qapp) -> None:
    panel, db = isolated_panel
    config_store.db_set_setting("llm.default", "cloud", path=db)
    panel.reload_from_db()
    assert panel._radio_for("cloud").isChecked()


def test_panel_enable_thinking_toggle(isolated_panel, qapp) -> None:
    panel, db = isolated_panel
    panel._radio_for("lmstudio").setChecked(True)
    panel.base_url_edit.setText("http://x")
    panel.model_edit.setText("m")
    panel.thinking_checkbox.setChecked(True)
    panel.save()
    assert config_store.db_get_setting("lmstudio.enable_thinking", path=db) == "True"