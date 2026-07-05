"""矛盾检测器（src/lifecycle/contradiction_detector.py - T22）

实现 V0.4_PIVOT.md §3.7 + DATA_MODEL.md §5「矛盾检测与 supersede」：

新段落创建后异步触发，扫描同 topic_label + 最近 90 天 + active
且未 supersede 的候选段，对每对调用 LLM 判断是否矛盾；返回
[{a_id, b_id, level, reason}]。再由调用方选择是否 ``apply_supersede``
或 ``user_override`` 撤销。

公共 API：
- 常量：``CONTRADICTION_WINDOW_DAYS`` / ``MAX_CANDIDATES`` /
  ``CONTENT_PREVIEW_CHARS`` / ``VALID_LEVELS`` / ``VALID_RELATIONS`` /
  ``VALID_ACTIONS``
- ``find_candidates(new_segment_id, path=None, now_ms=None,
  window_days=90, max_candidates=10) -> list[dict]``
- ``build_prompt(seg_a, seg_b) -> str``
- ``parse_verdict(raw) -> dict``
- ``detect_contradiction(new_segment_id, provider=None, path=None,
  now_ms=None) -> list[dict]``
- ``apply_supersede(a_id, b_id, path=None, now_ms=None) -> dict``
- ``user_override(a_id, action, b_id=None, path=None, now_ms=None) -> dict``
- ``detect_contradiction_async(...)``
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Union

from src.l0.segment_writer import get_segment, update_segment
from src.store.sqlite import execute, query

# ---------------------------------------------------------------------------
# 常量（V0.4_PIVOT.md §3.7）
# ---------------------------------------------------------------------------

CONTRADICTION_WINDOW_DAYS: int = 90
MAX_CANDIDATES: int = 10
CONTENT_PREVIEW_CHARS: int = 500
VALID_LEVELS: tuple[str, ...] = ("minor", "major", "full")
VALID_RELATIONS: tuple[str, ...] = (
    "supersedes", "related_to", "references", "derived_from",
)
VALID_ACTIONS: tuple[str, ...] = ("accept", "branch", "revoke")

_PROMPT_TEMPLATE: str = (
    "段落 A 说 {a_content}\n"
    "段落 B 说 {b_content}\n"
    "是否矛盾？级别？"
)
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*?\}")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _days_to_ms(days: int) -> int:
    return int(days) * 24 * 60 * 60 * 1000


def _validate_segment_id(segment_id: Any) -> str:
    if not segment_id or not isinstance(segment_id, str):
        raise ValueError("segment_id 必须是非空字符串")
    return str(segment_id)


def _get_segment_or_raise(segment_id: str, path) -> dict:
    sid = _validate_segment_id(segment_id)
    seg = get_segment(sid, path=path)
    if not seg:
        raise ValueError(f"段落不存在：{sid}")
    return seg


def _safe_text(seg: dict) -> str:
    """组合 L0 骨架（topic_label + fog_anchor）作为段落内容预览。

    L0-细节 messages.content 在 fog 后会 NULL；segments 表本身没有
    content 列，所以 LLM 输入侧用骨架组合代替（防 NULL）。
    """
    label = (seg.get("topic_label") or "").strip()
    anchor = (seg.get("fog_anchor") or "").strip()
    if label and anchor and anchor != label:
        text = f"{label}：{anchor}"
    else:
        text = anchor or label or ""
    return text[:CONTENT_PREVIEW_CHARS]


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _gen_relation_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 候选段查询
# ---------------------------------------------------------------------------


def find_candidates(
    new_segment_id: str,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
    window_days: int = CONTRADICTION_WINDOW_DAYS,
    max_candidates: int = MAX_CANDIDATES,
) -> list[dict]:
    """矛盾检测候选段：同 topic_label + 最近 window_days + active + 未 supersede。"""
    sid = _validate_segment_id(new_segment_id)
    new_seg = get_segment(sid, path=path)
    if not new_seg:
        return []
    topic = (new_seg.get("topic_label") or "").strip()
    if not topic:
        return []
    safe_window = (
        int(window_days) if isinstance(window_days, int) and window_days > 0
        else CONTRADICTION_WINDOW_DAYS
    )
    safe_max = (
        int(max_candidates) if isinstance(max_candidates, int) and max_candidates > 0
        else MAX_CANDIDATES
    )
    now = int(now_ms) if now_ms is not None else _now_ms()
    threshold_ms = now - _days_to_ms(safe_window)
    return query(
        "SELECT * FROM segments "
        "WHERE topic_label = ? AND silence_state = 'active' "
        "AND superseded_by IS NULL AND start_at >= ? "
        "AND segment_id != ? "
        "ORDER BY start_at DESC LIMIT ?",
        (topic, threshold_ms, sid, safe_max),
        path=path,
    )


# ---------------------------------------------------------------------------
# Prompt 构建 + LLM 响应解析
# ---------------------------------------------------------------------------


def build_prompt(seg_a: dict, seg_b: dict) -> str:
    """构造矛盾的 LLM prompt。任一段缺失返回空串。"""
    if not isinstance(seg_a, dict) or not isinstance(seg_b, dict):
        return ""
    a_text = _safe_text(seg_a)
    b_text = _safe_text(seg_b)
    if not a_text or not b_text:
        return ""
    return _PROMPT_TEMPLATE.format(a_content=a_text, b_content=b_text)


def parse_verdict(raw: Any) -> dict:
    """解析 LLM 输出 → 标准 verdict dict（容错）。

    输入：str / dict / bytes 皆可。
    输出：``{contradicts: bool, level: str, reason: str, raw: str}``。
    解析失败：``{False, '', '', str(raw)}``。
    """
    fallback: dict[str, Any] = {
        "contradicts": False, "level": "", "reason": "", "raw": "",
    }
    if raw is None:
        return fallback
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return {**fallback, "raw": str(raw)}
    else:
        text = str(raw)
    fallback["raw"] = text
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    data: Any = None
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        m = _JSON_BLOCK_RE.search(stripped)
        if m:
            try:
                data = json.loads(m.group(0))
            except (ValueError, TypeError):
                data = None
    if not isinstance(data, dict):
        return fallback

    # contradicts 字段
    contradicts_raw = data.get("contradicts")
    if isinstance(contradicts_raw, bool):
        contradicts = contradicts_raw
    else:
        sval = str(contradicts_raw or "").strip().lower()
        contradicts = sval in ("true", "yes", "1", "矛盾", "是")

    # level 字段
    level_raw = str(data.get("level") or "").strip().lower()
    if level_raw not in VALID_LEVELS:
        alias = {
            "小": "minor", "轻微": "minor", "minor_contradiction": "minor",
            "中": "major", "重大": "major", "部分矛盾": "major",
            "完全": "full", "严重": "full", "全部": "full",
            "none": "", "no": "", "否": "", "不矛盾": "",
        }
        level_raw = alias.get(level_raw, "")
    if level_raw and level_raw not in VALID_LEVELS:
        level_raw = ""
    if not contradicts:
        level_raw = ""

    reason = str(data.get("reason") or "").strip()
    if len(reason) > 200:
        reason = reason[:200]
    return {
        "contradicts": bool(contradicts),
        "level": level_raw, "reason": reason, "raw": text,
    }


def _call_provider(provider: Any, prompt: str) -> dict:
    """调一次 provider.generate；任何异常吞掉，返回非矛盾 verdict。"""
    if provider is None or not hasattr(provider, "generate"):
        return {"contradicts": False, "level": "", "reason": "no_provider", "raw": ""}
    try:
        raw = provider.generate(prompt)
    except (RuntimeError, ValueError, TypeError) as exc:
        return {
            "contradicts": False, "level": "",
            "reason": f"err:{exc!s}"[:80], "raw": "",
        }
    return parse_verdict(raw)


def _level_score(level: str) -> float:
    """矛盾级别 → 权重：minor=0.3 / major=0.6 / full=1.0。"""
    return {"minor": 0.3, "major": 0.6, "full": 1.0}.get(
        str(level).strip().lower(), 0.0,
    )


# ---------------------------------------------------------------------------
# 主入口：detect_contradiction
# ---------------------------------------------------------------------------


def detect_contradiction(
    new_segment_id: str,
    provider: Any = None,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> list[dict]:
    """矛盾检测主入口：找候选 → 两两比 → 汇总返回。

    无 provider 时仍返回候选空 verdict（便于离线 / 单测）。
    """
    sid = _validate_segment_id(new_segment_id)
    seg_new = get_segment(sid, path=path)
    if not seg_new:
        return []
    candidates = find_candidates(sid, path=path, now_ms=now_ms)
    if not candidates:
        return []

    results: list[dict] = []
    for cand in candidates:
        a_id, b_id = sid, cand["segment_id"]
        prompt = build_prompt(seg_new, cand)
        if not prompt:
            results.append({
                "a_id": a_id, "b_id": b_id,
                "contradicts": False, "level": "", "reason": "empty_prompt",
                "score": 0.0,
            })
            continue
        verdict = _call_provider(provider, prompt)
        level = str(verdict.get("level") or "")
        results.append({
            "a_id": a_id, "b_id": b_id,
            "contradicts": bool(verdict.get("contradicts")),
            "level": level if verdict.get("contradicts") else "",
            "reason": str(verdict.get("reason") or ""),
            "score": _level_score(level) if verdict.get("contradicts") else 0.0,
        })
    return results


# ---------------------------------------------------------------------------
# apply_supersede
# ---------------------------------------------------------------------------


def apply_supersede(
    a_id: str,
    b_id: str,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> dict:
    """写入 B 取代 A 的关系 + 标记 A 被取代。"""
    sid_a = _validate_segment_id(a_id)
    sid_b = _validate_segment_id(b_id)
    if sid_a == sid_b:
        raise ValueError("a_id 与 b_id 必须不同")
    seg_a = _get_segment_or_raise(sid_a, path=path)
    _get_segment_or_raise(sid_b, path=path)  # 存在性校验

    now = int(now_ms) if now_ms is not None else _now_ms()
    relation_id = _gen_relation_id()

    execute(
        "INSERT INTO segment_relations "
        "(relation_id, seg_a_id, seg_b_id, relation_type, "
        "weight, auto_created, created_at, expires_at) "
        "VALUES (?, ?, ?, 'supersedes', 1.0, 1, ?, NULL)",
        (relation_id, sid_a, sid_b, now),
        path=path,
    )
    update_segment(
        sid_a, path=path,
        superseded_by=sid_b,
        supersedes_count=_coerce_int(seg_a.get("supersedes_count"), 0) + 1,
    )
    try:
        execute(
            "INSERT INTO score_events "
            "(segment_id, event_type, delta, old_score, "
            "new_score, reason, created_at) "
            "VALUES (?, 'supersede_log', NULL, NULL, NULL, ?, ?)",
            (sid_a, f"apply_supersede: {sid_a} -> {sid_b}", now),
            path=path,
        )
    except RuntimeError:
        pass
    return {
        "relation_id": relation_id, "a_id": sid_a, "b_id": sid_b,
        "type": "supersedes", "auto_created": 1, "created_at": now,
        "level": str(seg_a.get("contradiction_level") or ""),
    }


# ---------------------------------------------------------------------------
# user_override（accept / branch / revoke）
# ---------------------------------------------------------------------------


def _revoke_relations(
    a_id: str, b_id: Optional[str],
    path: Optional[Union[Path, str]],
) -> int:
    """删除 a_id 涉及的 supersedes / related_to 关系。"""
    rel_types = ("supersedes", "related_to")
    placeholders = ",".join("?" for _ in rel_types)
    if b_id:
        rows = query(
            "SELECT relation_id FROM segment_relations "
            f"WHERE relation_type IN ({placeholders}) "
            "AND ((seg_a_id = ? AND seg_b_id = ?) "
            "OR (seg_a_id = ? AND seg_b_id = ?))",
            (*rel_types, a_id, b_id, b_id, a_id),
            path=path,
        )
    else:
        rows = query(
            "SELECT relation_id FROM segment_relations "
            f"WHERE relation_type IN ({placeholders}) "
            "AND (seg_a_id = ? OR seg_b_id = ?)",
            (*rel_types, a_id, a_id),
            path=path,
        )
    deleted = 0
    for row in rows:
        rid = row.get("relation_id")
        if not rid:
            continue
        try:
            execute(
                "DELETE FROM segment_relations WHERE relation_id = ?",
                (rid,),
                path=path,
            )
            deleted += 1
        except RuntimeError:
            continue
    return deleted


def user_override(
    a_id: str,
    action: str,
    b_id: Optional[str] = None,
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> dict:
    """用户对矛盾检测结果的人工覆盖（accept / branch / revoke）。"""
    sid_a = _validate_segment_id(a_id)
    if not isinstance(action, str):
        raise ValueError(f"action 必须是 {VALID_ACTIONS} 之一")
    norm = action.strip().lower()
    if norm not in VALID_ACTIONS:
        raise ValueError(f"action 必须是 {VALID_ACTIONS} 之一，收到：{action!r}")
    if norm in ("accept", "branch") and not b_id:
        raise ValueError(f"action={norm!r} 必须提供 b_id")
    if b_id:
        sid_b = _validate_segment_id(b_id)
        if sid_b == sid_a:
            raise ValueError("a_id 与 b_id 必须不同")
        _get_segment_or_raise(sid_b, path=path)
    else:
        sid_b = ""
    _get_segment_or_raise(sid_a, path=path)
    now = int(now_ms) if now_ms is not None else _now_ms()

    if norm == "accept":
        result = apply_supersede(sid_a, sid_b, path=path, now_ms=now)
        return {
            "action": "accept", "a_id": sid_a, "b_id": sid_b,
            "relation_id": result["relation_id"],
            "created_at": result["created_at"],
        }
    if norm == "branch":
        relation_id = _gen_relation_id()
        execute(
            "INSERT INTO segment_relations "
            "(relation_id, seg_a_id, seg_b_id, relation_type, "
            "weight, auto_created, created_at, expires_at) "
            "VALUES (?, ?, ?, 'related_to', 1.0, 0, ?, NULL)",
            (relation_id, sid_a, sid_b, now),
            path=path,
        )
        try:
            execute(
                "INSERT INTO score_events "
                "(segment_id, event_type, delta, old_score, "
                "new_score, reason, created_at) "
                "VALUES (?, 'branch_log', NULL, NULL, NULL, ?, ?)",
                (sid_a, f"user_override branch: {sid_a} ~ {sid_b}", now),
                path=path,
            )
        except RuntimeError:
            pass
        return {
            "action": "branch", "a_id": sid_a, "b_id": sid_b,
            "relation_id": relation_id, "created_at": now,
        }
    # revoke：删关系，恢复 A 的 superseded_by 为 NULL
    # 注：segment_writer.update_segment 会跳过 None 值，需直接 execute
    deleted = _revoke_relations(sid_a, sid_b or None, path=path)
    try:
        execute(
            "UPDATE segments SET superseded_by = NULL WHERE segment_id = ?",
            (sid_a,),
            path=path,
        )
    except RuntimeError:
        pass
    try:
        execute(
            "INSERT INTO score_events "
            "(segment_id, event_type, delta, old_score, "
            "new_score, reason, created_at) "
            "VALUES (?, 'revoke_log', NULL, NULL, NULL, ?, ?)",
            (
                sid_a,
                f"user_override revoke: a={sid_a} b={sid_b or '*'} deleted={deleted}",
                now,
            ),
            path=path,
        )
    except RuntimeError:
        pass
    return {
        "action": "revoke", "a_id": sid_a, "b_id": sid_b or None,
        "deleted_relations": int(deleted), "created_at": now,
    }


# ---------------------------------------------------------------------------
# 异步入口 + 关系查询
# ---------------------------------------------------------------------------


def detect_contradiction_async(
    new_segment_id: str,
    provider: Any = None,
    path: Optional[Union[Path, str]] = None,
    on_done: Optional[Callable[[list[dict]], None]] = None,
) -> threading.Thread:
    """异步跑矛盾检测（daemon 线程）。返回 Thread 句柄，调用方负责 ``.start()``。"""
    def _runner() -> None:
        try:
            results = detect_contradiction(
                new_segment_id, provider=provider, path=path,
            )
        except Exception:
            results = []
        if callable(on_done):
            try:
                on_done(results)
            except Exception:
                pass

    return threading.Thread(
        target=_runner,
        name=f"contradiction-{_validate_segment_id(new_segment_id)[:8]}",
        daemon=True,
    )


def list_supersede_relations(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """列出与某段相关的 supersedes / related_to 关系。"""
    sid = _validate_segment_id(segment_id)
    rel_types = ("supersedes", "related_to")
    placeholders = ",".join("?" for _ in rel_types)
    return query(
        "SELECT * FROM segment_relations "
        f"WHERE relation_type IN ({placeholders}) "
        "AND (seg_a_id = ? OR seg_b_id = ?) "
        "ORDER BY created_at DESC",
        (*rel_types, sid, sid),
        path=path,
    )


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


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


__all__ = [
    "CONTRADICTION_WINDOW_DAYS", "MAX_CANDIDATES",
    "CONTENT_PREVIEW_CHARS", "VALID_LEVELS", "VALID_RELATIONS",
    "VALID_ACTIONS",
    "find_candidates", "build_prompt", "parse_verdict",
    "detect_contradiction", "apply_supersede", "user_override",
    "detect_contradiction_async",
    "list_supersede_relations", "get_relation",
]
