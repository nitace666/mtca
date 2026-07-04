"""src/recall/fog_protocol.py 测试：7 个核心行为。

按用户 prompt 列出的 7 个必测点：
1. test_clear_passes_through
2. test_fogged_once_returns_skeleton
3. test_fogged_once_transitions_to_archived
4. test_archived_excluded
5. test_silent_excluded
6. test_dormant_score_halved
7. test_supersede_announces

设计：纯函数协议，不依赖 DB；直接构造 dict 验证映射 / 过滤 / 标注逻辑。
"""

from __future__ import annotations

from src.recall.fog_protocol import (
    apply_fog_protocol,
    apply_supersede,
    filter_silent_segments,
)


# ---------------------------------------------------------------------------
# 测试 1: fog_state='clear' 原样保留
# ---------------------------------------------------------------------------


def test_clear_passes_through() -> None:
    """clear 状态的段应原样保留，messages 和 fog_state 都不变。"""
    results = [
        {
            "segment_id": "seg-clear-1",
            "fog_state": "clear",
            "messages": [
                {"role": "user", "content": "原内容 alpha"},
                {"role": "assistant", "content": "原内容 beta"},
            ],
        },
        {
            "segment_id": "seg-clear-2",
            # 缺省 fog_state 时按 clear 处理
            "messages": [{"role": "user", "content": "缺省态"}],
        },
    ]

    out = apply_fog_protocol(results)

    assert len(out) == 2
    assert out[0]["segment_id"] == "seg-clear-1"
    assert out[0]["fog_state"] == "clear"
    assert out[0]["messages"] == [
        {"role": "user", "content": "原内容 alpha"},
        {"role": "assistant", "content": "原内容 beta"},
    ]
    assert out[1]["segment_id"] == "seg-clear-2"
    # 缺省 fog_state 时函数按 clear 处理；输出保留原 dict
    assert out[1].get("fog_state", "clear") == "clear"
    assert out[1]["messages"] == [{"role": "user", "content": "缺省态"}]


# ---------------------------------------------------------------------------
# 测试 2: fogged_once 返回骨架消息
# ---------------------------------------------------------------------------


def test_fogged_once_returns_skeleton() -> None:
    """fogged_once 应把 messages 替换为「片段已删除（[fog_anchor]）」系统消息。"""
    results = [
        {
            "segment_id": "seg-fog-1",
            "fog_state": "fogged_once",
            "fog_anchor": "漫剧主角设定",
            "messages": [
                {"role": "user", "content": "被擦除的 L0 原文"},
            ],
        }
    ]

    out = apply_fog_protocol(results)

    assert len(out) == 1
    msgs = out[0]["messages"]
    assert isinstance(msgs, list) and len(msgs) == 1
    sys_msg = msgs[0]
    assert sys_msg["role"] == "system"
    assert "片段已删除" in sys_msg["content"]
    assert "漫剧主角设定" in sys_msg["content"]
    # 原 messages 已被擦除，不应出现
    assert all("被擦除的 L0 原文" not in (m.get("content") or "") for m in msgs)


# ---------------------------------------------------------------------------
# 测试 3: fogged_once 一次性转移（fog_state 标 archived）
# ---------------------------------------------------------------------------


def test_fogged_once_transitions_to_archived() -> None:
    """fogged_once 调用一次后，dict 的 fog_state 应标 'archived'；
    再调用 ``apply_fog_protocol`` 应被完全移除（一次性提示）。"""
    results = [
        {
            "segment_id": "seg-once",
            "fog_state": "fogged_once",
            "fog_anchor": "锚点句",
            "messages": [{"role": "user", "content": "x"}],
        }
    ]

    # ---- 第一次调用：fogged_once → archived ----
    out1 = apply_fog_protocol(results)
    assert len(out1) == 1
    assert out1[0]["fog_state"] == "archived"
    # 提示消息含 fog_anchor
    assert any("锚点句" in (m.get("content") or "") for m in out1[0]["messages"])

    # ---- 第二次调用：用第一次的输出作输入 → archived 段被移除 ----
    out2 = apply_fog_protocol(out1)
    assert out2 == []

    # ---- 入参未被修改（纯函数） ----
    assert results[0]["fog_state"] == "fogged_once"
    assert results[0]["messages"] == [{"role": "user", "content": "x"}]


# ---------------------------------------------------------------------------
# 测试 4: archived 状态从结果中移除
# ---------------------------------------------------------------------------


def test_archived_excluded() -> None:
    """fog_state='archived' 的段应完全从结果中移除（混合输入场景）。"""
    results = [
        {
            "segment_id": "seg-arch",
            "fog_state": "archived",
            "messages": [],
        },
        {
            "segment_id": "seg-clear",
            "fog_state": "clear",
            "messages": [{"role": "user", "content": "active content"}],
        },
        {
            "segment_id": "seg-arch-2",
            "fog_state": "archived",
            "messages": [{"role": "user", "content": "should not appear"}],
        },
    ]

    out = apply_fog_protocol(results)

    assert len(out) == 1
    assert out[0]["segment_id"] == "seg-clear"
    sids = [r["segment_id"] for r in out]
    assert "seg-arch" not in sids
    assert "seg-arch-2" not in sids


