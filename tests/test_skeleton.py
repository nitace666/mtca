"""src/l0/skeleton.py 测试：5 个核心行为 + 3 个边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.l0.skeleton import (
    ANCHOR_MAX_LEN,
    STOPWORDS,
    build_skeleton,
    extract_keywords,
    generate_anchor,
    save_skeleton,
)
from src.l0.session_writer import create_session, write_message
from src.store.sqlite import query


# ---------------------------------------------------------------------------
# 测试 1：generate_anchor 截断到 ≤ 20 字
# ---------------------------------------------------------------------------


def test_generate_anchor_short() -> None:
    """generate_anchor 应把超长 user 消息截断到 ≤ ANCHOR_MAX_LEN。"""
    long_msg = (
        "我们今天讨论的主题是关于 MTCA 项目在 2026 年下半年的开发"
        "路线图与里程碑规划"
    )
    messages = [{"role": "user", "content": long_msg, "seq": 1}]
    anchor = generate_anchor(messages)
    assert isinstance(anchor, str)
    assert len(anchor) <= ANCHOR_MAX_LEN
    assert len(anchor) > 0


# ---------------------------------------------------------------------------
# 测试 2：generate_anchor 处理中文优先于英文 / 取首条 user
# ---------------------------------------------------------------------------


def test_generate_anchor_chinese() -> None:
    """中文内容应正确取首条 user 消息并截断。"""
    messages = [
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "请帮我设计一个本地优先的长期记忆中间件方案"},
        {"role": "assistant", "content": "好的，我先了解一下你的需求..."},
    ]
    anchor = generate_anchor(messages)
    assert isinstance(anchor, str)
    # 优先取 user 消息而不是 system
    assert "记忆" in anchor or "中间件" in anchor or "设计" in anchor
    assert len(anchor) <= ANCHOR_MAX_LEN


# ---------------------------------------------------------------------------
# 测试 3：extract_keywords 过滤停用词
# ---------------------------------------------------------------------------


def test_extract_keywords_filters_stopwords() -> None:
    """提取的关键词中不应包含 STOPWORDS 中的词。"""
    messages = [
        {"role": "user", "content": "我们的项目是关于 MTCA 的长期记忆系统"},
        {"role": "assistant", "content": "MTCA 看起来是一个本地优先的存储方案"},
        {"role": "user", "content": "对，MTCA 关注用户主权和不可变性"},
    ]
    kws = extract_keywords(messages, top_k=5)
    assert isinstance(kws, list)
    # 停用词 "的 / 是 / 我" 等不应出现
    for kw in kws:
        assert kw not in STOPWORDS, f"停用词 '{kw}' 不应出现在关键词中"
    # 至少包含有意义的领域词
    assert "MTCA" in kws or "记忆" in kws or "项目" in kws


# ---------------------------------------------------------------------------
# 测试 4：build_skeleton 返回 dict
# ---------------------------------------------------------------------------


def test_build_skeleton_returns_dict(mtca_db: Path) -> None:
    """build_skeleton 应返回包含 anchor/keywords/message_count/time_range 的 dict。"""
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "今天我们讨论 MTCA 的骨架设计", path=mtca_db)
    write_message(sid, "assistant", "好的，我来分析锚点与关键词", path=mtca_db)

    skel = build_skeleton(sid, path=mtca_db)

    assert isinstance(skel, dict)
    assert set(skel.keys()) >= {
        "anchor", "keywords", "message_count", "time_range", "topic_label",
    }
    assert isinstance(skel["anchor"], str)
    assert isinstance(skel["keywords"], list)
    assert skel["message_count"] == 2
    assert isinstance(skel["time_range"], tuple)
    assert len(skel["time_range"]) == 2
    assert skel["time_range"][0] > 0
    assert skel["time_range"][1] >= skel["time_range"][0]


# ---------------------------------------------------------------------------
# 测试 5：save_skeleton 写库到 segments 表
# ---------------------------------------------------------------------------


def test_save_skeleton_writes_to_db(mtca_db: Path) -> None:
    """save_skeleton 后 segments 表应有 1 条记录，fog_anchor=anchor。"""
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "测试一下锚点句的落库", path=mtca_db)
    write_message(sid, "assistant", "好的", path=mtca_db)

    skel = build_skeleton(sid, path=mtca_db)
    seg_id = save_skeleton(sid, skel, path=mtca_db)

    assert isinstance(seg_id, str) and len(seg_id) >= 32

    rows = query(
        "SELECT segment_id, session_id, topic_label, fog_anchor, "
        "start_msg_seq, end_msg_seq, start_at, end_at "
        "FROM segments WHERE session_id = ?",
        (sid,), path=mtca_db,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["segment_id"] == seg_id
    assert row["session_id"] == sid
    assert row["fog_anchor"] == skel["anchor"]
    assert row["topic_label"] == skel["anchor"]
    assert row["end_msg_seq"] == skel["message_count"]
    assert row["start_at"] == skel["time_range"][0]
    assert row["end_at"] == skel["time_range"][1]


# ---------------------------------------------------------------------------
# 测试 6（边界）：空消息列表
# ---------------------------------------------------------------------------


def test_generate_anchor_empty_messages() -> None:
    """空消息列表应返回空串而非抛异常。"""
    assert generate_anchor([]) == ""
    # 无 user 角色时回退到首条任意角色
    assert generate_anchor([{"role": "system", "content": ""}]) == ""


def test_extract_keywords_no_meaningful() -> None:
    """全停用词消息应返回空列表。"""
    msgs = [{"role": "user", "content": "的了是在不"}]
    assert extract_keywords(msgs, top_k=5) == []
    # top_k=0 直接返回空
    assert extract_keywords(msgs, top_k=0) == []


# ---------------------------------------------------------------------------
# 测试 7（边界）：save_skeleton 幂等（L0-骨架不可变）
# ---------------------------------------------------------------------------


def test_save_skeleton_is_idempotent(mtca_db: Path) -> None:
    """对同一 session 二次 save_skeleton 不应再插入新段。"""
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "幂等性测试消息", path=mtca_db)

    skel1 = build_skeleton(sid, path=mtca_db)
    seg_id_1 = save_skeleton(sid, skel1, path=mtca_db)

    # 修改 anchor 再 save（应被忽略，因骨架不可变）
    skel2 = dict(skel1)
    skel2["anchor"] = "完全不同的新锚点句"
    seg_id_2 = save_skeleton(sid, skel2, path=mtca_db)

    assert seg_id_1 == seg_id_2  # 返回同一段 ID
    rows = query(
        "SELECT fog_anchor FROM segments WHERE session_id = ?",
        (sid,), path=mtca_db,
    )
    assert len(rows) == 1
    assert rows[0]["fog_anchor"] == skel1["anchor"]  # 旧 anchor 保留


# ---------------------------------------------------------------------------
# 测试 8（边界）：save_skeleton 字段缺失抛 ValueError
# ---------------------------------------------------------------------------


def test_save_skeleton_rejects_invalid_skeleton(mtca_db: Path) -> None:
    """skeleton 字段缺失应抛 ValueError，segments 表无写入。"""
    sid = create_session(path=mtca_db)
    with pytest.raises(ValueError):
        save_skeleton(sid, {"anchor": "x"}, path=mtca_db)  # 缺 time_range
    with pytest.raises(ValueError):
        save_skeleton(sid, {"time_range": (1, 2)}, path=mtca_db)  # 缺 anchor
    with pytest.raises(ValueError):
        save_skeleton(sid, {"anchor": "x", "time_range": "bad"}, path=mtca_db)

    rows = query(
        "SELECT COUNT(*) AS n FROM segments WHERE session_id = ?",
        (sid,), path=mtca_db,
    )
    assert rows[0]["n"] == 0
