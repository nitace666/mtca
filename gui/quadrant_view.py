"""MTCA 4 象限视图（gui/quadrant_view.py — T34）。

M2.5_PLAYBOOK.md §8：把 segments 按 (urgency, importance) 二维坐标分到
Q1/Q2/Q3/Q4 四象限，顶部加 URGENT 红条，底部预留状态栏（由主窗口挂）。

设计要点：
- 顶部 URGENT 红条：``list_expired()`` 非空时显示，黄色高亮。
- 中部 2x2 网格：``QGridLayout``，每象限是一个 ``QGroupBox``（带 22%
  透明度的象限色背景 + 2px 实线边框），内嵌 QLabel 计数 +
  ``QListWidget`` top-5 段（topic_label 截断 30 字 + current_score +
  urgency/importance）。
- 30 秒自动 ``QTimer`` refresh（让过期紧急段及时进入 URGENT 条）。
- 双击列表项 → ``segment_activated(str)`` 信号，主窗口加载详情。

公共 API：
- ``QuadrantView(QWidget)``
- ``QUADRANT_COLORS`` / ``QUADRANT_LABELS`` 常量（颜色 + 中文标签）
- ``segment_activated`` 信号
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.l0.quadrant import get_quadrant
from src.store.sqlite import MTCA_DB_PATH, query


# ---------------------------------------------------------------------------
# 常量：象限配色 + 中文标签
# ---------------------------------------------------------------------------

# Q1 红 / Q2 青 / Q3 橙 / Q4 灰 —— 见 M2.5_PLAYBOOK.md §2.2
QUADRANT_COLORS: dict[str, str] = {
    "Q1": "#FF6B6B",   # 重要 + 紧急
    "Q2": "#4ECDC4",   # 重要 + 不紧急
    "Q3": "#FFA07A",   # 不重要 + 紧急
    "Q4": "#A8A8A8",   # 两者都不沾
}

QUADRANT_LABELS: dict[str, str] = {
    "Q1": "Q1 重要 + 紧急",
    "Q2": "Q2 重要 + 不紧急",
    "Q3": "Q3 不重要 + 紧急",
    "Q4": "Q4 都不沾",
}

# 列表里展示的 top-N 段
_TOP_N: int = 5

# topic_label 截断字数
_LABEL_MAX_CHARS: int = 30

# 自动 refresh 间隔（毫秒）：30 秒
_REFRESH_INTERVAL_MS: int = 30_000

# URGENT 条样式（黄底黑字加粗）
_URGENT_STYLE: str = (
    "background-color: #FFD700; color: #000; "
    "padding: 8px; font-weight: bold;"
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _resolve_path(path: Optional[Union[Path, str]]) -> Union[Path, str]:
    """None → 默认 DB 路径；其他原样透传。"""
    return path if path is not None else MTCA_DB_PATH


def _truncate(text: str, max_chars: int = _LABEL_MAX_CHARS) -> str:
    """中文 / 英文统一的截断：超过 max_chars 加省略号。"""
    if not text:
        return "(无标题)"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "\u2026"


def _format_score(score: Optional[float]) -> str:
    """分数显示：None → '-'；其他保留 2 位小数。"""
    if score is None:
        return "-"
    try:
        return f"{float(score):.2f}"
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# 主控件
# ---------------------------------------------------------------------------


class QuadrantView(QWidget):
    """4 象限视图：URGENT 红条 + 2x2 grid + 30s 自动 refresh。"""

    # 双击列表项时抛 segment_id
    segment_activated = Signal(str)

    def __init__(
        self,
        db_path: Optional[Union[Path, str]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """初始化布局 + 第一次 refresh + 启动 30s 定时器。"""
        super().__init__(parent)
        self._db_path = _resolve_path(db_path)
        self.urgent_ids: list[str] = []
        # 子控件容器（_build_ui 里填充）
        self.quadrant_lists: dict[str, QListWidget] = {}
        self.quadrant_counts: dict[str, QLabel] = {}

        self._build_ui()
        self._refresh()

        # 30s 自动 refresh（让紧急过期及时显示在红条）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(_REFRESH_INTERVAL_MS)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def set_db_path(self, db_path: Optional[Union[Path, str]]) -> None:
        """切换数据库路径；下次 refresh 生效。"""
        self._db_path = _resolve_path(db_path)

    def db_path(self) -> Optional[Union[Path, str]]:
        """当前数据库路径。"""
        return self._db_path

    def refresh(self) -> int:
        """外部手动调用刷新。返回已渲染的段总数。"""
        return self._refresh()

    # ------------------------------------------------------------------
    # 布局构造
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """构造 UI：URGENT 条 + 2x2 象限 grid。"""
        root = QVBoxLayout()

        # 1) 顶部 URGENT 红条（默认隐藏）
        self.urgent_bar = QLabel("")
        self.urgent_bar.setFrameStyle(QFrame.Shape.StyledPanel)
        self.urgent_bar.setVisible(False)
        self.urgent_bar.setWordWrap(True)
        root.addWidget(self.urgent_bar)

        # 2) 中部 2x2 象限 grid
        grid = QGridLayout()
        for i, q in enumerate(("Q1", "Q2", "Q3", "Q4")):
            box = QGroupBox(QUADRANT_LABELS[q])
            color = QUADRANT_COLORS[q]
            # 22 = 0x22 = 13% alpha，叠加底色做淡背景
            box.setStyleSheet(
                f"QGroupBox {{ background-color: {color}22; "
                f"border: 2px solid {color}; }}"
            )
            v = QVBoxLayout()
            count_label = QLabel("(0 段)")
            v.addWidget(count_label)

            list_widget = QListWidget()
            list_widget.itemDoubleClicked.connect(self._on_item_activated)
            self.quadrant_lists[q] = list_widget
            self.quadrant_counts[q] = count_label
            v.addWidget(list_widget)

            box.setLayout(v)
            grid.addWidget(box, i // 2, i % 2)
        root.addLayout(grid)

        self.setLayout(root)

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------

    def _refresh(self) -> int:
        """重拉 URGENT + segments → 更新 UI。返回渲染段总数。"""
        self._refresh_urgent_bar()
        segments = self._query_all_segments()
        quad_data = self._group_by_quadrant(segments)
        self._populate_quadrant_lists(quad_data)
        return sum(len(v) for v in quad_data.values())

    def _refresh_urgent_bar(self) -> None:
        """拉 list_expired → 更新 URGENT 红条可见性 + 文案。"""
        # 延迟 import 避免模块加载时序问题
        from src.lifecycle.urgent_tracker import list_expired

        try:
            rows = list_expired(path=self._db_path)
        except (ValueError, RuntimeError):
            rows = []
        self.urgent_ids = [r["segment_id"] for r in rows]
        if self.urgent_ids:
            self.urgent_bar.setText(
                f"\u26A0 {len(self.urgent_ids)} 段紧急过期待处理（双击查看）"
            )
            self.urgent_bar.setStyleSheet(_URGENT_STYLE)
            self.urgent_bar.setVisible(True)
        else:
            self.urgent_bar.setVisible(False)
            self.urgent_bar.setText("")

    def _query_all_segments(self) -> list[dict]:
        """拉取全表 segments（不走 list_segments，避开 1000 上限）。"""
        try:
            rows = query(
                "SELECT segment_id, topic_label, urgency_level, "
                "importance_level, current_score, urgent_state "
                "FROM segments WHERE current_tier != 'L3_hidden' "
                "ORDER BY current_score DESC",
                path=self._db_path,
            )
        except (ValueError, RuntimeError):
            return []
        return [dict(r) for r in rows]

    @staticmethod
    def _group_by_quadrant(segments: list[dict]) -> dict[str, list[dict]]:
        """按 (urgency, importance) 分桶到 4 象限。"""
        quad: dict[str, list[dict]] = {"Q1": [], "Q2": [], "Q3": [], "Q4": []}
        for seg in segments:
            try:
                urg = float(seg.get("urgency_level") or 0.0)
                imp = float(seg.get("importance_level") or 0.0)
            except (TypeError, ValueError):
                urg, imp = 0.0, 0.0
            quad[get_quadrant(urg, imp)].append(seg)
        return quad

    def _populate_quadrant_lists(
        self,
        quad_data: dict[str, list[dict]],
    ) -> None:
        """把分组结果灌进 4 个 QListWidget，每象限 top-N。"""
        for q, segs in quad_data.items():
            count_label = self.quadrant_counts[q]
            list_widget = self.quadrant_lists[q]

            list_widget.clear()
            count_label.setText(f"({len(segs)} 段)")

            # top-N 按 current_score 降序（query 已排过序，截断即可）
            for seg in segs[:_TOP_N]:
                item = self._make_segment_item(seg)
                list_widget.addItem(item)

            # 超过 top-N 时给个占位提示
            extra = len(segs) - _TOP_N
            if extra > 0:
                placeholder = QListWidgetItem(f"\u2026 还有 {extra} 段")
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                list_widget.addItem(placeholder)

    def _make_segment_item(self, seg: dict) -> QListWidgetItem:
        """单条 segment → QListWidgetItem（含 segment_id 隐藏数据）。"""
        topic = _truncate(seg.get("topic_label") or "")
        score = _format_score(seg.get("current_score"))
        urg = _format_score(seg.get("urgency_level"))
        imp = _format_score(seg.get("importance_level"))
        urgent_state = (seg.get("urgent_state") or "").strip()
        prefix = "\u26A0 " if urgent_state == "tracking" else ""

        text = f"{prefix}{topic} | score={score} | u={urg} i={imp}"
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, seg.get("segment_id") or "")

        # URGENT tracking 段加粗（视觉强调）
        if urgent_state in ("tracking", "expired"):
            font = QFont()
            font.setBold(True)
            item.setFont(font)

        return item

    # ------------------------------------------------------------------
    # 信号回调
    # ------------------------------------------------------------------

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        """双击列表项 → 抛 segment_activated(segment_id)。"""
        if item is None:
            return
        seg_id = item.data(Qt.ItemDataRole.UserRole)
        if not seg_id:
            return
        self.segment_activated.emit(str(seg_id))


__all__ = ["QuadrantView", "QUADRANT_COLORS", "QUADRANT_LABELS"]
