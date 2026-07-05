"""MTCA 知识图谱视图（gui/graph_view.py — T25）。

USER_CONTROLS.md §3.4：用 ``QGraphicsView`` / ``QGraphicsScene`` 渲染段落
节点 + 4 种关系边；支持时间倒带 / 话题过滤 / 静默态多选 + 节点拖拽 +
滚轮缩放 + 点击高亮 + 双击跳转。

设计要点：
- 节点：``NodeItem(QGraphicsEllipseItem)``；半径按 ``current_score`` 映射
  （对数尺度）；填充色按 ``silence_state``（active 蓝 / dormant 灰 /
  silent 暗灰）；``fogged_once`` 红框；``superseded_by`` 走删除线样式。
- 边：``EdgeItem(QGraphicsPathItem)``；4 种关系 4 种颜色（references /
  supersedes / related_to / derived_from），带箭头，cosmetic pen 防止
  缩放后线宽异常。
- 布局：自写 spring-electrical 力导向模型；初始按圆周均匀分布，迭代
  60 次后收敛；O(n²) 排斥力 + Hooke 引力（仅对有关系的节点对）。
- 交互：Ctrl+滚轮缩放（裸滚轮交给 view 的 ScrollHandDrag 平移）；
  节点 ``ItemIsMovable`` 拖动后通过 ``itemChange`` 通知邻接边更新路径。
- 信号：``segment_focus(str)`` —— 双击节点时抛出 segment_id，主窗口
  切换到段落详情 Tab 并加载。

公共 API：
- ``GraphView(QWidget)``
- ``set_db_path(path)`` / ``refresh()``
- ``segment_focus`` 信号
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from PySide6.QtCore import QLineF, QPointF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from src.l0.segment_writer import list_segments
from src.store.sqlite import query

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 节点半径范围（按 importance 对数映射）
_MIN_NODE_RADIUS: float = 7.0
_MAX_NODE_RADIUS: float = 28.0
_SCORE_LOG_DIVISOR: float = 5.0  # log10(score+1)/5 归一到 [0, 1]

# 场景画布尺寸（用于初始布局 + 边界夹紧）
_SCENE_WIDTH: float = 2400.0
_SCENE_HEIGHT: float = 1800.0

# 力导向布局参数
_LAYOUT_ITERATIONS: int = 60
_IDEAL_EDGE_LEN: float = 110.0
_REPULSE_STRENGTH: float = 6500.0
_HOOKE_K: float = 0.08
_MAX_DISP: float = 18.0
_TIME_STEP: float = 1.0

# 视图缩放
_ZOOM_FACTOR: float = 1.15
_MIN_ZOOM: float = 0.2
_MAX_ZOOM: float = 4.0

# 时间滑块：用 0~SLIDER_MAX 表示「从最早到最新」进度（避免 int32 溢出）
_SLIDER_MAX: int = 10_000

# 颜色：节点（按 silence_state）
_COLOR_ACTIVE: QColor = QColor(0x21, 0x96, 0xF3)      # 蓝
_COLOR_DORMANT: QColor = QColor(0x9E, 0x9E, 0x9E)    # 灰
_COLOR_SILENT: QColor = QColor(0x42, 0x42, 0x42)     # 暗灰
_COLOR_DEFAULT: QColor = QColor(0xB0, 0xBE, 0xC5)    # 兜底浅灰

# 颜色：边（按 relation_type）
_COLOR_REFERENCES: QColor = QColor(0x19, 0x76, 0xD2)   # 深蓝
_COLOR_SUPERSEDES: QColor = QColor(0xD3, 0x2F, 0x2F)   # 红
_COLOR_RELATED_TO: QColor = QColor(0x38, 0x8E, 0x3C)   # 绿
_COLOR_DERIVED_FROM: QColor = QColor(0x7B, 0x1F, 0xA2) # 紫

# 颜色：其他视觉信号
_COLOR_HIGHLIGHT: QColor = QColor(0xFF, 0xC1, 0x07)    # 琥珀色
_COLOR_FOGGED_BORDER: QColor = QColor(0xB0, 0x00, 0x20) # 深红
_COLOR_BG: QColor = QColor(0xFA, 0xFA, 0xFA)           # 场景背景

# 静默态候选（V0.4_PIVOT.md §3.6）
_SILENCE_STATES: tuple[str, ...] = ("active", "dormant", "silent")

# 关系类型候选（与 src.relations.graph.VALID_RELATION_TYPES 对齐）
_RELATION_TYPES: tuple[str, ...] = (
    "references", "supersedes", "related_to", "derived_from",
)

# 列表上限（防止超大图卡顿）
_MAX_NODES: int = 400

# 按钮尺寸（USER_RULES §8）
_BTN_MIN_WIDTH: int = 80
_BTN_MIN_HEIGHT: int = 32


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _coerce_score(value: Any) -> float:
    """把 current_score 规整为非负浮点数。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < 0 or math.isnan(v) or math.isinf(v):
        return 0.0
    return v


