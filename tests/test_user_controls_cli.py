"""tests/test_user_controls_cli.py — T32 user_controls CLI 测试补全。

目标：覆盖 src/cli/user_controls.py 公共 API（4 cmd_* + 2 parse_* + click CLI），
提升 src/cli/* 覆盖率 0% → 60%。

风格：
- 4 个 cmd_* 函数 -> mock 下层（保证单元级别隔离 + 不依赖 DB）
- 2 个 parse_* 函数 -> 纯函数，直接调（无外部依赖）
- click CLI -> ClickRunner 集成（mock 下层避开 DB）
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from src.cli.user_controls import (
    cli,
    cmd_important,
    cmd_cycle,
    cmd_archive,
    cmd_fog,
    parse_command,
    parse_natural,
)


# ============================================================================
# 命令函数（4 个，每个 mock 下层函数验证调用）
# ============================================================================

def test_cmd_important_invokes_mark_important(mtca_db: Path) -> None:
    """cmd_important 调 scoring.mark_important，返回其结果。"""
    with mock.patch("src.cli.user_controls._mark_important") as m:
        m.return_value = 1
        rows = cmd_important("seg-uuid-1", path=mtca_db)
        assert rows == 1
        m.assert_called_once_with("seg-uuid-1", path=mtca_db)


def test_cmd_important_passes_path(mtca_db: Path) -> None:
    """cmd_important 把 path 传给下层。"""
    with mock.patch("src.cli.user_controls._mark_important") as m:
        m.return_value = 0
        cmd_important("seg-x", path=mtca_db)
        args, kwargs = m.call_args
        assert kwargs.get("path") == mtca_db


def test_cmd_important_passes_default_path(mtca_db: Path) -> None:
    """不传 path 时默认 None（让下层用 MTCA_DB_PATH）。"""
    with mock.patch("src.cli.user_controls._mark_important") as m:
        m.return_value = 0
        cmd_important("seg-x")
        args, kwargs = m.call_args
        assert kwargs.get("path") is None


def test_cmd_cycle_writes_tag(mtca_db: Path) -> None:
    """cmd_cycle 把 tag 一并传下层。"""
    with mock.patch("src.cli.user_controls._mark_cycle") as m:
        m.return_value = 1
        cmd_cycle("seg-uuid", "周一", path=mtca_db)
        m.assert_called_once_with("seg-uuid", "周一", path=mtca_db)


def test_cmd_cycle_returns_rows(mtca_db: Path) -> None:
    """cmd_cycle 返回下层返回值。"""
    with mock.patch("src.cli.user_controls._mark_cycle") as m:
        m.return_value = 0
        assert cmd_cycle("seg", "每天", path=mtca_db) == 0
        m.return_value = 3
        assert cmd_cycle("seg", "月初", path=mtca_db) == 3


def test_cmd_archive_calls_archive(mtca_db: Path) -> None:
    """cmd_archive 调 scoring.archive。"""
    with mock.patch("src.cli.user_controls._archive") as m:
        m.return_value = 1
        rows = cmd_archive("seg-uuid", path=mtca_db)
        assert rows == 1
        m.assert_called_once_with("seg-uuid", path=mtca_db)


def test_cmd_fog_calls_fog_engine_with_user_caller(mtca_db: Path) -> None:
    """cmd_fog 调 fog_segment 时固定 called_by='user'（AI 无权）。"""
    with mock.patch("src.cli.user_controls._fog_segment") as m:
        m.return_value = True
        ok = cmd_fog("seg-uuid", "锚点句", path=mtca_db)
        assert ok is True
        m.assert_called_once_with("seg-uuid", "锚点句", called_by="user", path=mtca_db)


def test_cmd_fog_called_by_always_user(mtca_db: Path) -> None:
    """cmd_fog 永远 called_by='user'，无法被 override。"""
    with mock.patch("src.cli.user_controls._fog_segment") as m:
        m.return_value = True
        cmd_fog("seg", "anchor")
        args, kwargs = m.call_args
        assert kwargs.get("called_by") == "user"


# ============================================================================
# parse_command（前缀指令解析）
# ============================================================================

def test_parse_command_slash_important_with_uuid() -> None:
    """/重要 + UUID 段 ID 解析。"""
    result = parse_command("/重要 abc-1234-5678-9abc-def0-1234567890ab")
    assert result == ("important", ["abc-1234-5678-9abc-def0-1234567890ab"])


def test_parse_command_slash_cycle_with_tag() -> None:
    """/循环 + UUID + tag。"""
    sid = "12345678-1234-1234-1234-1234567890ab"
    result = parse_command(f"/循环 {sid} 周一")
    assert result == ("cycle", [sid, "周一"])


def test_parse_command_slash_archive() -> None:
    """/归档 + UUID。"""
    sid = "abcdef00-0000-0000-0000-000000000001"
    result = parse_command(f"/归档 {sid}")
    assert result == ("archive", [sid])


def test_parse_command_slash_fog_with_anchor() -> None:
    """/雾化 + UUID + --anchor + 锚点。"""
    sid = "abcdef00-0000-0000-0000-000000000002"
    result = parse_command(f"/雾化 {sid} --anchor 关键句")
    assert result == ("fog", [sid, "--anchor", "关键句"])


def test_parse_command_mid_text() -> None:
    """前缀命令出现在行中也能识别（USER_CONTROLS.md §1）。"""
    sid = "abcdef00-0000-0000-0000-000000000003"
    result = parse_command(f"请帮我 /重要 {sid}")
    assert result == ("important", [sid])


def test_parse_command_trailing() -> None:
    """前缀在尾：先 UUID 后 /重要（解析时 UUID 不进 args）。"""
    sid = "abcdef00-0000-0000-0000-000000000004"
    result = parse_command(f"{sid} /重要")
    assert result == ("important", [])


def test_parse_command_unknown_prefix_returns_none() -> None:
    """未知前缀 /zzz 返回 None。"""
    result = parse_command("/zzz abc-123")
    assert result is None


def test_parse_command_empty_returns_none() -> None:
    """空字符串 / 纯空白返回 None。"""
    assert parse_command("") is None
    assert parse_command("   ") is None


def test_parse_command_non_string_returns_none() -> None:
    """非字符串输入返回 None。"""
    assert parse_command(None) is None  # type: ignore[arg-type]
    assert parse_command(123) is None  # type: ignore[arg-type]


def test_parse_command_no_prefix_returns_none() -> None:
    """无前缀也无 UUID 时返回 None。"""
    assert parse_command("hello world") is None


# ============================================================================
# parse_natural（自然语言中文识别）
# ============================================================================

def test_parse_natural_important() -> None:
    """自然语言：把 X 标记为重要。"""
    sid = "abcdef00-0000-0000-0000-000000000010"
    result = parse_natural(f"把 {sid} 标记为重要")
    assert result == ("important", [sid])


def test_parse_natural_archive() -> None:
    """自然语言：归档 X。"""
    sid = "abcdef00-0000-0000-0000-000000000011"
    result = parse_natural(f"把 {sid} 归档")
    assert result == ("archive", [sid])


def test_parse_natural_cycle_with_tag() -> None:
    """自然语言：循环 X tag（专用正则）。"""
    sid = "abcdef00-0000-0000-0000-000000000012"
    result = parse_natural(f"循环 {sid} 周一")
    assert result == ("cycle", [sid, "周一"])


def test_parse_natural_fog_with_anchor_keyword() -> None:
    """自然语言：雾化 X 关键词 ...（专用正则）。"""
    sid = "abcdef00-0000-0000-0000-000000000013"
    result = parse_natural(f"雾化 {sid} 关键词 AI 记忆锚点")
    assert result is not None
    cmd, args = result
    assert cmd == "fog"
    assert args[0] == sid
    # 锚点会被归一化（去空白 + 截到 ANCHOR_MAX_LEN）
    assert isinstance(args[1], str)
    assert len(args[1]) <= 20


def test_parse_natural_priority_important_over_archive() -> None:
    """关键词优先：重要 > 归档 > 循环 > 雾化。"""
    sid = "abcdef00-0000-0000-0000-000000000014"
    text = f"把 {sid} 归档，因为也很重要"
    result = parse_natural(text)
    assert result == ("important", [sid])


def test_parse_natural_empty_returns_none() -> None:
    """空 / 空白返回 None。"""
    assert parse_natural("") is None
    assert parse_natural("   ") is None


def test_parse_natural_no_keyword_no_uuid_returns_none() -> None:
    """无关键词无 UUID 匹配返回 None。"""
    assert parse_natural("今天天气真好") is None


def test_parse_natural_non_string_returns_none() -> None:
    """非字符串返回 None。"""
    assert parse_natural(None) is None  # type: ignore[arg-type]
    assert parse_natural(42) is None  # type: ignore[arg-type]


def test_parse_natural_keyword_without_uuid_returns_none() -> None:
    """有关键词但缺 UUID（自然语言找不到锚点）返回 None。"""
    assert parse_natural("这段很重要") is None
    assert parse_natural("循环 每周一") is None


# ============================================================================
# 模块导出 / 公共 API 完整性
# ============================================================================

def test_user_controls_public_api() -> None:
    """user_controls 模块暴露 cmd_* / parse_* / main / cli。"""
    import src.cli.user_controls as m
    for name in [
        "cmd_important", "cmd_cycle", "cmd_archive", "cmd_fog",
        "parse_command", "parse_natural",
        "main", "cli",
    ]:
        assert hasattr(m, name), f"missing: {name}"


def test_anchor_max_len_constant() -> None:
    """ANCHOR_MAX_LEN = 20 与 fog_engine 一致。"""
    from src.cli.user_controls import ANCHOR_MAX_LEN as cli_anchor
    from src.fog.fog_engine import ANCHOR_MAX_LEN as fog_anchor
    assert cli_anchor == fog_anchor == 20


# ============================================================================
# click CLI 集成（CliRunner）— 提升覆盖率到 click 入口路径
# ============================================================================

def test_click_important_invokes_handler() -> None:
    """click important 子命令调 cmd_important，输出 [OK]。"""
    runner = CliRunner()
    with mock.patch("src.cli.user_controls._mark_important", return_value=0):
        result = runner.invoke(cli, ["important", "seg-x"])
    assert result.exit_code == 0, result.output
    assert "已 /重要 seg-x" in result.output


def test_click_important_propagates_value_error(mtca_db: Path) -> None:
    """下层抛 ValueError 时 click 输出 [ERR] 并 exit 2。"""
    runner = CliRunner()
    with mock.patch(
        "src.cli.user_controls._mark_important",
        side_effect=ValueError("not found"),
    ):
        result = runner.invoke(cli, ["important", "seg-x", "--db", str(mtca_db)])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_click_cycle_invokes_handler() -> None:
    """click cycle 子命令调 cmd_cycle。"""
    runner = CliRunner()
    with mock.patch("src.cli.user_controls._mark_cycle", return_value=2):
        result = runner.invoke(cli, ["cycle", "seg-x", "周一"])
    assert result.exit_code == 0, result.output
    assert "已 /循环 周一 seg-x" in result.output


def test_click_archive_invokes_handler() -> None:
    """click archive 子命令调 cmd_archive。"""
    runner = CliRunner()
    with mock.patch("src.cli.user_controls._archive", return_value=1):
        result = runner.invoke(cli, ["archive", "seg-x"])
    assert result.exit_code == 0, result.output
    assert "已 /归档 seg-x" in result.output


def test_click_fog_invokes_handler() -> None:
    """click fog 子命令调 cmd_fog。"""
    runner = CliRunner()
    with mock.patch("src.cli.user_controls._fog_segment", return_value=True):
        result = runner.invoke(cli, ["fog", "seg-x", "--anchor", "锚点"])
    assert result.exit_code == 0, result.output
    assert "已 /雾化 seg-x" in result.output


def test_click_fog_permission_error_exits_2() -> None:
    """PermissionError 时 click fog exit 2。"""
    runner = CliRunner()
    with mock.patch(
        "src.cli.user_controls._fog_segment",
        side_effect=PermissionError("AI 无权"),
    ):
        result = runner.invoke(cli, ["fog", "seg-x", "--anchor", "锚点"])
    assert result.exit_code == 2
    assert "AI 无权" in result.output


def test_click_help_shows_commands() -> None:
    """mtca-user --help 列出 4 个子命令。"""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for sub in ("important", "cycle", "archive", "fog"):
        assert sub in result.output


def test_argparse_fallback_important(monkeypatch: pytest.MonkeyPatch) -> None:
    """--argparse 显式触发 argparse 分支，跑 important 子命令。"""
    from src.cli.user_controls import _argparse_main
    with mock.patch("src.cli.user_controls._mark_important", return_value=0):
        rc = _argparse_main(["important", "seg-x"])
    assert rc == 0


def test_argparse_fallback_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse archive 分支。"""
    from src.cli.user_controls import _argparse_main
    with mock.patch("src.cli.user_controls._archive", return_value=0):
        rc = _argparse_main(["archive", "seg-x"])
    assert rc == 0


def test_argparse_fallback_fog(monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse fog 分支。"""
    from src.cli.user_controls import _argparse_main
    with mock.patch("src.cli.user_controls._fog_segment", return_value=True):
        rc = _argparse_main(["fog", "seg-x", "--anchor", "锚点"])
    assert rc == 0


def test_argparse_fallback_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse 子命令抛 ValueError -> 返回 2。"""
    from src.cli.user_controls import _argparse_main
    with mock.patch(
        "src.cli.user_controls._mark_important",
        side_effect=ValueError("boom"),
    ):
        rc = _argparse_main(["important", "seg-x"])
    assert rc == 2
