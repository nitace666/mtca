"""src/relations/graph.py 测试（T23）

测试覆盖（用户 prompt 要求的 6 类 + 边界）：

1. test_create_relation               create 4 种关系 + dedupe + 非法 type/段
2. test_get_related                   get_related 默认 related_to + types 过滤
3. test_get_topic_cluster_bfs         BFS depth=1/2/3 邻居层数正确
4. test_get_supersede_chain_recursive 多段链 + 被取代者反向追溯 + 截断
5. test_auto_create_relations         related_to + derived_from + supersedes 触发
6. test_export_to_json_format         nodes / edges / center_id 结构 + 截断
7. test_delete_and_list_relations     delete + list + filter by type
8. test_constants_and_dedupe          常量校验 + dedupe + 权重夹紧
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from src.lifecycle.contradiction_detector import apply_supersede
from src.l0.session_writer import create_session
from src.relations.graph import (
    AUTO_CANDIDATE_LIMIT,
    DEFAULT_RELATION_TYPES,
    DERIVED_KEYWORD_OVERLAP,
    DERIVED_TIME_GAP_MS,
    EXPORT_NODE_MAX,
    MAX_DEPTH,
    VALID_RELATION_TYPES,
    WEIGHT_MAX,
    WEIGHT_MIN,
    auto_create_relations,
    create_relation,
    delete_relation,
    export_to_json,
    get_relation,
    get_related,
    get_supersede_chain,
    get_topic_cluster,
    list_relations,
)
from src.store.sqlite import execute, query


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _days_ago_ms(days: float) -> int:
    """N 天前毫秒时间戳。"""
    return _now_ms() - int(days * 24 * 60 * 60 * 1000)


def _insert_segment(
    path: Path,
    topic_label: str = "漫剧方案",
    silence_state: str = "active",
    start_days_ago: int = 1,
    fog_anchor: str = "漫剧方案",
    start_at_ms: int | None = None,
) -> str:
    """直接 INSERT 一条 segments 记录（绕过 L0-骨架触发器）。"""
    sid = create_session(path=path)
    seg_id = str(uuid.uuid4())
    start_at = (
        int(start_at_ms) if start_at_ms is not None else _days_ago_ms(start_days_ago)
    )
    end_at = start_at + 60_000
    execute(
        "INSERT INTO segments "
        "(segment_id, session_id, start_msg_seq, end_msg_seq, "
        "start_at, end_at, topic_label, fog_anchor, "
        "silence_state) "
        "VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?)",
        (
            seg_id, sid,
            start_at, end_at,
            topic_label, fog_anchor,
            silence_state,
        ),
        path=path,
    )
    return seg_id


class MockProvider:
    """可配置的 mock LLM provider：固定返回矛盾。"""

    def __init__(
        self,
        contradicts: bool = True,
        level: str = "major",
        reason: str = "mock",
    ) -> None:
        self.contradicts = contradicts
        self.level = level
        self.reason = reason

    def generate(self, prompt: str, **kwargs: Any) -> str:
        payload = {
            "contradicts": self.contradicts,
            "level": self.level,
            "reason": self.reason,
        }
        return json.dumps(payload, ensure_ascii=False)

    def embed(self, text: str) -> list[float]:
        return []

    def is_available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# 测试 1：create_relation（4 种关系 + dedupe + 校验）
# ---------------------------------------------------------------------------


def test_create_relation(mtca_db: Path) -> None:
    """create_relation 4 种关系类型都能建；返回 dict 完整。"""
    segs = [_insert_segment(mtca_db) for _ in range(2)]
    a_id, b_id = segs

    for rtype in VALID_RELATION_TYPES:
        result = create_relation(a_id, b_id, rtype, path=mtca_db)
        assert result["relation_id"]
        assert result["seg_a_id"] == a_id
        assert result["seg_b_id"] == b_id
        assert result["relation_type"] == rtype
        assert result["weight"] == 1.0
        assert result["auto_created"] == 0
        assert result["created_at"] > 0
        assert result["deduped"] is False

    # 4 行 segment_relations
    rows = query(
        "SELECT relation_type FROM segment_relations "
        "WHERE (seg_a_id = ? AND seg_b_id = ?) OR (seg_a_id = ? AND seg_b_id = ?)",
        (a_id, b_id, b_id, a_id),
        path=mtca_db,
    )
    assert {r["relation_type"] for r in rows} == set(VALID_RELATION_TYPES)


def test_create_relation_dedupe(mtca_db: Path) -> None:
    """同方向同类型关系重复创建 → deduped=True，不新增行。"""
    a_id = _insert_segment(mtca_db)
    b_id = _insert_segment(mtca_db)

    r1 = create_relation(a_id, b_id, "related_to", path=mtca_db)
    r2 = create_relation(a_id, b_id, "related_to", path=mtca_db)

    assert r1["deduped"] is False
    assert r2["deduped"] is True
    assert r1["relation_id"] == r2["relation_id"]

    rows = query(
        "SELECT * FROM segment_relations "
        "WHERE seg_a_id = ? AND seg_b_id = ? AND relation_type = 'related_to'",
        (a_id, b_id),
        path=mtca_db,
    )
    assert len(rows) == 1


def test_create_relation_validates(mtca_db: Path) -> None:
    """非法 relation_type / 段不存在 / seg_a == seg_b 都抛 ValueError。"""
    a_id = _insert_segment(mtca_db)
    b_id = _insert_segment(mtca_db)

    with pytest.raises(ValueError):
        create_relation(a_id, b_id, "unknown_type", path=mtca_db)
    with pytest.raises(ValueError):
        create_relation("", b_id, "related_to", path=mtca_db)
    with pytest.raises(ValueError):
        create_relation(a_id, b_id, "", path=mtca_db)
    with pytest.raises(ValueError):
        create_relation(a_id, a_id, "related_to", path=mtca_db)
    with pytest.raises(ValueError):
        create_relation(a_id, "nonexistent-id", "related_to", path=mtca_db)


def test_create_relation_weight_clamps(mtca_db: Path) -> None:
    """weight 越界自动夹紧到 [WEIGHT_MIN, WEIGHT_MAX]。"""
    a_id = _insert_segment(mtca_db)
    b_id = _insert_segment(mtca_db)

    r_high = create_relation(a_id, b_id, "related_to", weight=999.0, path=mtca_db)
    assert r_high["weight"] == WEIGHT_MAX

    r_low = create_relation(
        _insert_segment(mtca_db), _insert_segment(mtca_db),
        "related_to", weight=-5.0, path=mtca_db,
    )
    assert r_low["weight"] == WEIGHT_MIN


def test_create_relation_auto_flag(mtca_db: Path) -> None:
    """auto=True → auto_created=1；auto=False → 0。"""
    a_id = _insert_segment(mtca_db)
    b_id = _insert_segment(mtca_db)
    r_auto = create_relation(
        a_id, b_id, "related_to", auto=True, path=mtca_db,
    )
    r_manual = create_relation(
        _insert_segment(mtca_db), _insert_segment(mtca_db),
        "derived_from", auto=False, path=mtca_db,
    )
    assert r_auto["auto_created"] == 1
    assert r_manual["auto_created"] == 0


# ---------------------------------------------------------------------------
# 测试 2：get_related
# ---------------------------------------------------------------------------


def test_get_related(mtca_db: Path) -> None:
    """get_related 默认 related_to；types 过滤；端双向匹配。"""
    center = _insert_segment(mtca_db)
    a_id = _insert_segment(mtca_db)
    b_id = _insert_segment(mtca_db)

    # center → a (related_to)
    create_relation(center, a_id, "related_to", path=mtca_db)
    # b → center (references，反向也能命中 center 的相关段)
    create_relation(b_id, center, "references", path=mtca_db)
    # 无关段（center → unrelated，references 类型） → 默认 types 不应包含
    unrel = _insert_segment(mtca_db)
    create_relation(center, unrel, "references", path=mtca_db)

    # 默认：related_to
    related = get_related(center, path=mtca_db)
    ids = {r["segment_id"] for r in related}
    assert ids == {a_id}

    # 多种类型
    related2 = get_related(
        center, types=["related_to", "references"], path=mtca_db,
    )
    ids2 = {r["segment_id"] for r in related2}
    # center 端相关段 = a (related_to out) + unrel (references out) + b (references in)
    assert ids2 == {a_id, unrel, b_id}

    # 仅 references：center 作为 b_id 端（被 b 引用）也命中
    related3 = get_related(center, types=["references"], path=mtca_db)
    ids3 = {r["segment_id"] for r in related3}
    # center 自己排除；unrel 是中心 outgoing references；b_id 是中心 incoming references
    assert ids3 == {unrel, b_id}

    # DEFAULT_RELATION_TYPES 校验
    assert DEFAULT_RELATION_TYPES == ("related_to",)
    assert set(VALID_RELATION_TYPES) == {
        "references", "supersedes", "related_to", "derived_from",
    }


def test_get_related_dedup_multi_path(mtca_db: Path) -> None:
    """同一段经多条 related_to 命中 → 只出现一次。"""
    center = _insert_segment(mtca_db)
    target = _insert_segment(mtca_db)
    create_relation(center, target, "related_to", path=mtca_db)
    create_relation(target, center, "related_to", path=mtca_db)
    out = get_related(center, path=mtca_db)
    assert len(out) == 1
    assert out[0]["segment_id"] == target


def test_list_relations_filters(mtca_db: Path) -> None:
    """list_relations 按 types 过滤关系类型。"""
    center = _insert_segment(mtca_db)
    a = _insert_segment(mtca_db)
    b = _insert_segment(mtca_db)
    create_relation(center, a, "related_to", path=mtca_db)
    create_relation(center, b, "references", path=mtca_db)
    create_relation(a, b, "derived_from", path=mtca_db)

    # 默认 types=related_to：只命中 center ↔ a
    only_rel = list_relations(center, path=mtca_db)
    types = {r["relation_type"] for r in only_rel}
    assert types == {"related_to"}

    # 全 4 种：center 涉及 2 条 + a 涉及 1 条 (中心间接)
    all_types = list_relations(
        center, types=list(VALID_RELATION_TYPES), path=mtca_db,
    )
    seen_types = {r["relation_type"] for r in all_types}
    assert {"related_to", "references"} <= seen_types


def test_delete_and_get_relation(mtca_db: Path) -> None:
    """delete_relation + get_relation 正常路径。"""
    a = _insert_segment(mtca_db)
    b = _insert_segment(mtca_db)
    r = create_relation(a, b, "related_to", path=mtca_db)

    assert get_relation(r["relation_id"], path=mtca_db) is not None
    deleted = delete_relation(r["relation_id"], path=mtca_db)
    assert deleted == 1
    assert get_relation(r["relation_id"], path=mtca_db) is None

    # 非法 relation_id
    with pytest.raises(ValueError):
        delete_relation("", path=mtca_db)
    assert get_relation("nonexistent-id", path=mtca_db) is None


# ---------------------------------------------------------------------------
# 测试 3：get_topic_cluster BFS
# ---------------------------------------------------------------------------


def test_get_topic_cluster_bfs(mtca_db: Path) -> None:
    """BFS depth=1 → 仅 1-hop；depth=2 → 含 2-hop；去重。"""
    segs = {f"s{i}": _insert_segment(mtca_db) for i in range(5)}
    s0, s1, s2, s3, s4 = (
        segs["s0"], segs["s1"], segs["s2"], segs["s3"], segs["s4"],
    )

    # 链: s0 --(related_to)--> s1 --(references)--> s2 --(derived_from)--> s3
    # s4 孤立
    create_relation(s0, s1, "related_to", path=mtca_db)
    create_relation(s1, s2, "references", path=mtca_db)
    create_relation(s2, s3, "derived_from", path=mtca_db)

    # depth=1：s0 + s1
    cluster_1 = get_topic_cluster(s0, depth=1, path=mtca_db)
    ids_1 = {c["segment_id"] for c in cluster_1}
    assert ids_1 == {s0, s1}

    # depth=2：s0 + s1 + s2（s3 是 3-hop，不进）
    cluster_2 = get_topic_cluster(s0, depth=2, path=mtca_db)
    ids_2 = {c["segment_id"] for c in cluster_2}
    assert ids_2 == {s0, s1, s2}
    # 起始段排第一
    assert cluster_2[0]["segment_id"] == s0

    # depth=3：含 s3
    cluster_3 = get_topic_cluster(s0, depth=3, path=mtca_db)
    ids_3 = {c["segment_id"] for c in cluster_3}
    assert s3 in ids_3
    # s4 孤立，永远不进
    assert s4 not in ids_3


def test_get_topic_cluster_depth_clamp(mtca_db: Path) -> None:
    """depth 越界 / 非整数自动夹紧到 [1, MAX_DEPTH]。"""
    s0 = _insert_segment(mtca_db)
    s1 = _insert_segment(mtca_db)
    create_relation(s0, s1, "related_to", path=mtca_db)

    # 0 → 1
    cluster_0 = get_topic_cluster(s0, depth=0, path=mtca_db)
    ids_0 = {c["segment_id"] for c in cluster_0}
    assert s1 in ids_0

    # 999 → MAX_DEPTH（至少含 s1）
    cluster_max = get_topic_cluster(s0, depth=999, path=mtca_db)
    ids_max = {c["segment_id"] for c in cluster_max}
    assert s1 in ids_max
    assert MAX_DEPTH >= 2


def test_get_topic_cluster_validates(mtca_db: Path) -> None:
    """段不存在 / 空 segment_id 抛 ValueError。"""
    with pytest.raises(ValueError):
        get_topic_cluster("", path=mtca_db)
    with pytest.raises(ValueError):
        get_topic_cluster("nonexistent-id", path=mtca_db)


# ---------------------------------------------------------------------------
# 测试 4：get_supersede_chain 递归
# ---------------------------------------------------------------------------


def test_get_supersede_chain_recursive(mtca_db: Path) -> None:
    """A → B → C → D 链：get_supersede_chain(A) 返回 [A, B, C, D]。"""
    a = _insert_segment(mtca_db, start_days_ago=10)
    b = _insert_segment(mtca_db, start_days_ago=8)
    c = _insert_segment(mtca_db, start_days_ago=6)
    d = _insert_segment(mtca_db, start_days_ago=4)

    apply_supersede(a, b, path=mtca_db)
    apply_supersede(b, c, path=mtca_db)
    apply_supersede(c, d, path=mtca_db)

    chain_a = get_supersede_chain(a, path=mtca_db)
    ids_a = [seg["segment_id"] for seg in chain_a]
    assert ids_a == [a, b, c, d]

    # 中间段 B：先向上到 D（链尾）→ 再向下没 → 仍是完整链
    chain_b = get_supersede_chain(b, path=mtca_db)
    ids_b = [seg["segment_id"] for seg in chain_b]
    assert ids_b == [b, c, d]

    # 末尾 D：仅 [D]
    chain_d = get_supersede_chain(d, path=mtca_db)
    ids_d = [seg["segment_id"] for seg in chain_d]
    assert ids_d == [d]


def test_get_supersede_chain_self_to_self(mtca_db: Path) -> None:
    """孤立段（无 supersede 关系）→ 仅返回自身。"""
    s = _insert_segment(mtca_db)
    chain = get_supersede_chain(s, path=mtca_db)
    assert len(chain) == 1
    assert chain[0]["segment_id"] == s


# ---------------------------------------------------------------------------
# 测试 5：auto_create_relations
# ---------------------------------------------------------------------------


def test_auto_create_relations(mtca_db: Path) -> None:
    """auto：同 topic → related_to；时间相邻 + 关键词重叠 → derived_from；
    矛盾检测 → supersedes。
    """
    base = _now_ms()
    # 主段（5 分钟前）
    center = _insert_segment(
        mtca_db, topic_label="漫剧方案", fog_anchor="漫剧方案 制作 计划",
        start_at_ms=base - 5 * 60 * 1000,
    )
    # 同 topic（10 分钟前）→ 应建 related_to
    same_topic = _insert_segment(
        mtca_db, topic_label="漫剧方案", fog_anchor="漫剧方案 制作 计划",
        start_at_ms=base - 10 * 60 * 1000,
    )
    # 关键词重叠 + 时间相邻（1 分钟前）→ 应建 derived_from
    kw_neighbor = _insert_segment(
        mtca_db, topic_label="工作项目", fog_anchor="漫剧方案 制作 排期",
        start_at_ms=base - 1 * 60 * 1000,
    )
    # 时间相邻但关键词不重叠 → 不建 derived_from
    no_kw_neighbor = _insert_segment(
        mtca_db, topic_label="工作项目", fog_anchor="财务结算流程",
        start_at_ms=base - 2 * 60 * 1000,
    )
    # 矛盾检测候选（同 topic + 5 天前 → 90 天窗口内）
    contradict_cand = _insert_segment(
        mtca_db, topic_label="漫剧方案", fog_anchor="漫剧方案 制作 计划",
        start_at_ms=base - 5 * 24 * 60 * 60 * 1000,
    )

    provider = MockProvider(contradicts=True, level="major")
    result = auto_create_relations(
        center, provider=provider, path=mtca_db,
    )

    # related_to：same_topic + contradict_cand 都同 topic
    assert result["related_to"] >= 1
    # derived_from：kw_neighbor 触发
    assert result["derived_from"] >= 1
    # supersedes：contradict_cand 触发
    assert result["supersedes"] >= 1
    # 返回结构完整
    assert "relations" in result
    assert "contradictions" in result
    assert isinstance(result["relations"], list)

    # DB 校验：3 类关系都写入了
    rels = query(
        "SELECT relation_type FROM segment_relations "
        "WHERE seg_a_id = ? OR seg_b_id = ?",
        (center, center),
        path=mtca_db,
    )
    types = {r["relation_type"] for r in rels}
    assert {"related_to", "derived_from", "supersedes"} <= types

    # no_kw_neighbor 不应有 derived_from 关系
    no_kw_rels = query(
        "SELECT * FROM segment_relations WHERE "
        "(seg_a_id = ? AND seg_b_id = ?) OR "
        "(seg_a_id = ? AND seg_b_id = ?)",
        (center, no_kw_neighbor, no_kw_neighbor, center),
        path=mtca_db,
    )
    assert not any(
        r["relation_type"] == "derived_from" for r in no_kw_rels
    )

    # 矛盾检测的 B 被 supersede 标记
    from src.l0.segment_writer import get_segment
    after = get_segment(contradict_cand, path=mtca_db)
    assert after["superseded_by"] == center

    # 常量校验
    assert DERIVED_TIME_GAP_MS > 0
    assert DERIVED_KEYWORD_OVERLAP >= 1
    assert AUTO_CANDIDATE_LIMIT > 0


def test_auto_create_relations_no_provider(mtca_db: Path) -> None:
    """无 provider 时矛盾检测走空 verdict；related_to 仍正常。"""
    center = _insert_segment(mtca_db, topic_label="漫剧方案")
    same = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=2)
    result = auto_create_relations(center, provider=None, path=mtca_db)
    assert result["related_to"] >= 1
    # 无矛盾触发 → supersedes 必为 0
    assert result["supersedes"] == 0
    # contradictions 列表至少为空
    assert result["contradictions"] == [] or all(
        not c.get("contradicts") for c in result["contradictions"]
    )


def test_auto_create_relations_validates(mtca_db: Path) -> None:
    """空 segment_id / 不存在段抛 ValueError。"""
    with pytest.raises(ValueError):
        auto_create_relations("", path=mtca_db)
    with pytest.raises(ValueError):
        auto_create_relations("nonexistent-id", path=mtca_db)


# ---------------------------------------------------------------------------
# 测试 6：export_to_json
# ---------------------------------------------------------------------------


def test_export_to_json_format(mtca_db: Path) -> None:
    """export_to_json 输出结构正确：center_id / depth / nodes / edges。"""
    base = _now_ms()
    s0 = _insert_segment(
        mtca_db, topic_label="漫剧方案", fog_anchor="漫剧 方案 A",
        start_at_ms=base - 10 * 60 * 1000,
    )
    s1 = _insert_segment(
        mtca_db, topic_label="漫剧方案", fog_anchor="漫剧 方案 B",
        start_at_ms=base - 5 * 60 * 1000,
    )
    s2 = _insert_segment(
        mtca_db, topic_label="工作项目", fog_anchor="漫剧 方案 C",
        start_at_ms=base - 1 * 60 * 1000,
    )
    create_relation(s0, s1, "related_to", path=mtca_db)
    create_relation(s1, s2, "derived_from", path=mtca_db)

    out = export_to_json(s0, depth=2, path=mtca_db)

    # 顶层字段
    assert out["center_id"] == s0
    assert out["depth"] == 2
    assert isinstance(out["nodes"], list)
    assert isinstance(out["edges"], list)
    assert "truncated" in out

    # 节点字段
    node_by_id = {n["id"]: n for n in out["nodes"]}
    assert s0 in node_by_id
    assert s1 in node_by_id
    node0 = node_by_id[s0]
    for key in (
        "topic_label", "fog_anchor", "start_at", "end_at",
        "fog_state", "silence_state",
    ):
        assert key in node0

    # 边字段
    assert len(out["edges"]) >= 2
    edge = out["edges"][0]
    for key in ("source", "target", "type", "weight", "auto_created", "created_at"):
        assert key in edge

    # JSON 可序列化（确保不漏特殊对象）
    json.dumps(out, ensure_ascii=False)


def test_export_to_json_depth_truncation(mtca_db: Path) -> None:
    """depth=1 仅导出 1-hop 邻居；无关系时只含中心节点。"""
    base = _now_ms()
    s0 = _insert_segment(
        mtca_db, topic_label="漫剧方案", fog_anchor="漫剧 A",
        start_at_ms=base - 10 * 60 * 1000,
    )
    s1 = _insert_segment(
        mtca_db, topic_label="漫剧方案", fog_anchor="漫剧 B",
        start_at_ms=base - 5 * 60 * 1000,
    )
    s2 = _insert_segment(
        mtca_db, topic_label="工作项目", fog_anchor="漫剧 C",
        start_at_ms=base - 1 * 60 * 1000,
    )
    # 真正孤立段（没有任何关系）
    lonely = _insert_segment(
        mtca_db, topic_label="财务", fog_anchor="独立话题",
        start_at_ms=base - 30 * 60 * 1000,
    )
    create_relation(s0, s1, "related_to", path=mtca_db)
    create_relation(s1, s2, "derived_from", path=mtca_db)

    out1 = export_to_json(s0, depth=1, path=mtca_db)
    ids1 = {n["id"] for n in out1["nodes"]}
    # depth=1：s0 + s1（s2 是 2-hop，不进）
    assert ids1 == {s0, s1}

    # 真正孤立的段：仅有自己
    out_lonely = export_to_json(lonely, depth=2, path=mtca_db)
    ids_lonely = {n["id"] for n in out_lonely["nodes"]}
    assert ids_lonely == {lonely}


# ---------------------------------------------------------------------------
# 测试 7：常量和边界
# ---------------------------------------------------------------------------


def test_constants_and_dedupe(mtca_db: Path) -> None:
    """常量 + dedupe + 权重夹紧综合。"""
    # 常量
    assert WEIGHT_MIN == 0.0
    assert WEIGHT_MAX == 10.0
    assert MAX_DEPTH >= 2
    assert EXPORT_NODE_MAX > 0

    # dedupe
    a = _insert_segment(mtca_db)
    b = _insert_segment(mtca_db)
    r1 = create_relation(a, b, "related_to", path=mtca_db)
    r2 = create_relation(a, b, "related_to", path=mtca_db)
    assert r1["relation_id"] == r2["relation_id"]
    assert r2["deduped"] is True

    # 反向（A→B 与 B→A 是不同方向，dedupe 不应合并）
    c = _insert_segment(mtca_db)
    d = _insert_segment(mtca_db)
    r3 = create_relation(c, d, "related_to", path=mtca_db)
    r4 = create_relation(d, c, "related_to", path=mtca_db)
    assert r3["relation_id"] != r4["relation_id"]
    assert r4["deduped"] is False