def _radius_from_score(score: Any) -> float:
    """importance（current_score）→ 节点半径。"""
    s = _coerce_score(score)
    if s <= 0:
        return _MIN_NODE_RADIUS
    norm = min(1.0, math.log10(s + 1.0) / _SCORE_LOG_DIVISOR)
    return _MIN_NODE_RADIUS + norm * (_MAX_NODE_RADIUS - _MIN_NODE_RADIUS)


def _silence_color(silence_state: str) -> QColor:
    """silence_state → 节点填充色。"""
    s = (silence_state or "active").strip().lower()
    if s == "active":
        return _COLOR_ACTIVE
    if s == "dormant":
        return _COLOR_DORMANT
    if s == "silent":
        return _COLOR_SILENT
    return _COLOR_DEFAULT


def _relation_color(rel_type: str) -> QColor:
    """relation_type → 边颜色。"""
    r = (rel_type or "").strip().lower()
    if r == "references":
        return _COLOR_REFERENCES
    if r == "supersedes":
        return _COLOR_SUPERSEDES
    if r == "related_to":
        return _COLOR_RELATED_TO
    if r == "derived_from":
        return _COLOR_DERIVED_FROM
    return QColor(0x60, 0x60, 0x60)


def _format_date(ms: int) -> str:
    """毫秒时间戳 → YYYY-MM-DD 字符串。"""
    if not ms:
        return "----"
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return "----"


# ---------------------------------------------------------------------------
# NodeItem：段落节点
# ---------------------------------------------------------------------------


