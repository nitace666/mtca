"""LLM 设置面板（gui/llm_settings_panel.py — C1.7）

提供 4 个 provider radio（ollama / lmstudio / llamacpp / cloud）+ 最小字段：
- base_url
- model
- enable_thinking checkbox

从 settings 表加载 / 保存，自动同步 TOML。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.llm import config_store

try:
    from src.store.sqlite import MTCA_DB_PATH
except ImportError:  # pragma: no cover
    from store.sqlite import MTCA_DB_PATH


_VALID_PROVIDERS: tuple[str, ...] = ("ollama", "lmstudio", "llamacpp", "cloud")


class LLMSettingsPanel(QWidget):
    """LLM 后端配置面板。

    使用：
        panel = LLMSettingsPanel()
        panel.reload_from_db()  # 从 DB 加载
        panel.save()            # 写回 DB + 同步 TOML
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("LLM 设置")

        # 4 个 provider radio
        self._radios: dict[str, QRadioButton] = {}
        provider_box = QGroupBox("LLM Provider")
        provider_layout = QVBoxLayout()
        for name in _VALID_PROVIDERS:
            r = QRadioButton(name)
            self._radios[name] = r
            provider_layout.addWidget(r)
        provider_box.setLayout(provider_layout)

        # 最小字段
        form_box = QGroupBox("后端参数")
        form = QFormLayout()
        self.base_url_edit = QLineEdit()
        self.model_edit = QLineEdit()
        self.thinking_checkbox = QCheckBox("启用 thinking 模式（Qwen3 推理）")
        form.addRow("base_url:", self.base_url_edit)
        form.addRow("model:", self.model_edit)
        form.addRow("", self.thinking_checkbox)
        form_box.setLayout(form)

        # 按钮
        self.save_btn = QPushButton("保存")
        self.save_btn.clicked.connect(self.save)
        self.reload_btn = QPushButton("重新加载")
        self.reload_btn.clicked.connect(self.reload_from_db)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.reload_btn)
        btn_row.addWidget(self.save_btn)

        # 整体布局
        root = QVBoxLayout()
        root.addWidget(QLabel("MTCA LLM 后端配置"))
        root.addWidget(provider_box)
        root.addWidget(form_box)
        root.addLayout(btn_row)
        root.addStretch(1)
        self.setLayout(root)

        # 初始加载
        self.reload_from_db()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def _radio_for(self, provider: str) -> QRadioButton:
        return self._radios[provider]

    def _selected_provider(self) -> str:
        for name, radio in self._radios.items():
            if radio.isChecked():
                return name
        return _VALID_PROVIDERS[0]

    def reload_from_db(self) -> None:
        """从 DB settings 加载所有字段。"""
        provider = config_store.db_get_setting("llm.default", path=MTCA_DB_PATH)
        if not provider or provider not in _VALID_PROVIDERS:
            provider = _VALID_PROVIDERS[0]
        # radio
        for name, radio in self._radios.items():
            radio.setChecked(name == provider)
        # base_url / model / enable_thinking
        self.base_url_edit.setText(
            config_store.db_get_setting(f"{provider}.base_url", path=MTCA_DB_PATH) or ""
        )
        self.model_edit.setText(
            config_store.db_get_setting(f"{provider}.model", path=MTCA_DB_PATH) or ""
        )
        et = config_store.db_get_setting(
            f"{provider}.enable_thinking", path=MTCA_DB_PATH
        )
        self.thinking_checkbox.setChecked((et or "False").lower() == "true")

    def save(self) -> None:
        """把当前 UI 状态写入 DB settings + 同步 TOML。"""
        provider = self._selected_provider()
        config_store.db_set_setting("llm.default", provider, path=MTCA_DB_PATH)
        if self.base_url_edit.text().strip():
            config_store.db_set_setting(
                f"{provider}.base_url",
                self.base_url_edit.text().strip(),
                path=MTCA_DB_PATH,
            )
        if self.model_edit.text().strip():
            config_store.db_set_setting(
                f"{provider}.model",
                self.model_edit.text().strip(),
                path=MTCA_DB_PATH,
            )
        config_store.db_set_setting(
            f"{provider}.enable_thinking",
            "True" if self.thinking_checkbox.isChecked() else "False",
            path=MTCA_DB_PATH,
        )
        config_store.sync_db_to_toml(path=MTCA_DB_PATH)