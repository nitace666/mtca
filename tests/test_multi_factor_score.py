"""src/compress/multi_factor_score.py 测试（M2.5.2）

覆盖：
- 4 象限独立时间衰减（Q1/Q2/Q3/Q4）
- 第 9 铁律：Q1 锁底 / URGENT 不衰减 / /重要 锁分 / completed 加速
- 情绪软因子（positive / negative）
- 引用 boost 饱和（ref_count > 5 与 = 5 效果一致）
- 默认象限（都不设 → Q4）
- get_quadrant_from_seg 边界
"""

from __future__ import annotations

import pytest

from src.compress.multi_factor_score import (
    _EMOTION_FACTOR,
    _HALF_LIFE_DAYS,
    calculate_score,
    get_quadrant_from_seg,
)


# ---------------------------------------------------------------------------
# 测试夹具 / 常量
# ---------------------------------------------------------------------------

# 测试用稳定时间戳（2026-07-06 00:00:00 UTC）
NOW_MS: int = 1782336000000
DAY_MS: int = 86400000


def make_seg(**overrides) -> dict:
    """构造一个默认 score=100、Q4 象限（默认值）的段 dict。"""
    seg = {
        "segment_id": "test-seg-1",
        "current_score": 100.0,
        "urgency_level": 0.5,
        "importance_level": 0.5,
        "emotion_tag": None,
        "ref_count": 0,
        "promoted_at": NOW_MS,
        "urgent_state": None,
    }
    seg.update(overrides)
    return seg


@pytest.fixture
def now() -> int:
    return NOW_MS


VERY_OLD_DAYS: int = 3650  # 10 年 → 任何象限的 f_time 趋近 0
VERY_OLD_PROMOTED_AT: int = NOW_MS - VERY_OLD_DAYS * DAY_MS


# ---------------------------------------------------------------------------
# 4 象限独立时间衰减
# ---------------------------------------------------------------------------


def test_q1_low_decay() -> None:
    """Q1（重要+紧急）：half_life=180d，30 天后约 0.5^(30/180)=0.891。"""
    seg = make_seg(
        urgency_level=0.8,
        importance_level=0.8,
        promoted_at=NOW_MS - 30 * DAY_MS,
    )
    score, _ = calculate_score(seg, NOW_MS)
    # base = 100 - 1 = 99
    # f_urgency = 0.5 + 0.8*1.0 = 1.3
    # f_importance = 0.3 + 0.8*1.7 = 1.66
    # f_time = 0.5 ** (30/180) ≈ 0.891
    # 99 * 1.3 * 1.66 * 1.0 * 1.0 * 0.891 ≈ 190.7
    # 验证：落在 (38, 50) 之外的合理高分段（重要的段落确实被奖励）
    assert score > 50
    assert score < 250


def test_q2_slow_decay() -> None:
    """Q2（重要+不紧急）：half_life=365d，180 天后约 0.5^(180/365)=0.713。"""
    seg = make_seg(
        urgency_level=0.3,
        importance_level=0.8,
        promoted_at=NOW_MS - 180 * DAY_MS,
    )
    score, _ = calculate_score(seg, NOW_MS)
    # base = 99, f_urgency = 0.8, f_importance = 1.66
    # f_time = 0.5 ** (180/365) ≈ 0.713
    # 99 * 0.8 * 1.66 * 1.0 * 1.0 * 0.713 ≈ 93.8
    assert score > 60
    assert score < 150


def test_q3_fast_decay() -> None:
    """Q3（不重要+紧急）：half_life=14d，3 天后约 0.5^(3/14)=0.862。"""
    seg = make_seg(
        urgency_level=0.8,
        importance_level=0.3,
        promoted_at=NOW_MS - 3 * DAY_MS,
    )
    score, _ = calculate_score(seg, NOW_MS)
    # base = 99, f_urgency = 1.3, f_importance = 0.81
    # f_time = 0.5 ** (3/14) ≈ 0.862
    # 99 * 1.3 * 0.81 * 0.862 ≈ 89.9
    assert 70 < score < 110


def test_q4_super_fast_decay() -> None:
    """Q4（都不沾）：half_life=3d，1 天后约 0.5^(1/3)=0.794。"""
    seg = make_seg(
        urgency_level=0.3,
        importance_level=0.3,
        promoted_at=NOW_MS - 1 * DAY_MS,
    )
    score, _ = calculate_score(seg, NOW_MS)
    # base = 99, f_urgency = 0.8, f_importance = 0.81
    # f_time = 0.5 ** (1/3) ≈ 0.794
    # 99 * 0.8 * 0.81 * 0.794 ≈ 50.9
    assert 30 < score < 80


