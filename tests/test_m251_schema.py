"""M2.5.1 schema 迁移 + quadrant 派生函数测试。

覆盖：
1. 旧 db（无 5 字段）跑 init_db 后 5 字段应存在（向后兼容）
2. 重复跑 init_db 不报错（幂等）
3. 新 db 直接含 5 字段，默认值正确
4-7. quadrant 函数 4 个象限
8. quadrant 边界行为（0.7 含，0.6999 不含）
9. update_segment 接受新字段 urgency_level / importance_level / emotion_tag /
   expires_at_ms / urgent_state
10. 旧数据未被迁移破坏（默认值生效）
11. update_segment 仍拒绝未知字段（白名单保护）
"""

from __future__ import annotations

import sqlite3

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.l0.quadrant import get_quadrant
from src.l0.segment_writer import (
    get_segment,
    update_segment,
    write_segments,
)
from src.l0.session_writer import create_session
from src.store.sqlite import get_connection, init_db, query


# ---------------------------------------------------------------------------
# 工具：与 test_segment_writer.py 风格一致
# ---------------------------------------------------------------------------


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    dt = datetime(year, month, day, hour, minute)
    return int(dt.timestamp() * 1000)


def _seed_message(
    session_id: str,
    seq: int,
    role: str,
    content: str,
    created_at: int,
    path: Path,
) -> str:
    message_id = str(uuid.uuid4())
    token_count = max(0, len(content) // 4)
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO messages "
            "(message_id, session_id, seq, role, content, "
            "tool_calls, tool_results, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (message_id, session_id, seq, role, content, token_count, created_at),
        )
    return message_id


def _seed_messages(
    session_id: str,
    items: list[tuple[int, str, str, int]],
    path: Path,
) -> None:
    for seq, role, content, ts in items:
        _seed_message(session_id, seq, role, content, ts, path)


def _segments_columns(path: Path) -> set[str]:
    """读取 segments 表的列名集合（直接 sqlite3.connect，绕过
    get_connection 的自动迁移，保持裸读语义）。"""
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("PRAGMA table_info(segments)").fetchall()
        return {row[1] for row in rows}
    finally:
        conn.close()


def _drop_m251_columns(path: Path) -> None:
    """模拟 pre-M2.5.1 状态：移除 segments 表的 5 新增列 + 相关索引
    （直接 sqlite3.connect，绕过 get_connection 的自动迁移，保证
    DROP 后的状态对调用方可见）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DROP INDEX IF EXISTS idx_segments_urgent")
        for col in (
            "urgency_level",
            "importance_level",
            "emotion_tag",
            "expires_at_ms",
            "urgent_state",
        ):
            conn.execute(f"ALTER TABLE segments DROP COLUMN {col}")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 测试 1: 迁移向旧 db 添加 5 字段
# ---------------------------------------------------------------------------


def test_migration_adds_columns_on_old_db(tmp_path: Path) -> None:
    """模拟 pre-M2.5.1 旧库（无 5 字段）→ 再跑 init_db → 5 字段应被 ALTER 添加。"""
    db = tmp_path / "old.db"

    # 第 1 步：用 init_db 建一份完整 DB（含 5 字段 + 索引）
    init_db(db).close()
    assert "urgency_level" in _segments_columns(db)

    # 第 2 步：模拟 pre-M2.5.1 状态
    _drop_m251_columns(db)
    assert "urgency_level" not in _segments_columns(db)

    # 第 3 步：再跑 init_db → 迁移应回填 5 字段
    init_db(db).close()

    cols = _segments_columns(db)
    for col in (
        "urgency_level",
        "importance_level",
        "emotion_tag",
        "expires_at_ms",
        "urgent_state",
    ):
        assert col in cols, f"列 {col} 应被迁移添加"

    # 索引也应被重建
    with get_connection(db) as conn:
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_segments_urgent'"
        ).fetchall()
    assert idx_rows, "idx_segments_urgent 应被迁移重建"


# ---------------------------------------------------------------------------
# 测试 2: 迁移幂等（重复执行 init_db 不报错）
# ---------------------------------------------------------------------------


def test_migration_idempotent(tmp_path: Path) -> None:
    """跑两次 init_db 不应抛异常。"""
    db = tmp_path / "d.db"
    init_db(db).close()
    init_db(db).close()  # 第二次：应平滑通过
    cols = _segments_columns(db)
    assert {"urgency_level", "importance_level"}.issubset(cols)


# ---------------------------------------------------------------------------
# 测试 3: 新 db 直接含 5 字段 + 默认值
# ---------------------------------------------------------------------------


def test_new_db_has_5_columns_default_values(tmp_path: Path) -> None:
    """新 db：5 字段在迁移后存在；旧段落的 urgency/importance 默认 0.5。"""
    db = tmp_path / "new.db"
    init_db(db).close()

    sid = create_session(path=db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        db,
    )
    ids = write_segments(sid, path=db)

    # 默认值应为 0.5（中性）
    rows = query(
        "SELECT urgency_level, importance_level, emotion_tag, "
        "expires_at_ms, urgent_state "
        "FROM segments WHERE segment_id = ?",
        (ids[1],),
        path=db,
    )
    assert rows, "段落应存在"
    row = rows[0]
    assert float(row["urgency_level"]) == 0.5
    assert float(row["importance_level"]) == 0.5
    assert row["emotion_tag"] is None
    assert row["expires_at_ms"] is None
    assert row["urgent_state"] is None


# ---------------------------------------------------------------------------
# 测试 4-8: quadrant 函数 4 个象限 + 边界
# ---------------------------------------------------------------------------


def test_quadrant_q1_important_and_urgent() -> None:
    assert get_quadrant(urgency=0.8, importance=0.8) == "Q1"


def test_quadrant_q2_important_not_urgent() -> None:
    assert get_quadrant(urgency=0.3, importance=0.8) == "Q2"


def test_quadrant_q3_not_important_urgent() -> None:
    assert get_quadrant(urgency=0.8, importance=0.3) == "Q3"


def test_quadrant_q4_neither() -> None:
    assert get_quadrant(urgency=0.3, importance=0.3) == "Q4"


def test_quadrant_boundary_inclusive_at_0_7() -> None:
    """0.7 视为高位（含）；0.6999 不算。"""
    assert get_quadrant(urgency=0.7, importance=0.7) == "Q1"
    assert get_quadrant(urgency=0.7, importance=0.6999) == "Q3"
    assert get_quadrant(urgency=0.6999, importance=0.7) == "Q2"
    assert get_quadrant(urgency=0.6999, importance=0.6999) == "Q4"


def test_quadrant_default_args() -> None:
    """默认参数 (0.5, 0.5) 落在 Q4（两者都 < 0.7）。"""
    assert get_quadrant() == "Q4"


# ---------------------------------------------------------------------------
# 测试 9: update_segment 接受新字段
# ---------------------------------------------------------------------------


def test_update_segment_accepts_m251_fields(mtca_db: Path) -> None:
    """update_segment 应接受 urgency_level / importance_level / emotion_tag /
    expires_at_ms / urgent_state，且字段能正确落库。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        mtca_db,
    )
    ids = write_segments(sid, path=mtca_db)
    para_id = ids[1]

    expires_ms = base + 7 * 24 * 3600 * 1000
    n = update_segment(
        para_id,
        path=mtca_db,
        urgency_level=0.9,
        importance_level=0.8,
        emotion_tag="positive",
        expires_at_ms=expires_ms,
        urgent_state="tracking",
    )
    assert n == 1

    row = get_segment(para_id, path=mtca_db)
    assert row is not None
    assert abs(float(row["urgency_level"]) - 0.9) < 1e-6
    assert abs(float(row["importance_level"]) - 0.8) < 1e-6
    assert row["emotion_tag"] == "positive"
    assert int(row["expires_at_ms"]) == expires_ms
    assert row["urgent_state"] == "tracking"


