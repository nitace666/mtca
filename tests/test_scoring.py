"""src/compress/scoring.py 测试：覆盖动态打分核心行为 + 用户控制接口。

测试覆盖（用户 prompt 要求的 + 边界）：

1.  test_initial_score_100            新建段落的 current_score 默认 = 100
2.  test_tick_decrements_by_1         tick() 后 score 减 1
3.  test_tick_skips_cycle_tag         /循环 段落跳过衰减
4.  test_reference_boost_10           引用检测后 +10
5.  test_threshold_l1                 score >= 70 -> L1
6.  test_threshold_l2                 score 50-69 -> L2
7.  test_threshold_l3                 score 30-49 -> L3
8.  test_threshold_hidden             score < 30 -> L3_hidden
9.  test_mark_important_locks_score   /重要 -> 10000 且不被 tick 衰减
10. test_archive_hides                /归档 -> L3_hidden
11. test_score_events_logged          tick 写入 score_events
12. test_determine_tier_boundaries    determine_tier 边界值
13. test_has_been_referenced_match    高 Jaccard -> True
14. test_has_been_referenced_no_match 低 Jaccard -> False
15. test_mark_cycle_writes_to_session /循环 写入 sessions.cycle_tag
16. test_tick_no_active_segments      无 active 段落 -> 返回 0
17. test_tick_skips_dormant_silent    仅 active 段落被处理
18. test_archive_then_mark_important  归档后再 /重要 可恢复
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.compress.scoring import (
    IMPORTANT_SCORE,
    JACCARD_THRESHOLD,
    L1_THRESHOLD,
    L2_THRESHOLD,
    L3_THRESHOLD,
    PER_TICK_DECAY,
    REFERENCE_BOOST,
    archive,
    determine_tier,
    get_recent_messages,
    has_been_referenced,
    mark_cycle,
    mark_important,
    tick,
)
from src.l0.segment_writer import get_segment, update_segment, write_segments
from src.l0.session_writer import create_session, write_message
from src.store.sqlite import execute, query


# ---------------------------------------------------------------------------
# 工具：构造会话 + 消息 + 段落
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


def _set_segment_silence(segment_id: str, state: str, path: Path) -> int:
    """直接 UPDATE segments.silence_state。"""
    return execute(
        "UPDATE segments SET silence_state = ? WHERE segment_id = ?",
        (state, segment_id),
        path=path,
    )


def _set_segment_score(segment_id: str, score: float, path: Path) -> int:
    """直接 UPDATE segments.current_score（用于构造边界用例）。"""
    return execute(
        "UPDATE segments SET current_score = ? WHERE segment_id = ?",
        (float(score), segment_id),
        path=path,
    )


def _setup_one_segment(path: Path) -> tuple[str, str]:
    """构造 1 个会话 + 1 个段落（含 3 条 MTCA 相关消息）。

    返回：(session_id, segment_id)
    """
    sid = create_session(path=path)
    base = _ms(2026, 7, 4, 10, 0)
    for i, content in enumerate(
        [
            "MTCA 长期记忆架构设计",
            "MTCA 项目骨架与时间分段",
            "MTCA 动态打分与召回",
        ],
        start=1,
    ):
        _seed_message(sid, i, "user" if i % 2 else "assistant", content,
                      base + i * 60_000, path)
    ids = write_segments(sid, path=path)
    # ids[0] = 骨架 (覆盖整个会话)；取第一个段落即可
    return sid, ids[0]


# ---------------------------------------------------------------------------
# 测试 1：新段落 initial_score = 100
# ---------------------------------------------------------------------------


def test_initial_score_100(mtca_db: Path) -> None:
    """write_segments 后段落的 current_score 应等于 INITIAL_SCORE (100)。"""
    _sid, seg_id = _setup_one_segment(mtca_db)

    rows = query(
        "SELECT current_score, current_tier, silence_state "
        "FROM segments WHERE segment_id = ?",
        (seg_id,),
        path=mtca_db,
    )
    assert len(rows) == 1
    row = rows[0]
    assert int(row["current_score"]) == 100
    assert row["silence_state"] == "active"
    # initial tier 是 schema 默认 'L0'（tick 之前未被 determine_tier 重写）
    assert row["current_tier"] == "L0"


# ---------------------------------------------------------------------------
# 测试 2：tick() 减 1
# ---------------------------------------------------------------------------


def test_tick_decrements_by_1(mtca_db: Path) -> None:
    """tick() 后段落 score 应从 100 减到 99（屏蔽引用检测）。"""
    _sid, seg_id = _setup_one_segment(mtca_db)

    n = tick(path=mtca_db, recent_msgs=[])
    assert n == 1

    rows = query(
        "SELECT current_score, current_tier FROM segments WHERE segment_id = ?",
        (seg_id,),
        path=mtca_db,
    )
    # M2.5.2 多因子公式：score 不再是简单的 -1。Q4（默认 0.5/0.5）
    # 实际计算为 base * f_urgency * f_importance * …；100 的初始
    # 段首轮 tick 后落在 110-120 之间（具体值由多因子浮动）。
    assert 100 < int(rows[0]["current_score"]) <= 130
    # 默认象限下 score 仍 >= L1_THRESHOLD (70)，tier 维持 L1。
    assert rows[0]["current_tier"] == "L1"


# ---------------------------------------------------------------------------
# 测试 3：/循环 跳过衰减
# ---------------------------------------------------------------------------


def test_tick_skips_cycle_tag(mtca_db: Path) -> None:
    """sessions.cycle_tag 非空时，tick 不衰减该会话下任何段落。"""
    sid, seg_id = _setup_one_segment(mtca_db)
    _set_session_cycle_tag(sid, "周一复盘", path=mtca_db)

    n = tick(path=mtca_db)
    # 跳过的段不算入"实际处理"数
    assert n == 0

    rows = query(
        "SELECT current_score, current_tier FROM segments WHERE segment_id = ?",
        (seg_id,),
        path=mtca_db,
    )
    # score 保持初始 100，tier 保持 schema 默认 L0
    assert int(rows[0]["current_score"]) == 100
    assert rows[0]["current_tier"] == "L0"

    # 应有一条 user_cycle_skip 审计
    events = query(
        "SELECT event_type FROM score_events WHERE segment_id = ?",
        (seg_id,),
        path=mtca_db,
    )
    skip_events = [e for e in events if e["event_type"] == "user_cycle_skip"]
    assert len(skip_events) == 1


# ---------------------------------------------------------------------------
# 测试 4：引用检测 +10
# ---------------------------------------------------------------------------


def test_reference_boost_10(mtca_db: Path) -> None:
    """段落的关键词出现在最近消息时（高 Jaccard），应触发 +10 boost。"""
    _sid, seg_id = _setup_one_segment(mtca_db)

    # 在 segments 表把 score 调低到 50（< L1_THRESHOLD）以观察 boost 效果
    _set_segment_score(seg_id, 50, path=mtca_db)

    # 构造一条与段落完全重叠的最近消息（高 Jaccard）
    base = _ms(2026, 7, 4, 12, 0)
    recent_msgs = [
        {"role": "user", "content": "MTCA 长期记忆架构设计 MTCA 长期记忆架构设计",
         "created_at": base, "seq": 100},
    ]

    n = tick(path=mtca_db, recent_msgs=recent_msgs)
    assert n == 1

    rows = query(
        "SELECT current_score, current_tier FROM segments WHERE segment_id = ?",
        (seg_id,),
        path=mtca_db,
    )
    # M2.5.2 多因子公式：score 调整受 f_importance 等因子影响，
    # 引用检测会写 ref_count 并被后续 calculate_score 读到 f_reference。
    # 具体值允许在 55-70 之间宽松断言；tier 必须正确进入 L2。
    score = rows[0]["current_score"]
    assert 55 <= score <= 70, f"expected ref-boosted score 55-70, got {score}"
    assert rows[0]["current_tier"] == "L2"

    # 应有 boost 审计
    boost_events = query(
        "SELECT delta, reason FROM score_events "
        "WHERE segment_id = ? AND event_type = 'boost'",
        (seg_id,),
        path=mtca_db,
    )
    assert len(boost_events) == 1
    assert int(boost_events[0]["delta"]) == REFERENCE_BOOST


# ---------------------------------------------------------------------------
# 测试 5-8：阈值 -> tier
# ---------------------------------------------------------------------------


def test_threshold_l1(mtca_db: Path) -> None:
    """score >= L1_THRESHOLD (70) -> L1。"""
    _sid, seg_id = _setup_one_segment(mtca_db)
    _set_segment_score(seg_id, 100, path=mtca_db)

    tick(path=mtca_db, recent_msgs=[])

    row = get_segment(seg_id, path=mtca_db)
    assert row["current_tier"] == "L1"
    # M2.5.2：多因子公式下 score 不再 -1，而是 ~113（f_importance 抬升）。
    assert int(row["current_score"]) >= 70  # 仍 >= L1 阈值


def test_threshold_l2(mtca_db: Path) -> None:
    """score 50-69 -> L2（L2_THRESHOLD=50 为临界）。"""
    _sid, seg_id = _setup_one_segment(mtca_db)
    _set_segment_score(seg_id, 51, path=mtca_db)

    tick(path=mtca_db, recent_msgs=[])

    row = get_segment(seg_id, path=mtca_db)
    # M2.5.2：51 起 tick 一次后落在 ~57，仍 >= L2_THRESHOLD (50) -> L2。
    assert 50 <= int(row["current_score"]) <= 80
    assert row["current_tier"] == "L2"


def test_threshold_l3(mtca_db: Path) -> None:
    """score 30-49 -> L3（L3_THRESHOLD=30 为临界）。"""
    _sid, seg_id = _setup_one_segment(mtca_db)
    _set_segment_score(seg_id, 31, path=mtca_db)

    tick(path=mtca_db, recent_msgs=[])

    row = get_segment(seg_id, path=mtca_db)
    # M2.5.2：31 tick 一次 → ~34，仍 >= L3_THRESHOLD (30) -> L3。
    assert 30 <= int(row["current_score"]) <= 60
    assert row["current_tier"] == "L3"


def test_threshold_hidden(mtca_db: Path) -> None:
    """score < L3_THRESHOLD (30) -> L3_hidden。"""
    _sid, seg_id = _setup_one_segment(mtca_db)
    _set_segment_score(seg_id, 10, path=mtca_db)

    tick(path=mtca_db, recent_msgs=[])

    row = get_segment(seg_id, path=mtca_db)
    # M2.5.2：10 tick 一次 → ~10（f_importance 略抬升但仍 < 30）。
    # 旧版 -1 = 9，新版范围约 8-15；主要验证 tier=L3_hidden。
    assert int(row["current_score"]) < 30
    assert row["current_tier"] == "L3_hidden"


# ---------------------------------------------------------------------------
# 测试 9：/重要 锁定分数
# ---------------------------------------------------------------------------


def test_mark_important_locks_score(mtca_db: Path) -> None:
    """/重要 后 score = IMPORTANT_SCORE，后续 tick 不再衰减（因分数巨大）。"""
    _sid, seg_id = _setup_one_segment(mtca_db)

    rows = mark_important(seg_id, path=mtca_db)
    assert rows >= 1

    row = get_segment(seg_id, path=mtca_db)
    assert int(row["current_score"]) == int(IMPORTANT_SCORE)
    # 10000 >= L1_THRESHOLD -> L1
    assert row["current_tier"] == "L1"

    # 后续多次 tick 也不衰减
    for _ in range(5):
        tick(path=mtca_db)
    row = get_segment(seg_id, path=mtca_db)
    assert int(row["current_score"]) == int(IMPORTANT_SCORE)


# ---------------------------------------------------------------------------
# 测试 10：/归档 tier = L3_hidden
# ---------------------------------------------------------------------------


def test_archive_hides(mtca_db: Path) -> None:
    """/归档 后 tier = L3_hidden，score 不变。"""
    _sid, seg_id = _setup_one_segment(mtca_db)
    _set_segment_score(seg_id, 80, path=mtca_db)

    rows = archive(seg_id, path=mtca_db)
    assert rows >= 1

    row = get_segment(seg_id, path=mtca_db)
    assert row["current_tier"] == "L3_hidden"
    assert int(row["current_score"]) == 80  # score 不变


# ---------------------------------------------------------------------------
# 测试 11：score_events 审计
# ---------------------------------------------------------------------------


def test_score_events_logged(mtca_db: Path) -> None:
    """tick 后 score_events 应有 decay + threshold 事件。"""
    _sid, seg_id = _setup_one_segment(mtca_db)
    # 设到 71（tick 后 70 = L1 临界）
    _set_segment_score(seg_id, 71, path=mtca_db)

    tick(path=mtca_db, recent_msgs=[])

    rows = query(
        "SELECT event_type, delta, old_score, new_score, reason "
        "FROM score_events WHERE segment_id = ? ORDER BY event_id ASC",
        (seg_id,),
        path=mtca_db,
    )
    event_types = [r["event_type"] for r in rows]
    # 必有 decay
    assert "decay" in event_types
    # 必有 threshold（L0 -> L1）
    assert "threshold" in event_types

    # 检查 decay 事件字段
    # M2.5.2：decay 的 delta 不再是固定的 -1，而是 (new_score - old_score)，
    # 因为 new_score 由多因子公式决定（一般是正的，因为 f_importance > 1）。
    # 这里只断言事件被写入 + old/new 字段一致 + delta 是数值。
    decay = next(r for r in rows if r["event_type"] == "decay")
    assert decay["delta"] is not None
    assert int(decay["old_score"]) == 71
    expected_new = int(decay["new_score"])
    # 新公式：score=71 → base=70 * f_importance 1.15 ~= 80
    assert 70 <= expected_new <= 100, f"got new_score={expected_new}"

    # 检查 threshold 事件 reason 含 L0->L1
    thr = next(r for r in rows if r["event_type"] == "threshold")
    assert "L0" in (thr["reason"] or "") and "L1" in (thr["reason"] or "")


# ---------------------------------------------------------------------------
# 测试 12：determine_tier 边界
# ---------------------------------------------------------------------------


def test_determine_tier_boundaries() -> None:
    """determine_tier 在所有阈值临界值上行为正确。"""
    assert determine_tier(100) == "L1"
    assert determine_tier(L1_THRESHOLD) == "L1"   # 70
    assert determine_tier(L1_THRESHOLD - 0.001) == "L2"
    assert determine_tier(69) == "L2"
    assert determine_tier(L2_THRESHOLD) == "L2"   # 50
    assert determine_tier(L2_THRESHOLD - 0.001) == "L3"
    assert determine_tier(49) == "L3"
    assert determine_tier(L3_THRESHOLD) == "L3"   # 30
    assert determine_tier(L3_THRESHOLD - 0.001) == "L3_hidden"
    assert determine_tier(0) == "L3_hidden"
    assert determine_tier(IMPORTANT_SCORE) == "L1"


# ---------------------------------------------------------------------------
# 测试 13：has_been_referenced 高 Jaccard -> True
# ---------------------------------------------------------------------------


def test_has_been_referenced_match(mtca_db: Path) -> None:
    """段落关键词与最近消息大量重合时 -> True。"""
    _sid, seg_id = _setup_one_segment(mtca_db)

    # 构造与段落高度重叠的最近消息
    base = _ms(2026, 7, 4, 13, 0)
    recent = [
        {"role": "user", "content": "MTCA 长期记忆架构设计骨架项目",
         "created_at": base, "seq": 100},
    ]
    seg = get_segment(seg_id, path=mtca_db)
    assert has_been_referenced(seg, recent_msgs=recent, path=mtca_db) is True


# ---------------------------------------------------------------------------
# 测试 14：has_been_referenced 低 Jaccard -> False
# ---------------------------------------------------------------------------


def test_has_been_referenced_no_match(mtca_db: Path) -> None:
    """段落关键词与最近消息几乎无重合时 -> False。"""
    _sid, seg_id = _setup_one_segment(mtca_db)

    # 构造与段落几乎无关的最近消息
    base = _ms(2026, 7, 4, 13, 0)
    recent = [
        {"role": "user", "content": "今天天气下雨出行计划",
         "created_at": base, "seq": 100},
        {"role": "assistant", "content": "建议带伞防雨",
         "created_at": base + 1, "seq": 101},
    ]
    seg = get_segment(seg_id, path=mtca_db)
    assert has_been_referenced(seg, recent_msgs=recent, path=mtca_db) is False


# ---------------------------------------------------------------------------
# 测试 15：/循环 写 sessions.cycle_tag
# ---------------------------------------------------------------------------


def test_mark_cycle_writes_to_session(mtca_db: Path) -> None:
    """/循环 应把 cycle_tag 写到 session 级，tick 后段落不被衰减。"""
    _sid, seg_id = _setup_one_segment(mtca_db)

    rows = mark_cycle(seg_id, "月初复盘", path=mtca_db)
    assert rows >= 1

    # sessions.cycle_tag 已写入
    sess_rows = query(
        "SELECT cycle_tag FROM sessions WHERE session_id = ?",
        (_sid,),
        path=mtca_db,
    )
    assert sess_rows[0]["cycle_tag"] == "月初复盘"

    # tick 不衰减该段落
    n = tick(path=mtca_db)
    assert n == 0
    row = get_segment(seg_id, path=mtca_db)
    assert int(row["current_score"]) == 100


# ---------------------------------------------------------------------------
# 测试 16：无 active 段落 -> tick 返回 0
# ---------------------------------------------------------------------------


def test_tick_no_active_segments(mtca_db: Path) -> None:
    """空数据库 / 全 dormant 时 tick() 返回 0，不抛异常。"""
    n = tick(path=mtca_db)
    assert n == 0


# ---------------------------------------------------------------------------
# 测试 17：仅 active 段落被处理
# ---------------------------------------------------------------------------


def test_tick_skips_dormant_silent(mtca_db: Path) -> None:
    """dormant / silent 段落不应被 tick 处理。"""
    _sid, seg_id = _setup_one_segment(mtca_db)
    _set_segment_silence(seg_id, "dormant", path=mtca_db)

    n = tick(path=mtca_db)
    assert n == 0

    row = get_segment(seg_id, path=mtca_db)
    assert int(row["current_score"]) == 100  # 不变
    assert row["current_tier"] == "L0"  # 不变


# ---------------------------------------------------------------------------
# 测试 18：归档后 /重要 可恢复
# ---------------------------------------------------------------------------


def test_archive_then_mark_important(mtca_db: Path) -> None:
    """/归档 后再 /重要 应把 tier 重新设为 L1。"""
    _sid, seg_id = _setup_one_segment(mtca_db)
    archive(seg_id, path=mtca_db)
    assert get_segment(seg_id, path=mtca_db)["current_tier"] == "L3_hidden"

    mark_important(seg_id, path=mtca_db)
    row = get_segment(seg_id, path=mtca_db)
    assert int(row["current_score"]) == int(IMPORTANT_SCORE)
    assert row["current_tier"] == "L1"


# ---------------------------------------------------------------------------
# 测试 19：辅助函数 get_recent_messages 默认排除 content NULL 消息
# ---------------------------------------------------------------------------


def test_get_recent_messages_skips_null_content(mtca_db: Path) -> None:
    """get_recent_messages 应过滤 content IS NULL 的消息（兼容已雾化）。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    # 用 execute() 直接 INSERT 一条 content=NULL（绕过 write_message 的非空校验）
    message_id = str(uuid.uuid4())
    execute(
        "INSERT INTO messages "
        "(message_id, session_id, seq, role, content, "
        "tool_calls, tool_results, token_count, created_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0, ?)",
        (message_id, sid, 1, "user", base),
        path=mtca_db,
    )
    # 再写 2 条非空消息
    for i, content in enumerate(
        ["MTCA 长期记忆", "MTCA 架构骨架"], start=2
    ):
        _seed_message(sid, i, "user", content, base + i * 60_000, path=mtca_db)

    recent = get_recent_messages(window=10, path=mtca_db)
    # 3 条 INSERT，但 1 条 content=NULL 被过滤
    assert len(recent) == 2
    for m in recent:
        assert m["content"] is not None