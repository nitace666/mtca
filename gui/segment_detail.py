"""段落详情面板（gui/segment_detail.py — T13）。

USER_CONTROLS.md §3.2：段落详情（点击进入）。
当前 DB schema 已有字段：current_tier / current_score / fog_state /
silence_state / ref_count / superseded_by / topic_label / fog_anchor /
start_at / end_at。L1 / L2 / L3 内容在 M3 才入库，此处显式标注「待 L1/L2/L3 模块接入」。

设计要点：
- 复用 ``src.l0.segment_writer.get_segment`` 拉段落元信息。
- 复用 ``src.l0.session_writer.get_session_messages`` 拉段内消息
  （「展开 L0 全文」按需加载，避免大段冷数据白占内存）。
- 不持有业务状态；``show_segment(seg_id)`` 是唯一的入口。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.l0.segment_writer import get_segment
from src.l0.session_writer import get_session_messages

# M1 占位：L1 / L2 / L3 模块尚未接入（M3 才入库）
_PLACEHOLDER_L1: str = "(待 L1 摘要模块接入)"
_PLACEHOLDER_L2: str = "(待 L2 三元组模块接入)"
_PLACEHOLDER_L3: str = "(待 L3 标签模块接入)"

# 重要分数阈值（与 scoring.IMPORTANT_SCORE 对齐）
_IMPORTANT_SCORE: float = 10000.0


class SegmentDetail(QWidget):
    """段落详情面板（中间栏）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化布局与占位状态。"""
        super().__init__(parent)
        self._db_path: Optional[Union[Path, str]] = None
        self._seg_id: str = ""
        self._seg: Optional[dict] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ---- 段落标题 ----
        self._title_label = QLabel("未选择段落")
        from PySide6.QtGui import QFont
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(13)
        self._title_label.setFont(title_font)
        layout.addWidget(self._title_label)

        # ---- 元信息 ----
        meta_box = QGroupBox("段落状态")
        meta_form = QFormLayout(meta_box)
        self._tier_label = QLabel("-")
        self._score_label = QLabel("-")
        self._silence_label = QLabel("-")
        self._fog_label = QLabel("-")
        self._time_label = QLabel("-")
        self._ref_label = QLabel("-")
        self._super_label = QLabel("-")
        meta_form.addRow("档位 current_tier：", self._tier_label)
        meta_form.addRow("重要性 score：", self._score_label)
        meta_form.addRow("静默态 silence_state：", self._silence_label)
        meta_form.addRow("雾化状态 fog_state：", self._fog_label)
        meta_form.addRow("时间窗口：", self._time_label)
        meta_form.addRow("引用次数 ref_count：", self._ref_label)
        meta_form.addRow("被取代 superseded_by：", self._super_label)
        layout.addWidget(meta_box)

        # ---- L0-骨架 ----
        skel_box = QGroupBox("L0-骨架")
        skel_layout = QVBoxLayout(skel_box)
        self._skel_label = QLabel("(空)")
        self._skel_label.setWordWrap(True)
        skel_layout.addWidget(self._skel_label)
        layout.addWidget(skel_box)

        # ---- L1 / L2 / L3 占位 ----
        l1_box = QGroupBox("L1 摘要")
        l1_layout = QVBoxLayout(l1_box)
        self._l1_label = QLabel(_PLACEHOLDER_L1)
        self._l1_label.setWordWrap(True)
        l1_layout.addWidget(self._l1_label)
        layout.addWidget(l1_box)

        l2_box = QGroupBox("L2 三元组")
        l2_layout = QVBoxLayout(l2_box)
        self._l2_label = QLabel(_PLACEHOLDER_L2)
        self._l2_label.setWordWrap(True)
        l2_layout.addWidget(self._l2_label)
        layout.addWidget(l2_box)

        l3_box = QGroupBox("L3 标签")
        l3_layout = QVBoxLayout(l3_box)
        self._l3_label = QLabel(_PLACEHOLDER_L3)
        self._l3_label.setWordWrap(True)
        l3_layout.addWidget(self._l3_label)
        layout.addWidget(l3_box)

        # ---- 展开 L0 全文（默认收起） ----
        self._expand_btn = QPushButton("展开 L0 全文")
        self._expand_btn.setCheckable(True)
        self._expand_btn.setChecked(False)
        self._expand_btn.toggled.connect(self._on_expand_toggled)
        layout.addWidget(self._expand_btn)

        self._l0_text = QPlainTextEdit()
        self._l0_text.setReadOnly(True)
        self._l0_text.setPlaceholderText("(点击「展开 L0 全文」按需加载)")
        self._l0_text.setVisible(False)
        self._l0_text.setMinimumHeight(180)
        layout.addWidget(self._l0_text, 1)

        self._show_empty()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def set_db_path(self, db_path: Optional[Union[Path, str]]) -> None:
        """设置数据库路径；下次 show_segment 时生效。"""
        self._db_path = db_path

    def show_segment(self, seg_id: str) -> None:
        """按 segment_id 加载并刷新详情面板。"""
        self._seg_id = seg_id or ""
        self._expand_btn.setChecked(False)
        self._l0_text.setVisible(False)
        if not self._seg_id:
            self._show_empty()
            return
        try:
            seg = get_segment(self._seg_id, path=self._db_path)
        except RuntimeError as e:
            self._show_error(f"加载段落失败：{e}")
            return
        if not seg:
            self._show_error(f"段落不存在：{self._seg_id}")
            return
        self._seg = seg
        self._render(seg)

    def current_segment_id(self) -> str:
        """返回当前显示的 segment_id。"""
        return self._seg_id

    # ------------------------------------------------------------------
    # 内部：渲染
    # ------------------------------------------------------------------

    def _show_empty(self) -> None:
        """未选择段落时的占位渲染。"""
        self._title_label.setText("未选择段落")
        self._tier_label.setText("-")
        self._score_label.setText("-")
        self._silence_label.setText("-")
        self._fog_label.setText("-")
        self._time_label.setText("-")
        self._ref_label.setText("-")
        self._super_label.setText("-")
        self._skel_label.setText("(空)")
        self._l1_label.setText(_PLACEHOLDER_L1)
        self._l2_label.setText(_PLACEHOLDER_L2)
        self._l3_label.setText(_PLACEHOLDER_L3)

    def _show_error(self, msg: str) -> None:
        """加载失败时的占位渲染。"""
        self._title_label.setText("加载失败")
        self._tier_label.setText("-")
        self._score_label.setText("-")
        self._silence_label.setText("-")
        self._fog_label.setText("-")
        self._time_label.setText("-")
        self._ref_label.setText("-")
        self._super_label.setText("-")
        self._skel_label.setText(msg)
        self._l1_label.setText(_PLACEHOLDER_L1)
        self._l2_label.setText(_PLACEHOLDER_L2)
        self._l3_label.setText(_PLACEHOLDER_L3)
        self._seg = None

    def _render(self, seg: dict) -> None:
        """渲染段落元信息 + 骨架字段。"""
        topic = seg.get("topic_label") or "(无标题)"
        self._title_label.setText(topic)
        tier = seg.get("current_tier") or "-"
        score = seg.get("current_score")
        try:
            score_str = f"{float(score):.1f}"
        except (TypeError, ValueError):
            score_str = str(score)
        try:
            if float(score) >= _IMPORTANT_SCORE:
                score_str += "  (锁定 / /重要)"
        except (TypeError, ValueError):
            pass
        self._tier_label.setText(str(tier))
        self._score_label.setText(score_str)
        self._silence_label.setText(str(seg.get("silence_state") or "-"))
        self._fog_label.setText(str(seg.get("fog_state") or "-"))
        start_at = int(seg.get("start_at") or 0)
        end_at = int(seg.get("end_at") or 0)
        self._time_label.setText(
            f"{start_at} ~ {end_at}"
            if start_at and end_at else "-"
        )
        self._ref_label.setText(str(seg.get("ref_count") or 0))
        sup = seg.get("superseded_by") or "(无)"
        self._super_label.setText(str(sup))
        anchor = seg.get("fog_anchor") or ""
        if anchor:
            self._skel_label.setText(f"topic_label: {topic}\n"
                                      f"fog_anchor:  {anchor}")
        else:
            self._skel_label.setText(f"topic_label: {topic}")

    def _on_expand_toggled(self, checked: bool) -> None:
        """展开 / 收起 L0 全文。"""
        if checked:
            self._expand_btn.setText("收起 L0 全文")
            self._l0_text.setVisible(True)
            self._load_l0_messages()
        else:
            self._expand_btn.setText("展开 L0 全文")
            self._l0_text.setVisible(False)

    def _load_l0_messages(self) -> None:
        """按需加载段内消息并填充 L0 全文框。"""
        if not self._seg:
            self._l0_text.setPlainText("(无段落上下文)")
            return
        session_id = self._seg.get("session_id")
        start_seq = int(self._seg.get("start_msg_seq") or 0)
        end_seq = int(self._seg.get("end_msg_seq") or 0)
        if not session_id or not start_seq or not end_seq:
            self._l0_text.setPlainText("(段落 seq 范围缺失)")
            return
        try:
            msgs = get_session_messages(session_id, path=self._db_path)
        except RuntimeError as e:
            self._l0_text.setPlainText(f"加载 L0 失败：{e}")
            return
        lines: list[str] = []
        for m in msgs:
            seq = int(m.get("seq") or 0)
            if seq < start_seq or seq > end_seq:
                continue
            role = m.get("role") or "?"
            content = m.get("content")
            if content is None:
                # 雾化后的占位（与 fog_engine 行为对齐）
                lines.append(f"[seq={seq}] {role}: <已雾化>")
            else:
                lines.append(f"[seq={seq}] {role}: {content}")
        if not lines:
            self._l0_text.setPlainText("(段内无消息)")
            return
        self._l0_text.setPlainText("\n".join(lines))


__all__ = ["SegmentDetail"]