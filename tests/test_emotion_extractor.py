"""tests/test_emotion_extractor.py — M2.5.6 感情提取测试

覆盖范围：
- _extract_emotion_from_llm_output 解析层：clean json / markdown fence / 5 alias
  / 别名映射 / 无效值兑中性 / 缺字段 / 坏 JSON
  / confidence 错位锁 / 空字符串
- extract_emotion 集成层：mock provider / 空文本 / provider 不可用
  / provider 异常

总计≥1 0 个测试。
"""
from __future__ import annotations

import pytest

from src.llm.emotion_extractor import (
    _extract_emotion_from_llm_output,
    extract_emotion,
)


# =====================================================================
# _extract_emotion_from_llm_output 解析层 测试（≥6）
# =====================================================================

def test_parse_clean_json():
    """标准 schema：{"emotion": "positive", "confidence": 0.9}。"""
    emo, conf = _extract_emotion_from_llm_output('{"emotion": "positive", "confidence": 0.9}')
    assert emo == "positive"
    assert abs(conf - 0.9) < 0.01

def test_parse_markdown_fence():
    """markdown fence ```json\n{...}\n``` 能被拆。"""
    raw = '```json\n{"emotion": "anxious", "confidence": 0.7}\n```'
    emo, conf = _extract_emotion_from_llm_output(raw)
    assert emo == "anxious"
    assert abs(conf - 0.7) < 0.01

def test_parse_alias_emotion_tag():
    """alias字段 emotion_tag 能被识别。"""
    emo, _ = _extract_emotion_from_llm_output('{"emotion_tag": "excited", "confidence": 0.6}')
    assert emo == "excited"

def test_parse_alias_emotion_label():
    """alias字段 emotion_label + 别名映射 happy→positive。"""
    emo, conf = _extract_emotion_from_llm_output('{"emotion_label": "happy", "confidence": 0.8}')
    assert emo == "positive"
    assert abs(conf - 0.8) < 0.01

def test_parse_alias_feeling():
    """alias字段 feeling + 别名映射 sad→negative。"""
    emo, _ = _extract_emotion_from_llm_output('{"feeling": "sad", "confidence": 0.7}')
    assert emo == "negative"

def test_parse_alias_label():
    """alias字段 label 能被识别。"""
    emo, _ = _extract_emotion_from_llm_output('{"label": "anxious", "confidence": 0.5}')
    assert emo == "anxious"

def test_parse_alias_happy_maps_to_positive():
    """happy → positive 别名映射。"""
    emo, _ = _extract_emotion_from_llm_output('{"emotion": "happy", "confidence": 0.5}')
    assert emo == "positive"

def test_parse_alias_worried_maps_to_anxious():
    """worried → anxious 别名映射。"""
    emo, _ = _extract_emotion_from_llm_output('{"emotion": "worried", "confidence": 0.5}')
    assert emo == "anxious"

def test_parse_invalid_emotion_falls_back_to_neutral():
    """不在5标准 + 无别名 → neutral。"""
    emo, _ = _extract_emotion_from_llm_output('{"emotion": "confused", "confidence": 0.5}')
    assert emo == "neutral"

def test_parse_missing_emotion_returns_neutral():
    """缺字段 → neutral。"""
    emo, conf = _extract_emotion_from_llm_output('{"confidence": 0.5}')
    assert emo == "neutral"
    assert abs(conf - 0.5) < 0.01

def test_parse_malformed_json_returns_neutral():
    """坏 JSON → neutral。"""
    emo, _ = _extract_emotion_from_llm_output("garbage data {not json}")
    assert emo == "neutral"

def test_parse_clamps_confidence_above_1():
    """confidence > 1.0 → 锁到1.0。"""
    _, conf = _extract_emotion_from_llm_output('{"emotion": "neutral", "confidence": 1.5}')
    assert conf <= 1.0
    assert abs(conf - 1.0) < 0.01

def test_parse_clamps_confidence_below_0():
    """confidence < 0.0 → 锁到0.0。"""
    _, conf = _extract_emotion_from_llm_output('{"emotion": "neutral", "confidence": -0.5}')
    assert conf >= 0.0
    assert abs(conf - 0.0) < 0.01

def test_parse_empty_returns_neutral():
    """空字符串 → (neutral, 0.0)。"""
    emo, conf = _extract_emotion_from_llm_output("")
    assert emo == "neutral"
    assert conf == 0.0

def test_parse_alias_priority_emotion_first():
    """多 alias 同时出现，emotion 字段优先。"""
    emo, _ = _extract_emotion_from_llm_output(
        '{"emotion": "positive", "emotion_label": "sad", "confidence": 0.5}'
    )
    assert emo == "positive"


# =====================================================================
# extract_emotion 集成层 测试（≥2）
# =====================================================================

class _MockAvailableProvider:
    """mock 一个可用 provider，返回固定 JSON。"""

    def is_available(self) -> bool:
        return True

    def generate(self, prompt, **kwargs):
        return '{"emotion": "excited", "confidence": 0.9}'


class _MockUnavailableProvider:
    """mock 一个不可用 provider（is_available=False）。"""

    def is_available(self) -> bool:
        return False

    def generate(self, prompt, **kwargs):
        raise RuntimeError("provider unavailable should not be called")


class _MockRaisingProvider:
    """mock provider在 generate 时抛异常。"""

    def is_available(self) -> bool:
        return True

    def generate(self, prompt, **kwargs):
        raise RuntimeError("simulated LLM failure")


def test_extract_emotion_with_mock_provider():
    """mock 可用 provider → 返回 dict {emotion, confidence}。"""
    result = extract_emotion("今天的进展很顺利!", provider=_MockAvailableProvider())
    assert result is not None
    assert result["emotion"] == "excited"
    assert abs(result["confidence"] - 0.9) < 0.01

def test_extract_emotion_empty_text_returns_None():
    """空文本 → 不调 LLM，返回 None。"""
    result = extract_emotion("", provider=_MockAvailableProvider())
    assert result is None

def test_extract_emotion_whitespace_only_returns_None():
    """全空格文本 → None。"""
    result = extract_emotion("   \n\t  ", provider=_MockAvailableProvider())
    assert result is None

def test_extract_emotion_provider_unavailable_returns_None():
    """provider.is_available=False → None，不调 generate。"""
    result = extract_emotion("hello", provider=_MockUnavailableProvider())
    assert result is None

def test_extract_emotion_provider_exception_returns_None():
    """provider.generate 抛异常 → None（不影响主流程）。"""
    result = extract_emotion("hello", provider=_MockRaisingProvider())
    assert result is None

def test_extract_emotion_unknown_emotion_falls_back_to_neutral():
    """LLM 返回非 5 标准 + 无别名的 emotion
    → 被 _extract 兑成 neutral，extract 返回 {"emotion": "neutral", ...}。"""

    class _UnknownEmotionProvider:
        def is_available(self):
            return True

        def generate(self, prompt, **kwargs):
            return '{"emotion": "alien_emotion", "confidence": 0.5}'

    result = extract_emotion("hello", provider=_UnknownEmotionProvider())
    assert result is not None
    assert result["emotion"] == "neutral"
    assert abs(result["confidence"] - 0.5) < 0.01
