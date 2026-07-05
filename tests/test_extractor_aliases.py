"""测试 C1_fix1: schema 兼容性（content / fact / text / RDF 三元组）。"""
from src.llm.extractor import _extract_fact_text


def test_extract_text_field():
    """{"text": "..."} → 提取 text。"""
    result = _extract_fact_text({"text": "hello"})
    assert result == "hello"


def test_extract_fact_field_alias():
    """{"fact": "..."} → 提取 fact。"""
    assert _extract_fact_text({"fact": "world"}) == "world"


def test_extract_content_field():
    """{"content": "..."} → 提取 content。"""
    assert _extract_fact_text({"content": "standard"}) == "standard"


def test_extract_rdf_triple_basic():
    """{"subject": "...", "predicate": "...", "object": "..."} → "subject predicate object"。"""
    result = _extract_fact_text({
        "subject": "张三", "predicate": "居住于", "object": "北京"
    })
    assert result == "张三 居住于 北京"


def test_extract_rdf_triple_with_time():
    """加 time 字段 → "subject predicate object（time）"。"""
    result = _extract_fact_text({
        "subject": "李四", "predicate": "去", "object": "东京", "time": "去年3月"
    })
    assert result == "李四 去 东京（去年3月）"


def test_extract_empty_dict_returns_empty():
    """空 dict → ""。"""
    assert _extract_fact_text({}) == ""


def test_extract_partial_rdf_skipped():
    """RDF 缺字段 → 跳过（不是合法三元组）。"""
    assert _extract_fact_text({"subject": "x", "predicate": "y"}) == ""
    assert _extract_fact_text({"subject": "x", "object": "z"}) == ""
    assert _extract_fact_text({"predicate": "y", "object": "z"}) == ""


def test_extract_priority_content_over_rdf():
    """content 优先于 RDF（同 dict 同时有 2 种）。"""
    result = _extract_fact_text({
        "content": "优先取这个",
        "subject": "x", "predicate": "y", "object": "z"
    })
    assert result == "优先取这个"


def test_extract_priority_fact_over_text():
    """fact 优先于 text。"""
    result = _extract_fact_text({"fact": "first", "text": "second"})
    assert result == "first"


def test_extract_strip_whitespace():
    """文本字段值含前后空格 → strip。"""
    assert _extract_fact_text({"content": "  spaced  "}) == "spaced"