class NodeItem(QGraphicsEllipseItem):
    """图谱节点：圆形 + 文本标签。"""

    def __init__(self, seg: dict, x: float, y: float) -> None:
        """按 current_score 决定半径 + 按 silence_state 着色。"""
        radius = _radius_from_score(seg.get("current_score"))
        super().__init__(-radius, -radius, 2 * radius, 2 * radius)
        self.setPos(x, y)
        self.seg = seg
        self._radius = radius
        self._edges: list["EdgeItem"] = []

        fill = _silence_color(seg.get("silence_state"))
        self.setBrush(QBrush(fill))

        # 边框：fogged_once → 红色加粗；superseded → 删除线样式（细）
        fog_state = (seg.get("fog_state") or "clear").lower()
        if fog_state == "fogged_once":
            self.setPen(QPen(_COLOR_FOGGED_BORDER, 3))
        elif seg.get("superseded_by"):
            self.setPen(QPen(QColor(0x88, 0x88, 0x88), 1, Qt.PenStyle.DashLine))
        else:
            self.setPen(QPen(QColor(0x33, 0x33, 0x33), 1))

        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True
        )
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True
        )
        self.setZValue(1)
        self.setAcceptHoverEvents(True)
        self.setToolTip(self._tooltip_text(seg))

        # 文本标签（白色，对比度优先）
        topic = (seg.get("topic_label") or "(无标题)").strip()
        if len(topic) > 10:
            topic = topic[:9] + "…"
        label = QGraphicsSimpleTextItem(topic, parent=self)
        label.setBrush(QBrush(QColor(0xFF, 0xFF, 0xFF)))
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        label.setFont(font)
        br = label.boundingRect()
        label.setPos(-br.width() / 2.0, -br.height() / 2.0)
        label.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True
        )

    @staticmethod
    def _tooltip_text(seg: dict) -> str:
        """构造 hover 提示。"""
        topic = seg.get("topic_label") or "(无标题)"
        silence = seg.get("silence_state") or "-"
        fog = seg.get("fog_state") or "-"
        score = _coerce_score(seg.get("current_score"))
        return (
            f"话题: {topic}\n"
            f"静默态: {silence}\n"
            f"雾化: {fog}\n"
            f"重要性: {score:.1f}\n"
            f"ID: {seg.get('segment_id') or ''}"
        )

    @property
    def radius(self) -> float:
        """节点半径（用于边端点偏移计算）。"""
        return self._radius

    @property
    def seg_id(self) -> str:
        """段落 ID。"""
        return str(self.seg.get("segment_id") or "")

    def add_edge(self, edge: "EdgeItem") -> None:
        """登记邻接边（拖动时联动更新）。"""
        self._edges.append(edge)

    def set_highlight(self, on: bool) -> None:
        """切换高亮描边。"""
        if on:
            self.setPen(QPen(_COLOR_HIGHLIGHT, 4))
        else:
            fog_state = (self.seg.get("fog_state") or "clear").lower()
            if fog_state == "fogged_once":
                self.setPen(QPen(_COLOR_FOGGED_BORDER, 3))
            elif self.seg.get("superseded_by"):
                self.setPen(QPen(QColor(0x88, 0x88, 0x88), 1,
                                 Qt.PenStyle.DashLine))
            else:
                self.setPen(QPen(QColor(0x33, 0x33, 0x33), 1))

    def itemChange(self, change, value):  # type: ignore[override]
        """节点被拖动后通知邻接边刷新路径。"""
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge in self._edges:
                edge.update_path()
        return super().itemChange(change, value)


# ---------------------------------------------------------------------------
# EdgeItem：关系边
# ---------------------------------------------------------------------------


class EdgeItem(QGraphicsPathItem):
    """关系边：带箭头的有向线段。"""

    def __init__(self, source: NodeItem, target: NodeItem, rel_type: str) -> None:
        """构造边并绑定源 / 目标节点 + 关系类型。"""
        super().__init__()
        self.source = source
        self.target = target
        self.rel_type = (rel_type or "").strip().lower()

        pen = QPen(_relation_color(self.rel_type), 1.6)
        pen.setCosmetic(True)  # 缩放时线宽保持
        self.setPen(pen)
        self.setBrush(QBrush(_relation_color(self.rel_type)))
        self.setZValue(0)

        # 互登记
        source.add_edge(self)
        target.add_edge(self)

        self.update_path()
        self.setToolTip(f"关系: {self.rel_type}")

    def update_path(self) -> None:
        """按当前源 / 目标位置重画路径（含箭头头部）。"""
        sp = self.source.scenePos()
        tp = self.target.scenePos()
        dx = tp.x() - sp.x()
        dy = tp.y() - sp.y()
        dist = math.hypot(dx, dy)
        if dist < 1e-3:
            return
        ux, uy = dx / dist, dy / dist
        rs = self.source.radius
        rt = self.target.radius
        start = QPointF(sp.x() + ux * rs, sp.y() + uy * rs)
        end = QPointF(tp.x() - ux * rt, tp.y() - uy * rt)

        path = QPainterPath(start)
        path.lineTo(end)

        # 箭头头部（V 字）
        arrow_len = 9.0
        wing = 5.0
        # 从 end 指向 start 的反方向
        back_ang = math.atan2(-dy, -dx)
        left = QPointF(
            end.x() + arrow_len * math.cos(back_ang - math.pi / 7),
            end.y() + arrow_len * math.sin(back_ang - math.pi / 7),
        )
        right = QPointF(
            end.x() + arrow_len * math.cos(back_ang + math.pi / 7),
            end.y() + arrow_len * math.sin(back_ang + math.pi / 7),
        )
        arrow = QPainterPath()
        arrow.moveTo(end)
        arrow.lineTo(left)
        arrow.lineTo(right)
        arrow.closeSubpath()
        # 用同色填充箭头
        self.setBrush(QBrush(_relation_color(self.rel_type)))
        # 把箭头合并到主路径（用 addPath 不行：QPainterPath 可直接合并子路径）
        path.addPath(arrow)
        self.setPath(path)


