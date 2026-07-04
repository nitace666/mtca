"""雾化确认对话框（gui/fog_dialog.py — T13）。

USER_CONTROLS.md §3.3：强警告 + 锚点句输入（≤20 字）+ 取消/确定按钮。

设计要点：
- 直接复用 ``src.fog.fog_engine.ANCHOR_MAX_LEN`` 作为锚点句上限，
  避免和后端常量走样。
- 锚点句输入实时校验：超过上限时禁用确定按钮并显示红色错误。
- 按 ESC 等价于取消；按 Enter 等价于确定（仅当校验通过）。
- 不直接调用雾化逻辑，只返回 ``(accepted: bool, anchor: str)``；
  调用方决定是否实际执行。
"""

from __future__ import annotations

from typing import Optional, Union

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.fog.fog_engine import ANCHOR_MAX_LEN

# 警告字体大小（与 M1 验收对齐：UI 不堆美观，足够醒目即可）
_WARN_FONT_PT: int = 14


class FogDialog(QDialog):
    """/雾化 确认对话框（强警告版）。

    使用方式：

    .. code-block:: python

        dlg = FogDialog(parent=main_win, topic="哲学宪法", seg_id="abc-...")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            anchor = dlg.anchor()
            # 把 (seg_id, anchor) 交给 fog_engine.fog_segment
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        topic: str = "",
        seg_id: str = "",
    ) -> None:
        """初始化对话框并填充段落上下文。"""
        super().__init__(parent)
        self._topic = topic or "(无标题)"
        self._seg_id = seg_id or ""
        self._anchor: str = ""

        self.setWindowTitle("雾化操作不可逆")
        self.setModal(True)
        # 最小宽度保证警告文本不被截
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        # ---- 1. 强警告标题 ----
        warn_label = QLabel("! 警告：雾化操作不可逆 !")
        warn_font = QFont()
        warn_font.setBold(True)
        warn_font.setPointSize(_WARN_FONT_PT)
        warn_label.setFont(warn_font)
        warn_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        warn_label.setStyleSheet("color: #b00020;")
        layout.addWidget(warn_label)

        # ---- 2. 段落上下文 ----
        ctx_text = (
            f"目标段落：{self._topic}\n"
            f"segment_id：{self._seg_id if self._seg_id else '(未选择)'}"
        )
        ctx_label = QLabel(ctx_text)
        ctx_label.setWordWrap(True)
        layout.addWidget(ctx_label)

        # ---- 3. 保留 / 擦除 / 后果说明 ----
        info = QPlainTextEdit()
        info.setReadOnly(True)
        info.setPlainText(
            "保留：话题标题 / 时间 / 关键词 / 锚点句\n"
            "擦除：完整对话 / 工具调用 / 长文本\n"
            "\n"
            "后果：\n"
            "* AI 召回时只能看到「已删除 + 关键词」\n"
            "* 之后不再主动提起\n"
            "* 不可恢复"
        )
        info.setFixedHeight(160)
        layout.addWidget(info)

        # ---- 4. 锚点句输入 + 校验提示 ----
        anchor_label = QLabel(f"锚点句（≤{ANCHOR_MAX_LEN} 字）：")
        layout.addWidget(anchor_label)
        self._anchor_edit = QLineEdit()
        self._anchor_edit.setPlaceholderText("例如：AI 记忆 / 长期记忆不可逆")
        self._anchor_edit.setMaxLength(ANCHOR_MAX_LEN)
        self._anchor_edit.textChanged.connect(self._on_anchor_changed)
        layout.addWidget(self._anchor_edit)

        self._err_label = QLabel("")
        self._err_label.setStyleSheet("color: #b00020;")
        layout.addWidget(self._err_label)

        # ---- 5. 取消 / 确定按钮 ----
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        self._button_box.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText("我确定要雾化")
        self._button_box.button(
            QDialogButtonBox.StandardButton.Ok
        ).setEnabled(False)
        self._button_box.accepted.connect(self._on_accept)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def anchor(self) -> str:
        """返回用户输入的锚点句（已 strip）。"""
        return self._anchor

    # ------------------------------------------------------------------
    # 内部回调
    # ------------------------------------------------------------------

    def _on_anchor_changed(self, text: str) -> None:
        """锚点句变更：长度校验 + 启用/禁用确定按钮。"""
        ok_btn = self._button_box.button(QDialogButtonBox.StandardButton.Ok)
        stripped = text.strip()
        if not stripped:
            self._err_label.setText("锚点句不能为空")
            ok_btn.setEnabled(False)
            return
        # QLineEdit.setMaxLength 已在输入层截断，这里仍做一次保险校验
        if len(stripped) > ANCHOR_MAX_LEN:
            self._err_label.setText(
                f"锚点句超过 {ANCHOR_MAX_LEN} 字（当前 {len(stripped)}）"
            )
            ok_btn.setEnabled(False)
            return
        self._err_label.setText("")
        ok_btn.setEnabled(True)

    def _on_accept(self) -> None:
        """确定：二次确认（强警告）后真正接受。"""
        anchor = self._anchor_edit.text().strip()
        if not anchor or len(anchor) > ANCHOR_MAX_LEN:
            return
        # 二次弹窗（USER_CONTROLS §3.3 强调强警告）
        confirm = QMessageBox.question(
            self,
            "最后确认",
            f"确定要雾化段落「{self._topic}」吗？\n"
            f"此操作不可撤销，L0-细节将被物理擦除。\n\n"
            f"锚点句：{anchor}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._anchor = anchor
        self.accept()


__all__ = ["FogDialog", "ANCHOR_MAX_LEN"]


def run_smoke(widget: Optional[Union[QDialog, QWidget]] = None) -> int:
    """手工 smoke：构建对话框并 exec()；仅用于本地调试。"""
    from PySide6.QtWidgets import QApplication
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    dlg = FogDialog(parent=widget, topic="哲学宪法", seg_id="abc-123")
    rc = dlg.exec()
    print(f"[SMOKE] FogDialog exec -> {rc}, anchor={dlg.anchor()!r}")
    return rc