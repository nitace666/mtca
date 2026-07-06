"""MTCA 主窗口（gui/main_window.py — T13 / T25）。

USER_CONTROLS.md §3.1 主界面：顶部工具栏 + Tab 区域（时间线 / 详情 /
知识图谱 / 操作）。

设计要点：
- 工具栏 4 个按钮：搜索 / 新建 / 刷新 / 设置（行为与原 T13 版一致）。
- 中央 ``QTabWidget`` 4 个 Tab：
  1. 时间线 ``TimelineView`` —— 左侧列表（T13）
  2. 详情 ``SegmentDetail`` —— 段落元信息 + L0/L1/L2/L3（T13）
  3. 知识图谱 ``GraphView`` —— 关系图谱视图（T25）
  4. 操作 —— 4 个段落操作按钮（T13）
- 操作按钮 4 个：雾化 / 归档 / 重要 / 循环，全部走
  ``src.cli.user_controls`` 提供的 4 个 cmd_* 函数（USER_CONTROLS §2 +
  §3.3），GUI 与 CLI 行为完全一致。
- 知识图谱双击节点 → 切到「详情」Tab + 加载段落 + 同步右侧操作面板。
- 不修改数据库结构；不开新文件；只组装 UI + 调用现有后端。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from src.cli.user_controls import cmd_archive, cmd_cycle, cmd_fog, cmd_important
from src.errors import classify_exception
from src.l0.segment_writer import get_segment, write_segments
from src.l0.session_writer import create_session
from src.recall.recall_engine import search_segments
from src.store.sqlite import MTCA_DB_PATH

from gui.fog_dialog import FogDialog
from gui.graph_view import GraphView
from gui.quadrant_view import QuadrantView
from gui.segment_detail import SegmentDetail
from gui.timeline_view import TimelineView

# 按钮最小高度（USER_RULES §8：GUI 按钮 ≥ 80×30，DPI 适配）
_BTN_MIN_WIDTH: int = 80
_BTN_MIN_HEIGHT: int = 32

# 搜索回写时显示的段落上限
_SEARCH_LIMIT: int = 100

# 循环标签候选（USER_CONTROLS §1：周一~周日 / 月初 / 月末 / 每天）
_CYCLE_TAGS: tuple[str, ...] = (
    "周一", "周二", "周三", "周四", "周五", "周六", "周日",
    "月初", "月末", "每天",
)

# Tab 索引常量（便于双击节点切到详情 Tab）
_TAB_TIMELINE: int = 0
_TAB_DETAIL: int = 1
_TAB_GRAPH: int = 2
_TAB_ACTIONS: int = 3
_TAB_QUADRANT: int = 4


def _show_error(parent: QWidget, title: str, exc: BaseException) -> None:
    """GUI 统一错误显示（M2.5.8 A-3）：分类异常 + QMessageBox 友好提示。

    标题：``❌ {title}``
    正文：``{message}\n\n💡 建议：{suggestion}``（suggestion 为空时只显示 message）

    通过 :func:`src.errors.classify_exception` 把内部异常翻译为
    :class:`UserFacingError`，由本函数统一渲染。
    用户看不到 Python traceback，只看到中文 + 可操作建议。
    """
    wrapped = classify_exception(exc)
    body = wrapped.message
    if wrapped.suggestion:
        body = f"{wrapped.message}\n\n💡 建议：{wrapped.suggestion}"
    QMessageBox.warning(parent, f"❌ {title}", body)


class MainWindow(QMainWindow):
    """MTCA GUI 主窗口。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化主窗口与 Tab 布局。"""
        super().__init__(parent)
        self.setWindowTitle("MTCA — 长期记忆中间件")
        self.resize(1280, 800)

        self._db_path: Optional[Union[Path, str]] = None
        self._current_seg_id: str = ""

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()

    # ------------------------------------------------------------------
    # 布局构造
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        """顶部工具栏：搜索 / 新建 / 刷新 / 设置。"""
        toolbar = QToolBar("主工具栏", self)
        toolbar.setMovable(False)
        toolbar.setIconSize(toolbar.iconSize())
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        act_search = QAction("搜索", self)
        act_search.triggered.connect(self._on_search)
        toolbar.addAction(act_search)

        act_new = QAction("新建", self)
        act_new.triggered.connect(self._on_new_session)
        toolbar.addAction(act_new)

        act_refresh = QAction("刷新", self)
        act_refresh.triggered.connect(self._on_refresh)
        toolbar.addAction(act_refresh)

        act_settings = QAction("设置", self)
        act_settings.triggered.connect(self._on_settings)
        toolbar.addAction(act_settings)

    def _build_central(self) -> None:
        """中央 4 Tab：时间线 / 详情 / 知识图谱 / 操作。"""
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)

        # ---- Tab 0：时间线 ----
        self._timeline = TimelineView()
        self._timeline.segment_selected.connect(self._on_segment_selected)
        self._tabs.addTab(self._timeline, "时间线")

        # ---- Tab 1：详情 ----
        self._detail = SegmentDetail()
        self._tabs.addTab(self._detail, "详情")

        # ---- Tab 2：知识图谱 ----
        self._graph = GraphView()
        self._graph.segment_focus.connect(self._on_segment_focus)
        self._tabs.addTab(self._graph, "知识图谱")

        # ---- Tab 3：操作 ----
        self._action_panel = self._build_action_panel()
        self._tabs.addTab(self._action_panel, "操作")

        # ---- Tab 4：4 象限（T34）----
        self._quadrant = QuadrantView()
        self._quadrant.segment_activated.connect(self._on_quadrant_activated)
        self._tabs.addTab(self._quadrant, "4 象限")

        self.setCentralWidget(self._tabs)

    def _build_action_panel(self) -> QWidget:
        """构造「操作」Tab 内的按钮面板。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(QLabel("段落操作（先选择段落）"))

        self._btn_important = self._make_action_btn("/重要", self._on_important)
        self._btn_cycle = self._make_action_btn("/循环", self._on_cycle)
        self._btn_archive = self._make_action_btn("/归档", self._on_archive)
        self._btn_fog = self._make_action_btn("/雾化", self._on_fog)

        for btn in (
            self._btn_important, self._btn_cycle,
            self._btn_archive, self._btn_fog,
        ):
            layout.addWidget(btn)
        layout.addSpacing(8)

        self._action_status = QLabel("未选择段落")
        self._action_status.setWordWrap(True)
        layout.addWidget(self._action_status)
        layout.addStretch(1)

        # 初始禁用：未选段
        self._set_action_buttons_enabled(False)
        return panel

    def _build_statusbar(self) -> None:
        """底部状态栏：DB 路径 + 当前操作结果。"""
        bar = QStatusBar(self)
        self.setStatusBar(bar)
        self._db_label = QLabel(f"DB：{self._describe_db()}")
        bar.addPermanentWidget(self._db_label)

    def _make_action_btn(self, label: str, slot) -> QPushButton:
        """构造符合 DPI 适配要求的操作按钮。"""
        btn = QPushButton(label)
        btn.setMinimumSize(_BTN_MIN_WIDTH, _BTN_MIN_HEIGHT)
        btn.clicked.connect(slot)
        return btn

    # ------------------------------------------------------------------
    # Tab 切换 / DB 同步
    # ------------------------------------------------------------------

    def _apply_db_path(self, db_path: Optional[Union[Path, str]]) -> None:
        """把所有子控件的 DB 路径同步更新。"""
        self._db_path = db_path
        self._timeline.set_db_path(db_path)
        self._detail.set_db_path(db_path)
        self._graph.set_db_path(db_path)
        self._quadrant.set_db_path(db_path)

    # ------------------------------------------------------------------
    # 工具栏回调
    # ------------------------------------------------------------------

    def _on_search(self) -> None:
        """搜索：弹输入框 → 走 search_segments → 重建左侧列表。"""
        query_text, ok = QInputDialog.getText(
            self, "搜索段落", "关键词（topic_label 模糊匹配）："
        )
        if not ok:
            return
        query_text = (query_text or "").strip()
        if not query_text:
            self.statusBar().showMessage("搜索关键词为空，已刷新", 3000)
            self._timeline.refresh()
            return
        try:
            results = search_segments(
                query=query_text, limit=_SEARCH_LIMIT, path=self._db_path,
            )
        except (ValueError, RuntimeError) as e:
            QMessageBox.warning(self, "搜索失败", str(e))
            return
        # 直接把 search_segments 结果灌进 TimelineView 的内置模型
        self._timeline.refresh()
        self.statusBar().showMessage(
            f"搜索「{query_text}」命中 {len(results)} 段", 5000
        )

    def _on_new_session(self) -> None:
        """新建会话：topic_label → create_session → 尝试 write_segments。"""
        topic, ok = QInputDialog.getText(
            self, "新建会话", "话题标签 topic_label（可空）："
        )
        if not ok:
            return
        topic_clean = (topic or "").strip() or None
        try:
            session_id = create_session(
                agent_source="user",
                topic_label=topic_clean,
                path=self._db_path,
            )
            # 无消息时 write_segments 也会写一条骨架；这里不强制写段落
            try:
                write_segments(session_id, path=self._db_path)
            except (ValueError, RuntimeError):
                # 没有消息时不会生成 L0-B 段落，骨架依旧入库
                pass
        except (ValueError, RuntimeError) as e:
            QMessageBox.warning(self, "新建失败", str(e))
            return
        self.statusBar().showMessage(f"已新建会话：{session_id}", 5000)
        self._timeline.refresh()
        self._quadrant.refresh()

    def _on_refresh(self) -> None:
        """刷新：重拉时间线 + 当前段落详情 + 知识图谱。"""
        n = self._timeline.refresh()
        if self._current_seg_id:
            self._detail.show_segment(self._current_seg_id)
        self._graph.refresh()
        self._quadrant.refresh()
        self.statusBar().showMessage(f"已刷新，共 {n} 段", 3000)

    def _on_settings(self) -> None:
        """设置：选择 DB 文件路径。"""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "选择 MTCA 数据库文件",
            str(MTCA_DB_PATH.parent),
            "SQLite DB (*.db);;所有文件 (*.*)",
        )
        if not path_str:
            return
        self._apply_db_path(path_str)
        self._db_label.setText(f"DB：{self._describe_db()}")
        self.statusBar().showMessage(f"DB 已切换：{path_str}", 5000)
        self._timeline.refresh()
        self._graph.refresh()
        self._quadrant.set_db_path(path_str)
        self._quadrant.refresh()

    # ------------------------------------------------------------------
    # 段落选中回调（来自 TimelineView）
    # ------------------------------------------------------------------

    def _on_segment_selected(self, seg_id: str) -> None:
        """左侧选中段落后，加载详情 + 更新操作面板状态。"""
        self._current_seg_id = seg_id
        self._detail.show_segment(seg_id)
        self._set_action_status_from_seg(seg_id)
        self._set_action_buttons_enabled(True)

    def _on_segment_focus(self, seg_id: str) -> None:
        """知识图谱双击节点 → 切到详情 Tab + 加载段落。"""
        self._current_seg_id = seg_id
        self._detail.show_segment(seg_id)
        self._set_action_status_from_seg(seg_id)
        self._set_action_buttons_enabled(True)
        # 切到详情 Tab
        self._tabs.setCurrentIndex(_TAB_DETAIL)
        self.statusBar().showMessage(
            f"已跳转到段落详情：{seg_id}", 5000
        )

    def _on_quadrant_activated(self, seg_id: str) -> None:
        """4 象限双击 -> 切到详情 Tab + 加载段落（与图谱一致）。"""
        self._on_segment_focus(seg_id)

    def _set_action_status_from_seg(self, seg_id: str) -> None:
        """按 seg_id 更新「操作」Tab 底部的状态文本。"""
        if not seg_id:
            self._action_status.setText("未选择段落")
            return
        try:
            seg = get_segment(seg_id, path=self._db_path)
        except (ValueError, RuntimeError):
            seg = None
        topic = (seg or {}).get("topic_label") or "(无标题)"
        self._action_status.setText(f"当前段落：{topic}\nID：{seg_id}")

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        """启用 / 禁用右侧 4 个操作按钮。"""
        for btn in (
            self._btn_important, self._btn_cycle,
            self._btn_archive, self._btn_fog,
        ):
            btn.setEnabled(enabled)

    # ------------------------------------------------------------------
    # 4 个操作按钮回调
    # ------------------------------------------------------------------

    def _ensure_seg_id(self) -> str:
        """校验当前选中段落；非空返回 seg_id，否则空串。"""
        return self._current_seg_id or ""

    def _on_important(self) -> None:
        """/重要：score → 10000。"""
        seg_id = self._ensure_seg_id()
        if not seg_id:
            return
        try:
            rows = cmd_important(seg_id, path=self._db_path)
        except (ValueError, RuntimeError) as e:
            _show_error(self, "/重要 失败", e)
            return
        except PermissionError as e:
            _show_error(self, "/重要 失败", e)
            return
        except sqlite3.DatabaseError as e:
            _show_error(self, "/重要 失败", e)
            return
        except Exception as e:
            _show_error(self, "/重要 失败", e)
            return
        self.statusBar().showMessage(f"已 /重要 {seg_id}（rows={rows}）", 5000)
        self._timeline.refresh()
        self._detail.show_segment(seg_id)
        self._graph.refresh()

    def _on_cycle(self) -> None:
        """/循环 X：选 cycle_tag。"""
        seg_id = self._ensure_seg_id()
        if not seg_id:
            return
        tag, ok = QInputDialog.getItem(
            self, "/循环", "选择循环标签：", list(_CYCLE_TAGS), 0, False
        )
        if not ok or not tag:
            return
        try:
            rows = cmd_cycle(seg_id, tag, path=self._db_path)
        except (ValueError, RuntimeError) as e:
            _show_error(self, "/循环 失败", e)
            return
        except PermissionError as e:
            _show_error(self, "/循环 失败", e)
            return
        except sqlite3.DatabaseError as e:
            _show_error(self, "/循环 失败", e)
            return
        except Exception as e:
            _show_error(self, "/循环 失败", e)
            return
        self.statusBar().showMessage(f"已 /循环 {tag} {seg_id}（rows={rows}）", 5000)
        self._timeline.refresh()
        self._detail.show_segment(seg_id)
        self._graph.refresh()

    def _on_archive(self) -> None:
        """/归档：tier → L3_hidden。"""
        seg_id = self._ensure_seg_id()
        if not seg_id:
            return
        try:
            rows = cmd_archive(seg_id, path=self._db_path)
        except (ValueError, RuntimeError) as e:
            _show_error(self, "/归档 失败", e)
            return
        except PermissionError as e:
            _show_error(self, "/归档 失败", e)
            return
        except sqlite3.DatabaseError as e:
            _show_error(self, "/归档 失败", e)
            return
        except Exception as e:
            _show_error(self, "/归档 失败", e)
            return
        self.statusBar().showMessage(f"已 /归档 {seg_id}（rows={rows}）", 5000)
        self._timeline.refresh()
        self._detail.show_segment(seg_id)
        self._graph.refresh()

    def _on_fog(self) -> None:
        """/雾化：弹 FogDialog 强警告对话框。"""
        seg_id = self._ensure_seg_id()
        if not seg_id:
            return
        try:
            seg = get_segment(seg_id, path=self._db_path)
        except (ValueError, RuntimeError):
            seg = None
        topic = (seg or {}).get("topic_label") or "(无标题)"
        dlg = FogDialog(parent=self, topic=topic, seg_id=seg_id)
        if dlg.exec() != FogDialog.DialogCode.Accepted:
            self.statusBar().showMessage("已取消雾化", 3000)
            return
        anchor = dlg.anchor()
        try:
            ok = cmd_fog(seg_id, anchor, path=self._db_path)
        except (ValueError, PermissionError, RuntimeError) as e:
            _show_error(self, "/雾化 失败", e)
            return
        except sqlite3.DatabaseError as e:
            _show_error(self, "/雾化 失败", e)
            return
        except Exception as e:
            _show_error(self, "/雾化 失败", e)
            return
        self.statusBar().showMessage(
            f"已 /雾化 {seg_id} -> {ok}", 5000
        )
        self._timeline.refresh()
        self._detail.show_segment(seg_id)
        self._graph.refresh()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _describe_db(self) -> str:
        """状态栏显示的 DB 路径（None 时回退默认）。"""
        if self._db_path:
            return str(self._db_path)
        return str(MTCA_DB_PATH)


__all__ = ["MainWindow"]