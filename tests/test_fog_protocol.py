"""src/recall/fog_protocol.py 测试：30 个核心行为。

T15 雾化测试：原 8 个 + 新增 22 个 = 30 个用例。

覆盖矩阵（按用户 T15 prompt 列出的 6 类场景）：
- 状态机：clear → fogged_once → archived 的完整转移（新增 4）
- fog 边界：anchor 缺失 / 空值 / 未知态 / 字段保留（新增 3）
- silence 边界：未知态 / 零分 / 非法分 / 缺省（新增 4）
- supersede 边界：零计数 / 多计数 / 多对一 / 链式 / 缺省日期（新增 5）
- 组合：fog×supersede × silence 叠加（新增 3）
- 边界：非 dict 输入 / 空输入 / 字段保留（新增 3）

注：用户 prompt 列出的「AI PermissionError / 7 天防骚扰」分属
fog_engine.py（T9） 与 lifecycle 引擎（T20/T21），不在本协议文件
范围内；其测试分别在 test_fog_engine.py 与未来 T26 文件中。

设计：纯函数协议，不依赖 DB；直接构造 dict 验证映射 / 过滤 / 标注。
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


# ===========================================================================
# T15 新增：状态机（clear → fogged_once → archived）完整转移
# ===========================================================================


def test_fog_state_machine_full_cycle() -> None:
    """clear → fogged_once（一次性提示）→ archived（永久归档）三态完整转移。

    场景：同一段依次经过 4 次 fog_protocol 调用，验证状态机演化路径：
        T0 clear      → 原样
        T1 fogged_once → 一次性返回骨架 + 标 archived
        T2 archived    → 被移除
        T3 archived    → 仍被移除（幂等稳定）
    """
    seg = {
        "segment_id": "seg-fsm",
        "fog_state": "clear",
        "fog_anchor": "状态机锚点",
        "messages": [
            {"role": "user", "content": "原始 L0"},
            {"role": "assistant", "content": "原始 L0 reply"},
        ],
    }

    # ---- T0：clear 态原样保留 ----
    out0 = apply_fog_protocol([dict(seg)])
    assert len(out0) == 1
    assert out0[0]["fog_state"] == "clear"
    assert len(out0[0]["messages"]) == 2

    # ---- T1：fogged_once 一次性提示 + 标 archived ----
    seg["fog_state"] = "fogged_once"
    out1 = apply_fog_protocol([dict(seg)])
    assert len(out1) == 1
    assert out1[0]["fog_state"] == "archived"
    msgs = out1[0]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert "状态机锚点" in msgs[0]["content"]
    # 原始消息已被擦除
    assert "原始 L0" not in msgs[0]["content"]

    # ---- T2：以 T1 输出（archived）再调用 → 完全移除 ----
    out2 = apply_fog_protocol(out1)
    assert out2 == []

    # ---- T3：archived 态输入仍被移除（幂等稳定）----
    archived_input = [{
        "segment_id": "seg-fsm",
        "fog_state": "archived",
        "fog_anchor": "状态机锚点",
        "messages": [{"role": "system", "content": "片段已删除"}],
    }]
    out3 = apply_fog_protocol(archived_input)
    assert out3 == []


def test_fogged_once_idempotent_second_call_drops() -> None:
    """fogged_once 第二次调用：第一次返回的 dict（archived）再次进入协议必被移除。"""
    first_pass = apply_fog_protocol([{
        "segment_id": "once",
        "fog_state": "fogged_once",
        "fog_anchor": "唯一锚点",
        "messages": [{"role": "user", "content": "x"}],
    }])
    # 第一次：返回 1 条（已 archived）
    assert len(first_pass) == 1
    assert first_pass[0]["fog_state"] == "archived"

    # 第二次：以第一次输出为入参 → 应被完全过滤（一次性提示语义）
    second_pass = apply_fog_protocol(first_pass)
    assert second_pass == []

    # 第三次：以 archived 直入 → 同样被过滤
    third_pass = apply_fog_protocol([{
        "segment_id": "once",
        "fog_state": "archived",
        "messages": [],
    }])
    assert third_pass == []


def test_fogged_once_preserves_unrelated_fields() -> None:
    """fogged_once 段除了 messages / fog_state 改写，其他字段应原样保留。"""
    seg = {
        "segment_id": "keep-others",
        "fog_state": "fogged_once",
        "fog_anchor": "锚点",
        "session_id": "sess-1",
        "topic_label": "项目骨架",
        "start_at": 1700000000000,
        "current_score": 88.5,
        "current_tier": "L0",
        "superseded_by": None,
        "supersedes_count": 0,
        "silence_state": "active",
        "extra_meta": {"user_tag": "important"},
    }

    out = apply_fog_protocol([dict(seg)])

    assert len(out) == 1
    r = out[0]
    # 必改字段
    assert r["fog_state"] == "archived"
    assert r["messages"] == [{
        "role": "system",
        "content": "片段已删除（锚点）",
    }]
    # 必保字段
    assert r["segment_id"] == "keep-others"
    assert r["session_id"] == "sess-1"
    assert r["topic_label"] == "项目骨架"
    assert r["start_at"] == 1700000000000
    assert r["current_score"] == 88.5
    assert r["current_tier"] == "L0"
    assert r["superseded_by"] is None
    assert r["supersedes_count"] == 0
    assert r["silence_state"] == "active"
    assert r["extra_meta"] == {"user_tag": "important"}


def test_archived_state_persistent_marker() -> None:
    """archived 状态应被协议持续识别并过滤（持久化标记语义）。"""
    # 输入始终标 archived 的段：协议视为已归档，直接跳过
    archived_seg = {
        "segment_id": "perm-arch",
        "fog_state": "archived",
        "messages": [{"role": "user", "content": "不应出现"}],
    }

    # 多次独立调用，结果稳定为空
    for _ in range(5):
        out = apply_fog_protocol([dict(archived_seg)])
        assert out == []


# ===========================================================================
# T15 新增：apply_fog_protocol 边界
# ===========================================================================


def test_fogged_once_missing_anchor_fallback() -> None:
    """fogged_once 段 fog_anchor 字段缺失（None）应使用兜底文案「已删除」。"""
    seg = {
        "segment_id": "no-anchor",
        "fog_state": "fogged_once",
        "fog_anchor": None,
        "messages": [{"role": "user", "content": "原内容"}],
    }

    out = apply_fog_protocol([seg])

    assert len(out) == 1
    msg = out[0]["messages"][0]
    assert msg["role"] == "system"
    assert "已删除" in msg["content"]
    assert msg["content"].startswith("片段已删除（")
    assert msg["content"].endswith("）")


def test_fogged_once_empty_anchor_fallback() -> None:
    """fogged_once 段 fog_anchor 是空串 / 纯空白应回落兜底文案。"""
    for bad in ("", "   ", "\t", "\n"):
        seg = {
            "segment_id": f"empty-{hash(bad)}",
            "fog_state": "fogged_once",
            "fog_anchor": bad,
            "messages": [],
        }
        out = apply_fog_protocol([seg])
        assert len(out) == 1
        msg = out[0]["messages"][0]
        assert "已删除" in msg["content"], f"anchor={bad!r} 时应兜底"


def test_unknown_fog_state_treated_as_clear() -> None:
    """未知 fog_state（如 'pending' / 'foo'）按 clear 处理，原样保留。"""
    for bad_state in ("pending", "foo", "frozen", "", "CLEAR"):
        seg = {
            "segment_id": f"u-{bad_state}",
            "fog_state": bad_state,
            "messages": [{"role": "user", "content": "原"}],
        }
        out = apply_fog_protocol([seg])
        assert len(out) == 1
        # 未知态原样返回（state 不被改写）
        assert out[0]["fog_state"] == bad_state
        assert out[0]["messages"] == [{"role": "user", "content": "原"}]


# ===========================================================================
# T15 新增：filter_silent_segments 边界
# ===========================================================================


def test_unknown_silence_state_treated_as_active() -> None:
    """未知 silence_state（如 'pending' / 'archived_silence'）按 active 保留。"""
    for bad_state in ("pending", "frozen", "", "ACTIVE", None):
        seg = {"segment_id": "u-s", "silence_state": bad_state, "score": 42.0}
        out = filter_silent_segments([seg])
        assert len(out) == 1
        assert out[0]["score"] == 42.0   # 未被 ×0.5
        assert out[0]["silence_state"] == bad_state


def test_dormant_zero_score_still_zero() -> None:
    """dormant 段 score=0：×0.5 后仍为 0（不应产生 -0.0 或 NaN）。"""
    seg = {"segment_id": "z", "silence_state": "dormant", "score": 0}
    out = filter_silent_segments([seg])
    assert len(out) == 1
    assert out[0]["score"] == 0


def test_dormant_invalid_score_falls_back_to_zero() -> None:
    """dormant 段 score 是非法值（None / 'x'）应回落为 0，再 ×0.5。"""
    cases = [
        {"segment_id": "i1", "silence_state": "dormant", "score": None},
        {"segment_id": "i2", "silence_state": "dormant", "score": "x"},
        {"segment_id": "i3", "silence_state": "dormant"},  # 缺省
    ]
    out = filter_silent_segments(cases)
    assert len(out) == 3
    for s in out:
        # 非法 / 缺省 → _to_float 回落到 0 → 0 × 0.5 = 0
        assert s["score"] == 0


def test_dormant_default_include_dormant_true() -> None:
    """filter_silent_segments 默认 include_dormant=True；显式传 True 行为一致。"""
    seg = {"segment_id": "d", "silence_state": "dormant", "score": 100.0}
    out_default = filter_silent_segments([seg])
    out_explicit = filter_silent_segments([seg], include_dormant=True)
    assert out_default[0]["score"] == 50.0
    assert out_explicit[0]["score"] == 50.0


# ===========================================================================
# T15 新增：apply_supersede 边界
# ===========================================================================


def test_supersede_zero_count_no_announcement() -> None:
    """supersedes_count=0 时不追加 system 注解（避免给未取代者贴标签）。"""
    seg = {
        "segment_id": "z-b",
        "supersedes_count": 0,
        "messages": [{"role": "user", "content": "原内容"}],
    }
    out = apply_supersede([seg], today_iso="2026-07-05")
    assert len(out) == 1
    # messages 应保持原样，未追加 system 注解
    assert out[0]["messages"] == [{"role": "user", "content": "原内容"}]


def test_supersede_multiple_count_announces_once() -> None:
    """supersedes_count=5 也只追加一条 system 注解（不是 5 条）。"""
    seg = {
        "segment_id": "multi",
        "supersedes_count": 5,
        "messages": [{"role": "user", "content": "新方案"}],
    }
    out = apply_supersede([seg], today_iso="2026-07-05")
    assert len(out) == 1
    sys_msgs = [
        m for m in out[0]["messages"]
        if m.get("role") == "system"
        and "本方案取代了其他方案" in (m.get("content") or "")
    ]
    assert len(sys_msgs) == 1, "supersedes_count>0 应只追加 1 条 system 注解"
    assert "2026-07-05" in sys_msgs[0]["content"]


def test_supersede_multiple_paragraphs_to_same_replacement() -> None:
    """A1 与 A2 同时被 B 取代：A1、A2 都移除，B 加注 1 条注解。"""
    results = [
        {"segment_id": "a1", "superseded_by": "b", "messages": []},
        {"segment_id": "a2", "superseded_by": "b", "messages": []},
        {
            "segment_id": "b",
            "supersedes_count": 2,
            "messages": [{"role": "user", "content": "终版"}],
        },
    ]
    out = apply_supersede(results, today_iso="2026-07-05")
    sids = [r["segment_id"] for r in out]
    assert "a1" not in sids
    assert "a2" not in sids
    assert "b" in sids
    assert len(out) == 1

    # B 只追加一条注解（不是 2 条）
    sys_msgs = [
        m for m in out[0]["messages"]
        if m.get("role") == "system"
        and "本方案取代了其他方案" in (m.get("content") or "")
    ]
    assert len(sys_msgs) == 1


def test_supersede_chain() -> None:
    """链式取代：A→B→C；只 C 应保留，A 与 B 都移除（C 不再被取代）。"""
    results = [
        {"segment_id": "a", "superseded_by": "b", "messages": []},
        {
            "segment_id": "b",
            "superseded_by": "c",
            "supersedes_count": 1,
            "messages": [{"role": "user", "content": "中间版"}],
        },
        {
            "segment_id": "c",
            "supersedes_count": 1,
            "messages": [{"role": "user", "content": "终版"}],
        },
    ]
    out = apply_supersede(results, today_iso="2026-07-05")
    sids = [r["segment_id"] for r in out]
    assert "a" not in sids
    assert "b" not in sids
    assert "c" in sids
    assert len(out) == 1

    # C 加注 1 条 system 注解
    sys_msgs = [
        m for m in out[0]["messages"]
        if m.get("role") == "system"
    ]
    assert len(sys_msgs) == 1
    assert "2026-07-05" in sys_msgs[0]["content"]


def test_supersede_default_today_iso() -> None:
    """today_iso=None 应使用今日日期（验证格式而非具体值）。"""
    seg = {
        "segment_id": "today",
        "supersedes_count": 1,
        "messages": [{"role": "user", "content": "新"}],
    }
    out = apply_supersede([seg], today_iso=None)
    assert len(out) == 1
    sys_msg = out[0]["messages"][-1]
    assert sys_msg["role"] == "system"
    # 形如 "本方案取代了其他方案（YYYY-MM-DD）"
    import re as _re
    assert _re.search(r"\d{4}-\d{2}-\d{2}", sys_msg["content"])


# ===========================================================================
# T15 新增：组合（fog × supersede × silence 叠加）
# ===========================================================================


def test_fog_then_supersede_compose() -> None:
    """先 apply_fog_protocol → 再 apply_supersede：fogged_once 标 archived 后，
    archived 段若被 supersede 取代仍应消失；clear + 被 supersede 的段按 supersede 移除。"""
    raw = [
        # 雾化段 + 被 B 取代 → 应消失（archived 优先被 fog 过滤）
        {
            "segment_id": "a",
            "fog_state": "archived",
            "superseded_by": "b",
            "messages": [],
        },
        # 雾化段（fogged_once → archived）+ 自身取代者（supersedes_count=1）
        {
            "segment_id": "b",
            "fog_state": "fogged_once",
            "fog_anchor": "B 锚点",
            "supersedes_count": 1,
            "messages": [{"role": "user", "content": "原"}],
        },
        # 透明段
        {
            "segment_id": "c",
            "fog_state": "clear",
            "messages": [{"role": "user", "content": "无关"}],
        },
    ]

    step1 = apply_fog_protocol(raw)
    # 雾化后：a 已 archived（fog 第一步移除）；b → archived + 骨架；
    # c 原样
    sids1 = [r["segment_id"] for r in step1]
    assert "a" not in sids1
    assert "b" in sids1
    assert "c" in sids1
    # b 在 fog 协议后应标 archived
    b_after_fog = next(r for r in step1 if r["segment_id"] == "b")
    assert b_after_fog["fog_state"] == "archived"

    step2 = apply_supersede(step1, today_iso="2026-07-05")
    # 第二步：a 已被 fog 移除（不进入结果）；b 因 supersedes_count=1
    # 仍会被 supersede 加注 system 注解（supersede 看的是字段，与 fog 顺序无关）
    sids2 = [r["segment_id"] for r in step2]
    assert "b" in sids2
    assert "c" in sids2
    assert len(step2) == 2
    # b 的 messages：fog 骨架（system「片段已删除（B 锚点）」）+ supersede 注解
    b_msgs = next(r for r in step2 if r["segment_id"] == "b")["messages"]
    fog_skel = [m for m in b_msgs if "片段已删除" in (m.get("content") or "")]
    sup_note = [m for m in b_msgs if "本方案取代了其他方案" in (m.get("content") or "")]
    assert len(fog_skel) == 1
    assert len(sup_note) == 1
    # supersede 注解在 fog 骨架之后追加
    assert b_msgs.index(sup_note[0]) > b_msgs.index(fog_skel[0])
    # c 的 messages 原样
    c_msgs = next(r for r in step2 if r["segment_id"] == "c")["messages"]
    assert c_msgs == [{"role": "user", "content": "无关"}]


def test_supersede_then_fog_compose() -> None:
    """先 apply_supersede → 再 apply_fog_protocol：supersede 移除被取代段，
    剩下的 fogged_once 段再走一次性骨架提示。"""
    raw = [
        {"segment_id": "a", "superseded_by": "b", "messages": []},
        {
            "segment_id": "b",
            "supersedes_count": 1,
            "fog_state": "fogged_once",
            "fog_anchor": "B 锚点",
            "messages": [{"role": "user", "content": "原"}],
        },
    ]

    step1 = apply_supersede(raw, today_iso="2026-07-05")
    # a 被移除；b 保留 + 加注
    assert [r["segment_id"] for r in step1] == ["b"]
    assert any(
        "本方案取代了其他方案" in (m.get("content") or "")
        for m in step1[0]["messages"]
    )

    step2 = apply_fog_protocol(step1)
    # b 被雾化：messages 被替换为骨架；supersede 注解随之消失（被骨架覆盖）
    assert len(step2) == 1
    assert step2[0]["fog_state"] == "archived"
    msgs = step2[0]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert "B 锚点" in msgs[0]["content"]
    assert not any(
        "本方案取代了其他方案" in (m.get("content") or "")
        for m in msgs
    )


def test_fog_and_silence_combined() -> None:
    """混合段同时表达 fog_state 与 silence_state：
    - clear + active → 保留
    - fogged_once + dormant → fog 替换 + dormant ×0.5（fog 优先，骨架消息不参与分数）
    - archived + silent → 被 fog 过滤（fog 在前时）
    - clear + silent → 被 silence 过滤（silence 在前时）
    """
    raw = [
        {"segment_id": "1", "fog_state": "clear",
         "silence_state": "active", "score": 80.0,
         "messages": [{"role": "user", "content": "ok"}]},
        {"segment_id": "2", "fog_state": "fogged_once",
         "fog_anchor": "A2", "silence_state": "dormant",
         "score": 100.0, "messages": [{"role": "user", "content": "x"}]},
        {"segment_id": "3", "fog_state": "archived",
         "silence_state": "silent", "score": 50.0, "messages": []},
        {"segment_id": "4", "fog_state": "clear",
         "silence_state": "silent", "score": 60.0,
         "messages": [{"role": "user", "content": "y"}]},
    ]

    # 先 fog → 再 silence（典型流水线顺序）
    after_fog = apply_fog_protocol(raw)
    sids = [r["segment_id"] for r in after_fog]
    assert "3" not in sids                # archived 被 fog 过滤
    assert "2" in sids                    # fogged_once 留（骨架）
    assert "1" in sids
    assert "4" in sids

    # 注：silence 字段在 fog 输出中原样保留；silence 协议对 messages
    # 不感知，只对 score 做处理
    seg2 = next(r for r in after_fog if r["segment_id"] == "2")
    assert seg2["silence_state"] == "dormant"
    assert seg2["score"] == 100.0   # fog 协议不修改 score

    after_silence = filter_silent_segments(after_fog)
    final_sids = [r["segment_id"] for r in after_silence]
    # silent（4）被移除；dormant（2）保留 + ×0.5；active（1）保留
    assert "4" not in final_sids
    assert "2" in final_sids
    assert "1" in final_sids
    seg2_final = next(r for r in after_silence if r["segment_id"] == "2")
    assert seg2_final["score"] == 50.0   # 100 × 0.5


# ===========================================================================
# T15 新增：通用边界（非 dict / 空 / 字段保留）
# ===========================================================================


def test_non_dict_items_in_results_are_skipped() -> None:
    """results 中混入 None / str / int 等非 dict 元素应被跳过而不抛异常。"""
    raw = [
        None,                                       # 非 dict
        "string-item",                              # 非 dict
        42,                                         # 非 dict
        {"segment_id": "valid", "fog_state": "clear",
         "messages": [{"role": "user", "content": "ok"}]},
        ["nested", "list"],                         # 非 dict
    ]

    out_fog = apply_fog_protocol(raw)
    assert len(out_fog) == 1
    assert out_fog[0]["segment_id"] == "valid"

    out_silence = filter_silent_segments(raw)  # type: ignore[arg-type]
    assert len(out_silence) == 1
    assert out_silence[0]["segment_id"] == "valid"

    out_super = apply_supersede(raw)  # type: ignore[arg-type]
    # None / str / int / list 全部跳过；valid 段不参与 supersede，原样保留
    assert len(out_super) == 1
    assert out_super[0]["segment_id"] == "valid"


def test_empty_inputs_return_empty() -> None:
    """所有三个函数对空 list 输入应返回空 list（不抛异常）。"""
    assert apply_fog_protocol([]) == []
    assert filter_silent_segments([]) == []
    assert apply_supersede([]) == []

    # 显式 None 不会让函数走到 list 路径（按类型契约），传入 [] 是合法调用
    assert apply_fog_protocol([]).__class__ is list
    assert filter_silent_segments([]).__class__ is list
    assert apply_supersede([]).__class__ is list


def test_all_three_protocols_preserve_unrelated_fields() -> None:
    """三个协议函数都应原样保留段 dict 的非目标字段（不破坏其他 metadata）。"""
    rich_seg = {
        "segment_id": "rich",
        "session_id": "sess-X",
        "topic_label": "project骨架",
        "fog_anchor": "锚点",
        "fog_state": "fogged_once",
        "silence_state": "dormant",
        "current_score": 75.0,
        "current_tier": "L0",
        "start_at": 1700000000000,
        "end_at": 1700003600000,
        "superseded_by": None,
        "supersedes_count": 0,
        "ref_count": 3,
        "user_meta": {"tag": "vip"},
        "messages": [{"role": "user", "content": "orig"}],
    }

    # ---- apply_fog_protocol：保留除 messages / fog_state 外所有字段 ----
    after_fog = apply_fog_protocol([dict(rich_seg)])[0]
    assert after_fog["session_id"] == "sess-X"
    assert after_fog["topic_label"] == "project骨架"
    assert after_fog["fog_anchor"] == "锚点"
    assert after_fog["silence_state"] == "dormant"
    assert after_fog["current_score"] == 75.0
    assert after_fog["current_tier"] == "L0"
    assert after_fog["start_at"] == 1700000000000
    assert after_fog["ref_count"] == 3
    assert after_fog["user_meta"] == {"tag": "vip"}
    # 改写字段
    assert after_fog["fog_state"] == "archived"
    assert after_fog["messages"] != [{"role": "user", "content": "orig"}]

    # ---- apply_supersede：保留所有字段，仅 messages 可能追加 ----
    rich_seg["supersedes_count"] = 2
    after_super = apply_supersede([dict(rich_seg)], today_iso="2026-07-05")[0]
    assert after_super["segment_id"] == "rich"
    assert after_super["session_id"] == "sess-X"
    assert after_super["topic_label"] == "project骨架"
    assert after_super["current_tier"] == "L0"
    assert after_super["ref_count"] == 3
    assert after_super["user_meta"] == {"tag": "vip"}
    # 原 messages 保留
    assert any(
        m.get("content") == "orig"
        for m in after_super["messages"]
    )
    # 注解追加
    assert any(
        "本方案取代了其他方案" in (m.get("content") or "")
        for m in after_super["messages"]
    )

    # ---- filter_silent_segments：仅 score 可能变化（×0.5）----
    # 注：协议只读 ``score`` 字段；与 ``current_score`` 是不同字段（DB 段级 vs 协议段）
    silence_input = dict(rich_seg)
    silence_input["score"] = 75.0
    after_silence = filter_silent_segments([silence_input])[0]
    assert after_silence["segment_id"] == "rich"
    assert after_silence["topic_label"] == "project骨架"
    assert after_silence["current_tier"] == "L0"
    assert after_silence["ref_count"] == 3
    assert after_silence["user_meta"] == {"tag": "vip"}
    # dormant: score ×0.5（75 → 37.5）
    assert after_silence["score"] == 37.5
    # messages / fog_state / silence_state 原样
    assert after_silence["messages"] == [{"role": "user", "content": "orig"}]
    assert after_silence["fog_state"] == "fogged_once"
    assert after_silence["silence_state"] == "dormant"


# ===========================================================================
# T15 新增：同段多次召回 / 多次归档（边界）
# ===========================================================================


def test_same_segment_multiple_recalls_consistent() -> None:
    """同段在多次 apply_fog_protocol 调用下行为一致：第一次一次性提示，
    之后归档态稳定过滤（幂等）。"""
    seg = {
        "segment_id": "multi-recall",
        "fog_state": "fogged_once",
        "fog_anchor": "锚",
        "messages": [{"role": "user", "content": "x"}],
    }

    results_seq: list[list[dict]] = []
    current = [dict(seg)]
    for i in range(4):
        current = apply_fog_protocol(current)
        results_seq.append(current)

    # 第 1 次：返回 1 条（archived + 骨架）
    assert len(results_seq[0]) == 1
    assert results_seq[0][0]["fog_state"] == "archived"

    # 第 2 次起：空
    for i in (1, 2, 3):
        assert results_seq[i] == [], f"第 {i + 1} 次召回应为空"


def test_same_segment_archive_via_multiple_paths() -> None:
    """同一段可通过两条独立路径归档：(A) fogged_once 自然转移；
    (B) 直接标 archived 直入。两条路径都应被协议一致过滤。"""
    # 路径 A：fogged_once 走 1 次
    path_a = apply_fog_protocol([{
        "segment_id": "x",
        "fog_state": "fogged_once",
        "fog_anchor": "A",
        "messages": [],
    }])
    # 再走 1 次（用 path_a 作输入）→ 应消失
    path_a_final = apply_fog_protocol(path_a)
    assert path_a_final == []

    # 路径 B：直接 archived 入参
    path_b = apply_fog_protocol([{
        "segment_id": "x",
        "fog_state": "archived",
        "messages": [{"role": "system", "content": "片段已删除"}],
    }])
    assert path_b == []

    # 两条路径最终结果一致：空列表
    assert path_a_final == path_b


def test_multiple_fogged_once_segments_independent() -> None:
    """多段同时 fogged_once：每段独立处理，互不干扰（每段都返回独立骨架）。"""
    raw = [
        {"segment_id": "f1", "fog_state": "fogged_once",
         "fog_anchor": "锚1", "messages": []},
        {"segment_id": "f2", "fog_state": "fogged_once",
         "fog_anchor": "锚2", "messages": []},
        {"segment_id": "f3", "fog_state": "fogged_once",
         "fog_anchor": "锚3", "messages": []},
    ]

    out = apply_fog_protocol(raw)

    assert len(out) == 3
    for r in out:
        assert r["fog_state"] == "archived"
        assert r["messages"][0]["role"] == "system"
    # 每段骨架内容含各自锚点
    anchors = [r["messages"][0]["content"] for r in out]
    assert any("锚1" in a for a in anchors)
    assert any("锚2" in a for a in anchors)
    assert any("锚3" in a for a in anchors)