# ---------------------------------------------------------------------------
# 测试 10: 旧数据未被迁移破坏（默认值生效）
# ---------------------------------------------------------------------------


def test_old_segment_keeps_existing_fields_after_migration(mtca_db: Path) -> None:
    """迁移后写入的段落应保留原字段；新字段默认中性。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        mtca_db,
    )
    ids = write_segments(sid, path=mtca_db)
    para_id = ids[1]

    # 仅修改一个旧字段
    n = update_segment(para_id, path=mtca_db, current_score=55.5)
    assert n == 1

    row = get_segment(para_id, path=mtca_db)
    assert row is not None
    assert abs(float(row["current_score"]) - 55.5) < 1e-6
    # 新字段保持默认
    assert float(row["urgency_level"]) == 0.5
    assert float(row["importance_level"]) == 0.5
    assert row["emotion_tag"] is None


# ---------------------------------------------------------------------------
# 测试 11: update_segment 仍拒绝未知字段（白名单保护）
# ---------------------------------------------------------------------------


def test_update_segment_still_rejects_unknown_field(mtca_db: Path) -> None:
    """M2.5.1 新字段加入后，未知字段仍应被静默丢弃。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        mtca_db,
    )
    ids = write_segments(sid, path=mtca_db)
    para_id = ids[1]

    # 混入一个未知字段 + 一个合法 M2.5.1 字段
    n = update_segment(
        para_id,
        path=mtca_db,
        urgency_level=0.42,
        not_a_real_column=1.0,
    )
    assert n == 1  # 合法字段应生效

    row = get_segment(para_id, path=mtca_db)
    assert row is not None
    assert abs(float(row["urgency_level"]) - 0.42) < 1e-6


# ---------------------------------------------------------------------------
# 测试 12: get_connection() 触发 M2.5.1 schema 迁移（旧库向后兼容）
# ---------------------------------------------------------------------------


def test_get_connection_triggers_migration(tmp_path: Path) -> None:
    """get_connection() 必须触发 _migrate_to_v2()（向后兼容旧库）。

    背景：M2.5.1 上线前用户的 DB 不含 5 字段；GUI / CLI 走 get_connection()
    而非 init_db()，因此旧用户首次启动 GUI 会因缺列导致 4 象限 Tab 崩溃。
    修复：get_connection() 在 _apply_pragmas 之后调 _migrate_to_v2()。
    """
    db = tmp_path / "legacy.db"

    # 1. 建一份完整 DB（含 5 字段 + 索引）
    init_db(db).close()
    assert "urgency_level" in _segments_columns(db)

    # 2. 模拟 pre-M2.5.1 状态：移除 5 字段 + 相关索引
    _drop_m251_columns(db)
    assert "urgency_level" not in _segments_columns(db)

    # 3. 直接走 get_connection（**不调 init_db**），关闭后检查
    with get_connection(db) as conn:
        pass  # 上下文块内 _migrate_to_v2 应已被触发

    # 4. 关连接后验证 5 字段都回来了
    cols = _segments_columns(db)
    for col in (
        "urgency_level",
        "importance_level",
        "emotion_tag",
        "expires_at_ms",
        "urgent_state",
    ):
        assert col in cols, f"列 {col} 应被 get_connection 触发回填"
