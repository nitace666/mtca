"""tests/test_error_friendly.py — M2.5.8 A-3 错误信息友好化。

TDD 红测试：验证 UserFacingError 字段 + CLI 边界友好输出。
这些测试在 src/errors.py + CLI 边界改造前应失败（红）。

覆盖：
- UserFacingError 字段断言（message / suggestion / code）
- CLI DB 错误 → 中文友好 stderr + 退出码 2
- CLI FileNotFound 错误 → 中文友好 stderr + 退出码 1
- CLI UserFacingError(code=3) → 退出码 3（LLM 错误路径）

注意：所有 click 命令通过 CliRunner 调用；render_timeline 用 mock.patch
注入异常；stderr 与 exit_code 是 CliRunner 的标准字段。
"""
from __future__ import annotations

import sqlite3
from unittest import mock

import pytest
from click.testing import CliRunner

from src.cli.timeline import cli as timeline_cli


# ---------------------------------------------------------------------------
# 1. UserFacingError 字段
# ---------------------------------------------------------------------------


def test_user_facing_error_format() -> None:
    """UserFacingError 应有 message / suggestion / code 三个字段。"""
    from src.errors import UserFacingError  # 红：模块不存在 -> ImportError

    err = UserFacingError(
        message="数据库坏了",
        suggestion="检查 ~/.mtca/mtca.db 是否存在且可写",
        code=2,
    )
    assert err.message == "数据库坏了"
    assert err.suggestion == "检查 ~/.mtca/mtca.db 是否存在且可写"
    assert err.code == 2
    # 必须继承 Exception 才能 raise / except
    assert isinstance(err, Exception)


def test_user_facing_error_suggests_action() -> None:
    """建议字段应可读、可操作（中文动词 + 长度 > 4）。"""
    from src.errors import UserFacingError

    err = UserFacingError(
        message="LLM 调用失败",
        suggestion="检查 ~/.mtca/config.toml 中 provider 配置，或网络是否通畅",
        code=3,
    )
    assert err.suggestion is not None
    assert len(err.suggestion) > 4
    # 中文友好：含动词"检查"
    assert "检查" in err.suggestion


# ---------------------------------------------------------------------------
# 2. CLI 边界 — 友好 stderr
# ---------------------------------------------------------------------------


def test_cli_db_error_friendly() -> None:
    """sqlite3.DatabaseError 应被翻译为中文 + 建议。"""
    runner = CliRunner()
    with mock.patch(
        "src.cli.timeline.render_timeline",
        side_effect=sqlite3.DatabaseError("disk I/O error"),
    ):
        result = runner.invoke(
            timeline_cli, ["timeline", "--db", "/tmp/x.db"]
        )
    # 当前 _click_timeline 只 catch (ValueError, RuntimeError)，
    # DatabaseError 冒泡到 CliRunner，被打印成 traceback + exit_code=1。
    # 期望（绿后）：exit_code=2 + stderr 含中文友好信息。
    assert result.exit_code == 2, (
        f"DB 错误应退出码 2，实际 {result.exit_code}；stderr={result.stderr!r}"
    )
    assert "数据库错误" in result.stderr, (
        f"stderr 应含'数据库错误'，实际={result.stderr!r}"
    )
    assert "建议" in result.stderr, (
        f"stderr 应含'建议'，实际={result.stderr!r}"
    )


def test_cli_file_not_found_friendly() -> None:
    """FileNotFoundError 应被翻译为中文 + 建议（EXIT_USER_ERROR）。"""
    runner = CliRunner()
    with mock.patch(
        "src.cli.timeline.render_timeline",
        side_effect=FileNotFoundError(
            22, "no such file", "/no/such/path.db"
        ),
    ):
        result = runner.invoke(
            timeline_cli, ["timeline", "--db", "/no/such/path.db"]
        )
    # 期望（绿后）：exit_code=1 + stderr 含"文件不存在"+"建议"
    assert result.exit_code == 1, (
        f"FileNotFoundError 应退出码 1，实际 {result.exit_code}；stderr={result.stderr!r}"
    )
    assert "文件不存在" in result.stderr, (
        f"stderr 应含'文件不存在'，实际={result.stderr!r}"
    )
    assert "建议" in result.stderr, (
        f"stderr 应含'建议'，实际={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 3. CLI 边界 — 退出码
# ---------------------------------------------------------------------------


def test_cli_db_error_exit_code_2() -> None:
    """DB 错误应退出码 = 2 (EXIT_DB_ERROR)。"""
    from src.errors import EXIT_DB_ERROR  # 红：模块不存在 -> ImportError

    runner = CliRunner()
    with mock.patch(
        "src.cli.timeline.render_timeline",
        side_effect=sqlite3.DatabaseError("integrity boom"),
    ):
        result = runner.invoke(timeline_cli, ["timeline", "--db", "/x.db"])
    assert result.exit_code == EXIT_DB_ERROR == 2


def test_cli_user_facing_error_exit_code_3() -> None:
    """UserFacingError(code=3) 应被尊重 → 退出码 3 (EXIT_LLM_ERROR)。"""
    from src.errors import EXIT_LLM_ERROR, UserFacingError

    runner = CliRunner()
    with mock.patch(
        "src.cli.timeline.render_timeline",
        side_effect=UserFacingError(
            message="LLM 调用失败",
            suggestion="检查 provider 配置",
            code=EXIT_LLM_ERROR,
        ),
    ):
        result = runner.invoke(timeline_cli, ["timeline", "--db", "/x.db"])
    assert result.exit_code == EXIT_LLM_ERROR == 3



# ---------------------------------------------------------------------------
# 4. click.BadParameter —— 参数解析阶段友好化（M2.5.8 A-3.5）
# ---------------------------------------------------------------------------


def test_click_bad_parameter_friendly() -> None:
    """click.BadParameter 应被 _TimelineGroup.invoke 翻译为中文 + 建议 + 退出码 1。"""
    from click.testing import CliRunner
    from src.cli.timeline import cli as timeline_cli

    runner = CliRunner()
    result = runner.invoke(timeline_cli, ["timeline", "--period", "invalid"])
    assert result.exit_code == 1, (
        f"--period invalid 应退出码 1，实际 {result.exit_code}；stderr={result.stderr!r}"
    )
    assert "参数" in result.stderr, f"stderr 应含'参数'，实际={result.stderr!r}"
    assert "--period" in result.stderr, f"stderr 应含'--period'，实际={result.stderr!r}"
    assert "非法" in result.stderr, f"stderr 应含'非法'，实际={result.stderr!r}"
    assert "建议" in result.stderr, f"stderr 应含'建议'，实际={result.stderr!r}"


def test_click_bad_parameter_exit_code_1() -> None:
    """click.BadParameter 退出码应为 1 (EXIT_USER_ERROR)。"""
    from click.testing import CliRunner
    from src.errors import EXIT_USER_ERROR
    from src.cli.timeline import cli as timeline_cli

    runner = CliRunner()
    result = runner.invoke(timeline_cli, ["timeline", "--period", "invalid"])
    assert result.exit_code == EXIT_USER_ERROR == 1
