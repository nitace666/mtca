"""MTCA 雾化召回协议 (v0.4 / T9)

实现 3 个纯函数，对召回结果施加 fog / silence / supersede 规则：

- ``apply_fog_protocol(results)``
    处理 fog 三态：clear / fogged_once / archived
- ``filter_silent_segments(segments, include_dormant=True)``
    过滤静默态：active / dormant（×0.5）/ silent（移除）
- ``apply_supersede(results, today_iso=None)``
    处理矛盾取代：A 被 B supersede → 移除 A，B 加注

设计要点：
- **纯函数**：不写 DB，不修改入参；返回新 list / 新 dict
- **fog 状态转移仅在 dict 内**（不再二次擦除、不写 score_events），
  fogged_once 第一次召回返回骨架提示后，把 dict 标 ``archived``，
  下次再调用 ``apply_fog_protocol`` 自动被过滤掉（一次性提示）
- **dormant 分数减半**：保留进默认召回，仅降权
- **supersede 注解**：以 system role 消息追加在 B 的 messages 末尾
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# dormant 段分数衰减系数（半衰）
_DORMANT_SCORE_FACTOR: float = 0.5

# fogged_once 骨架消息模板：片段已删除（[fog_anchor]）
_FOG_SKELETON_PREFIX: str = "片段已删除（"
_FOG_SKELETON_SUFFIX: str = "）"

# fog_anchor 缺失时的回退文案
_FOG_ANCHOR_FALLBACK: str = "已删除"

# 矛盾取代注解模板
_SUPERSEDE_NOTICE_TEMPLATE: str = "本方案取代了其他方案（{date}）"

# 默认 fog_state（缺失字段时回落）
_DEFAULT_FOG_STATE: str = "clear"

# 默认 silence_state（缺失字段时回落）
_DEFAULT_SILENCE_STATE: str = "active"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _to_float(value: Any, default: float = 0.0) -> float:
    """规整值为 float；非法值回落 default。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    """规整值为 int；非法值回落 default。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_today_iso(today_iso: Optional[str]) -> str:
    """解析日期 ISO 字符串；None 用今日。"""
    if today_iso:
        return str(today_iso)
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# 公开 API 1：fog 三态协议
# ---------------------------------------------------------------------------


def apply_fog_protocol(results: list[dict]) -> list[dict]:
    """应用 fog 三态协议到召回结果（纯函数，不写 DB）。

    行为（与 ``V0.4_PIVOT.md §3.1`` 对齐）：

    - ``fog_state='clear'``：原样保留，messages 不变。
    - ``fog_state='fogged_once'``：替换 ``messages`` 为
      ``[{role:'system', content:'片段已删除（[fog_anchor]）}]``；
      并把 dict 的 ``fog_state`` 标记为 ``'archived'``（一次性提示）。
    - ``fog_state='archived'``：从结果中移除（不再主动召回）。

    参数：
        results: 召回结果 list，每项是 dict，至少含 ``fog_state`` /
            ``fog_anchor`` / ``messages`` 字段。

    返回：
        新 list（不修改入参）；元素为浅拷贝的 dict。
    """
    out: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        state = str(r.get("fog_state") or _DEFAULT_FOG_STATE)

        if state == "archived":
            # 永久归档：跳过
            continue

        if state == "fogged_once":
            # 一次性提示：替换 messages + 标 archived（dict 内）
            anchor_raw = r.get("fog_anchor")
            anchor = str(anchor_raw).strip() if anchor_raw else ""
            if not anchor:
                anchor = _FOG_ANCHOR_FALLBACK
            new_r = dict(r)
            new_r["messages"] = [{
                "role": "system",
                "content": f"{_FOG_SKELETON_PREFIX}{anchor}{_FOG_SKELETON_SUFFIX}",
            }]
            new_r["fog_state"] = "archived"
            out.append(new_r)
            continue

        # clear 或其他未知态：原样保留
        out.append(dict(r))
    return out


# ---------------------------------------------------------------------------
# 公开 API 2：静默态过滤
# ---------------------------------------------------------------------------


def filter_silent_segments(
    segments: list[dict],
    include_dormant: bool = True,
) -> list[dict]:
    """按静默态过滤段落（纯函数）。

    行为（与 ``V0.4_PIVOT.md §3.6`` 对齐）：

    - ``active``：保留（score 不变）。
    - ``dormant``：保留，score × 0.5（仅当 ``include_dormant=True``）。
    - ``silent``：移除（不主动召回）。

    参数：
        segments: 段落 list，每项是 dict，至少含 ``silence_state`` /
            ``score`` 字段。
        include_dormant: 是否保留 dormant 段；``False`` 时 dormant 也被过滤。

    返回：
        新 list（不修改入参）；dormant 元素为浅拷贝 + score 重算。
    """
    out: list[dict] = []
    for s in segments:
        if not isinstance(s, dict):
            continue
        state = str(s.get("silence_state") or _DEFAULT_SILENCE_STATE)

        if state == "silent":
            # 不主动召回
            continue

        if state == "dormant":
            if not include_dormant:
                continue
            new_s = dict(s)
            new_s["score"] = _to_float(s.get("score")) * _DORMANT_SCORE_FACTOR
            out.append(new_s)
            continue

        # active 或其他未知态：原样保留
        out.append(dict(s))
    return out


# ---------------------------------------------------------------------------
# 公开 API 3：矛盾取代
# ---------------------------------------------------------------------------


def apply_supersede(
    results: list[dict],
    today_iso: Optional[str] = None,
) -> list[dict]:
    """应用矛盾取代规则（纯函数）。

    行为（与 ``V0.4_PIVOT.md §3.7`` 对齐）：

    - A.superseded_by == B → 移除 A（不返回）。
    - B.supersedes_count > 0 → 在 B 的 messages 末尾追加一条 system
      注解：「本方案取代了其他方案（[date]）」。

    参数：
        results: 召回结果 list，每项是 dict，至少含 ``segment_id`` /
            ``superseded_by`` / ``supersedes_count`` / ``messages`` 字段。
        today_iso: 形如 ``'2026-07-05'`` 的日期字符串；None 时用今日。

    返回：
        新 list（不修改入参）；被取代段不在结果中。
    """
    today = _resolve_today_iso(today_iso)

    # 第一遍：收集所有被取代段的 segment_id
    superseded_ids: set[Any] = set()
    for r in results:
        if isinstance(r, dict) and r.get("superseded_by"):
            sid = r.get("segment_id")
            if sid is not None:
                superseded_ids.add(sid)

    out: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue

        sid = r.get("segment_id")
        if sid in superseded_ids:
            # A 被 B 取代：移除
            continue

        new_r = dict(r)
        sup_count = _to_int(r.get("supersedes_count"))
        if sup_count > 0:
            # B 是取代者：在 messages 末尾追加 system 注解
            msgs = list(new_r.get("messages") or [])
            msgs.append({
                "role": "system",
                "content": _SUPERSEDE_NOTICE_TEMPLATE.format(date=today),
            })
            new_r["messages"] = msgs
        out.append(new_r)
    return out


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


__all__ = [
    "apply_fog_protocol",
    "filter_silent_segments",
    "apply_supersede",
]