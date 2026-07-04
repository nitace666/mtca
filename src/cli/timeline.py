"""CLI 时间线（src/cli/timeline.py — T12 / v0.4）

按 USER_CONTROLS.md §6 用 rich 库实现 MTCA 时间线 + 树状视图，
便于开发期直接看数据库内容。

颜色规则（与 §6.1 对齐）：
  * active      → 亮色（cyan）
  * dormant     → 灰色（grey50）
  * silent      → 暗色（dim grey23）
  * fogged_once → 红框（bold white on red）
  * superseded  → 删除线（strike）

``render_timeline`` / ``render_tree`` 返回 rich Console 捕获的字符串，
便于 GUI 嵌入或测试断言；不直接打印、不修改数据库。

公共 API：
- ``render_timeline(period='day', project=None, query_text=None,
  limit=100, path=None) -> str``
- ``render_tree(project=None, limit=200, path=None) -> str``
- ``main()`` click 组装的 CLI 入口 + argparse fallback
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Optional, Union

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from src.recall.recall_engine import search_segments
from src.store.sqlite import query


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_VALID_PERIODS: frozenset[str] = frozenset({"day", "week", "month"})
_PERIOD_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30}
_TIMELINE_LIMIT: int = 100
_TREE_LIMIT: int = 200

# 段落状态 → (rich style, 标签)
_STYLE_ACTIVE = "bold cyan"
_STYLE_DORMANT = "grey50"
_STYLE_SILENT = "dim italic grey23"
_STYLE_FOGGED = "bold white on red"
_STYLE_SUPERSEDED = "strike grey50"
_STYLE_ARCHIVED = "dim grey15"
_STYLE_HEADER = "bold magenta"
_STYLE_SUBHEAD = "bold yellow"
_STYLE_EMPTY = "italic yellow"
_STYLE_META = "dim"

_LABEL_SUPERSEDED = "[superseded]"
_LABEL_FOGGED = "[/雾化]"
_LABEL_ARCHIVED = "[archived]"
_LABEL_SILENT = "[silent]"
_LABEL_DORMANT = "[dormant]"
_LABEL_ACTIVE = "[active]"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _period_window_ms(period: str, now_ms: Optional[int] = None) -> tuple[int, int]:
    """period 对应的时间窗口 (start_ms, end_ms)。"""
    if now_ms is None:
        now_ms = _now_ms()
    days = _PERIOD_DAYS.get(period, 1)
    return now_ms - days * 24 * 3600 * 1000, now_ms


def _format_day(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d") if ms else "----"


def _format_time_range(start_ms: int, end_ms: int) -> str:
    if not start_ms or not end_ms:
        return "----"
    s = datetime.fromtimestamp(int(start_ms) / 1000)
    e = datetime.fromtimestamp(int(end_ms) / 1000)
    if s.date() == e.date():
        return f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
    return f"{_format_day(start_ms)} → {_format_day(end_ms)}"


def _project_name_from_topic(topic: str, fallback: Optional[str] = None) -> str:
    """从 topic_label 提取项目名（首个非空白 token；无则 fallback）。"""
    text = (topic or "").strip()
    if not text:
        return fallback or "(无项目)"
    first = text.split(maxsplit=1)[0] if text.split() else ""
    return first or fallback or "(无项目)"


def _label_color_for(style: str) -> str:
    return "magenta" if style == _STYLE_ACTIVE else "yellow"


# ---------------------------------------------------------------------------
# 数据访问
# ---------------------------------------------------------------------------


def _fetch_segments(
    project: Optional[str],
    start_ms: Optional[int],
    end_ms: Optional[int],
    query_text: Optional[str],
    limit: int,
    path: Optional[Union[Path, str]],
) -> list[dict]:
    """拉段落（带 project / period / query 过滤）。"""
    if limit <= 0:
        limit = _TIMELINE_LIMIT
    if query_text and str(query_text).strip():
        tw = (start_ms, end_ms) if (start_ms is not None and end_ms is not None) else None
        return search_segments(
            query=query_text, time_window=tw,
            topics=[project] if project else None,
            limit=limit, path=path,
        )
    clauses: list[str] = []
    params: list = []
    if start_ms is not None and end_ms is not None:
        clauses += ["start_at >= ?", "start_at <= ?"]
        params += [int(start_ms), int(end_ms)]
    if project:
        clauses.append("topic_label LIKE ?")
        params.append(f"%{project}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT segment_id, session_id, current_tier, current_score, "
        "topic_label, fog_anchor, start_at, end_at, fog_state, silence_state, "
        f"superseded_by FROM segments{where} ORDER BY start_at DESC LIMIT ?"
    )
    params.append(int(limit))
    return query(sql, tuple(params), path=path)


# ---------------------------------------------------------------------------
# 段落样式 + 渲染
# ---------------------------------------------------------------------------


def _classify_segment(seg: dict) -> tuple[str, str]:
    """按 superseded > fogged_once > archived > silent > dormant > active 返回 (style, label)。"""
    fog_state = (seg.get("fog_state") or "clear").lower()
    silence_state = (seg.get("silence_state") or "active").lower()
    if seg.get("superseded_by"):
        return _STYLE_SUPERSEDED, _LABEL_SUPERSEDED
    if fog_state == "fogged_once":
        return _STYLE_FOGGED, _LABEL_FOGGED
    if fog_state == "archived":
        return _STYLE_ARCHIVED, _LABEL_ARCHIVED
    if silence_state == "silent":
        return _STYLE_SILENT, _LABEL_SILENT
    if silence_state == "dormant":
        return _STYLE_DORMANT, _LABEL_DORMANT
    return _STYLE_ACTIVE, _LABEL_ACTIVE


def _render_segment_line(seg: dict) -> Text:
    """段落渲染为 rich Text 行（时间、标题、锚点、状态标签）。"""
    style, label = _classify_segment(seg)
    topic = seg.get("topic_label") or "(无标题)"
    anchor = seg.get("fog_anchor") or ""
    score = seg.get("current_score") or 0
    time_range = _format_time_range(int(seg.get("start_at") or 0),
                                    int(seg.get("end_at") or 0))
    line = Text()
    line.append(f"{time_range}  ", style=_STYLE_META)
    line.append(f"{topic}", style=style)
    if anchor:
        line.append(f"  ({anchor})", style="dim italic")
    try:
        score_str = f"{float(score):.0f}"
    except (TypeError, ValueError):
        score_str = "0"
    line.append(f"  score={score_str}", style=_STYLE_META)
    line.append(f"  {label}", style=_label_color_for(style))
    return line


def _group_by_day(segments: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for seg in segments:
        day = _format_day(int(seg.get("start_at") or 0))
        groups.setdefault(day, []).append(seg)
    return groups


# ---------------------------------------------------------------------------
# render_timeline
# ---------------------------------------------------------------------------


def render_timeline(
    period: str = "day",
    project: Optional[str] = None,
    query_text: Optional[str] = None,
    limit: int = _TIMELINE_LIMIT,
    path: Optional[Union[Path, str]] = None,
) -> str:
    """渲染时间线视图，返回 rich Console 捕获的字符串。"""
    if period not in _VALID_PERIODS:
        raise ValueError(f"period 非法：'{period}'，仅允许 {sorted(_VALID_PERIODS)}")

    start_ms, end_ms = _period_window_ms(period)
    segments = _fetch_segments(project, start_ms, end_ms, query_text, limit, path)
    console = Console(file=StringIO(), force_terminal=True, width=120, record=True)

    title = f"MTCA 时间线  period={period}"
    if project:
        title += f"  project={project}"
    if query_text:
        title += f"  query={query_text}"

    if not segments:
        msg = Text("无段落数据。", style=_STYLE_EMPTY)
        console.print(Panel(msg, title=title, border_style="yellow"))
        return console.export_text(styles=True)

    groups = _group_by_day(segments)
    root = Tree(Text(title, style=_STYLE_HEADER), style=_STYLE_HEADER)
    for day in sorted(groups.keys(), reverse=True):
        day_segs = groups[day]
        n_active = sum(1 for s in day_segs if (s.get("silence_state") or "active") == "active")
        n_fog = sum(1 for s in day_segs if (s.get("fog_state") or "clear") == "fogged_once")
        n_super = sum(1 for s in day_segs if s.get("superseded_by"))
        meta = (f"[{_STYLE_META}]{len(day_segs)} 段 / {n_active} active / "
                f"{n_fog} /雾化 / {n_super} superseded[/]")
        branch = root.add(f"[{_STYLE_SUBHEAD}]{day}[/]  {meta}")
        for seg in sorted(day_segs, key=lambda s: s.get("start_at") or 0, reverse=True):
            branch.add(_render_segment_line(seg))

    console.print(root)
    return console.export_text(styles=True)


# ---------------------------------------------------------------------------
# render_tree
# ---------------------------------------------------------------------------


def render_tree(
    project: Optional[str] = None,
    limit: int = _TREE_LIMIT,
    path: Optional[Union[Path, str]] = None,
) -> str:
    """渲染项目级树状视图，返回 rich Console 捕获的字符串。"""
    start_ms, end_ms = _period_window_ms("month")
    segments = _fetch_segments(project, start_ms, end_ms, None, limit, path)
    console = Console(file=StringIO(), force_terminal=True, width=120, record=True)

    title = "MTCA 树状视图"
    if project:
        title += f"  project={project}"

    if not segments:
        msg = Text("无段落数据。", style=_STYLE_EMPTY)
        console.print(Panel(msg, title=title, border_style="yellow"))
        return console.export_text(styles=True)

    by_project: dict[str, list[dict]] = {}
    for seg in segments:
        topic = seg.get("topic_label") or "(无标题)"
        proj_name = project or _project_name_from_topic(topic, fallback="(未分类)")
        by_project.setdefault(proj_name, []).append(seg)

    root = Tree(Text(title, style=_STYLE_HEADER), style=_STYLE_HEADER)
    for proj_name in sorted(by_project.keys()):
        proj_segs = by_project[proj_name]
        n_fog = sum(1 for s in proj_segs if (s.get("fog_state") or "clear") == "fogged_once")
        proj_branch = root.add(
            f"[{_STYLE_SUBHEAD}]{proj_name}[/]  "
            f"[{_STYLE_META}]（{len(proj_segs)} 段 / {n_fog} /雾化）[/]"
        )
        for seg in sorted(proj_segs, key=lambda s: s.get("start_at") or 0, reverse=True):
            proj_branch.add(_render_segment_line(seg))

    console.print(root)
    return console.export_text(styles=True)


# ---------------------------------------------------------------------------
# click CLI 入口
# ---------------------------------------------------------------------------


def _echo_err(msg: str) -> None:
    click.echo(f"[ERR] {msg}", err=True)


@click.group(help="MTCA 时间线 CLI（T12）")
def cli() -> None:
    """MTCA 时间线组：timeline / tree。"""


@cli.command("timeline")
@click.option("--period", default="day",
              type=click.Choice(["day", "week", "month"], case_sensitive=False),
              help="时间窗口：day / week / month")
@click.option("--project", default=None, help="项目过滤（topic_label LIKE 匹配）")
@click.option("--query", "query_text", default=None, help="关键词搜索（FTS5 + topic_label）")
@click.option("--limit", default=_TIMELINE_LIMIT, type=int, help=f"返回上限（默认 {_TIMELINE_LIMIT}）")
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None,
              help="数据库路径（默认 ~/.mtca/mtca.db）")
def _click_timeline(period: str, project: Optional[str], query_text: Optional[str],
                    limit: int, db_path: Optional[str]) -> None:
    """mtca timeline [--period=...] [--query ...] [--project ...]"""
    try:
        output = render_timeline(period=period, project=project,
                                 query_text=query_text, limit=limit, path=db_path)
    except (ValueError, RuntimeError) as e:
        _echo_err(str(e))
        sys.exit(2)
    click.echo(output, nl=False)


@cli.command("tree")
@click.option("--project", default=None, help="项目过滤（topic_label LIKE 匹配）")
@click.option("--limit", default=_TREE_LIMIT, type=int, help=f"返回上限（默认 {_TREE_LIMIT}）")
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None,
              help="数据库路径（默认 ~/.mtca/mtca.db）")
def _click_tree(project: Optional[str], limit: int, db_path: Optional[str]) -> None:
    """mtca tree [--project=X]"""
    try:
        output = render_tree(project=project, limit=limit, path=db_path)
    except (ValueError, RuntimeError) as e:
        _echo_err(str(e))
        sys.exit(2)
    click.echo(output, nl=False)


# ---------------------------------------------------------------------------
# argparse fallback
# ---------------------------------------------------------------------------


def _argparse_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mtca", description="MTCA 时间线 CLI（T12 argparse fallback）")
    parser.add_argument("--db", dest="db_path", default=None, help="数据库路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tl = sub.add_parser("timeline", help="时间线视图")
    p_tl.add_argument("--period", default="day", choices=["day", "week", "month"])
    p_tl.add_argument("--project", default=None)
    p_tl.add_argument("--query", default=None)
    p_tl.add_argument("--limit", default=_TIMELINE_LIMIT, type=int)

    p_tr = sub.add_parser("tree", help="项目树状视图")
    p_tr.add_argument("--project", default=None)
    p_tr.add_argument("--limit", default=_TREE_LIMIT, type=int)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "timeline":
            print(render_timeline(period=args.period, project=args.project,
                                  query_text=args.query, limit=args.limit, path=args.db_path), end="")
        elif args.cmd == "tree":
            print(render_tree(project=args.project, limit=args.limit, path=args.db_path), end="")
    except (ValueError, RuntimeError) as e:
        print(f"[ERR] {e}", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI 入口：click 优先；异常时回落 argparse。"""
    if len(sys.argv) > 1 and sys.argv[1] == "--argparse":
        sys.exit(_argparse_main(sys.argv[2:]))
    try:
        cli()
    except SystemExit:
        raise
    except Exception:
        sys.exit(_argparse_main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "render_timeline", "render_tree",
    "main", "cli",
]