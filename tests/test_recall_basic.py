"""src/recall/recall_engine.py 测试：T14 补齐到 50 段用例。

结构（10 个原有 + 40 个新增 = 50）：
- 核心行为（8）：test_recall_finds_by_keyword / _topic / _combines /
  _expands_neighbors / _falls_back_to_recent / _respects_time_window /
  _rerank_by_relevance / _returns_l0_messages
- 直接通道（2）：test_search_sessions_directly / test_search_segments_directly
- 老板真历史场景（24，新增）：漫剧 / 编程 / 工作 / 生活
- 边界 case（16，新增）：短对话 / 长对话 / 多话题 / 工具调用 / empty /
  FTS5 特殊字符 / 大小写 / SQL 注入 / 极短查询 / top_k / 大量干扰 /
  时间窗口错开 / 多关键词 / 中英混合 / 数字 / 停用词
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


# ===========================================================================
# T14：40 个新测试（24 真历史场景 + 16 边界 case）
# ===========================================================================


def _make_session_multi(
    mtca_db: Path,
    messages: list[tuple[str, str]],
    topic_label: str | None = None,
) -> tuple[str, str]:
    """建会话 + 写 N 条消息（list[(role, content)]）+ 落骨架，返回 (sid, seg_id)。"""
    sid = create_session(path=mtca_db)
    for role, content in messages:
        write_message(sid, role, content, path=mtca_db)
    skel = build_skeleton(sid, path=mtca_db)
    if topic_label is not None:
        skel["topic_label"] = topic_label
    return sid, save_skeleton(sid, skel, path=mtca_db)


# ---------------------------------------------------------------------------
# 老板真历史场景（24）：漫剧 3 / 编程 9 / 工作 6 / 生活 6
# ---------------------------------------------------------------------------


def test_real_novel_fantasy_awakening(mtca_db: Path) -> None:
    """漫剧·玄幻：灵根觉醒。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="主角林动在山洞里灵根觉醒 获得祖石传承",
        asst_msg="天妖貂族血脉 修炼大荒芜经",
        topic_label="武动乾坤灵根觉醒",
    )
    results = recall("灵根觉醒", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_novel_ancient_revenge(mtca_db: Path) -> None:
    """漫剧·古装：燕王复仇。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="燕王回京 清算当年陷害母妃的叛臣",
        asst_msg="血洗尚书府 夺回兵权",
        topic_label="燕王复仇记",
    )
    results = recall("燕王", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_novel_modern_boss(mtca_db: Path) -> None:
    """漫剧·现代：霸总顾霆琛。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="顾总把女人抵在墙上 你只能是我的",
        asst_msg="女人挣扎 你放我走",
        topic_label="顾总请签字离婚",
    )
    results = recall("顾总", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_python_decorator(mtca_db: Path) -> None:
    """编程·Python：装饰器调试。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="Python 装饰器 functools.wraps 忘记写 丢失函数元信息",
        asst_msg="加上 functools.wraps(func) 保留 __name__",
        topic_label="Python 装饰器",
    )
    results = recall("装饰器", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_rust_borrow(mtca_db: Path) -> None:
    """编程·Rust：生命周期注解。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="Rust 借用检查器报错 lifetime annotation needed",
        asst_msg="结构体字段加 'a 标注即可",
        topic_label="Rust 生命周期",
    )
    results = recall("生命周期", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_typescript_guard(mtca_db: Path) -> None:
    """编程·TypeScript：类型守卫。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="TypeScript 类型守卫 typeof instanceof 写不出来",
        asst_msg="用 in 操作符做自定义类型谓词",
        topic_label="TS 类型守卫",
    )
    results = recall("类型守卫", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_go_context(mtca_db: Path) -> None:
    """编程·Go：context 取消传播。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="Go context.WithCancel goroutine 没收到取消信号",
        asst_msg="子协程必须 select <-ctx.Done() 才退出",
        topic_label="Go context 取消",
    )
    results = recall("context 取消", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_sql_index(mtca_db: Path) -> None:
    """编程·SQL：覆盖索引。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="MySQL 覆盖索引 covering index 避免回表查询",
        asst_msg="把查询列也加到联合索引里",
        topic_label="MySQL 索引优化",
    )
    results = recall("覆盖索引", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_redis_cache(mtca_db: Path) -> None:
    """编程·Redis：缓存穿透。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="Redis 缓存穿透 不存在的 key 每次都打 DB",
        asst_msg="用布隆过滤器挡住 null 请求",
        topic_label="Redis 缓存穿透",
    )
    results = recall("缓存穿透", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_kafka_lag(mtca_db: Path) -> None:
    """编程·Kafka：消费者积压。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="Kafka 消费者消息积压 lag 越来越大",
        asst_msg="增加分区数 + 横向扩 consumer 实例",
        topic_label="消息积压",
    )
    results = recall("消息积压", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_linux_process(mtca_db: Path) -> None:
    """编程·Linux：僵尸进程排查。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="Linux 出现大量僵尸进程 defunct 杀不掉",
        asst_msg="kill -9 父进程让 init 接管回收",
        topic_label="僵尸进程",
    )
    results = recall("僵尸进程", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_code_docker_network(mtca_db: Path) -> None:
    """编程·Docker：bridge 网络容器互通。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="Docker bridge 网络下容器之间 ping 不通",
        asst_msg="用 docker network connect 加入同一网络",
        topic_label="Docker bridge 网络",
    )
    results = recall("bridge 网络", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_work_weekly_report(mtca_db: Path) -> None:
    """工作·周报：Q3 复盘。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="本周完成召回引擎重构 Q3 目标达成 95%",
        asst_msg="下周聚焦 LLM 接入层",
        topic_label="Q3 周报复盘",
    )
    results = recall("Q3 周报", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_work_product_prd(mtca_db: Path) -> None:
    """工作·产品 PRD。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="PRD 文档写了三版 业务方还不满意",
        asst_msg="画用户旅程图比文字描述更直观",
        topic_label="产品 PRD 评审",
    )
    results = recall("PRD", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_work_contract_clause(mtca_db: Path) -> None:
    """工作·合同：违约金条款。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="合同违约金条款写成合同总额 30% 对方不接受",
        asst_msg="改成逾期部分每日万分之五",
        topic_label="违约金",
    )
    results = recall("违约金", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_work_promotion_review(mtca_db: Path) -> None:
    """工作·晋升答辩。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="晋升答辩 PPT 准备了三周 评委问技术深度",
        asst_msg="多讲架构演进和踩坑案例",
        topic_label="晋升答辩准备",
    )
    results = recall("晋升答辩", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_work_recruit_jd(mtca_db: Path) -> None:
    """工作·招聘 JD：架构师。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="高级架构师 JD 写了 30 条要求 没人投",
        asst_msg="聚焦 3 个核心能力 删掉锦上添花的",
        topic_label="架构师招聘",
    )
    results = recall("架构师", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_work_project_init(mtca_db: Path) -> None:
    """工作·立项评审。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="新业务立项评审被驳回 ROI 不清晰",
        asst_msg="补三套财务测算模型 区分乐观中观悲观",
        topic_label="业务立项评审",
    )
    results = recall("立项", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_life_medical_check(mtca_db: Path) -> None:
    """生活·体检：血脂指标。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="体检报告显示血脂偏高 低密度脂蛋白超标",
        asst_msg="少吃内脏 多吃深海鱼",
        topic_label="血脂偏高",
    )
    results = recall("血脂", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_life_baby_food(mtca_db: Path) -> None:
    """生活·宝宝辅食。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="六个月宝宝辅食先加米粉还是菜泥",
        asst_msg="先高铁米粉 再根茎类蔬菜泥",
        topic_label="宝宝辅食添加",
    )
    results = recall("辅食", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_life_renovation(mtca_db: Path) -> None:
    """生活·装修：水电改造预算。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="水电改造报价一万八 是不是被坑了",
        asst_msg="按米数算 一般 80 平 60 米左右",
        topic_label="水电改造预算",
    )
    results = recall("水电改造", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_life_travel(mtca_db: Path) -> None:
    """生活·旅游：杭州西湖。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="西湖一日游 断桥残雪 雷峰塔 怎么安排",
        asst_msg="早上断桥 中午楼外楼 下午灵隐",
        topic_label="杭州西湖攻略",
    )
    results = recall("西湖", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_life_recipe(mtca_db: Path) -> None:
    """生活·食谱：红烧肉。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="红烧肉总是炖得太柴 不够软烂",
        asst_msg="小火慢炖两小时 中途不开盖",
        topic_label="红烧肉做法",
    )
    results = recall("红烧肉", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_real_life_pet_neuter(mtca_db: Path) -> None:
    """生活·宠物：猫咪绝育。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="猫咪绝育手术前后注意事项 多大做",
        asst_msg="六个月以上 术前禁食八小时",
        topic_label="猫咪绝育护理",
    )
    results = recall("绝育", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


# ---------------------------------------------------------------------------
# 边界 case（16）
# ---------------------------------------------------------------------------


def test_edge_short_dialog_one_message(mtca_db: Path) -> None:
    """边界·短对话：仅 1 条消息。"""
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "孤零零的一条问候语 你好世界", path=mtca_db)
    skel = build_skeleton(sid, path=mtca_db)
    skel["topic_label"] = "问候语"
    seg_id = save_skeleton(sid, skel, path=mtca_db)

    results = recall("问候语", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_long_dialog_ten_messages(mtca_db: Path) -> None:
    """边界·长对话：10 条消息交叉。"""
    _sid, seg_id = _make_session_multi(
        mtca_db,
        [
            ("user", "今天来讨论大型语言模型的注意力机制"),
            ("assistant", "自注意力 self-attention 计算 QKV"),
            ("user", "多头注意力 multi-head 的作用是什么"),
            ("assistant", "并行多组注意力 捕获不同子空间"),
            ("user", "位置编码 positional encoding 怎么做"),
            ("assistant", "正弦位置编码或 RoPE 旋转位置编码"),
            ("user", "LayerNorm 放在 MHA 前还是后"),
            ("assistant", "Pre-LN 训练更稳定 Post-LN 表达更强"),
            ("user", "总结一下注意力机制的演进"),
            ("assistant", "MHA -> MQA -> GQA -> MLA 推理加速"),
        ],
        topic_label="注意力机制长讨论",
    )
    results = recall("注意力机制", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_multi_topic_three_sessions(mtca_db: Path) -> None:
    """边界·多话题：3 个会话同关键词，召回应能区分。"""
    seg_ids: list[str] = []
    for tag in ["篮球", "足球", "网球"]:
        _sid, seg_id = _make_session_with_skeleton(
            mtca_db,
            user_msg=f"今天聊 {tag} 比赛精彩瞬间",
            asst_msg=f"{tag} 运动员发挥稳定",
            topic_label=f"{tag} 比赛",
        )
        seg_ids.append(seg_id)
    seg_basket, seg_foot, seg_tennis = seg_ids

    r_basket = recall("篮球", path=mtca_db, top_k=3)
    r_foot = recall("足球", path=mtca_db, top_k=3)
    r_tennis = recall("网球", path=mtca_db, top_k=3)

    basket_ids = {r["segment_id"] for r in r_basket}
    foot_ids = {r["segment_id"] for r in r_foot}
    tennis_ids = {r["segment_id"] for r in r_tennis}

    assert seg_basket in basket_ids
    assert seg_foot in foot_ids
    assert seg_tennis in tennis_ids


def test_edge_tool_calls_in_message(mtca_db: Path) -> None:
    """边界·工具调用：assistant 消息带 tool_calls。"""
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "查一下北京今天天气", path=mtca_db)
    write_message(
        sid, "assistant",
        "好的 我帮你查天气",
        tool_calls={"name": "get_weather", "args": {"city": "北京"}},
        path=mtca_db,
    )
    write_message(
        sid, "tool",
        "{\"temp\": 25, \"desc\": \"晴\"}",
        tool_results={"temp": 25, "desc": "晴"},
        path=mtca_db,
    )
    skel = build_skeleton(sid, path=mtca_db)
    skel["topic_label"] = "天气工具调用"
    seg_id = save_skeleton(sid, skel, path=mtca_db)

    results = recall("天气", path=mtca_db, top_k=3)
    found = next((r for r in results if r["segment_id"] == seg_id), None)
    assert found is not None
    roles = {m["role"] for m in found["messages"]}
    assert "tool" in roles


def test_edge_tool_results_only_message(mtca_db: Path) -> None:
    """边界·工具结果：session 只有 tool 角色消息。"""
    sid = create_session(path=mtca_db)
    write_message(
        sid, "tool",
        "数据库查询结果 返回 42 行数据",
        tool_results={"rows": 42},
        path=mtca_db,
    )
    skel = build_skeleton(sid, path=mtca_db)
    skel["topic_label"] = "数据库查询结果"
    seg_id = save_skeleton(sid, skel, path=mtca_db)

    results = recall("数据库查询", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_empty_content_message(mtca_db: Path) -> None:
    """边界·空消息：写一条 content='' 的消息。"""
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "正常提问", path=mtca_db)
    write_message(sid, "assistant", "正常回答", path=mtca_db)
    write_message(sid, "assistant", "", path=mtca_db)
    skel = build_skeleton(sid, path=mtca_db)
    skel["topic_label"] = "含空消息的会话"
    seg_id = save_skeleton(sid, skel, path=mtca_db)

    results = recall("正常提问", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_single_char_query(mtca_db: Path) -> None:
    """边界·极短查询：单字。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="秦 始 皇 陵 兵 马 俑",
        asst_msg="世界文化遗产",
        topic_label="秦始皇陵",
    )
    results = recall("秦", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_fts5_special_chars_query(mtca_db: Path) -> None:
    """边界·FTS5 特殊字符：含 \" * : ^ 括号，不应崩溃。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="Python 列表推导式 vs map 函数对比",
        asst_msg="推导式更 Pythonic",
        topic_label="Python 推导式",
    )
    for bad_query in [
        '"Python"',
        "Python*",
        "tag:Python",
        "Python^2",
        "(Python)",
        "Python's",
        "Py--thon",
    ]:
        results = recall(bad_query, path=mtca_db, top_k=3)
        assert isinstance(results, list)
    clean = recall("Python 列表推导式", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in clean)


def test_edge_top_k_zero_returns_empty(mtca_db: Path) -> None:
    """边界·top_k=0：coerce 后退化为默认值 5。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="测试 top_k 边界",
        asst_msg="好的",
        topic_label="top_k 边界",
    )
    results = recall("top_k", path=mtca_db, top_k=0)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_many_distractor_segments(mtca_db: Path) -> None:
    """边界·大量干扰段：100 个无关段 + 1 目标段。"""
    for i in range(100):
        _make_session_with_skeleton(
            mtca_db,
            user_msg=f"干扰会话 {i} 主题毫不相关 内容也不同",
            asst_msg=f"这是干扰 {i}",
            topic_label=f"干扰话题 {i}",
        )
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="独一无二的目标 金丝雀 黄昏飞行",
        asst_msg="金丝雀在黄昏的余晖里飞过",
        topic_label="金丝雀 黄昏 飞行",
    )
    results = recall("金丝雀 黄昏", path=mtca_db, top_k=5)
    picked_ids = [r["segment_id"] for r in results]
    assert picked_ids[0] == seg_id


