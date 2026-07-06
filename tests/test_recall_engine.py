"""src/recall/recall_engine.rerank 测试（M2.5.8 §7 #1：emotion 进 rerank）

新增 3 个测试覆盖 emotion 权重：
- test_rerank_emotion_weight：excited 段排在 positive 段前
- test_rerank_neutral_no_boost：neutral 段排在 negative 段后（neutral 加分 = 0）
- test_rerank_weight_sum：3 权重和 = 1.0

所有 candidate dict 携带 emotion_tag 字段（segments 表字段），
rerank 函数读取并查 _EMOTION_RERANK_WEIGHT 映射表。
"""

from __future__ import annotations

from src.recall.recall_engine import rerank


QUERY = "人工智能 调试 测试"

SHARED_TEXT_KEYS: dict = {
    "topic_label": "人工智能 调试 测试",
    "fog_anchor": "测试",
}


def _make_candidate(
    segment_id: str,
    emotion_tag,
    current_score: float = 50.0,
) -> dict:
    return {
        "segment_id": segment_id,
        "session_id": "s1",
        "current_score": current_score,
        "emotion_tag": emotion_tag,
        **SHARED_TEXT_KEYS,
    }


def test_rerank_emotion_weight() -> None:
    """excited 段应排在 positive 段前（同 score、jaccard 一致）。

    修复前：rerank 不看 emotion_tag，stable sort -> positive 在前 -> 失败。
    修复后：excited emotion_weight=1.0 > positive=0.7 -> excited 在前。
    """
    candidates = [
        _make_candidate("seg-positive", "positive"),
        _make_candidate("seg-excited", "excited"),
    ]
    ranked = rerank(QUERY, candidates)
    first_id = ranked[0]["segment_id"]
    assert first_id == "seg-excited", (
        f"excited emotion_weight=1.0 应 > positive=0.7，实际第一名 segment_id={first_id!r}"
    )


def test_rerank_neutral_no_boost() -> None:
    """neutral emotion_weight=0.0 -> neutral 段排在 negative 段后。

    修复前：stable sort 保留 [neutral, negative] 输入顺序 -> neutral 在前 -> 失败。
    修复后：negative emotion_weight=0.5 > neutral=0.0 -> negative 在前。
    """
    candidates = [
        _make_candidate("seg-neutral", "neutral"),
        _make_candidate("seg-negative", "negative"),
    ]
    ranked = rerank(QUERY, candidates)
    first_id = ranked[0]["segment_id"]
    assert first_id == "seg-negative", (
        f"neutral 加分=0，应排在 negative(emotion_weight=0.5)后，实际第一名={first_id!r}"
    )


def test_rerank_weight_sum() -> None:
    """3 个 rerank 权重 J/S/E 必须和为 1.0。"""
    from src.recall.recall_engine import (
        _RERANK_WEIGHT_J,
        _RERANK_WEIGHT_S,
        _RERANK_WEIGHT_E,
    )
    total = float(_RERANK_WEIGHT_J) + float(_RERANK_WEIGHT_S) + float(_RERANK_WEIGHT_E)
    assert abs(total - 1.0) < 1e-9, (
        f"3 权重和必须 = 1.0（实际 {total} = "
        f"J={_RERANK_WEIGHT_J} + S={_RERANK_WEIGHT_S} + E={_RERANK_WEIGHT_E}）"
    )