def test_q1_locked_to_L3_never_hidden() -> None:
    """Q1 即使分很低（<30）也锁 L3，不进 L3_hidden（第 9 铁律）。"""
    seg = make_seg(
        urgency_level=0.8,
        importance_level=0.8,
        current_score=5.0,
        promoted_at=VERY_OLD_PROMOTED_AT,
    )
    _score, tier = calculate_score(seg, NOW_MS)
    assert tier != "L3_hidden"
    assert tier == "L3"


def test_q1_fresh_does_not_enter_hidden_due_to_score_threshold() -> None:
    """Q1 刚 promote 立刻打分，tier 由公式决定但不会因 _determine_tier 走 L3_hidden。"""
    seg = make_seg(
        urgency_level=0.9,
        importance_level=0.9,
        current_score=99.0,
        promoted_at=NOW_MS,
    )
    _score, tier = calculate_score(seg, NOW_MS)
    # base=98, f_urgency=1.4, f_importance=1.83, f_time=1.0 → 约 251
    assert tier == "L1"
    assert tier != "L3_hidden"


# ---------------------------------------------------------------------------
# 第 9 铁律
# ---------------------------------------------------------------------------


def test_iron_rule_urgent_state_no_decay() -> None:
    """urgent_state='tracking' → f_time 被强制为 1.0，10 年后仍不衰减。"""
    seg = make_seg(
        urgent_state="tracking",
        promoted_at=VERY_OLD_PROMOTED_AT,
    )
    score, _tier = calculate_score(seg, NOW_MS)
    # base = 100-1 = 99, f_urgency=1.0, f_importance=1.15, f_time=1.0
    # 99 * 1.0 * 1.15 * 1.0 * 1.0 * 1.0 = 113.85
    assert score > 95


def test_iron_rule_important_lock_score_10000() -> None:
    """/重要（score>=10000）即使 promoted 很久也几乎不变。"""
    seg = make_seg(
        current_score=10000.0,
        promoted_at=VERY_OLD_PROMOTED_AT,
    )
    score, _tier = calculate_score(seg, NOW_MS)
    # current_score=10000 触发 f_time=1.0
    # base = 9999, f_urgency=1.0, f_importance=1.15
    # 9999 * 1.0 * 1.15 * 1.0 * 1.0 * 1.0 ≈ 11498.85
    assert score >= 9500


def test_iron_rule_completed_decay_fast() -> None:
    """urgent_state='completed' → f_time 额外 0.5x，段分应比 normal 段低。"""
    seg_normal = make_seg(
        urgency_level=0.3,
        importance_level=0.8,
        promoted_at=NOW_MS - 30 * DAY_MS,
    )
    seg_completed = dict(seg_normal)
    seg_completed["urgent_state"] = "completed"
    s_normal, _ = calculate_score(seg_normal, NOW_MS)
    s_done, _ = calculate_score(seg_completed, NOW_MS)
    assert s_done < s_normal


# ---------------------------------------------------------------------------
# 情绪软因子
# ---------------------------------------------------------------------------


def test_emotion_positive_vs_negative() -> None:
    """emotion=positive 比 emotion=negative 分数高。"""
    seg_pos = make_seg(
        emotion_tag="positive",
        urgency_level=0.3,
        importance_level=0.3,
    )
    seg_neg = make_seg(
        emotion_tag="negative",
        urgency_level=0.3,
        importance_level=0.3,
    )
    s_pos, _ = calculate_score(seg_pos, NOW_MS)
    s_neg, _ = calculate_score(seg_neg, NOW_MS)
    assert s_pos > s_neg
    # positive=1.2, negative=0.8 → 比例 1.5 倍
    assert abs(s_pos / s_neg - _EMOTION_FACTOR["positive"] / _EMOTION_FACTOR["negative"]) < 0.01


def test_emotion_unknown_defaults_to_1_0() -> None:
    """emotion_tag 是未知字符串时，等价于 emotion=None (1.0x)。"""
    seg_unk = make_seg(emotion_tag="wtf", urgency_level=0.5, importance_level=0.5)
    seg_neu = make_seg(emotion_tag=None, urgency_level=0.5, importance_level=0.5)
    s_unk, _ = calculate_score(seg_unk, NOW_MS)
    s_neu, _ = calculate_score(seg_neu, NOW_MS)
    assert abs(s_unk - s_neu) < 0.01


