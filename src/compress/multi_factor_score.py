"""
src/compress/multi_factor_score.py — M2.5.2 多因子打分公式

替换线性 -1/tick 衰减，按 4 象限配 half_life + 多因子加权。
满足 MTCA 第 9 铁律：Q1 + URGENT + /重要 永不遗忘。
"""
from __future__ import annotations

from typing import Any

# 4 象限的半衰期（天）
_HALF_LIFE_DAYS: dict[str, int] = {
    "Q1": 180,  # 重要+紧急 → 慢衰（半年）
    "Q2": 365,  # 重要+不紧急 → 极慢衰（1 年）
    "Q3": 14,   # 不重要+紧急 → 快衰（2 周）
    "Q4": 3,    # 都不沾 → 极快衰（3 天）
}

_EMOTION_FACTOR: dict[str, float] = {
    "positive": 1.2,
    "excited":  1.15,
    "anxious":  1.1,
    "neutral":  1.0,
    "negative": 0.8,
    None:       1.0,
}


def get_quadrant_from_seg(seg: dict) -> str:
    """段 dict → 象限字符串。"""
    import math
    urg = float(seg.get("urgency_level", 0.5) or 0.5)
    imp = float(seg.get("importance_level", 0.5) or 0.5)
    if imp >= 0.7 and urg >= 0.7:
        return "Q1"
    if imp >= 0.7:
        return "Q2"
    if urg >= 0.7:
        return "Q3"
    return "Q4"


def calculate_score(seg: dict, now_ms: int) -> tuple[float, str]:
    """多因子打分。返回 (new_score, new_tier)。
    
    公式：base * f_urgency * f_importance * f_emotion * f_reference * f_time
    满足第 9 铁律：Q1 + URGENT + /重要 段 f_time=1.0（不衰减）
    """
    # 基础衰减（每 tick -1，保留旧逻辑的稳定性兜底）
    current_score = float(seg.get("current_score", 100.0) or 0.0)
    base = max(0.0, current_score - 1.0)
    
    # 4 因子
    urg = float(seg.get("urgency_level", 0.5) or 0.5)
    imp = float(seg.get("importance_level", 0.5) or 0.5)
    emotion = seg.get("emotion_tag")
    f_urgency    = 0.5 + urg * 1.0      # 0.5x ~ 1.5x
    f_importance = 0.3 + imp * 1.7      # 0.3x ~ 2.0x
    f_emotion    = _EMOTION_FACTOR.get(emotion, 1.0)
    f_reference  = 1.0 + min(int(seg.get("ref_count", 0) or 0), 5) * 0.1
    
    # 时间衰减（指数）
    quadrant = get_quadrant_from_seg(seg)
    half_life_days = _HALF_LIFE_DAYS[quadrant]
    promoted_at = int(seg.get("promoted_at", now_ms) or now_ms)
    age_days = max(0, (now_ms - promoted_at) / 86400000)
    f_time = 0.5 ** (age_days / half_life_days)
    
    # 第 9 铁律：3 种永不遗忘例外
    # 1. /重要 段（current_score ≥ 10000）：直接锁定 score 不变。
    #    不仅 f_time=1.0，还要避免 f_importance 等因子把 score 放大——
    #    /重要 的语义是"完全冻结"，不能被任何自动打分逻辑改变。
    if current_score >= 10000.0:
        return float(current_score), "L1"
    # 2. URGENT 段（用户在追踪）
    if seg.get("urgent_state") == "tracking":
        f_time = 1.0
    # 3. 已过期但用户回复"重要"
    if (seg.get("urgent_state") or "") == "completed":
        # 已完成 → 降权加速
        f_time *= 0.5
    
    new_score = base * f_urgency * f_importance * f_emotion * f_reference * f_time
    
    # tier 决定
    new_tier = _determine_tier(new_score, quadrant)
    return new_score, new_tier


def _determine_tier(score: float, quadrant: str) -> str:
    """根据 score + quadrant 决定 tier。
    
    第 9 铁律加强：Q1 永不进 L3_hidden。
    其他象限按 score 阈值。
    """
    if quadrant == "Q1" and score < 30.0:
        return "L3"        # Q1 锁底：宁可 L3 也不 L3_hidden
    if score >= 70.0:
        return "L1"
    if score >= 50.0:
        return "L2"
    if score >= 30.0:
        return "L3"
    return "L3_hidden"


__all__ = ["calculate_score", "get_quadrant_from_seg", "_HALF_LIFE_DAYS", "_EMOTION_FACTOR"]
