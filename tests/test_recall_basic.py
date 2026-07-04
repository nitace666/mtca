"""src/recall/recall_engine.py 测试：8 个核心行为（剩余 T14 补齐 50 段用例）。

按用户 prompt 列出的 8 个必测点：
1. test_recall_finds_by_keyword
2. test_recall_finds_by_topic
3. test_recall_combines_channels
4. test_recall_expands_neighbors
5. test_recall_falls_back_to_recent
6. test_recall_respects_time_window
7. test_recall_rerank_by_relevance
8. test_recall_returns_l0_messages

附 2 个直接通道验证：
- test_search_sessions_directly
- test_search_segments_directly
"""

from __future__ import annotations

import time as _time
import uuid
from pathlib import Path

import pytest

from src.l0.skeleton import build_skeleton, save_skeleton
from src.l0.session_writer import create_session, write_message
from src.recall.recall_engine import (
    expand_neighbors,
    recall,
    recall_with_fallback,
    rerank,
    search_segments,
    search_sessions,
)
from src.store.sqlite import execute

# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------


def _make_session_with_skeleton(
    mtca_db: Path,
    user_msg: str,
    asst_msg: str,
    topic_label: str | None = None,
) -> tuple[str, str]:
    """建会话 + 写 2 条消息 + 落骨架，返回 (session_id, segment_id)。

    骨架段是 L0-骨架（start_msg_seq=1, end_msg_seq=msg_count），
    消息自动通过 messages_fts_ai 触发器进入 FTS5 索引。
    """
    sid = create_session(path=mtca_db)
    write_message(sid, "user", user_msg, path=mtca_db)
    write_message(sid, "assistant", asst_msg, path=mtca_db)
    skel = build_skeleton(sid, path=mtca_db)
    if topic_label is not None:
        skel["topic_label"] = topic_label
    seg_id = save_skeleton(sid, skel, path=mtca_db)
    return sid, seg_id


def _make_raw_segment(
    mtca_db: Path,
    session_id: str,
    start_at_ms: int,
    end_at_ms: int,
    topic_label: str | None = None,
    current_score: float = 100.0,
) -> str:
    """直接 INSERT 一段（不依赖时间分段器、无消息）。返回 segment_id。"""
    seg_id = str(uuid.uuid4())
    execute(
        "INSERT INTO segments "
        "(segment_id, session_id, start_msg_seq, end_msg_seq, "
        "start_at, end_at, topic_label, fog_anchor, current_score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            seg_id,
            session_id,
            1, 1,
            start_at_ms,
            end_at_ms,
            topic_label,
            topic_label,
            current_score,
        ),
        path=mtca_db,
    )
    return seg_id


# ---------------------------------------------------------------------------
# 测试 1：FTS5 关键词召回
# ---------------------------------------------------------------------------


def test_recall_finds_by_keyword(mtca_db: Path) -> None:
    """recall('python') 应找到包含 'python' 关键词的会话段（L0-细节 FTS5 通道）。"""
    sid_a, seg_a = _make_session_with_skeleton(
        mtca_db,
        user_msg="今天来聊 python 编程语言的设计哲学",
        asst_msg="Python 的优雅在于其简洁和明确",
        topic_label="Python学习",
    )
    _make_session_with_skeleton(
        mtca_db,
        user_msg="今天讨论 JavaScript 的事件循环",
        asst_msg="JS 是单线程基于事件循环的",
        topic_label="JS学习",
    )

    results = recall("python", path=mtca_db, top_k=5)

    assert isinstance(results, list)
    assert len(results) >= 1
    seg_ids = [r["segment_id"] for r in results]
    assert seg_a in seg_ids
    # 召回应返回该段的 L0 原文（messages 非空）
    found = next(r for r in results if r["segment_id"] == seg_a)
    assert isinstance(found["messages"], list)
    assert any("python" in (m.get("content") or "").lower()
               for m in found["messages"])