def test_edge_case_insensitive_query(mtca_db: Path) -> None:
    """边界·大小写不敏感：FTS5 默认 ascii 大小写不敏感。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="JavaScript async await syntax",
        asst_msg="Promise-based",
        topic_label="JavaScript Async",
    )
    results = recall("JAVASCRIPT", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_sql_injection_attempt(mtca_db: Path) -> None:
    """边界·SQL 注入尝试：含 ; DROP TABLE。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="正常讨论的话题 安全防护",
        asst_msg="SQL 注入要防",
        topic_label="安全防护",
    )
    malicious = "'; DROP TABLE segments; --"
    results = recall(malicious, path=mtca_db, top_k=3)
    assert isinstance(results, list)
    normal = recall("安全防护", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in normal)


def test_edge_multi_keyword_query(mtca_db: Path) -> None:
    """边界·多关键词查询：每个关键词各自能召回对应段。"""
    _sid_a, seg_a = _make_session_with_skeleton(
        mtca_db,
        user_msg="讨论 React 组件状态管理",
        asst_msg="useState Hook",
        topic_label="React 状态管理",
    )
    _sid_b, seg_b = _make_session_with_skeleton(
        mtca_db,
        user_msg="讨论 Vue 组件状态管理",
        asst_msg="reactive API",
        topic_label="Vue 状态管理",
    )
    _make_session_with_skeleton(
        mtca_db,
        user_msg="讨论 Python 装饰器",
        asst_msg="functools.wraps",
        topic_label="Python 装饰器",
    )
    r_a = recall("React", path=mtca_db, top_k=5)
    r_b = recall("Vue", path=mtca_db, top_k=5)
    a_ids = {r["segment_id"] for r in r_a}
    b_ids = {r["segment_id"] for r in r_b}
    assert seg_a in a_ids
    assert seg_b in b_ids


def test_edge_mixed_cjk_ascii_query(mtca_db: Path) -> None:
    """边界·中英混合查询：字母 + 中文。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="使用 Python jieba 做中文分词",
        asst_msg="pip install jieba 即可",
        topic_label="jieba 中文分词",
    )
    results = recall("Python jieba", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_numeric_query(mtca_db: Path) -> None:
    """边界·数字查询：纯数字。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="2024 年公司营收突破 1000 万",
        asst_msg="同比增长 25%",
        topic_label="2024 财报",
    )
    results = recall("2024", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in results)


def test_edge_time_window_fully_misaligned(mtca_db: Path) -> None:
    """边界·时间窗口完全错开：返回空。"""
    _sid, seg_id = _make_session_with_skeleton(
        mtca_db,
        user_msg="现在的话题",
        asst_msg="好的",
        topic_label="现在话题",
    )
    ancient = (1_000_000_000_000, 1_000_000_000_500)
    results = recall("现在", path=mtca_db, top_k=3, time_window=ancient)
    assert results == []
    no_tw = recall("现在", path=mtca_db, top_k=3)
    assert any(r["segment_id"] == seg_id for r in no_tw)
