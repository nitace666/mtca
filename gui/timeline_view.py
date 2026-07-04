"""时间线 / 话题树视图（gui/timeline_view.py — T13）。

USER_CONTROLS.md §3.1：左侧时间线列表 + §6.1 时间线 / §6.2 树状切换。

设计要点：
- 用 QTreeView + QStandardItemModel 承载两种视图（共用一个 widget，
  通过 ``set_mode`` 切换数据源，避免双 widget 状态漂移）。
- 数据源复用 ``src.l0.segment_writer.list_segments``，不直接写 SQL。
- 节点文本格式与 ``src.cli.timeline._render_segment_line`` 对齐，
  保证 GUI / CLI 视觉一致（M1 验收友好）。
- 选中段落后通过 ``segment_selected`` 信号把 ``segment_id`` 传给
  主窗口，主窗口再加载段落详情。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from src.l0.segment_writer import list_segments

# 视图模式常量（暴露给主窗口持久化选择）
MODE_TIMELINE: str = "timeline"
MODE_TREE: str = "tree"
_VALID_MODES: frozenset[str] = frozenset({MODE_TIMELINE, MODE_TREE})

# 列表上限（与 cli/timeline.py 对齐）
_DEFAULT_LIMIT: int = 200

# 段落状态颜色（与 cli/timeline._STYLE_* 对齐）
_FOGGED_COLOR: str = "#b00020"
_SUPERSEDED_COLOR: str = "#888888"
_ARCHIVED_COLOR: str = "#aaaaaa"
_ACTIVE_COLOR: str = "#000000"


class TimelineView(QWidget):
    """时间线 / 话题树视图组件。

    信号：
        ``segment_selected(str)`` —— 选中段落后发出 segment_id。
    """

    segment_selected = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化布局与默认模型。"""
        super().__init__(parent)
        self._mode: str = MODE_TIMELINE
        self._db_path: Optional[Union[Path, str]] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # ---- 顶部：模式切换 ----
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("视图："))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("时间线", MODE_TIMELINE)
        self._mode_combo.addItem("话题树", MODE_TREE)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        toolbar.addWidget(self._mode_combo)
        toolbar.addStretch(1)
        self._count_label = QLabel("0 段")
        toolbar.addWidget(self._count_label)
        layout.addLayout(toolbar)

        # ---- 树状视图 ----
        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels(["段落"])
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setUniformRowHeights(True)
        self._tree.setHeaderHidden(False)
        self._tree.setAnimated(False)
        self._tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._tree.clicked.connect(self._on_tree_clicked)
        layout.addWidget(self._tree, 1)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def set_db_path(self, db_path: Optional[Union[Path, str]]) -> None:
        """设置数据库路径；下次 refresh 时生效。"""
        self._db_path = db_path

    def set_mode(self, mode: str) -> None:
        """切换视图模式（mode ∈ {timeline, tree}）。"""
        if mode not in _VALID_MODES:
            return
        if mode == self._mode:
            return
        self._mode = mode
        idx = self._mode_combo.findData(mode)
        if idx >= 0:
            self._mode_combo.blockSignals(True)
            self._mode_combo.setCurrentIndex(idx)
            self._mode_combo.blockSignals(False)
        self.refresh()

    def mode(self) -> str:
        """当前视图模式。"""
        return self._mode

    def refresh(self) -> int:
        """从 DB 重新拉段落并重建模型。

        返回：本次加载到的叶子段数（不含分组节点）。
        """
        segments = list_segments(limit=_DEFAULT_LIMIT, path=self._db_path)
        self._model.removeRows(0, self._model.rowCount())

        if not segments:
            placeholder = QStandardItem("无段落数据。请先创建会话 / 写入消息。")
            placeholder.setEnabled(False)
            self._model.appendRow(placeholder)
            self._count_label.setText("0 段")
            self._tree.expandAll()
            return 0

        if self._mode == MODE_TIMELINE:
            n = self._populate_timeline(segments)
        else:
            n = self._populate_tree(segments)
        self._count_label.setText(f"{n} 段")
        self._tree.expandAll()
        return n

    # ------------------------------------------------------------------
    # 内部：数据渲染
    # ------------------------------------------------------------------

    @staticmethod
    def _format_day(ms: int) -> str:
        """毫秒时间戳 → 日期字符串。"""
        if not ms:
            return "----"
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")

    @staticmethod
    def _format_time(ms: int) -> str:
        """毫秒时间戳 → HH:MM。"""
        if not ms:
            return "--:--"
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%H:%M")

    @staticmethod
    def _project_from_topic(topic: str) -> str:
        """从 topic_label 提取项目名（首个非空白 token）。"""
        text = (topic or "").strip()
        if not text:
            return "(未分类)"
        parts = text.split(maxsplit=1)
        return parts[0] if parts else "(未分类)"

    def _style_for(self, seg: dict) -> tuple[str, str]:
        """返回 (前景色, 状态标签)，与 cli/timeline._classify_segment 对齐。"""
        fog_state = (seg.get("fog_state") or "clear").lower()
        silence_state = (seg.get("silence_state") or "active").lower()
        if seg.get("superseded_by"):
            return _SUPERSEDED_COLOR, "[superseded]"
        if fog_state == "fogged_once":
            return _FOGGED_COLOR, "[/雾化]"
        if fog_state == "archived":
            return _ARCHIVED_COLOR, "[archived]"
        if silence_state == "silent":
            return _ARCHIVED_COLOR, "[silent]"
        if silence_state == "dormant":
            return _SUPERSEDED_COLOR, "[dormant]"
        return _ACTIVE_COLOR, "[active]"

    def _make_segment_item(self, seg: dict) -> QStandardItem:
        """生成段落叶子节点。"""
        color, label = self._style_for(seg)
        start_at = int(seg.get("start_at") or 0)
        end_at = int(seg.get("end_at") or 0)
        topic = seg.get("topic_label") or "(无标题)"
        score = seg.get("current_score")
        try:
            score_str = f"{float(score):.0f}"
        except (TypeError, ValueError):
            score_str = "0"
        text = (
            f"{self._format_time(start_at)}-{self._format_time(end_at)}  "
            f"{topic}  score={score_str}  {label}"
        )
        item = QStandardItem(text)
        item.setData(seg.get("segment_id"), Qt.ItemDataRole.UserRole)
        item.setForeground(Qt.GlobalColor.black)
        # 让 Qt 实际渲染用前景色（部分样式走 setForeground 才生效）
        item.setData(color, Qt.ItemDataRole.ForegroundRole)
        return item

    def _make_group(self, name: str, meta: str = "") -> QStandardItem:
        """生成分组节点（不可选）。"""
        text = f"{name}  {meta}" if meta else name
        item = QStandardItem(text)
        item.setSelectable(False)
        item.setEnabled(True)
        from PySide6.QtGui import QBrush
        item.setForeground(QBrush(Qt.GlobalColor.darkMagenta))
        return item

    def _populate_timeline(self, segments: list[dict]) -> int:
        """按日分组，渲染时间线视图。"""
        groups: dict[str, list[dict]] = {}
        for seg in segments:
            day = self._format_day(int(seg.get("start_at") or 0))
            groups.setdefault(day, []).append(seg)

        n = 0
        for day in sorted(groups.keys(), reverse=True):
            day_segs = sorted(
                groups[day],
                key=lambda s: s.get("start_at") or 0,
                reverse=True,
            )
            n_fog = sum(
                1 for s in day_segs
                if (s.get("fog_state") or "clear") == "fogged_once"
            )
            group = self._make_group(day, f"({len(day_segs)} 段 / {n_fog} /雾化)")
            self._model.appendRow(group)
            for seg in day_segs:
                group.appendRow(self._make_segment_item(seg))
                n += 1
        return n

    def _populate_tree(self, segments: list[dict]) -> int:
        """按项目分组，渲染树状视图。"""
        groups: dict[str, list[dict]] = {}
        for seg in segments:
            topic = seg.get("topic_label") or "(无标题)"
            proj = self._project_from_topic(topic)
            groups.setdefault(proj, []).append(seg)

        n = 0
        for proj in sorted(groups.keys()):
            proj_segs = sorted(
                groups[proj],
                key=lambda s: s.get("start_at") or 0,
                reverse=True,
            )
            n_fog = sum(
                1 for s in proj_segs
                if (s.get("fog_state") or "clear") == "fogged_once"
            )
            group = self._make_group(proj, f"({len(proj_segs)} 段 / {n_fog} /雾化)")
            self._model.appendRow(group)
            for seg in proj_segs:
                group.appendRow(self._make_segment_item(seg))
                n += 1
        return n

    # ------------------------------------------------------------------
    # 内部：信号回调
    # ------------------------------------------------------------------

    def _on_mode_changed(self, _index: int) -> None:
        """视图模式切换 → 重新拉数据。"""
        data = self._mode_combo.currentData()
        if isinstance(data, str):
            self._mode = data
        self.refresh()

    def _on_tree_clicked(self, index) -> None:
        """点击段落叶子节点 → 发送 segment_selected。"""
        item = self._model.itemFromIndex(index.siblingAtColumn(0))
        if item is None:
            return
        seg_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(seg_id, str) and seg_id:
            self.segment_selected.emit(seg_id)


__all__ = [
    "TimelineView",
    "MODE_TIMELINE",
    "MODE_TREE",
]