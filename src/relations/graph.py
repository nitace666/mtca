"""MTCA 关系图谱（src/relations/graph.py - T23）

实现 V0.4_PIVOT.md §3.8「多向引用图谱」4 种关系类型的 CRUD + 遍历 +
自动发现 + JSON 导出。底层表 ``segment_relations`` 在 sqlite.py 建好。

设计要点：
- 关系方向 ``seg_a_id → seg_b_id``；``get_related`` 同时匹配两端。
- 4 种关系：references / supersedes / related_to / derived_from。
- ``auto_create_relations`` 复用 ``extract_keywords`` 提关键词；复用
  ``detect_contradiction`` + ``apply_supersede`` 做矛盾 → supersede。
- BFS 走邻接表，深度上限 ``MAX_DEPTH`` 防爆栈。

公共 API：
- 常量：``VALID_RELATION_TYPES`` / ``DEFAULT_RELATION_TYPES`` /
  ``WEIGHT_MIN`` / ``WEIGHT_MAX`` / ``MAX_DEPTH`` /
  ``DERIVED_TIME_GAP_MS`` / ``DERIVED_KEYWORD_OVERLAP``
- ``create_relation / get_relation / list_relations / delete_relation``
- ``get_related / get_topic_cluster / get_supersede_chain``
- ``auto_create_relations / export_to_json``
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional, Union

from src.l0.segment_writer import get_segment
from src.l0.skeleton import extract_keywords
from src.lifecycle.contradiction_detector import (
    apply_supersede,
    detect_contradiction,
)
from src.store.sqlite import execute, query

# 常量（V0.4_PIVOT.md §3.8 4 种关系类型）

# 合法关系类型
VALID_RELATION_TYPES: tuple[str, ...] = (
    "references", "supersedes", "related_to", "derived_from",
)

# 默认 get_related 过滤的关系类型（related_to）
DEFAULT_RELATION_TYPES: tuple[str, ...] = ("related_to",)

# 权重范围（M1 防御性约束；越界自动夹紧）
WEIGHT_MIN: float = 0.0
WEIGHT_MAX: float = 10.0

# BFS 深度上限（防止异常配置导致内存爆栈）
MAX_DEPTH: int = 6

# ``derived_from`` 触发条件：
# 时间相邻窗口（同 session 内 / 跨 session 60 分钟内）+ 关键词重叠 ≥ 该值
DERIVED_TIME_GAP_MS: int = 60 * 60 * 1000  # 1 小时
DERIVED_KEYWORD_OVERLAP: int = 1  # 至少 1 个共同关键词

# auto_create_relations 关联候选数量上限（每关系类型单独限）
AUTO_CANDIDATE_LIMIT: int = 20

# ``get_supersede_chain`` 链长上限（防止环 / 异常）
SUPERSEDE_CHAIN_MAX: int = 50

# ``export_to_json`` 单图节点上限（防止导出爆炸）
EXPORT_NODE_MAX: int = 200


# 内部工具


def _now_ms() -> int:
    """返回当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _coerce_num(value: Any, default: float, caster) -> float:
    """通用 coerce：None / 非法值回落到 default。"""
    if value is None:
        return default
    try:
        return float(caster(value))
    except (TypeError, ValueError):
        return default


def _coerce_weight(weight: Any) -> float:
    """规整 weight 到 [WEIGHT_MIN, WEIGHT_MAX]。"""
    val = _coerce_num(weight, default=1.0, caster=float)
    return max(WEIGHT_MIN, min(WEIGHT_MAX, val))


def _validate_segment_id(segment_id: Any, name: str = "segment_id") -> str:
    """校验 segment_id 合法（用于 raise）。"""
    if not segment_id or not isinstance(segment_id, str):
        raise ValueError(f"{name} 必须是非空字符串")
    return str(segment_id)


