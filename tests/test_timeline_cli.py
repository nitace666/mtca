"""tests/test_timeline_cli.py — T32 timeline CLI 测试补全。

目标：覆盖 src/cli/timeline.py 时间线 / 树状视图 / click CLI 入口，
提升 src/cli/* 0% → 60%。
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from src.cli.timeline import (
    cli,
    render_timeline,
    render_tree,
    _period_window_ms,
    _format_day,
    _format_time_range,
    _project_name_from_topic,
    _label_color_for,
)


# ============================================================================
# 内部辅助函数（纯函数，直接调验证行为）
# ============================================================================

def test_period_window_ms_day() -> None:
    """day 窗口 = 1 天（end_ms - start_ms == 86_400_000）。"""
    now = 1_700_000_000_000  # 固定 now_ms 便于断言
    start, end = _period_window_ms("day", now_ms=now)
    assert end == now
    assert start == now - 24 * 3600 * 1000


def test_period_window_ms_week() -> None:
    """week 窗口 = 7 天。"""
    now = 1_700_000_000_000
    start, end = _period_window_ms("week", now_ms=now)
    assert end - start == 7 * 24 * 3600 * 1000


def test_period_window_ms_month() -> None:
    """month 窗口 = 30 天。"""
    now = 1_700_000_000_000
    start, end = _period_window_ms("month", now_ms=now)
    assert end - start == 30 * 24 * 3600 * 1000


def test_format_day_returns_iso() -> None:
    """固定毫秒戳格式化 YYYY-MM-DD（用本地时区，避免 UTC 漂移）。"""
    # 任意 ms，只要返回值是 ISO 8 位日期就 OK
    assert len(_format_day(1_705_276_800_000)) == 10  # "YYYY-MM-DD"
    assert _format_day(1_705_276_800_000)[4] == "-"
    assert _format_day(1_705_276_800_000)[7] == "-"


def test_format_day_zero_returns_dashes() -> None:
    """ms=0 时返回 '----' 占位。"""
    assert _format_day(0) == "----"
    assert _format_day(None) == "----"  # type: ignore[arg-type]


def test_format_time_range_same_day() -> None:
    """同日内的时间范围：HH:MM-HH:MM（不应含日期分隔符）。"""
    s = 1_705_276_800_000  # 2024-01-15
    e = 1_705_280_400_000  # 同日 +1h
    out = _format_time_range(s, e)
    assert "-" in out
    assert "→" not in out  # 同日不应出现跨日箭头
    # 两段 HH:MM 形式
    parts = out.split("-")
    assert len(parts) == 2
    h1, m1 = parts[0].split(":")
    h2, m2 = parts[1].split(":")
    assert int(h1) == (int(h2) - 1) % 24 or int(h1) == int(h2)


def test_format_time_range_cross_day() -> None:
    """跨日：YYYY-MM-DD → YYYY-MM-DD。"""
    s = 1_705_276_800_000  # 2024-01-15
    e = 1_705_363_200_000  # +1 天
    out = _format_time_range(s, e)
    assert "→" in out


def test_format_time_range_zero_returns_dashes() -> None:
    """0 / 空 -> '----'。"""
    assert _format_time_range(0, 0) == "----"
    assert _format_time_range(100, 0) == "----"


def test_project_name_from_topic_first_token() -> None:
    """从 topic_label 取首个非空白 token。"""
    assert _project_name_from_topic("MTCA 召回优化") == "MTCA"
    assert _project_name_from_topic("fog") == "fog"


def test_project_name_from_topic_empty_uses_fallback() -> None:
    """空 topic + 显式 fallback -> fallback。"""
    assert _project_name_from_topic("", fallback="X") == "X"
    assert _project_name_from_topic("   ", fallback="X") == "X"
    assert _project_name_from_topic(None, fallback="X") == "X"  # type: ignore[arg-type]


def test_project_name_from_topic_default_fallback() -> None:
    """无 fallback 时默认 '(无项目)'（半角括号，与源码一致）。"""
    assert _project_name_from_topic("") == "(无项目)"
    assert _project_name_from_topic("   ") == "(无项目)"
    assert _project_name_from_topic(None) == "(无项目)"  # type: ignore[arg-type]


def test_label_color_for_active_is_magenta() -> None:
    """active 用 magenta 配色。"""
    from src.cli.timeline import _STYLE_ACTIVE
    assert _label_color_for(_STYLE_ACTIVE) == "magenta"


def test_label_color_for_non_active_is_yellow() -> None:
    """非 active 用 yellow 配色（grey50 / dim / strike 等）。"""
    from src.cli.timeline import _STYLE_SUPERSEDED
    assert _label_color_for(_STYLE_SUPERSEDED) == "yellow"


# ============================================================================
# render_timeline / render_tree: 空 DB / 多 session
# ============================================================================

def test_render_timeline_empty_db(tmp_path: Path) -> None:
    """空 DB 渲染不应崩，返回字符串。"""
    from src.store.sqlite import init_db
    db = tmp_path / "empty.db"
    init_db(db)
    result = render_timeline(path=db, period="day", limit=10)
    assert isinstance(result, str)


def test_render_timeline_with_one_session(tmp_path: Path) -> None:
    """1 个 session 也能渲染（边界）。"""
    from src.store.sqlite import init_db
    from src.l0.session_writer import create_session, write_message
    db = tmp_path / "one.db"
    init_db(db)
    sid = create_session(path=db)
    write_message(sid, "user", "测试", path=db)
    write_message(sid, "assistant", "响应", path=db)
    result = render_timeline(path=db, period="day", limit=10)
    assert isinstance(result, str)


def test_render_tree_empty_db(tmp_path: Path) -> None:
    """空 DB 树状视图不应崩。"""
    from src.store.sqlite import init_db
    db = tmp_path / "empty.db"
    init_db(db)
    result = render_tree(path=db, limit=10)
    assert isinstance(result, str)


def test_render_tree_with_sessions(tmp_path: Path) -> None:
    """多 session 树状。"""
    from src.store.sqlite import init_db
    from src.l0.session_writer import create_session
    db = tmp_path / "multi.db"
    init_db(db)
    for _ in range(3):
        create_session(path=db)
    result = render_tree(path=db, limit=10)
    assert isinstance(result, str)
    assert len(result) > 0


# ============================================================================
# 段落分支覆盖（mock _fetch_segments 触发 _classify_segment / _render_segment_line）
# ============================================================================

def _make_seg(
    *,
    sid: str = "s1",
    topic: str | None = "话题A",
    start_at: int = 1_700_000_000_000,
    end_at: int = 1_700_000_360_000,
    current_score: int = 100,
    fog_state: str | None = None,
    silence_state: str | None = None,
    superseded_by: str | None = None,
    fog_anchor: str | None = None,
) -> dict:
    """构造与 _fetch_segments 返回结构一致的字典。"""
    return {
        "segment_id": sid,
        "session_id": "ssn1",
        "current_tier": "L1",
        "current_score": current_score,
        "topic_label": topic,
        "fog_anchor": fog_anchor,
        "start_at": start_at,
        "end_at": end_at,
        "fog_state": fog_state,
        "silence_state": silence_state,
        "superseded_by": superseded_by,
    }


def test_render_timeline_classifies_all_states(tmp_path: Path) -> None:
    """_classify_segment 各分支：active/dormant/silent/fogged/archived/superseded 全覆盖。"""
    from src.store.sqlite import init_db
    db = tmp_path / "states.db"
    init_db(db)

    segs = [
        _make_seg(sid="a1", topic="active段"),
        _make_seg(sid="d1", topic="dormant段", silence_state="dormant"),
        _make_seg(sid="s2", topic="silent段", silence_state="silent"),
        _make_seg(sid="f1", topic="fogged段", fog_state="fogged_once"),
        _make_seg(sid="ar1", topic="archived段", fog_state="archived"),
        _make_seg(sid="sp1", topic="superseded段", superseded_by="o1"),
        # fog_state 大小写容错
        _make_seg(sid="f2", topic="Fogged 大写", fog_state="Fogged_Once"),
    ]
    with mock.patch("src.cli.timeline._fetch_segments", return_value=segs):
        result = render_timeline(path=db, period="day", limit=20)

    assert isinstance(result, str)
    # 各段都进渲染
    for s in segs:
        assert s["topic_label"] in result
    # 部分状态标签出现
    assert "[superseded]" in result
    assert "[/雾化]" in result or "[archived]" in result


def test_render_tree_classifies_with_branching(tmp_path: Path) -> None:
    """render_tree 按 project 分组 + 多状态段落都进入分支。"""
    from src.store.sqlite import init_db
    db = tmp_path / "tree.db"
    init_db(db)
    segs = [
        _make_seg(sid="a1", topic="MTCA active", silence_state="active"),
        _make_seg(sid="f1", topic="MTCA fogged", fog_state="fogged_once"),
        _make_seg(sid="x1", topic="other project", silence_state="dormant"),
    ]
    with mock.patch("src.cli.timeline._fetch_segments", return_value=segs):
        result = render_tree(path=db, limit=10)

    assert isinstance(result, str)
    # 两个项目根出现
    assert "MTCA" in result
    assert "other" in result
    # 雾化计数
    assert "1 /雾化" in result


def test_render_timeline_group_by_day(tmp_path: Path) -> None:
    """_group_by_day 按 day 把多段时间段落分组。"""
    from src.store.sqlite import init_db
    db = tmp_path / "group.db"
    init_db(db)
    # 相差 1 天的两个段落
    segs = [
        _make_seg(sid="a", start_at=1_705_276_800_000, end_at=1_705_277_200_000),
        _make_seg(sid="b", start_at=1_705_363_200_000, end_at=1_705_363_600_000),
    ]
    with mock.patch("src.cli.timeline._fetch_segments", return_value=segs):
        result = render_timeline(path=db, period="month", limit=10)
    # 两个日期头都出现
    assert isinstance(result, str)
    # 两 day 分组都出现 + 两段话题都进入渲染
    assert result.count("话题A") == 2
    assert result.count("2024-") == 2  # 简化：两 ISO 日期前缀(r"2024-\d\d-\d\d", result))) == 2
def test_render_with_query_text(tmp_path: Path) -> None:
    """query_text 不为空时走 search_segments 路径（被 mock）。"""
    from src.store.sqlite import init_db
    db = tmp_path / "q.db"
    init_db(db)
    with mock.patch(
        "src.cli.timeline.search_segments",
        return_value=[_make_seg(sid="q1", topic="搜索结果")],
    ) as m:
        result = render_timeline(
            path=db, period="day", query_text="关键词", limit=5,
        )
    assert "搜索结果" in result
    m.assert_called_once()


def test_render_timeline_with_project_filter(tmp_path: Path) -> None:
    """project= 过滤时仍渲染（即便结果为 0）。"""
    from src.store.sqlite import init_db
    db = tmp_path / "p.db"
    init_db(db)
    with mock.patch(
        "src.cli.timeline._fetch_segments", return_value=[],
    ) as m:
        result = render_timeline(
            path=db, period="day", project="MTCA", limit=10,
        )
    assert isinstance(result, str)
    m.assert_called_once()
    # _fetch_segments 第一个位置参数是 project
    assert m.call_args.args[0] == "MTCA"


def test_render_segment_line_score_string_handles_non_numeric() -> None:
    """score 不是数字时 _render_segment_line 仍渲染（失败用 '0'）。"""
    from src.cli.timeline import _render_segment_line
    seg = _make_seg(current_score="not-a-number")  # type: ignore[arg-type]
    text = _render_segment_line(seg)
    # 返回 rich Text，str() 应包含 score=0
    assert "score=0" in str(text)


def test_classify_segment_superseded_overrides_fogged() -> None:
    """superseded 优先级 > fogged_once。"""
    from src.cli.timeline import (
        _classify_segment,
        _STYLE_SUPERSEDED,
    )
    style, _ = _classify_segment(_make_seg(
        superseded_by="other-seg",
        fog_state="fogged_once",
    ))
    assert style == _STYLE_SUPERSEDED


def test_classify_handles_missing_fields() -> None:
    """字段缺失时默认 fallback 到 clear + active（最轻量档，不应 crash）。"""
    from src.cli.timeline import _classify_segment, _STYLE_ACTIVE
    style, label = _classify_segment({})
    assert style == _STYLE_ACTIVE
    assert label == "[active]"


def test_classify_dormant_branch() -> None:
    """silence_state='dormant' 走 dormant 分支。"""
    from src.cli.timeline import _classify_segment, _STYLE_DORMANT
    style, label = _classify_segment(_make_seg(silence_state="dormant"))
    assert style == _STYLE_DORMANT
    assert "[dormant]" in label


def test_classify_archived_branch() -> None:
    """fog_state='archived'（非 fogged_once）走 archived 分支。"""
    from src.cli.timeline import _classify_segment, _STYLE_ARCHIVED
    style, label = _classify_segment(_make_seg(fog_state="archived"))
    assert style == _STYLE_ARCHIVED
    assert "[archived]" in label


# ============================================================================
# 模块导出验证
# ============================================================================

def test_timeline_public_api() -> None:
    """timeline 模块暴露 render_timeline + render_tree + main + cli。"""
    import src.cli.timeline as m
    for name in ["render_timeline", "render_tree", "main", "cli"]:
        assert hasattr(m, name), f"missing: {name}"


# ============================================================================
# click CLI 集成 — 覆盖 _click_timeline / _click_tree / main / argparse fallback
# ============================================================================

def test_click_help_shows_subcommands() -> None:
    """mtca --help 列出 timeline + tree 两个子命令。"""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for sub in ("timeline", "tree"):
        assert sub in result.output


def test_click_timeline_invokes_renderer() -> None:
    """click timeline 子命令调到 render_timeline。"""
    runner = CliRunner()
    with mock.patch(
        "src.cli.timeline.render_timeline", return_value="mocked-timeline",
    ) as m:
        result = runner.invoke(cli, ["timeline", "--period", "week"])
    assert result.exit_code == 0
    assert "mocked-timeline" in result.output
    m.assert_called_once()


def test_click_timeline_value_error_exits_2() -> None:
    """render_timeline 抛 ValueError 时 click exit 2。"""
    runner = CliRunner()
    with mock.patch(
        "src.cli.timeline.render_timeline",
        side_effect=ValueError("bad period"),
    ):
        result = runner.invoke(cli, ["timeline"])
    assert result.exit_code == 2
    assert "bad period" in result.output


def test_click_tree_invokes_renderer() -> None:
    """click tree 子命令调到 render_tree。"""
    runner = CliRunner()
    with mock.patch(
        "src.cli.timeline.render_tree", return_value="mocked-tree",
    ) as m:
        result = runner.invoke(cli, ["tree", "--project", "MTCA"])
    assert result.exit_code == 0
    assert "mocked-tree" in result.output
    m.assert_called_once()


def test_click_tree_runtime_error_exits_2() -> None:
    """render_tree 抛 RuntimeError 时 click exit 2。"""
    runner = CliRunner()
    with mock.patch(
        "src.cli.timeline.render_tree",
        side_effect=RuntimeError("db down"),
    ):
        result = runner.invoke(cli, ["tree"])
    assert result.exit_code == 2


def test_argparse_fallback_timeline() -> None:
    """argparse 分支：timeline 子命令调到 render_timeline。"""
    from src.cli.timeline import _argparse_main
    with mock.patch(
        "src.cli.timeline.render_timeline", return_value="argparse-out",
    ):
        rc = _argparse_main(["timeline", "--period", "month"])
    assert rc == 0


def test_argparse_fallback_tree() -> None:
    """argparse 分支：tree 子命令调到 render_tree。"""
    from src.cli.timeline import _argparse_main
    with mock.patch(
        "src.cli.timeline.render_tree", return_value="argparse-tree",
    ):
        rc = _argparse_main(["tree", "--project", "X"])
    assert rc == 0


def test_argparse_fallback_value_error_returns_2() -> None:
    """argparse 抛 ValueError -> 返 2。"""
    from src.cli.timeline import _argparse_main
    with mock.patch(
        "src.cli.timeline.render_timeline",
        side_effect=ValueError("boom"),
    ):
        rc = _argparse_main(["timeline"])
    assert rc == 2