# ---------------------------------------------------------------------------
# 测试 2：topic_label LIKE 召回
# ---------------------------------------------------------------------------


def test_recall_finds_by_topic(mtca_db: Path) -> None:
    """recall('数据库') 应通过 topic_label LIKE 通道命中数据库话题段。"""
    _sid_db, seg_db = _make_session_with_skeleton(
        mtca_db,
        user_msg="数据库设计需要规范化",
        asst_msg="是的，三范式是基础",
        topic_label="数据库设计",
    )
    _make_session_with_skeleton(
        mtca_db,
        user_msg="前端布局用 flexbox",
        asst_msg="flexbox 适合一维布局",
        topic_label="前端布局",
    )

    results = recall("数据库", path=mtca_db, top_k=5)

    assert isinstance(results, list)
    assert len(results) >= 1
    seg_ids = [r["segment_id"] for r in results]
    assert seg_db in seg_ids


# ---------------------------------------------------------------------------
# 测试 3：双通道合并
# ---------------------------------------------------------------------------


def test_recall_combines_channels(mtca_db: Path) -> None:
    """同一 query 可同时命中两个通道 → 召回应包含两个不同段。

    设计：
    - seg_x：内容含 'kubernetes'（FTS5 命中），topic=容器编排（无关键词）
    - seg_y：topic 含 'kubernetes'（LIKE 命中），内容=无关文字
    - query 'kubernetes' → 通道 1 命中 x，通道 2 命中 y → 合并结果含两者
    """
    _sid_x, seg_x = _make_session_with_skeleton(
        mtca_db,
        user_msg="kubernetes 集群的 pod 调度策略",
        asst_msg="默认调度器考虑资源与亲和性",
        topic_label="容器编排",
    )
    _sid_y, seg_y = _make_session_with_skeleton(
        mtca_db,
        user_msg="今天随便聊聊别的项目",
        asst_msg="好的",
        topic_label="kubernetes 部署",
    )

    results = recall("kubernetes", path=mtca_db, top_k=5)
    ids = {r["segment_id"] for r in results}
    assert seg_x in ids, "FTS5 通道应命中 seg_x"
    assert seg_y in ids, "topic LIKE 通道应命中 seg_y"


# ---------------------------------------------------------------------------
# 测试 4：邻居展开
# ---------------------------------------------------------------------------


def test_recall_expands_neighbors(mtca_db: Path) -> None:
    """recall + expand_neighbors(window=1) 应包含相邻段。"""
    sid = create_session(path=mtca_db)
    t0 = int(_time.time() * 1000)
    seg_left = _make_raw_segment(mtca_db, sid, t0, t0 + 1000,
                                 topic_label="前期话题", current_score=90.0)
    seg_mid = _make_raw_segment(mtca_db, sid, t0 + 60_000, t0 + 61_000,
                                topic_label="中期话题", current_score=80.0)
    seg_right = _make_raw_segment(mtca_db, sid, t0 + 120_000, t0 + 121_000,
                                  topic_label="后期话题", current_score=70.0)

    # 1) recall 后合并应包含邻居
    results = recall("中期", path=mtca_db, top_k=5)
    picked_ids = {r["segment_id"] for r in results}
    assert seg_mid in picked_ids
    assert seg_left in picked_ids
    assert seg_right in picked_ids

    # 2) expand_neighbors 直接验证窗口逻辑
    expanded = expand_neighbors([seg_mid], window=1, path=mtca_db)
    expanded_ids = {s["segment_id"] for s in expanded}
    assert seg_left in expanded_ids
    assert seg_right in expanded_ids

    # 3) window=0 → 返回空
    assert expand_neighbors([seg_mid], window=0, path=mtca_db) == []
    # 空输入 → 返回空
    assert expand_neighbors([], window=1, path=mtca_db) == []