def _validate_relation_type(relation_type: Any) -> str:
    """校验 relation_type 合法。"""
    if not relation_type or not isinstance(relation_type, str):
        raise ValueError("relation_type 必须是非空字符串")
    norm = relation_type.strip().lower()
    if norm not in VALID_RELATION_TYPES:
        raise ValueError(
            f"relation_type 必须是 {VALID_RELATION_TYPES} 之一，收到：{relation_type!r}"
        )
    return norm


def _get_segment_or_raise(segment_id: str, path: Optional[Union[Path, str]]) -> dict:
    """查询段落；不存在抛 ValueError。"""
    sid = _validate_segment_id(segment_id)
    seg = get_segment(sid, path=path)
    if not seg:
        raise ValueError(f"段落不存在：{sid}")
    return seg


def _normalize_types(types: Optional[list[str]]) -> tuple[str, ...]:
    """校验 types 列表并返回规范化元组。空 / None → DEFAULT_RELATION_TYPES。"""
    if not types:
        return DEFAULT_RELATION_TYPES
    normed: list[str] = []
    for t in types:
        if not t or not isinstance(t, str):
            continue
        nt = t.strip().lower()
        if nt in VALID_RELATION_TYPES:
            normed.append(nt)
    if not normed:
        raise ValueError(
            f"types 至少包含 1 个合法关系类型（{VALID_RELATION_TYPES}）"
        )
    return tuple(normed)


def _gen_relation_id() -> str:
    """生成关系 UUID。"""
    return str(uuid.uuid4())


# segment 关键词提取（基于 L0-骨架）


def _segment_text(seg: dict) -> str:
    """把段落骨架字段拼成单字符串（用于关键词提取）。"""
    parts: list[str] = []
    for key in ("topic_label", "fog_anchor"):
        val = seg.get(key)
        if val:
            parts.append(str(val))
    return " ".join(parts)


def _segment_keyword_set(seg: dict) -> set[str]:
    """从段落骨架字段提取关键词集合（复用 extract_keywords）。"""
    text = _segment_text(seg)
    if not text.strip():
        return set()
    msgs = [{"role": "user", "content": text, "seq": 1}]
    kws = extract_keywords(msgs, top_k=10)
    return set(kws)


# create_relation


def _find_existing_relation(
    seg_a: str,
    seg_b: str,
    relation_type: str,
    path: Optional[Union[Path, str]],
) -> Optional[dict]:
    """查找已存在的同方向同类型关系。"""
    rows = query(
        "SELECT * FROM segment_relations "
        "WHERE seg_a_id = ? AND seg_b_id = ? AND relation_type = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (seg_a, seg_b, relation_type),
        path=path,
    )
    return rows[0] if rows else None


def create_relation(
    seg_a: str,
    seg_b: str,
    relation_type: str,
    weight: float = 1.0,
    auto: bool = False,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> dict:
    """创建段与段之间的关系。返回 ``{relation_id, seg_a_id, seg_b_id,
    relation_type, weight, auto_created, created_at, deduped}``；
    ``deduped=True`` 表示同方向同类型关系已存在并命中。
    """
    sid_a = _validate_segment_id(seg_a, name="seg_a")
    sid_b = _validate_segment_id(seg_b, name="seg_b")
    if sid_a == sid_b:
        raise ValueError("seg_a 与 seg_b 必须不同")
    rtype = _validate_relation_type(relation_type)
    _get_segment_or_raise(sid_a, path=path)
    _get_segment_or_raise(sid_b, path=path)
    safe_weight = _coerce_weight(weight)
    existing = _find_existing_relation(sid_a, sid_b, rtype, path=path)
    if existing:
        return {
            "relation_id": existing["relation_id"],
            "seg_a_id": existing["seg_a_id"],
            "seg_b_id": existing["seg_b_id"],
            "relation_type": existing["relation_type"],
            "weight": float(existing.get("weight") or 1.0),
            "auto_created": int(existing.get("auto_created") or 0),
            "created_at": int(existing.get("created_at") or 0),
            "deduped": True,
        }
    now = int(now_ms) if now_ms is not None else _now_ms()
    relation_id = _gen_relation_id()
    execute(
        "INSERT INTO segment_relations "
        "(relation_id, seg_a_id, seg_b_id, relation_type, "
        "weight, auto_created, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (relation_id, sid_a, sid_b, rtype,
         float(safe_weight), 1 if auto else 0, now),
        path=path,
    )
    return {
        "relation_id": relation_id,
        "seg_a_id": sid_a, "seg_b_id": sid_b,
        "relation_type": rtype, "weight": float(safe_weight),
        "auto_created": 1 if auto else 0,
        "created_at": now, "deduped": False,
    }