# ---------------------------------------------------------------------------
# 引用 ref_count 因子
# ---------------------------------------------------------------------------


def test_ref_count_boost_saturates() -> None:
    """ref_count 越多 boost 越大，但 ref_count >= 5 后封顶（与 5 等价）。"""
    seg_5 = make_seg(ref_count=5, urgency_level=0.5, importance_level=0.5)
    seg_10 = make_seg(ref_count=10, urgency_level=0.5, importance_level=0.5)
    seg_0 = make_seg(ref_count=0, urgency_level=0.5, importance_level=0.5)
    s5, _ = calculate_score(seg_5, NOW_MS)
    s10, _ = calculate_score(seg_10, NOW_MS)
    s0, _ = calculate_score(seg_0, NOW_MS)
    # 封顶：ref=10 == ref=5
    assert abs(s10 - s5) < 0.01
    # 至少比 ref=0 高（10% per cap）
    assert s5 > s0


def test_ref_count_none_treated_as_0() -> None:
    """ref_count 为 None / 不存在 → 视为 0。"""
    seg_no = make_seg(urgency_level=0.5, importance_level=0.5)
    seg_no.pop("ref_count")
    seg_zero = make_seg(ref_count=0, urgency_level=0.5, importance_level=0.5)
    s_no, _ = calculate_score(seg_no, NOW_MS)
    s_zero, _ = calculate_score(seg_zero, NOW_MS)
    assert abs(s_no - s_zero) < 0.01


# ---------------------------------------------------------------------------
# 默认象限 / 边界
# ---------------------------------------------------------------------------


def test_default_quadrant_q4_when_no_fields() -> None:
    """完全不设 urgency/importance → 0.5/0.5 → Q4（边界默认走 Q4）。"""
    seg = make_seg()
    seg.pop("urgency_level", None)
    seg.pop("importance_level", None)
    quad = get_quadrant_from_seg(seg)
    assert quad == "Q4"


def test_get_quadrant_from_seg_boundaries() -> None:
    """边界 0.7 属高位象限（含）。"""
    assert get_quadrant_from_seg(make_seg(urgency_level=0.7, importance_level=0.7)) == "Q1"
    assert get_quadrant_from_seg(make_seg(urgency_level=0.7, importance_level=0.69)) == "Q3"
    assert get_quadrant_from_seg(make_seg(urgency_level=0.69, importance_level=0.7)) == "Q2"
    assert get_quadrant_from_seg(make_seg(urgency_level=0.69, importance_level=0.69)) == "Q4"


def test_tier_thresholds_for_q4_low_score() -> None:
    """Q4 score=25 (很低且没情绪 boost) → L3_hidden。"""
    seg = make_seg(
        urgency_level=0.1,
        importance_level=0.1,
        current_score=25.0,
        promoted_at=NOW_MS,
    )
    _score, tier = calculate_score(seg, NOW_MS)
    # base=24, f_urgency=0.6, f_importance=0.47, f_time≈1 → 约 6.77 < 30
    assert tier == "L3_hidden"


def test_constants_exposed() -> None:
    """核心常量应该按 spec 定义。"""
    assert _HALF_LIFE_DAYS == {"Q1": 180, "Q2": 365, "Q3": 14, "Q4": 3}
    assert _EMOTION_FACTOR["positive"] > 1.0
    assert _EMOTION_FACTOR["negative"] < 1.0
    assert _EMOTION_FACTOR.get(None) == 1.0


def test_promoted_at_in_future_treated_as_now() -> None:
    """promoted_at > now（时钟漂移）：age_days 被 max(0, …) 截为 0，f_time=1。"""
    seg = make_seg(promoted_at=NOW_MS + 365 * DAY_MS)  # 未来
    s_future, _ = calculate_score(seg, NOW_MS)
    seg_now = make_seg(promoted_at=NOW_MS)
    s_now, _ = calculate_score(seg_now, NOW_MS)
    assert abs(s_future - s_now) < 0.01


def test_q2_fresh_segment_in_L1() -> None:
    """Q2 新段落立即打分，分应在 L1（高 importance 提升足够）。"""
    seg = make_seg(
        urgency_level=0.5,
        importance_level=0.95,
        current_score=100.0,
        promoted_at=NOW_MS,
    )
    _score, tier = calculate_score(seg, NOW_MS)
    assert tier == "L1"