# ---------------------------------------------------------------------------
# GraphGraphicsView：带缩放 + 双击检测的 GraphicsView
# ---------------------------------------------------------------------------


class GraphGraphicsView(QGraphicsView):
    """承载 GraphScene 的 GraphicsView。

    - 滚轮：Ctrl+滚轮缩放；裸滚轮垂直滚动（默认行为）。
    - 双击：派发到主控件处理（节点跳转）。
    """

    node_double_clicked = Signal(str)  # segment_id

    def __init__(self, scene: QGraphicsScene, parent: Optional[QWidget] = None) -> None:
        """初始化 view；启用抗锯齿 + 平滑变换 + ScrollHandDrag。"""
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.SmartViewportUpdate
        )
        self.setMouseTracking(True)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        """Ctrl+滚轮缩放。"""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta == 0:
                return
            factor = _ZOOM_FACTOR if delta > 0 else 1.0 / _ZOOM_FACTOR
            cur = self.transform().m11()
            new_scale = cur * factor
            if new_scale < _MIN_ZOOM or new_scale > _MAX_ZOOM:
                event.accept()
                return
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        """双击节点 → 发信号；其他位置走默认。"""
        scene_pos = self.mapToScene(event.pos())
        # 缩放后 itemAt 需要传入 view 的 transform
        item = self.scene().itemAt(
            scene_pos, self.transform()
        )
        # 命中可能是子项（label）；递归找回父 NodeItem
        node: Optional[NodeItem] = None
        cur = item
        while cur is not None:
            if isinstance(cur, NodeItem):
                node = cur
                break
            cur = cur.parentItem()
        if node is not None:
            self.node_double_clicked.emit(node.seg_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


# ---------------------------------------------------------------------------
# GraphView：知识图谱主控件
# ---------------------------------------------------------------------------


class GraphView(QWidget):
    """知识图谱视图（USER_CONTROLS.md §3.4）。

    信号：
        ``segment_focus(str)`` —— 双击节点时携带 segment_id 抛出。
    """

    segment_focus = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化布局 + 默认状态。"""
        super().__init__(parent)
        self._db_path: Optional[Union[Path, str]] = None
        self._all_segments: list[dict] = []
        self._all_relations: list[dict] = []
        self._nodes_by_id: dict[str, NodeItem] = {}
        self._edges: list[EdgeItem] = []
        self._selected_node: Optional[NodeItem] = None
        self._time_min: int = 0
        self._time_max: int = 0

        self._build_ui()

    # ------------------------------------------------------------------
    # 布局构造
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """顶部过滤 + 时间滑块 + 画布 + 详情 + 底部按钮。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # ---- 过滤组（话题 + 静默态） ----
        filter_box = QGroupBox("过滤")
        filter_layout = QHBoxLayout(filter_box)
        filter_layout.setContentsMargins(8, 4, 8, 4)

        filter_layout.addWidget(QLabel("话题："))
        self._topic_combo = QComboBox()
        self._topic_combo.setMinimumWidth(140)
        self._topic_combo.addItem("全部", None)
        self._topic_combo.currentIndexChanged.connect(self._apply_filters)
        filter_layout.addWidget(self._topic_combo)

        filter_layout.addSpacing(12)
        filter_layout.addWidget(QLabel("静默态："))
        self._silence_checks: dict[str, QCheckBox] = {}
        for state in _SILENCE_STATES:
            cb = QCheckBox(state)
            cb.setChecked(True)
            cb.toggled.connect(self._apply_filters)
            self._silence_checks[state] = cb
            filter_layout.addWidget(cb)

        filter_layout.addStretch(1)
        self._count_label = QLabel("0 节点 / 0 边")
        filter_layout.addWidget(self._count_label)
        layout.addWidget(filter_box)

        # ---- 时间倒带 ----
        time_box = QGroupBox("时间倒带")
        time_layout = QHBoxLayout(time_box)
        time_layout.setContentsMargins(8, 4, 8, 4)
        self._time_min_label = QLabel("----")
        self._time_max_label = QLabel("----")
        self._time_slider = QSlider(Qt.Orientation.Horizontal)
        self._time_slider.setMinimum(0)
        self._time_slider.setMaximum(100)
        self._time_slider.setValue(100)
        self._time_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._time_slider.valueChanged.connect(self._on_time_changed)
        time_layout.addWidget(self._time_min_label)
        time_layout.addWidget(self._time_slider, 1)
        time_layout.addWidget(self._time_max_label)
        layout.addWidget(time_box)

        # ---- 画布 ----
        self._scene = QGraphicsScene()
        self._scene.setSceneRect(
            -_SCENE_WIDTH / 2, -_SCENE_HEIGHT / 2,
            _SCENE_WIDTH, _SCENE_HEIGHT,
        )
        self._scene.setBackgroundBrush(QBrush(_COLOR_BG))
        self._scene.selectionChanged.connect(self._on_selection_changed)

        self._view = GraphGraphicsView(self._scene)
        self._view.node_double_clicked.connect(self._on_node_dbl_clicked)
        layout.addWidget(self._view, 1)

        # ---- 节点详情 ----
        detail_box = QGroupBox("节点详情")
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(8, 4, 8, 4)
        self._detail_label = QLabel("点击节点查看详情；双击节点跳转到段落详情。")
        self._detail_label.setWordWrap(True)
        self._detail_label.setMinimumHeight(72)
        detail_layout.addWidget(self._detail_label)

        # ---- 图例 ----
        legend_layout = QHBoxLayout()
        legend_layout.addWidget(QLabel("图例："))
        legend_layout.addWidget(self._legend_swatch("active", _COLOR_ACTIVE))
        legend_layout.addWidget(self._legend_swatch("dormant", _COLOR_DORMANT))
        legend_layout.addWidget(self._legend_swatch("silent", _COLOR_SILENT))
        legend_layout.addSpacing(10)
        for rt in _RELATION_TYPES:
            legend_layout.addWidget(self._legend_swatch(rt, _relation_color(rt)))
        legend_layout.addStretch(1)
        detail_layout.addLayout(legend_layout)
        layout.addWidget(detail_box)

        # ---- 底部按钮 ----
        btn_layout = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.setMinimumSize(_BTN_MIN_WIDTH, _BTN_MIN_HEIGHT)
        refresh_btn.clicked.connect(self.refresh)
        btn_layout.addWidget(refresh_btn)

        relayout_btn = QPushButton("重新布局")
        relayout_btn.setMinimumSize(_BTN_MIN_WIDTH, _BTN_MIN_HEIGHT)
        relayout_btn.clicked.connect(self._relayout)
        btn_layout.addWidget(relayout_btn)

        reset_btn = QPushButton("复位缩放")
        reset_btn.setMinimumSize(_BTN_MIN_WIDTH, _BTN_MIN_HEIGHT)
        reset_btn.clicked.connect(self._reset_zoom)
        btn_layout.addWidget(reset_btn)

        btn_layout.addStretch(1)
        zoom_label = QLabel("提示：Ctrl+滚轮缩放；拖拽节点；拖拽空白处平移")
        zoom_label.setStyleSheet("color: #666;")
        btn_layout.addWidget(zoom_label)
        layout.addLayout(btn_layout)

    def _legend_swatch(self, text: str, color: QColor) -> QLabel:
        """构造一个色块 + 文字的图例标签。"""
        lbl = QLabel(f"  {text}  ")
        lbl.setAutoFillBackground(True)
        lbl.setStyleSheet(
            f"background-color: {color.name()}; color: white; "
            f"padding: 2px 8px; border-radius: 3px; font-weight: bold;"
        )
        return lbl

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def set_db_path(self, db_path: Optional[Union[Path, str]]) -> None:
        """设置数据库路径；下次 refresh 生效。"""
        self._db_path = db_path

    def db_path(self) -> Optional[Union[Path, str]]:
        """当前数据库路径（便于主窗口回传）。"""
        return self._db_path

    def refresh(self) -> int:
        """重拉段落 + 关系 → 更新控件。

        返回：可见节点数（0 表示空数据）。
        """
        # ---- 加载段落 ----
        try:
            segments = list_segments(limit=_MAX_NODES, path=self._db_path)
        except (ValueError, RuntimeError):
            segments = []
        self._all_segments = list(segments or [])

        # ---- 加载关系 ----
        try:
            relations = query(
                "SELECT * FROM segment_relations ORDER BY created_at ASC",
                path=self._db_path,
            )
        except (ValueError, RuntimeError):
            relations = []
        self._all_relations = list(relations or [])

        # ---- 话题下拉 ----
        topics = sorted({
            (s.get("topic_label") or "(无标题)").strip() or "(无标题)"
            for s in self._all_segments
        })
        self._topic_combo.blockSignals(True)
        self._topic_combo.clear()
        self._topic_combo.addItem("全部", None)
        for t in topics:
            self._topic_combo.addItem(t, t)
        self._topic_combo.blockSignals(False)

        # ---- 时间滑块 ----
        # QSlider 内部值是 int32（≤2^31≈21亿），毫秒时间戳会溢出。
        # 滑块用 0~SLIDER_MAX 表示「从最早到最新的进度」，实际过滤时
        # 通过 _slider_to_time 换算回毫秒时间戳。
        starts = [int(s.get("start_at") or 0) for s in self._all_segments]
        ends = [int(s.get("end_at") or 0) for s in self._all_segments]
        all_times = [t for t in starts + ends if t > 0]
        if all_times:
            self._time_min = min(all_times)
            self._time_max = max(all_times)
        else:
            self._time_min = 0
            self._time_max = 0
        self._time_min_label.setText(_format_date(self._time_min))
        self._time_max_label.setText(_format_date(self._time_max))
        self._time_slider.blockSignals(True)
        self._time_slider.setMinimum(0)
        self._time_slider.setMaximum(_SLIDER_MAX)
        self._time_slider.setValue(_SLIDER_MAX)
        self._time_slider.blockSignals(False)

        self._apply_filters()
        return len(self._nodes_by_id)

    # ------------------------------------------------------------------
    # 过滤 / 重布局
    # ------------------------------------------------------------------

    def _on_time_changed(self, _value: int) -> None:
        """时间滑块变更 → 重过滤。"""
        self._apply_filters()

    def _slider_to_time(self, slider_value: int) -> int:
        """滑块进度 → 实际毫秒时间戳（线性映射）。"""
        if self._time_max <= 0 or self._time_max <= self._time_min:
            # 无数据 / 单点：阈值极大（始终包含）
            return 2**62
        if slider_value <= 0:
            return self._time_min
        if slider_value >= _SLIDER_MAX:
            return self._time_max
        ratio = float(slider_value) / float(_SLIDER_MAX)
        return int(self._time_min + ratio * (self._time_max - self._time_min))

    def _apply_filters(self) -> None:
        """按过滤条件重建场景。"""
        # ---- 清空场景 ----
        self._scene.clear()
        self._nodes_by_id.clear()
        self._edges.clear()
        self._selected_node = None

        if not self._all_segments:
            self._count_label.setText("0 节点 / 0 边")
            return

        # ---- 过滤 ----
        time_threshold = self._slider_to_time(int(self._time_slider.value()))
        topic_filter = self._topic_combo.currentData()
        if isinstance(topic_filter, str) and not topic_filter:
            topic_filter = None
        selected_states = {
            s for s, cb in self._silence_checks.items() if cb.isChecked()
        }

        visible: list[dict] = []
        for seg in self._all_segments:
            end_at = int(seg.get("end_at") or 0)
            # 时间倒带：end_at <= 阈值 才显示
            if end_at <= 0 or end_at > time_threshold:
                continue
            topic = (seg.get("topic_label") or "(无标题)").strip() or "(无标题)"
            if topic_filter is not None and topic != topic_filter:
                continue
            silence = (seg.get("silence_state") or "active").lower()
            if silence not in selected_states:
                continue
            visible.append(seg)

        if not visible:
            self._count_label.setText("0 节点 / 0 边")
            return

        # ---- 限流 ----
        if len(visible) > _MAX_NODES:
            visible = visible[:_MAX_NODES]

        # ---- 建节点（圆周初始布局 + 随机扰动） ----
        n = len(visible)
        R = min(_SCENE_WIDTH, _SCENE_HEIGHT) / 3.0
        nodes: list[NodeItem] = []
        for i, seg in enumerate(visible):
            ang = 2.0 * math.pi * i / max(n, 1)
            x = R * math.cos(ang) + random.uniform(-25.0, 25.0)
            y = R * math.sin(ang) + random.uniform(-25.0, 25.0)
            node = NodeItem(seg, x, y)
            self._scene.addItem(node)
            sid = seg.get("segment_id")
            if sid:
                self._nodes_by_id[str(sid)] = node
            nodes.append(node)

        # ---- 过滤关系（两端都在可见集中） ----
        visible_ids = {
            str(s.get("segment_id")) for s in visible if s.get("segment_id")
        }
        visible_rels: list[dict] = []
        for r in self._all_relations:
            a = r.get("seg_a_id")
            b = r.get("seg_b_id")
            if a in visible_ids and b in visible_ids:
                visible_rels.append(r)

        # ---- 建边 ----
        for r in visible_rels:
            src = self._nodes_by_id.get(str(r.get("seg_a_id")))
            tgt = self._nodes_by_id.get(str(r.get("seg_b_id")))
            if src is None or tgt is None or src is tgt:
                continue
            rel_type = r.get("relation_type") or "related_to"
            edge = EdgeItem(src, tgt, rel_type)
            self._scene.addItem(edge)
            self._edges.append(edge)

        # ---- 力导向布局 ----
        self._force_layout(nodes, visible_rels)
        for edge in self._edges:
            edge.update_path()

        # ---- 视图居中 ----
        if nodes:
            self._view.centerOn(QPointF(0, 0))

        self._count_label.setText(
            f"{len(nodes)} 节点 / {len(visible_rels)} 边"
        )

    def _relayout(self) -> None:
        """重新打散初始位置 + 跑一次力导向。"""
        if not self._all_segments:
            return
        # 给现有节点一点扰动
        for node in self._nodes_by_id.values():
            node.setPos(
                node.pos().x() + random.uniform(-40.0, 40.0),
                node.pos().y() + random.uniform(-40.0, 40.0),
            )
        self._force_layout(list(self._nodes_by_id.values()), self._all_relations)
        for edge in self._edges:
            edge.update_path()

    def _reset_zoom(self) -> None:
        """视图变换复位。"""
        self._view.resetTransform()
        self._view.centerOn(QPointF(0, 0))

    # ------------------------------------------------------------------
    # 力导向布局（spring-electrical）
    # ------------------------------------------------------------------

    def _force_layout(
        self,
        nodes: list[NodeItem],
        edges: list[dict],
        iterations: int = _LAYOUT_ITERATIONS,
    ) -> None:
        """简单 spring-electrical 模型。

        排斥力：所有节点对（1/d²）；引力：仅对有边节点对（Hooke）。
        O(n²) 排斥力对 n≤400 量级足够（M1 验收）。
        """
        if not nodes:
            return
        positions: dict[int, QPointF] = {
            id(n): QPointF(n.scenePos().x(), n.scenePos().y())
            for n in nodes
        }
        half_w = _SCENE_WIDTH / 2.0 - 60.0
        half_h = _SCENE_HEIGHT / 2.0 - 60.0
        nodes_list = list(nodes)

        for _it in range(iterations):
            forces: dict[int, QPointF] = {
                id(n): QPointF(0.0, 0.0) for n in nodes_list
            }
            # 排斥力
            for i in range(len(nodes_list)):
                a = nodes_list[i]
                pa = positions[id(a)]
                for j in range(i + 1, len(nodes_list)):
                    b = nodes_list[j]
                    pb = positions[id(b)]
                    dx = pb.x() - pa.x()
                    dy = pb.y() - pa.y()
                    d2 = dx * dx + dy * dy
                    if d2 < 1.0:
                        d2 = 1.0
                        dx = random.uniform(-1.0, 1.0)
                        dy = random.uniform(-1.0, 1.0)
                    d = math.sqrt(d2)
                    fmag = _REPULSE_STRENGTH / d2
                    fx = fmag * dx / d
                    fy = fmag * dy / d
                    forces[id(a)] = QPointF(
                        forces[id(a)].x() - fx, forces[id(a)].y() - fy
                    )
                    forces[id(b)] = QPointF(
                        forces[id(b)].x() + fx, forces[id(b)].y() + fy
                    )

            # 引力
            for e in edges:
                src_id = str(e.get("seg_a_id") or "")
                tgt_id = str(e.get("seg_b_id") or "")
                src = self._nodes_by_id.get(src_id)
                tgt = self._nodes_by_id.get(tgt_id)
                if src is None or tgt is None or src is tgt:
                    continue
                if id(src) not in positions or id(tgt) not in positions:
                    continue
                ps = positions[id(src)]
                pt = positions[id(tgt)]
                dx = pt.x() - ps.x()
                dy = pt.y() - ps.y()
                d = math.hypot(dx, dy)
                if d < 1e-3:
                    continue
                fmag = (d - _IDEAL_EDGE_LEN) * _HOOKE_K
                fx = fmag * dx / d
                fy = fmag * dy / d
                forces[id(src)] = QPointF(
                    forces[id(src)].x() + fx, forces[id(src)].y() + fy
                )
                forces[id(tgt)] = QPointF(
                    forces[id(tgt)].x() - fx, forces[id(tgt)].y() - fy
                )

            # 步进 + 限幅 + 边界夹紧
            for n in nodes_list:
                fx = forces[id(n)].x()
                fy = forces[id(n)].y()
                mag = math.hypot(fx, fy)
                if mag > _MAX_DISP:
                    fx = fx / mag * _MAX_DISP
                    fy = fy / mag * _MAX_DISP
                nx = positions[id(n)].x() + fx * _TIME_STEP
                ny = positions[id(n)].y() + fy * _TIME_STEP
                if nx < -half_w:
                    nx = -half_w
                elif nx > half_w:
                    nx = half_w
                if ny < -half_h:
                    ny = -half_h
                elif ny > half_h:
                    ny = half_h
                positions[id(n)] = QPointF(nx, ny)

        for n in nodes_list:
            p = positions[id(n)]
            n.setPos(p)

    # ------------------------------------------------------------------
    # 交互回调
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        """节点选中 → 高亮 + 显示详情；取消选中 → 还原。"""
        items = self._scene.selectedItems()
        # 找到第一个 NodeItem
        node: Optional[NodeItem] = None
        for it in items:
            if isinstance(it, NodeItem):
                node = it
                break
        if node is None:
            if self._selected_node is not None:
                self._selected_node.set_highlight(False)
                self._selected_node = None
            self._detail_label.setText(
                "点击节点查看详情；双击节点跳转到段落详情。"
            )
            return

        if self._selected_node is not None and self._selected_node is not node:
            self._selected_node.set_highlight(False)
        node.set_highlight(True)
        self._selected_node = node

        seg = node.seg
        topic = seg.get("topic_label") or "(无标题)"
        silence = seg.get("silence_state") or "-"
        fog = seg.get("fog_state") or "-"
        score = _coerce_score(seg.get("current_score"))
        tier = seg.get("current_tier") or "-"
        super_id = seg.get("superseded_by") or "(无)"
        anchor = seg.get("fog_anchor") or ""

        info_lines = [
            f"ID：{node.seg_id}",
            f"话题：{topic}",
            f"静默态：{silence}",
            f"雾化：{fog}",
            f"档位：{tier}",
            f"重要性：{score:.1f}",
            f"被取代：{super_id}",
        ]
        if anchor:
            info_lines.append(f"锚点句：{anchor}")
        info_lines.append("(双击节点跳转到段落详情)")
        self._detail_label.setText("\n".join(info_lines))

    def _on_node_dbl_clicked(self, seg_id: str) -> None:
        """双击节点 → 派发给主窗口。"""
        if seg_id:
            self.segment_focus.emit(seg_id)


__all__ = [
    "GraphView",
    "GraphGraphicsView",
    "NodeItem",
    "EdgeItem",
]