# 查询 / 删除


def get_relation(
    relation_id: str,
    path: Optional[Union[Path, str]] = None,
) -> Optional[dict]:
    """按 relation_id 查关系行；不存在返回 None。"""
    if not relation_id or not isinstance(relation_id, str):
        return None
    rows = query(
        "SELECT * FROM segment_relations WHERE relation_id = ?",
        (relation_id,),
        path=path,
    )
    return rows[0] if rows else None


def list_relations(
    segment_id: str,
    types: Optional[list[str]] = None,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """列出与某段相关的全部关系（任一端匹配）。"""
    sid = _validate_segment_id(segment_id)
    norm_types = _normalize_types(types)
    placeholders = ",".join("?" for _ in norm_types)
    return query(
        "SELECT * FROM segment_relations "
        f"WHERE relation_type IN ({placeholders}) "
        "AND (seg_a_id = ? OR seg_b_id = ?) "
        "ORDER BY created_at DESC",
        (*norm_types, sid, sid),
        path=path,
    )


def delete_relation(
    relation_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """按 relation_id 删除关系行；返回受影响行数（0 或 1）。"""
    if not relation_id or not isinstance(relation_id, str):
        raise ValueError("relation_id 必须是非空字符串")
    return execute(
        "DELETE FROM segment_relations WHERE relation_id = ?",
        (relation_id,),
        path=path,
    )


# get_related


def get_related(
    segment_id: str,
    types: Optional[list[str]] = None,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """取与指定段相关的全部段落（按 types 过滤关系类型）。

    - ``types`` 默认 ``['related_to']``；传 None 等价默认。
    - 关系两端任一匹配即取对面段；按 segment_id 去重。
    """
    sid = _validate_segment_id(segment_id)
    norm_types = _normalize_types(types)
    placeholders = ",".join("?" for _ in norm_types)
    rows = query(
        "SELECT s.* FROM segment_relations r "
        "JOIN segments s ON "
        "(s.segment_id = r.seg_b_id AND r.seg_a_id = ?) "
        "OR (s.segment_id = r.seg_a_id AND r.seg_b_id = ?) "
        f"WHERE r.relation_type IN ({placeholders}) "
        "AND s.segment_id != ? "
        "ORDER BY r.created_at DESC",
        (sid, sid, *norm_types, sid),
        path=path,
    )
    # 去重（同一段可能经多条关系命中）
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        seg_id = row.get("segment_id")
        if seg_id and seg_id not in seen:
            seen.add(seg_id)
            out.append(row)
    return out


# get_topic_cluster（BFS）


def _build_adjacency(
    seed_ids: list[str],
    relation_types: tuple[str, ...],
    path: Optional[Union[Path, str]],
) -> dict[str, list[tuple[str, str]]]:
    """为 seeds 建邻接表：``seg_id -> [(neighbor_id, relation_type), ...]``。

    supersedes 仅保留 A→B；其他 3 种两端互通。
    """
    if not seed_ids:
        return {}
    placeholders = ",".join("?" for _ in relation_types)
    seeds_ph = ",".join("?" for _ in seed_ids)
    rows = query(
        "SELECT seg_a_id, seg_b_id, relation_type FROM segment_relations "
        f"WHERE relation_type IN ({placeholders}) "
        f"AND (seg_a_id IN ({seeds_ph}) OR seg_b_id IN ({seeds_ph}))",
        (*relation_types, *seed_ids, *seed_ids),
        path=path,
    )
    adj: dict[str, list[tuple[str, str]]] = {sid: [] for sid in seed_ids}
    for row in rows:
        a = row.get("seg_a_id")
        b = row.get("seg_b_id")
        rtype = row.get("relation_type")
        if not a or not b or not rtype:
            continue
        if rtype == "supersedes":
            adj.setdefault(a, []).append((b, rtype))
        else:
            adj.setdefault(a, []).append((b, rtype))
            adj.setdefault(b, []).append((a, rtype))
    return adj


def get_topic_cluster(
    segment_id: str,
    depth: int = 2,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """BFS 遍历：返回与起始段距离 ≤ depth 的全部段落。

    - depth=1 仅 1-hop；depth=2 含 2-hop。超 ``MAX_DEPTH`` 夹紧。
    - 起始段在前，其余按 BFS 顺序追加；按 segment_id 去重。
    """
    sid = _validate_segment_id(segment_id)
    _get_segment_or_raise(sid, path=path)
    safe_depth = max(1, min(int(depth) if isinstance(depth, int) else 2, MAX_DEPTH))
    queue: deque[tuple[str, int]] = deque([(sid, 0)])
    visited: dict[str, int] = {sid: 0}
    frontier: list[str] = [sid]
    while frontier and max(visited.values()) < safe_depth:
        adj = _build_adjacency(
            frontier, ("references", "related_to", "derived_from"), path=path,
        )
        next_frontier: list[str] = []
        for cur_id, cur_depth in list(queue):
            if cur_depth >= safe_depth:
                continue
            for nb_id, _rtype in adj.get(cur_id, []):
                if not nb_id or nb_id == cur_id or nb_id in visited:
                    continue
                visited[nb_id] = cur_depth + 1
                next_frontier.append(nb_id)
                queue.append((nb_id, cur_depth + 1))
        frontier = next_frontier
        if not next_frontier:
            break
    ids = list(visited.keys())
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = query(
        f"SELECT * FROM segments WHERE segment_id IN ({placeholders})",
        tuple(ids), path=path,
    )
    by_id = {r.get("segment_id"): r for r in rows if r.get("segment_id")}
    out: list[dict] = []
    seen: set[str] = set()
    if sid in by_id:
        out.append(by_id[sid])
        seen.add(sid)
    for sid_v in visited:
        if sid_v in seen:
            continue
        row = by_id.get(sid_v)
        if row:
            out.append(row)
            seen.add(sid_v)
    return out


# get_supersede_chain（递归查 superseded_by + 链头）


def get_supersede_chain(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """沿 ``superseded_by`` 字段递归拼出完整取代链（旧 → 新）。

    ``A.superseded_by = B`` 表示 A 被 B 取代，A 是旧段、B 是新段。
    链长上限 ``SUPERSEDE_CHAIN_MAX``，超限附 ``truncated=True``。
    """
    sid = _validate_segment_id(segment_id)
    _get_segment_or_raise(sid, path=path)
    sql = (
        "WITH RECURSIVE chain(seg_id, depth) AS ("
        "  SELECT segment_id, 0 FROM segments WHERE segment_id = ?"
        "  UNION ALL"
        "  SELECT s.superseded_by, c.depth + 1"
        "  FROM segments s JOIN chain c ON s.segment_id = c.seg_id"
        "  WHERE s.superseded_by IS NOT NULL AND c.depth < ?"
        ") SELECT seg_id FROM chain ORDER BY depth ASC"
    )
    rows = query(sql, (sid, SUPERSEDE_CHAIN_MAX - 1), path=path)
    if not rows:
        return []
    ids = [r["seg_id"] for r in rows if r.get("seg_id")]
    truncated = len(ids) >= SUPERSEDE_CHAIN_MAX
    placeholders = ",".join("?" for _ in ids)
    seg_rows = query(
        f"SELECT * FROM segments WHERE segment_id IN ({placeholders})",
        tuple(ids),
        path=path,
    )
    by_id = {r.get("segment_id"): r for r in seg_rows if r.get("segment_id")}
    out: list[dict] = []
    for cid in ids:
        row = by_id.get(cid)
        if row:
            entry = dict(row)
            if truncated:
                entry["truncated"] = True
            out.append(entry)
    return out


# auto_create_relations


def _find_same_topic_segments(
    segment: dict,
    limit: int,
    path: Optional[Union[Path, str]],
) -> list[dict]:
    """按 topic_label LIKE 找同话题其他段（排除自身）。"""
    topic = (segment.get("topic_label") or "").strip()
    if not topic:
        return []
    return query(
        "SELECT * FROM segments "
        "WHERE topic_label = ? AND segment_id != ? "
        "ORDER BY start_at DESC LIMIT ?",
        (topic, segment["segment_id"], int(limit)),
        path=path,
    )


def _find_time_neighbors(
    segment: dict,
    gap_ms: int,
    limit: int,
    path: Optional[Union[Path, str]],
) -> list[dict]:
    """找时间相邻段（start_at 差 ≤ gap_ms，排除自身）。"""
    start_at = int(_coerce_num(segment.get("start_at"), 0, caster=int))
    if start_at <= 0:
        return []
    lower = start_at - int(gap_ms)
    upper = start_at + int(gap_ms)
    return query(
        "SELECT * FROM segments "
        "WHERE segment_id != ? AND start_at BETWEEN ? AND ? "
        "ORDER BY start_at ASC LIMIT ?",
        (segment["segment_id"], lower, upper, int(limit)),
        path=path,
    )


def _keyword_overlap_count(a: set[str], b: set[str]) -> int:
    """两个关键词集合的重叠数。"""
    if not a or not b:
        return 0
    return len(a & b)


def auto_create_relations(
    segment_id: str,
    provider: Any = None,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> dict:
    """自动发现并建立关系：related_to / derived_from / supersedes。

    返回 ``{related_to, derived_from, supersedes, relations,
    contradictions}``，relations 含本次新建的全部关系 dict。
    """
    sid = _validate_segment_id(segment_id)
    seg = _get_segment_or_raise(sid, path=path)
    now = int(now_ms) if now_ms is not None else _now_ms()
    created: list[dict] = []
    n_rel = n_der = n_sup = 0
    seg_kws = _segment_keyword_set(seg)

    # 1. related_to：同 topic
    for cand in _find_same_topic_segments(seg, AUTO_CANDIDATE_LIMIT, path=path):
        r = create_relation(
            sid, cand["segment_id"], "related_to",
            weight=1.0, auto=True, path=path, now_ms=now,
        )
        if not r.get("deduped"):
            n_rel += 1
            created.append(r)

    # 2. derived_from：时间相邻 + 关键词重叠
    for cand in _find_time_neighbors(seg, DERIVED_TIME_GAP_MS, AUTO_CANDIDATE_LIMIT, path=path):
        if _keyword_overlap_count(seg_kws, _segment_keyword_set(cand)) < DERIVED_KEYWORD_OVERLAP:
            continue
        cand_start = int(_coerce_num(cand.get("start_at"), 0, caster=int))
        seg_start = int(_coerce_num(seg.get("start_at"), 0, caster=int))
        # 「更早的」是来源，「较新的」是衍生
        if cand_start <= seg_start:
            src_id, tgt_id = cand["segment_id"], sid
        else:
            src_id, tgt_id = sid, cand["segment_id"]
        r = create_relation(
            src_id, tgt_id, "derived_from",
            weight=0.8, auto=True, path=path, now_ms=now,
        )
        if not r.get("deduped"):
            n_der += 1
            created.append(r)

    # 3. supersedes：矛盾检测
    contradictions = detect_contradiction(
        sid, provider=provider, path=path, now_ms=now,
    )
    for c in contradictions:
        if not c.get("contradicts"):
            continue
        b_id = c.get("b_id")  # 旧段（被取代者）
        if not b_id or b_id == sid:
            continue
        try:
            # 语义：旧段 b_id 被新段 sid 取代
            sup_result = apply_supersede(b_id, sid, path=path, now_ms=now)
        except (ValueError, RuntimeError):
            continue
        n_sup += 1
        created.append({
            "relation_id": sup_result.get("relation_id"),
            "seg_a_id": b_id,
            "seg_b_id": sid,
            "relation_type": "supersedes",
            "weight": 1.0,
            "auto_created": 1,
            "created_at": sup_result.get("created_at", now),
            "deduped": False,
            "level": sup_result.get("level", ""),
        })

    return {
        "related_to": int(n_rel),
        "derived_from": int(n_der),
        "supersedes": int(n_sup),
        "relations": created,
        "contradictions": contradictions,
    }


# export_to_json


def _segment_node(seg: dict) -> dict:
    """段落 dict → GUI 节点 dict。"""
    return {
        "id": seg.get("segment_id"),
        "topic_label": seg.get("topic_label") or "",
        "fog_anchor": seg.get("fog_anchor") or "",
        "start_at": seg.get("start_at"),
        "end_at": seg.get("end_at"),
        "fog_state": seg.get("fog_state") or "clear",
        "silence_state": seg.get("silence_state") or "active",
        "current_score": seg.get("current_score"),
    }


def export_to_json(
    segment_id: str,
    depth: int = 2,
    path: Optional[Union[Path, str]] = None,
) -> dict:
    """导出指定段的关系子图为 JSON 友好的 dict（给 GUI 用）。

    结构：``{center_id, depth, nodes, edges, truncated}``。
    节点 / 边数超 ``EXPORT_NODE_MAX`` 自动截断并标 ``truncated=True``。
    """
    sid = _validate_segment_id(segment_id)
    _get_segment_or_raise(sid, path=path)
    safe_depth = max(1, min(int(depth) if isinstance(depth, int) else 2, MAX_DEPTH))
    cluster = get_topic_cluster(sid, depth=safe_depth, path=path)
    truncated = len(cluster) > EXPORT_NODE_MAX
    if truncated:
        cluster = cluster[:EXPORT_NODE_MAX]
    node_ids = [c.get("segment_id") for c in cluster if c.get("segment_id")]
    nodes = [_segment_node(c) for c in cluster]
    edges: list[dict] = []
    if node_ids:
        placeholders = ",".join("?" for _ in node_ids)
        rels = query(
            "SELECT * FROM segment_relations "
            f"WHERE seg_a_id IN ({placeholders}) "
            f"OR seg_b_id IN ({placeholders}) "
            "ORDER BY created_at ASC",
            (*node_ids, *node_ids),
            path=path,
        )
        node_set = set(node_ids)
        for r in rels:
            a = r.get("seg_a_id")
            b = r.get("seg_b_id")
            if not a or not b or a not in node_set or b not in node_set:
                continue
            edges.append({
                "source": a, "target": b,
                "type": r.get("relation_type"),
                "weight": float(r.get("weight") or 1.0),
                "auto_created": int(r.get("auto_created") or 0),
                "created_at": int(r.get("created_at") or 0),
            })
            if len(edges) >= EXPORT_NODE_MAX * 4:
                truncated = True
                break
    return {
        "center_id": sid, "depth": safe_depth,
        "nodes": nodes, "edges": edges, "truncated": truncated,
    }


# 导出


__all__ = [
    "VALID_RELATION_TYPES", "DEFAULT_RELATION_TYPES",
    "WEIGHT_MIN", "WEIGHT_MAX", "MAX_DEPTH",
    "DERIVED_TIME_GAP_MS", "DERIVED_KEYWORD_OVERLAP",
    "create_relation", "get_relation", "list_relations",
    "delete_relation",
    "get_related", "get_topic_cluster", "get_supersede_chain",
    "auto_create_relations", "export_to_json",
]