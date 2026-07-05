"""src/lifecycle/retention_engine.py 测试（T20）

测试覆盖（用户 prompt 要求的 8 条 + 边界）：

1.  test_initial_all_active            新段落默认 silence_state=active
2.  test_demote_after_retention_days   active 超期 tick → dormant
3.  test_dormant_to_silent_after_double dormant 超 2 倍期 tick → silent
4.  test_promote_resets_timer          promote 后 promoted_at 更新
5.  test_cycle_tag_skips_decay         /循环 段落跳过自动下沉
6.  test_lock_permanent_no_decay       锁定永久后永不衰减
7.  test_force_silent_skip_dormant     force_silent 跳 dormant 直 silent
8.  test_default_retention_values      4 类策略返回值正确
9.  test_demote_manual                 manual demote 单步下沉
10. test_promote_invalid_target        promote 非法 target 抛异常
11. test_get_retention_for_unknown     未知分类走兜底
12. test_tick_handles_no_candidates    无候选段时返回 0
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.lifecycle.retention_engine import (
    DEFAULT_BACKGROUND_INTERVAL,
    DEFAULT_RETENTION_POLICY,
    DORMANT_MULTIPLIER,
    FALLBACK_RETENTION_DAYS,
    PERMANENT,
    demote,
    force_silent,
    get_retention_for,
    lock_permanent,
    promote,
    start_background,
    stop_background,
    tick,
)
from src.l0.segment_writer import get_segment, update_segment, write_segments
from src.l0.session_writer import create_session, write_message
from src.store.sqlite import execute, query


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    """生成指定本地时间的毫秒时间戳。"""
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
    """直接 INSERT 一条带指定 created_at 的消息。"""
    message_id = str(uuid.uuid4())
    token_count = max(0, len(content) // 4)
    execute(
        "INSERT INTO messages "
        "(message_id, session_id, seq, role, content, "
        "tool_calls, tool_results, token_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
        (message_id, session_id, seq, role, content, token_count, created_at),
        path=path,
    )
    return message_id


def _set_session_cycle_tag(session_id: str, tag, path: Path) -> int:
    """直接 UPDATE sessions.cycle_tag（模拟 /循环）。"""
    return execute(
        "UPDATE sessions SET cycle_tag = ? WHERE session_id = ?",
        (tag, session_id),
        path=path,
    )


def _set_segment_field(segment_id: str, field: str, value, path: Path) -> int:
    """直接 UPDATE segments 表的指定字段（仅白名单字段）。"""
    seg = get_segment(segment_id, path=path)
    if not seg:
        raise ValueError(f"段落不存在：{segment_id}")
    return update_segment(segment_id, path=path, **{field: value})


def _setup_segment(
    path: Path,
    user_retention_days: int = 30,
    silence_state: str = "active",
    promoted_at: int = 0,
    messages: tuple[str, ...] = ("吃饭", "睡觉", "打豆豆"),
) -> tuple[str, str]:
    """构造一个会话 + 一个段落；返回 (session_id, segment_id)。

    段落 ``promoted_at`` 默认 0（远在过去），配合 user_retention_days
    即可构造「超期」用例；messages 的首条会被截断成 anchor（即 segment
    的 topic_label），供 ``_detect_category`` 启发式分类用。
    """
    sid = create_session(path=path)
    base = _ms(2026, 1, 1, 10, 0)
    for i, content in enumerate(messages, start=1):
        _seed_message(sid, i, "user", content, base + i * 60_000, path)
    ids = write_segments(sid, path=path)
    seg_id = ids[0]

    # 直接覆盖 user_retention_days / silence_state / promoted_at
    if user_retention_days != 0:
        update_segment(seg_id, path=path, user_retention_days=user_retention_days)
    if silence_state != "active":
        update_segment(seg_id, path=path, silence_state=silence_state)
    if promoted_at:
        update_segment(seg_id, path=path, promoted_at=promoted_at)
    else:
        # 默认 promoted_at = 大约 1 天前，确保 tick 不立即下沉（30 天未超）
        update_segment(seg_id, path=path, promoted_at=_now_ms() - 24 * 60 * 60 * 1000)

    return sid, seg_id


def _now_ms() -> int:
    """当前毫秒时间戳。"""
    import time
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 测试 1：新段落全部 active
# ---------------------------------------------------------------------------


def test_initial_all_active(mtca_db: Path) -> None:
    """write_segments 后所有段落的 silence_state 默认 = active。"""
    _sid, seg_id = _setup_segment(mtca_db)

    rows = query(
        "SELECT silence_state, promoted_at, user_retention_days "
        "FROM segments WHERE segment_id = ?",
        (seg_id,),
        path=mtca_db,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["silence_state"] == "active"
    assert row["promoted_at"] is not None
    assert int(row["promoted_at"]) > 0


# ---------------------------------------------------------------------------
# 测试 2：active 段超 retention_days 降 dormant
# ---------------------------------------------------------------------------


def test_demote_after_retention_days(mtca_db: Path) -> None:
    """active 段 promoted_at + retention*24h < now → tick 后降 dormant。"""
    # user_retention_days = 30；promoted_at = 100 天前
    ancient = _now_ms() - 100 * 24 * 60 * 60 * 1000
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="active",
        promoted_at=ancient,
    )

    n = tick(path=mtca_db)
    assert n == 1

    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "dormant"


# ---------------------------------------------------------------------------
# 测试 3：dormant 段超 2 倍 retention_days 降 silent
# ---------------------------------------------------------------------------


def test_dormant_to_silent_after_double(mtca_db: Path) -> None:
    """dormant 段 promoted_at + retention*2*24h < now → tick 后降 silent。"""
    # user_retention_days = 30；promoted_at = 70 天前
    #   active → dormant 阈值 = 30 天（已过，本次不触发）
    #   dormant → silent 阈值 = 60 天（已过，应该触发）
    ancient = _now_ms() - 70 * 24 * 60 * 60 * 1000
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="dormant",
        promoted_at=ancient,
    )

    n = tick(path=mtca_db)
    assert n == 1

    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "silent"


# ---------------------------------------------------------------------------
# 测试 4：promote 重置 promoted_at
# ---------------------------------------------------------------------------


def test_promote_resets_timer(mtca_db: Path) -> None:
    """promote 后 promoted_at 应被设为新值（不再触发超时）。"""
    # 远古 active 段，user_retention_days=30
    ancient = _now_ms() - 100 * 24 * 60 * 60 * 1000
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="active",
        promoted_at=ancient,
    )

    # 先 tick：会降 dormant
    n = tick(path=mtca_db)
    assert n == 1
    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "dormant"

    # promote 回 active
    promote(seg_id, target="active", path=mtca_db)
    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "active"
    new_promoted_at = int(row["promoted_at"])
    # promoted_at 应被刷新到「现在」附近
    assert abs(new_promoted_at - _now_ms()) < 5_000  # 5 秒误差

    # 此时再 tick 不应触发下沉（promoted_at 是新的）
    n2 = tick(path=mtca_db)
    assert n2 == 0
    row2 = get_segment(seg_id, path=mtca_db)
    assert row2["silence_state"] == "active"


# ---------------------------------------------------------------------------
# 测试 5：/循环 段落跳过自动下沉
# ---------------------------------------------------------------------------


def test_cycle_tag_skips_decay(mtca_db: Path) -> None:
    """sessions.cycle_tag 非空时，tick 不下沉该会话下任何段。"""
    ancient = _now_ms() - 100 * 24 * 60 * 60 * 1000
    sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="active",
        promoted_at=ancient,
    )
    _set_session_cycle_tag(sid, "周一复盘", path=mtca_db)

    n = tick(path=mtca_db)
    assert n == 0

    row = get_segment(seg_id, path=mtca_db)
    # cycle_tag 段应保持 active
    assert row["silence_state"] == "active"


# ---------------------------------------------------------------------------
# 测试 6：lock_permanent 后永不衰减
# ---------------------------------------------------------------------------


def test_lock_permanent_no_decay(mtca_db: Path) -> None:
    """lock_permanent 后即使远古 promoted_at 也不会被 tick 下沉。"""
    ancient = _now_ms() - 1000 * 24 * 60 * 60 * 1000  # 1000 天前
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="active",
        promoted_at=ancient,
    )

    lock_permanent(seg_id, path=mtca_db)
    row = get_segment(seg_id, path=mtca_db)
    # user_retention_days 应被置为 PERMANENT (-1)
    assert int(row["user_retention_days"]) == PERMANENT

    # tick 不应下沉
    n = tick(path=mtca_db)
    assert n == 0
    row2 = get_segment(seg_id, path=mtca_db)
    assert row2["silence_state"] == "active"


# ---------------------------------------------------------------------------
# 测试 7：force_silent 跳 dormant 直 silent
# ---------------------------------------------------------------------------


def test_force_silent_skip_dormant(mtca_db: Path) -> None:
    """force_silent 跳过 dormant，直接置为 silent。"""
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="active",
        promoted_at=_now_ms() - 24 * 60 * 60 * 1000,  # 1 天前，不会自动下沉
    )
    rows = force_silent(seg_id, path=mtca_db)
    assert rows == 1

    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "silent"

    # 应有 retention_force 审计
    events = query(
        "SELECT event_type, reason FROM score_events "
        "WHERE segment_id = ? AND event_type = 'retention_force'",
        (seg_id,),
        path=mtca_db,
    )
    assert len(events) >= 1
    assert "skip dormant" in (events[0]["reason"] or "")


# ---------------------------------------------------------------------------
# 测试 8：默认策略 4 类 retention 值正确
# ---------------------------------------------------------------------------


def test_default_retention_values() -> None:
    """4 类策略返回值与 V0.4_PIVOT.md §3.6 表对齐。"""
    assert get_retention_for("编程/技术") == 365
    assert get_retention_for("工作项目") == 180
    assert get_retention_for("生活/娱乐") == 30
    assert get_retention_for("健康/财务") == PERMANENT
    # 兜底
    assert get_retention_for("未知类目") == FALLBACK_RETENTION_DAYS
    assert get_retention_for("") == FALLBACK_RETENTION_DAYS
    # 策略表本身
    assert DEFAULT_RETENTION_POLICY["编程/技术"] == 365
    assert DEFAULT_RETENTION_POLICY["工作项目"] == 180
    assert DEFAULT_RETENTION_POLICY["生活/娱乐"] == 30
    assert DEFAULT_RETENTION_POLICY["健康/财务"] == PERMANENT
    assert DORMANT_MULTIPLIER == 2


# ---------------------------------------------------------------------------
# 测试 9：manual demote 单步下沉
# ---------------------------------------------------------------------------


def test_demote_manual(mtca_db: Path) -> None:
    """manual demote 按单步下沉（active→dormant），写入 score_events。"""
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="active",
        promoted_at=_now_ms(),
    )

    rows = demote(seg_id, path=mtca_db)
    assert rows == 1

    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "dormant"

    # 再 demote → silent
    demote(seg_id, path=mtca_db)
    row2 = get_segment(seg_id, path=mtca_db)
    assert row2["silence_state"] == "silent"

    # silent 段 demote 不再生效
    rows3 = demote(seg_id, path=mtca_db)
    assert rows3 == 0

    # 审计
    events = query(
        "SELECT event_type FROM score_events "
        "WHERE segment_id = ? AND event_type = 'retention_demote'",
        (seg_id,),
        path=mtca_db,
    )
    assert len(events) >= 2


# ---------------------------------------------------------------------------
# 测试 10：promote 非法 target 抛 ValueError
# ---------------------------------------------------------------------------


def test_promote_invalid_target(mtca_db: Path) -> None:
    """promote(target='silent') / 非法值应抛 ValueError。"""
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="dormant",
    )

    with pytest.raises(ValueError):
        promote(seg_id, target="silent", path=mtca_db)

    with pytest.raises(ValueError):
        promote(seg_id, target="bogus", path=mtca_db)

    with pytest.raises(ValueError):
        promote("", target="active", path=mtca_db)


# ---------------------------------------------------------------------------
# 测试 11：未知分类走兜底
# ---------------------------------------------------------------------------


def test_get_retention_for_unknown(mtca_db: Path) -> None:
    """未知 category 返回兜底天数。"""
    assert get_retention_for("未知/分类") == FALLBACK_RETENTION_DAYS
    assert get_retention_for(None) == FALLBACK_RETENTION_DAYS  # type: ignore[arg-type]
    assert get_retention_for("   ") == FALLBACK_RETENTION_DAYS


# ---------------------------------------------------------------------------
# 测试 12：无候选段时 tick 返回 0
# ---------------------------------------------------------------------------


def test_tick_handles_no_candidates(mtca_db: Path) -> None:
    """没有任何 active/dormant 段时 tick 返回 0，不抛异常。"""
    # 当前 mtca_db 已经有 1 个 active 段，先把它下沉到 silent
    sid = create_session(path=mtca_db)
    _seed_message(sid, 1, "user", "hi", _now_ms(), mtca_db)
    seg_id = write_segments(sid, path=mtca_db)[0]
    update_segment(seg_id, path=mtca_db, silence_state="silent")

    n = tick(path=mtca_db)
    # silent 段不会进入候选
    assert n == 0


# ---------------------------------------------------------------------------
# 额外：start_background / stop_background 接口存在且不会崩
# ---------------------------------------------------------------------------


def test_background_lifecycle(mtca_db: Path) -> None:
    """start_background 返回 dict handle；stop_background 能正确停止。"""
    # 用极短间隔（0.5 秒），跑 ~1.5 秒后停止
    handle = start_background(interval_seconds=0.5, path=mtca_db)
    assert isinstance(handle, dict)
    assert handle.get("stopped") is False

    # 给后台一点时间
    import time as _t
    _t.sleep(0.6)

    ok = stop_background(handle)
    assert ok is True
    assert handle.get("stopped") is True


# ---------------------------------------------------------------------------
# 额外：/循环 + lock_permanent 同时设，仍跳过
# ---------------------------------------------------------------------------


def test_lock_permanent_with_cycle(mtca_db: Path) -> None:
    """cycle_tag 优先于 lock_permanent 都不会下沉（双重保险）。"""
    ancient = _now_ms() - 1000 * 24 * 60 * 60 * 1000
    sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="active",
        promoted_at=ancient,
    )
    _set_session_cycle_tag(sid, "永久循环", path=mtca_db)
    lock_permanent(seg_id, path=mtca_db)

    n = tick(path=mtca_db)
    assert n == 0
    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "active"


# ---------------------------------------------------------------------------
# 额外：promote 到 active 后再 demote 走完三态机
# ---------------------------------------------------------------------------


def test_full_state_machine(mtca_db: Path) -> None:
    """完整三态机：active → dormant → silent → promote(active) → demote → silent。"""
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=30,
        silence_state="active",
        promoted_at=_now_ms(),
    )

    # 1. demote 到 dormant
    demote(seg_id, path=mtca_db)
    assert get_segment(seg_id, path=mtca_db)["silence_state"] == "dormant"

    # 2. demote 到 silent
    demote(seg_id, path=mtca_db)
    assert get_segment(seg_id, path=mtca_db)["silence_state"] == "silent"

    # 3. promote 回 dormant
    promote(seg_id, target="dormant", path=mtca_db)
    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "dormant"
    assert int(row["promoted_at"]) > 0

    # 4. promote 回 active
    promote(seg_id, target="active", path=mtca_db)
    assert get_segment(seg_id, path=mtca_db)["silence_state"] == "active"


# ---------------------------------------------------------------------------
# 额外：默认策略导入 + 常量值校验
# ---------------------------------------------------------------------------


def test_constants_export() -> None:
    """所有导出常量与 V0.4_PIVOT.md §3.6 对齐。"""
    from src.lifecycle import retention_engine as re

    assert re.PERMANENT == -1
    assert re.DORMANT_MULTIPLIER == 2
    assert re.DEFAULT_BACKGROUND_INTERVAL == 3600
    assert re.FALLBACK_RETENTION_DAYS == 90
    # 4 类策略齐
    assert set(re.DEFAULT_RETENTION_POLICY.keys()) == {
        "编程/技术", "工作项目", "生活/娱乐", "健康/财务",
    }


# ---------------------------------------------------------------------------
# 额外：user_retention_days 覆盖优先于 topic_label 启发式
# ---------------------------------------------------------------------------


def test_user_retention_days_override(mtca_db: Path) -> None:
    """user_retention_days=7 应优先于「生活/娱乐」分类的 30 天默认。"""
    ancient = _now_ms() - 10 * 24 * 60 * 60 * 1000  # 10 天前
    _sid, seg_id = _setup_segment(
        mtca_db,
        user_retention_days=7,  # 用户覆盖：7 天
        silence_state="active",
        promoted_at=ancient,
        messages=("生活/娱乐 - 测试段", "继续", "结束"),
    )

    n = tick(path=mtca_db)
    assert n == 1  # 10 > 7 → 应下沉
    row = get_segment(seg_id, path=mtca_db)
    assert row["silence_state"] == "dormant"
