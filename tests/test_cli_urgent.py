"""tests/test_cli_urgent.py — M2.5.4 CLI 微起追踪 3 命令测试

覆盖：
- _time_parse.parse_expires × 5（8 种输入 + 错误路径）
- cmd_urgent / cmd_done / cmd_postpone 命令函数 × 4
- click 集成 / argparse fallback × 2
- parse_command 中文前缀识别 × 2

总计 ≥ 13 个测试。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli.user_controls import (
    cli,
    cmd_done,
    cmd_postpone,
    cmd_urgent,
    parse_command,
)
from src.cli._time_parse import parse_expires
from src.lifecycle.urgent_tracker import (
    POSTPONE_DAYS,
    handle_urgent_response,
    tick_urgent,
    track_urgent,
)
from src.l0.segment_writer import get_segment
from src.l0.session_writer import create_session, write_message
from src.l0.skeleton import build_skeleton, save_skeleton
from src.store.sqlite import execute, init_db


# ===========================================================================
# _time_parse 5 个单测
# ===========================================================================

def test_parse_expires_tomorrow_alias():
    """明天 → +1d。"""
    now = 1_700_000_000_000
    ts = parse_expires("明天", now)
    assert ts - now == 86400 * 1000


def test_parse_expires_3d_format():
    """3d → +3d。"""
    now = 1_700_000_000_000
    ts = parse_expires("3d", now)
    assert ts - now == 3 * 86400 * 1000


def test_parse_expires_iso_date():
    """ISO 日期 → 该天 00:00 本地时间转 ms。

    Windows 下 datetime.strptime + .timestamp() 用本地时区，
    故不硬编码期望值，只验证范围。
    """
    ts = parse_expires("2026-07-10", 0)
    assert ts > 0
    # 2026-07-10T00:00:00 本地 → 合理范围 [1.78e12, 1.79e12)
    assert 1_780_000_000_000 <= ts < 1_790_000_000_000


def test_parse_expires_unix_ms_passthrough():
    """毫秒撒（13 位）原样返回。"""
    now = 1_700_000_000_000
    assert parse_expires("1700000000000", now) == 1700000000000


def test_parse_expires_invalid_raises():
    """垃圾输入 抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_expires("garbage", 0)
    with pytest.raises(ValueError):
        parse_expires("", 0)


# ===========================================================================
# fixture：创建含一个 segment 的临时库
# ===========================================================================

@pytest.fixture
def db_with_segment(tmp_path: Path):
    """返回 (db_path, segment_id)。"""
    db = tmp_path / "cli_urgent.db"
    init_db(db).close()
    sid = create_session(path=db)
    write_message(sid, "user", "hello", path=db)
    skel = build_skeleton(sid, path=db)
    skel["topic_label"] = "test_topic"
    seg_id = save_skeleton(sid, skel, path=db)
    return db, seg_id


# ===========================================================================
# cmd_urgent / cmd_done / cmd_postpone 函数级测试
# ===========================================================================

def test_cmd_urgent_basic(db_with_segment):
    """cmd_urgent('tomorrow') 写入 tracking，expires ± 几秒。"""
    db, seg_id = db_with_segment
    now = int(time.time() * 1000)
    result = cmd_urgent(seg_id, when="明天", path=db)
    assert result["urgent_state"] == "tracking"
    assert result["urgency_level"] == 1.0
    # expires ~ now + 1d（允许 10s 漂移）
    assert abs(result["expires_at_ms"] - (now + 86400 * 1000)) < 10_000


def test_cmd_urgent_with_ms_timestamp(db_with_segment):
    """直接传毫秒撒。"""
    db, seg_id = db_with_segment
    fixed = 1_800_000_000_000
    result = cmd_urgent(seg_id, when=str(fixed), path=db)
    assert result["expires_at_ms"] == fixed
    assert result["urgent_state"] == "tracking"


def test_cmd_urgent_with_iso_date(db_with_segment):
    """ISO 日期 —— 5d。"""
    db, seg_id = db_with_segment
    result = cmd_urgent(seg_id, when="5d", path=db)
    assert result["urgent_state"] == "tracking"
    assert result["expires_at_ms"] > int(time.time() * 1000)


def test_cmd_urgent_invalid_time_raises(db_with_segment):
    """无效时间串 抛 ValueError。"""
    db, seg_id = db_with_segment
    with pytest.raises(ValueError):
        cmd_urgent(seg_id, when="垃圾", path=db)


