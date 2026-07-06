"""L0 象限派生函数（M2.5.1）。

根据 (urgency, importance) 二维坐标判定段落所在的 4 象限，用于动态评分 /
衰减策略分派。

象限规则（边界包含于高位象限）：
    Q1：importance >= 0.7 且 urgency >= 0.7  （重要 + 紧急）
    Q2：importance >= 0.7 且 urgency <  0.7  （重要 + 不紧急）
    Q3：importance <  0.7 且 urgency >= 0.7  （不重要 + 紧急）
    Q4：其他                                  （两者都不沾）

边界阈值取 0.7。
"""

from __future__ import annotations

# 高位象限阈值（快照 M2.5_PLAYBOOK.md §2.2）
_IMPORTANCE_HIGH_THRESHOLD: float = 0.7
_URGENCY_HIGH_THRESHOLD: float = 0.7


def get_quadrant(urgency: float = 0.5, importance: float = 0.5) -> str:
    """根据 urgency/importance 返回段落所在象限。

    参数：
        urgency：时效压力（默认 0.5）。
        importance：长期价值（默认 0.5）。

    返回：
        "Q1" / "Q2" / "Q3" / "Q4" 四者之一。

    边界行为：
        0.7 被视为高位（含），0.6999 则不算高位。
    """
    if importance >= _IMPORTANCE_HIGH_THRESHOLD and urgency >= _URGENCY_HIGH_THRESHOLD:
        return "Q1"
    if importance >= _IMPORTANCE_HIGH_THRESHOLD:
        return "Q2"
    if urgency >= _URGENCY_HIGH_THRESHOLD:
        return "Q3"
    return "Q4"


__all__ = ["get_quadrant"]
