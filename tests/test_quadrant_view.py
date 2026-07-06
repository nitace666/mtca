"""tests/test_quadrant_view.py — M2.5.5 GUI 4 象限视图测试

覆盖：
- QuadrantView 创建不崩（空库 / 有数据 / 异常 DB）
- _refresh 把 segments 按 urgency/importance 分到 4 象限
- 计数标签 "(N 段)" 正确
- URGENT 条可见性：list_expired 空 → 隐藏；非空 → 显示
- URGENT tracking 段在列表里加粗
- 4 象限 4 种不同颜色
- 双击列表项把 segment_id 放在 UserRole，信号可触发
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.lifecycle.urgent_tracker import tick_urgent, track_urgent
from src.l0.quadrant import get_quadrant
from src.l0.session_writer import create_session, write_message
from src.l0.skeleton import build_skeleton, save_skeleton
from src.l0.segment_writer import update_segment
from src.store.sqlite import init_db

from gui.quadrant_view import QUADRANT_COLORS, QuadrantView


# ---------------------------------------------------------------------------
# Qt fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    """Qt 应用 fixture；不创建可见窗口（依赖 QT_QPA_PLATFORM=offscreen）。"""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# ---------------------------------------------------------------------------
# 数据 fixture
# ---------------------------------------------------------------------------


_QUAD_CONFIGS = [
    ("topic_q1_a", 0.80, 0.80),   # Q1 重要+紧急
    ("topic_q1_b", 0.75, 0.90),   # Q1
    ("topic_q2_a", 0.30, 0.80),   # Q2 重要+不紧急
    ("topic_q2_b", 0.20, 0.90),   # Q2
    ("topic_q3_a", 0.85, 0.30),   # Q3 不重要+紧急
    ("topic_q3_b", 0.70, 0.40),   # Q3
    ("topic_q4_a", 0.30, 0.30),   # Q4 都不沾
    ("topic_q4_b", 0.40, 0.20),   # Q4
]


@pytest.fixture
def db_with_4_quadrants(tmp_path: Path):
    """灌 8 段：每象限 2 段。返回 (db_path, [seg_id, ...])。"""
    db = tmp_path / "t_quad.db"
    init_db(db)
    seg_ids: list[str] = []
    for topic, urg, imp in _QUAD_CONFIGS:
        sid = create_session(path=db)
        write_message(sid, "user", "msg", path=db)
        skel = build_skeleton(sid, path=db)
        skel["topic_label"] = topic
        seg_id = save_skeleton(sid, skel, path=db)
        # save_skeleton 只接受 L0 字段，urgency/importance 走 update_segment
        update_segment(
            seg_id,
            urgency_level=urg,
            importance_level=imp,
            current_tier="L1",
            current_score=1.0,
            path=db,
        )
        seg_ids.append(seg_id)
    return db, seg_ids


# ---------------------------------------------------------------------------
# 1. 基础创建
# ---------------------------------------------------------------------------


def test_quadrant_view_creates(qapp, tmp_path: Path) -> None:
    """空 DB 上 QuadrantView 能创建，4 个象限 list 都存在。"""
    db = tmp_path / "empty_a.db"
    init_db(db)
    view = QuadrantView(db_path=db)
    assert view is not None
    for q in ("Q1", "Q2", "Q3", "Q4"):
        assert q in view.quadrant_lists
        assert q in view.quadrant_counts


# ---------------------------------------------------------------------------
# 2. _refresh 加载 segments 并分桶
# ---------------------------------------------------------------------------


def test_quadrant_view_refresh_loads_segments(
    qapp, db_with_4_quadrants,
) -> None:
    """8 段（每象限 2 段）→ 计数标签 "(2 段)" + list 各 2 项。"""
    db, _ids = db_with_4_quadrants
    view = QuadrantView(db_path=db)
    view._refresh()
    for q in ("Q1", "Q2", "Q3", "Q4"):
        assert view.quadrant_counts[q].text() == "(2 段)", q
        assert view.quadrant_lists[q].count() == 2, q


def test_quadrant_view_grouping_correct(
    qapp, db_with_4_quadrants,
) -> None:
    """确认 (urg, imp) 映射到正确象限。"""
    db, ids = db_with_4_quadrants
    view = QuadrantView(db_path=db)
    view._refresh()
    # 取出每个 list 的 segment_id，按段验证象限归属
    for i, (topic, urg, imp) in enumerate(_QUAD_CONFIGS):
        expected_q = get_quadrant(urg, imp)
        found_q = None
        for q in ("Q1", "Q2", "Q3", "Q4"):
            for row in range(view.quadrant_lists[q].count()):
                item = view.quadrant_lists[q].item(row)
                if item.data(Qt.ItemDataRole.UserRole) == ids[i]:
                    found_q = q
                    break
            if found_q:
                break
        assert found_q == expected_q, (
            f"segment[{i}] topic={topic} urg={urg} imp={imp} "
            f"expected {expected_q} got {found_q}"
        )


# ---------------------------------------------------------------------------
# 3. URGENT 条可见性
# ---------------------------------------------------------------------------


def test_urgent_bar_hidden_when_no_expired(
    qapp, db_with_4_quadrants,
) -> None:
    """没有 expired 段时 URGENT 条隐藏。"""
    db, _ids = db_with_4_quadrants
    view = QuadrantView(db_path=db)
    view._refresh()
    assert view.urgent_bar.isHidden() is True
    assert view.urgent_ids == []


def test_urgent_bar_shows_when_tracked_expired(
    qapp, db_with_4_quadrants,
) -> None:
    """track_urgent 设置一个已过期的 expires → URGENT 条显示且包含段数。"""
    db, ids = db_with_4_quadrants
    now_ms = int(time.time() * 1000)
    track_urgent(
        ids[0], expires_at_ms=now_ms - 1_000, path=db,
    )
    tick_urgent(path=db, now_ms=now_ms)
    view = QuadrantView(db_path=db)
    view._refresh()
    assert view.urgent_bar.isHidden() is False
    assert "1" in view.urgent_bar.text()
    assert view.urgent_ids == [ids[0]]


# ---------------------------------------------------------------------------
# 4. URGENT tracking 段加粗
# ---------------------------------------------------------------------------


def test_urgent_segment_bold_in_list(
    qapp, db_with_4_quadrants,
) -> None:
    """tracking 中的段在所属象限列表里加粗（视觉强调）。"""
    db, ids = db_with_4_quadrants
    now_ms = int(time.time() * 1000)
    # 不设 expires_at_ms 到期，保留 tracking 状态
    track_urgent(
        ids[0], expires_at_ms=now_ms + 86_400_000, path=db,
    )
    view = QuadrantView(db_path=db)
    view._refresh()
    # ids[0] 是 (0.8, 0.8) → Q1
    first_item = view.quadrant_lists["Q1"].item(0)
    assert first_item is not None
    assert first_item.font().bold() is True


# ---------------------------------------------------------------------------
# 5. 空 DB / 异常路径
# ---------------------------------------------------------------------------


def test_quadrant_view_handles_empty_db(qapp, tmp_path: Path) -> None:
    """空 DB：4 象限计数全 0，URGENT 条隐藏，refresh 不崩。"""
    db = tmp_path / "empty.db"
    init_db(db)
    view = QuadrantView(db_path=db)
    view._refresh()
    for q in ("Q1", "Q2", "Q3", "Q4"):
        assert view.quadrant_counts[q].text() == "(0 段)"
        assert view.quadrant_lists[q].count() == 0
    assert view.urgent_bar.isHidden() is True


def test_quadrant_view_handles_nonexistent_db(qapp, tmp_path: Path) -> None:
    """不存在的 DB 路径：refresh 不崩，URGENT 条隐藏。"""
    fake_db = tmp_path / "no_such.db"
    view = QuadrantView(db_path=fake_db)
    # _refresh 内部 query 抛 RuntimeError 时回退到 []
    view._refresh()
    assert view.urgent_bar.isHidden() is True


# ---------------------------------------------------------------------------
# 6. 视觉区分：4 种不同颜色
# ---------------------------------------------------------------------------


def test_quadrant_colors_have_4_distinct() -> None:
    """4 个象限有 4 种不同颜色（视觉区分）。"""
    assert len(set(QUADRANT_COLORS.values())) == 4
    for q in ("Q1", "Q2", "Q3", "Q4"):
        assert q in QUADRANT_COLORS


# ---------------------------------------------------------------------------
# 7. 双击信号
# ---------------------------------------------------------------------------


def test_double_click_emits_signal(qapp, db_with_4_quadrants) -> None:
    """双击列表项可读出 segment_id；segment_activated 信号能挂接。"""
    db, ids = db_with_4_quadrants

    captured: list[str] = []

    view = QuadrantView(db_path=db)
    view.segment_activated.connect(lambda sid: captured.append(sid))
    view._refresh()

    # 拿到 Q1 第 1 个 item 的 segment_id
    item = view.quadrant_lists["Q1"].item(0)
    assert item is not None
    seg_id = item.data(Qt.ItemDataRole.UserRole)
    assert seg_id is not None
    assert seg_id == ids[0]

    # 触发 _on_item_activated 验证信号
    view._on_item_activated(item)
    assert captured == [ids[0]]


def test_double_click_skips_items_without_seg_id(qapp) -> None:
    """占位项（无 UserRole 数据）双击不抛信号。"""
    from PySide6.QtWidgets import QListWidgetItem
    db_path = None  # 不需要 DB：直接构造控件 + 占位 item
    view = QuadrantView()
    captured: list[str] = []
    view.segment_activated.connect(lambda sid: captured.append(sid))
    placeholder = QListWidgetItem("... 还有 3 段")
    placeholder.setData(Qt.ItemDataRole.UserRole, "")
    view._on_item_activated(placeholder)
    assert captured == []
