"""src/llm/emotion_extractor.py — M2.5.6 情感提取

把一段对话提炼成 1 个 emotion（positive/negative/anxious/excited/neutral），
喂多因子打分公式的 f_emotion 因子（multi_factor_score._EMOTION_FACTOR）。

参考 M2.5.2 _extract_fact_text 的 schema-alias 容错（content / fact / text 3
alias），这里再做一次：emotion / emotion_tag / emotion_label / feeling / label
5 alias + 别名映射（happy→positive 等）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from src.llm.extractor import _get_provider


VALID_EMOTIONS = ("positive", "negative", "anxious", "excited", "neutral")

# 5 标准 emotion 的别名映射（qwen-heretic 等模型输出变体时兜底）
_EMOTION_ALIASES: dict[str, str] = {
    "happy": "positive", "good": "positive", "great": "positive", "joy": "positive",
    "sad": "negative", "bad": "negative", "angry": "negative", "frustrated": "negative",
    "worry": "anxious", "worried": "anxious", "stress": "anxious", "urgent": "anxious",
    "excited": "excited", "eager": "excited",
    "ok": "neutral", "fine": "neutral",
}


_EMOTION_PROMPT: str = (
    "你是一个情感识别助手。从对话中分析整体情感基调。\n"
    "返回严格的 JSON 对象，不要其他内容：\n"
    '{"emotion": "positive|negative|anxious|excited|neutral", '
    '"confidence": 0.0-1.0}\n'
)


def _extract_emotion_from_llm_output(raw: str) -> tuple[str, float]:
    """从 LLM 原始输出解析 (emotion, confidence)。

    容错 schema-alias（参考 _extract_fact_text 思路）：
    - 标准：{"emotion": "...", "confidence": ...}
    - alias：emotion_tag / emotion_label / feeling / label
    - markdown fence ```json ... ```
    - 坏 JSON / 缺字段 / 非字符串 emotion → 返回 ("neutral", 0.0)
    """
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return "neutral", 0.0
    # 去 markdown fence
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    # 尝试 JSON parse；失败回退到 {...} 子串
    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*?\}", raw)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    if not isinstance(data, dict):
        return "neutral", 0.0
    # 5 个字段 alias 顺序匹配
    emo_raw = (
        data.get("emotion") or data.get("emotion_tag")
        or data.get("emotion_label") or data.get("feeling")
        or data.get("label") or "neutral"
    )
    if not isinstance(emo_raw, str):
        return "neutral", 0.0
    emo_norm = emo_raw.strip().lower()
    # 别名 → 5 标准之一；仍不在 → neutral 兜底
    if emo_norm not in VALID_EMOTIONS:
        emo_norm = _EMOTION_ALIASES.get(emo_norm, "neutral")
    if emo_norm not in VALID_EMOTIONS:
        emo_norm = "neutral"
    # confidence 钳到 [0.0, 1.0]
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return emo_norm, max(0.0, min(1.0, conf))


def extract_emotion(
    text: str,
    provider: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """从对话文本提取 emotion + confidence。

    失败 / 无 provider / provider 不可用 → 返回 None（caller 走兜底）。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        prov = _get_provider(provider)
        if not prov.is_available():
            return None
    except Exception:
        return None
    try:
        # 拼成单条 user message（参考 _llm_generate 模式）；截断 2000 字防溢出
        user_msg = _EMOTION_PROMPT + "\n\n[对话]\n" + text[:2000] + "\n[/对话]"
        # emotion JSON 比 facts 短，max_tokens=128 够
        llm_text = prov.generate(user_msg, max_tokens=128)
    except Exception:
        return None
    emo, conf = _extract_emotion_from_llm_output(llm_text)
    if emo not in VALID_EMOTIONS:
        return None
    return {"emotion": emo, "confidence": conf}


__all__ = [
    "VALID_EMOTIONS",
    "_extract_emotion_from_llm_output",
    "extract_emotion",
]