# ---------------------------------------------------------------------------
# 测试 5：兜底（fallback）
# ---------------------------------------------------------------------------


def test_recall_falls_back_to_recent(mtca_db: Path) -> None:
    """recall 召回为空时，recall_with_fallback 应返回最近的会话列表。"""
    sid, _ = _make_session_with_skeleton(
        mtca_db,
        user_msg="随便聊聊天气",
        asst_msg="今天晴朗",
        topic_label="闲聊",
    )

    fallback = recall_with_fallback(
        "xyzkeyword_完全_不存在的_关键词_12345", path=mtca_db
    )

    assert isinstance(fallback, list)
    assert len(fallback) >= 1
    # fallback 元素带 is_fallback=True
    assert all(item.get("is_fallback") is True for item in fallback)
    # 我们的会话应在 fallback 列表里
    fallback_sids = {item["session_id"] for item in fallback}
    assert sid in fallback_sids
    # 字段齐全
    first = fallback[0]
    for key in ("session_id", "topic_label", "started_at", "message_count"):
        assert key in first


# ---------------------------------------------------------------------------
# 测试 6：time_window 过滤
# ---------------------------------------------------------------------------


def test_recall_respects_time_window(mtca_db: Path) -> None:
    """time_window 应过滤主通道的召回段（不含邻居扩展）。"""
    sid = create_session(path=mtca_db)
    t_early = 1_700_000_000_000   # 2023-11-14 附近
    t_late = t_early + 3_600_000  # +1h
    seg_early = _make_raw_segment(
        mtca_db, sid, t_early, t_early + 1000,
        topic_label="早期话题",
    )
    seg_late = _make_raw_segment(
        mtca_db, sid, t_late, t_late + 1000,
        topic_label="晚期话题",
    )

    # ---- 主通道 search_segments 应被 time_window 过滤 ----
    only_early_ch = search_segments(
        "话题", path=mtca_db, limit=10,
        time_window=(t_early - 1000, t_early + 5000),
    )
    only_early_ids = {r["segment_id"] for r in only_early_ch}
    assert seg_early in only_early_ids
    assert seg_late not in only_early_ids

    only_late_ch = search_segments(
        "话题", path=mtca_db, limit=10,
        time_window=(t_late - 1000, t_late + 5000),
    )
    only_late_ids = {r["segment_id"] for r in only_late_ch}
    assert seg_late in only_late_ids
    assert seg_early not in only_late_ids

    # ---- recall() 应至少包含主通道命中的段 ----
    recall_early = recall(
        "话题", path=mtca_db, top_k=5,
        time_window=(t_early - 1000, t_early + 5000),
    )
    recall_early_ids = {r["segment_id"] for r in recall_early}
    assert seg_early in recall_early_ids

    # ---- 窗口全空 → recall 返回空 ----
    empty = recall(
        "话题", path=mtca_db, top_k=5,
        time_window=(0, 1000),
    )
    assert empty == []

    # ---- 非法 time_window → ValueError ----
    with pytest.raises(ValueError):
        recall("话题", path=mtca_db, time_window=(100, 50))  # end < start


# ---------------------------------------------------------------------------
# 测试 7：rerank 按相关性排序
# ---------------------------------------------------------------------------