# ---------------------------------------------------------------------------
# 测试 5: silent 段被过滤
# ---------------------------------------------------------------------------


def test_silent_excluded() -> None:
    """silence_state='silent' 的段应被完全移除（不主动召回）。"""
    segments = [
        {"segment_id": "s-silent", "silence_state": "silent", "score": 80.0},
        {"segment_id": "s-active-1", "silence_state": "active", "score": 90.0},
        {"segment_id": "s-active-2", "silence_state": "active", "score": 70.0},
    ]

    out = filter_silent_segments(segments)

    sids = [s["segment_id"] for s in out]
    assert "s-silent" not in sids
    assert "s-active-1" in sids
    assert "s-active-2" in sids
    assert len(out) == 2
    # active 段分数不变
    s1 = next(s for s in out if s["segment_id"] == "s-active-1")
    assert s1["score"] == 90.0


# ---------------------------------------------------------------------------
# 测试 6: dormant 段 score × 0.5
# ---------------------------------------------------------------------------


def test_dormant_score_halved() -> None:
    """silence_state='dormant' 的段应保留且 score × 0.5；include_dormant=False 时过滤。"""
    segments = [
        {"segment_id": "d-100", "silence_state": "dormant", "score": 100.0},
        {"segment_id": "d-80", "silence_state": "dormant", "score": 80.0},
        {"segment_id": "a-90", "silence_state": "active", "score": 90.0},
    ]

    # ---- include_dormant=True（默认）----
    out = filter_silent_segments(segments)
    assert len(out) == 3

    d100 = next(s for s in out if s["segment_id"] == "d-100")
    d80 = next(s for s in out if s["segment_id"] == "d-80")
    a90 = next(s for s in out if s["segment_id"] == "a-90")

    assert d100["score"] == 50.0   # 100 × 0.5
    assert d80["score"] == 40.0    # 80 × 0.5
    assert a90["score"] == 90.0    # active 不变

    # ---- include_dormant=False → dormant 也被过滤 ----
    out2 = filter_silent_segments(segments, include_dormant=False)
    sids = [s["segment_id"] for s in out2]
    assert "d-100" not in sids
    assert "d-80" not in sids
    assert "a-90" in sids
    assert len(out2) == 1

    # ---- 入参未被修改（纯函数）----
    assert segments[0]["score"] == 100.0
    assert segments[1]["score"] == 80.0


# ---------------------------------------------------------------------------
# 测试 7: supersede 注解（A 移除、B 加注）
# ---------------------------------------------------------------------------


def test_supersede_announces() -> None:
    """A 被 B 取代：A 移除；B 在 messages 末尾追加 system 注解含日期。"""
    results = [
        {
            "segment_id": "seg-a",
            "superseded_by": "seg-b",
            "messages": [{"role": "user", "content": "旧方案正文"}],
        },
        {
            "segment_id": "seg-b",
            "supersedes_count": 1,
            "messages": [{"role": "user", "content": "新方案正文"}],
        },
        {
            "segment_id": "seg-c",
            # 未参与 supersede：原样
            "messages": [{"role": "user", "content": "无关段"}],
        },
    ]

    out = apply_supersede(results, today_iso="2026-07-05")

    # A 移除；B / C 保留
    sids = [r["segment_id"] for r in out]
    assert "seg-a" not in sids
    assert "seg-b" in sids
    assert "seg-c" in sids
    assert len(out) == 2

    # B 加注 system 消息，日期正确
    b = next(r for r in out if r["segment_id"] == "seg-b")
    msgs = b["messages"]
    assert isinstance(msgs, list)
    # 原消息保留
    assert any("新方案正文" in (m.get("content") or "") for m in msgs)
    # 注解追加在末尾
    last = msgs[-1]
    assert last["role"] == "system"
    assert "本方案取代了其他方案" in last["content"]
    assert "2026-07-05" in last["content"]

    # C 不参与 supersede：messages 不变
    c = next(r for r in out if r["segment_id"] == "seg-c")
    assert c["messages"] == [{"role": "user", "content": "无关段"}]

    # 入参未被修改（纯函数）
    assert results[0]["segment_id"] == "seg-a"
    assert len(results[0]["messages"]) == 1


# ---------------------------------------------------------------------------
# 额外测试：纯函数 + 入参不可变性
# ---------------------------------------------------------------------------


def test_pure_function_no_mutation() -> None:
    """三个函数均不应修改入参；空 list 输入应返回空 list。"""
    # ---- 空输入 ----
    assert apply_fog_protocol([]) == []
    assert filter_silent_segments([]) == []
    assert apply_supersede([]) == []

    # ---- 入参 list 长度不变 ----
    src = [
        {"segment_id": "x", "fog_state": "archived", "messages": []},
        {"segment_id": "y", "silence_state": "silent", "score": 1.0},
    ]
    apply_fog_protocol(src)
    apply_supersede(src)
    assert len(src) == 2

    # ---- 入参 dict 字段不变 ----
    sample = {
        "segment_id": "keep",
        "fog_state": "fogged_once",
        "fog_anchor": "abc",
        "messages": [{"role": "user", "content": "orig"}],
    }
    apply_fog_protocol([sample])
    assert sample["fog_state"] == "fogged_once"
    assert sample["messages"] == [{"role": "user", "content": "orig"}]