def test_cmd_done_only_on_tracking_raises(db_with_segment):
    """非 tracking 段调 cmd_done 抛 ValueError。"""
    db, seg_id = db_with_segment
    with pytest.raises(ValueError):
        cmd_done(seg_id, path=db)


def test_cmd_postpone_resets_to_tracking(db_with_segment):
    """先 urgent 再 postpone → state 回 tracking，expires +7d。"""
    db, seg_id = db_with_segment
    cmd_urgent(seg_id, when="明天", path=db)
    before = int(time.time() * 1000)
    result = cmd_postpone(seg_id, path=db)
    assert result["new_state"] == "tracking"
    expected = before + POSTPONE_DAYS * 86400 * 1000
    # 允许 10s 漂移
    assert abs(result["expires_at_ms"] - expected) < 10_000


def test_cmd_done_after_expired(db_with_segment):
    """先 urgent → 人为过期 → tick → done → completed。"""
    db, seg_id = db_with_segment
    cmd_urgent(seg_id, when="明天", path=db)
    # 人为把过期时间调到过去
    execute(
        "UPDATE segments SET expires_at_ms = ? WHERE segment_id = ?",
        (int(time.time() * 1000) - 1000, seg_id),
        path=db,
    )
    tick_urgent(path=db, now_ms=int(time.time() * 1000))
    result = cmd_done(seg_id, path=db)
    assert result["new_state"] == "completed"
    assert result["current_tier"] == "L2"
    # 落库验证：segment 状态已变
    seg = get_segment(seg_id, path=db)
    assert seg["urgent_state"] == "completed"


# ===========================================================================
# click CLI 集成测试（默认数据库路径，不可接口）
# ===========================================================================

def test_cli_click_urgent_with_default_db(tmp_path, monkeypatch):
    """click 'urgent' 调用走完整链路（默认数据库 + monkeypatch track_urgent）。"""
    runner = CliRunner()
    # 默认 DB 路径可能不存在，用 monkeypatch 拦截下层
    import src.cli.user_controls as uc

    monkeypatch.setattr(uc, "cmd_urgent", lambda *a, **kw: {
        "segment_id": "x", "urgent_state": "tracking",
        "expires_at_ms": 123, "urgency_level": 1.0,
    })
    result = runner.invoke(cli, ["urgent", "seg-uuid", "tomorrow"])
    assert result.exit_code == 0
    assert "✅" in result.output or "OK" in result.output or "已 /" in result.output


def test_cli_click_done_output_format():
    """click 'done' 在 错误输入下输出 ERR 并退出码 2。"""
    runner = CliRunner()
    result = runner.invoke(cli, ["done", "non-existent-seg-id-1234"])
    # 默认 DB 不存在，handle_urgent_response 抛 ValueError
    assert result.exit_code == 2


def test_cli_click_postpone_help():
    """click 'postpone' --help 能打印说明。"""
    runner = CliRunner()
    result = runner.invoke(cli, ["postpone", "--help"])
    assert result.exit_code == 0
    assert "postpone" in result.output.lower() or "维护" in result.output or "延期" in result.output


# ===========================================================================
# argparse fallback 测试
# ===========================================================================

def test_argparse_fallback_urgent_invalid_time(tmp_path):
    """argparse fallback 路径 —— 无效时间返回非零退出码。"""
    from src.cli.user_controls import _argparse_main
    import sys
    # --db 是顶层参数，子命令序变为 urgent <seg> <when>
    rc = _argparse_main(
        ["--db", str(tmp_path / "x.db"), "urgent", "seg-x", "garbage"]
    )
    assert rc == 2


# ===========================================================================
# parse_command 中文前缀识别
# ===========================================================================

def test_parse_command_urgent_prefix():
    """/紧急 前缀 → ('urgent', [...args])。"""
    cmd, args = parse_command("/紧急 seg-abc 明天")
    assert cmd == "urgent"
    assert args == ["seg-abc", "明天"]


def test_parse_command_done_postpone_prefix():
    """/完成、/延期前缀识别。"""
    cmd1, _ = parse_command("/完成 seg-abc")
    assert cmd1 == "done"
    cmd2, args2 = parse_command("/延期 seg-abc 3d")
    assert cmd2 == "postpone"
    assert args2 == ["seg-abc", "3d"]