def test_recall_rerank_by_relevance(mtca_db: Path) -> None:
    """query 与段 topic 关键词重叠越多，rerank 得分越高，排序越靠前。"""
    sid_a, seg_high = _make_session_with_skeleton(
        mtca_db,
        user_msg="MTCA 召回引擎 设计",
        asst_msg="双通道 + rerank",
        topic_label="MTCA 召回引擎",
    )
    sid_b, seg_low = _make_session_with_skeleton(
        mtca_db,
        user_msg="今天天气不错",
        asst_msg="是的",
        topic_label="天气闲聊",
    )

    ranked = rerank(
        "MTCA 召回",
        [
            {"segment_id": seg_low, "session_id": sid_b,
             "current_tier": "L0", "current_score": 100.0,
             "topic_label": "天气闲聊", "fog_anchor": "今天天气不错"},
            {"segment_id": seg_high, "session_id": sid_a,
             "current_tier": "L0", "current_score": 100.0,
             "topic_label": "MTCA 召回引擎", "fog_anchor": "MTCA 召回引擎 设计"},
        ],
        path=mtca_db,
    )

    assert len(ranked) == 2
    # 高相关段应在前面
    assert ranked[0]["segment_id"] == seg_high
    assert ranked[1]["segment_id"] == seg_low
    # score 字段存在且为 float
    for r in ranked:
        assert isinstance(r["score"], float)
    assert ranked[0]["score"] > ranked[1]["score"]

    # 空 query：退化为按 current_score 排序
    by_score = rerank(
        "",
        [
            {"segment_id": seg_low, "session_id": sid_b,
             "current_tier": "L0", "current_score": 50.0,
             "topic_label": "天气闲聊", "fog_anchor": ""},
            {"segment_id": seg_high, "session_id": sid_a,
             "current_tier": "L0", "current_score": 90.0,
             "topic_label": "MTCA", "fog_anchor": ""},
        ],
        path=mtca_db,
    )
    assert by_score[0]["segment_id"] == seg_high  # 90 > 50

    # 空候选 → 返回空
    assert rerank("anything", [], path=mtca_db) == []


# ---------------------------------------------------------------------------
# 测试 8：返回 L0 原文（messages）
# ---------------------------------------------------------------------------


def test_recall_returns_l0_messages(mtca_db: Path) -> None:
    """recall 结果中的 messages 字段应为段内全部 L0-细节消息。"""
    sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="原始消息内容 alpha",
        asst_msg="原始消息内容 beta",
        topic_label="L0原文测试",
    )

    results = recall("alpha", path=mtca_db, top_k=5)

    assert len(results) >= 1
    found = next(r for r in results if r["segment_id"] == seg_id)
    # 返回字段齐全
    for key in ("segment_id", "session_id", "tier", "score", "messages"):
        assert key in found
    assert found["session_id"] == sid
    assert found["tier"] in ("L0", "L1", "L2", "L3", "L3_hidden")

    # messages 是列表，包含我们写的两条
    msgs = found["messages"]
    assert isinstance(msgs, list)
    assert len(msgs) >= 2
    contents = [(m.get("content") or "") for m in msgs]
    assert any("alpha" in c for c in contents)
    assert any("beta" in c for c in contents)
    # 每条消息有 seq / role
    for m in msgs:
        assert "seq" in m
        assert "role" in m
        assert "content" in m


# ---------------------------------------------------------------------------
# 测试 9（额外）：search_sessions / search_segments 直接验证通道
# ---------------------------------------------------------------------------


def test_search_sessions_directly(mtca_db: Path) -> None:
    """search_sessions 直接调用应只走 FTS5 通道并返回段列表。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="golang goroutine 调度",
        asst_msg="GMP 模型",
        topic_label="Go语言",
    )

    rows = search_sessions("golang", limit=10, path=mtca_db)
    ids = [r["segment_id"] for r in rows]
    assert seg_id in ids

    rows_empty = search_sessions(
        "xyzkey_不存在的词_xyzkey", limit=10, path=mtca_db
    )
    assert rows_empty == []


def test_search_segments_directly(mtca_db: Path) -> None:
    """search_segments 直接调用应走 topic_label LIKE 通道。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="今天随便聊聊",
        asst_msg="嗯",
        topic_label="GraphQL 接口设计",
    )

    rows = search_segments("GraphQL", limit=10, path=mtca_db)
    ids = [r["segment_id"] for r in rows]
    assert seg_id in ids

    rows_empty = search_segments(
        "xyzkey_不存在的词_xyzkey", limit=10, path=mtca_db
    )
    assert rows_empty == []
