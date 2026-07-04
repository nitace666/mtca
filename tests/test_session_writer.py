"""src/l0/session_writer.py 测试：6 个核心行为 + 2 个边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.l0.session_writer import (
    create_session,
    end_session,
    get_session,
    get_session_messages,
    write_message,
)
from src.store.sqlite import query


# ---------------------------------------------------------------------------
# 测试 1：create_session 应返回 UUID 并落库
# ---------------------------------------------------------------------------


def test_create_session(mtca_db: Path) -> None:
    """create_session 应返回 UUID 字符串，且 sessions 表能查到该会话。"""
    sid = create_session(agent_source="openclaw", path=mtca_db)
    assert isinstance(sid, str) and len(sid) >= 32

    row = get_session(sid, path=mtca_db)
    assert row is not None
    assert row["session_id"] == sid
    assert row["agent_source"] == "openclaw"
    assert row["ended_at"] is None
    assert row["message_count"] == 0
    assert row["token_estimate"] == 0


# ---------------------------------------------------------------------------
# 测试 2：write_message 在同会话内单调递增 seq
# ---------------------------------------------------------------------------


def test_write_message_increments_seq(mtca_db: Path) -> None:
    """连续写入 3 条消息，seq 应依次为 1 / 2 / 3。"""
    sid = create_session(path=mtca_db)
    write_message(sid, "user", "msg-1", path=mtca_db)
    write_message(sid, "assistant", "msg-2", path=mtca_db)
    write_message(sid, "user", "msg-3", path=mtca_db)

    rows = get_session_messages(sid, path=mtca_db)
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["role"] for r in rows] == ["user", "assistant", "user"]
    assert all(isinstance(r["message_id"], str) and len(r["message_id"]) >= 32
               for r in rows)


# ---------------------------------------------------------------------------
# 测试 3：write_message 更新 sessions.message_count 与 token_estimate
# ---------------------------------------------------------------------------


def test_write_message_updates_session_stats(mtca_db: Path) -> None:
    """每次写入后 sessions.message_count +1，token_estimate 累加。"""
    sid = create_session(path=mtca_db)
    # content 长度 = 8 → token_count = 8 // 4 = 2
    write_message(sid, "user", "12345678", path=mtca_db)
    # content 长度 = 16 → token_count = 4
    write_message(sid, "assistant", "x" * 16, path=mtca_db)

    row = get_session(sid, path=mtca_db)
    assert row["message_count"] == 2
    assert row["token_estimate"] == 6  # 2 + 4


# ---------------------------------------------------------------------------
# 测试 4：get_session_messages 返回按 seq 升序的全部字段
# ---------------------------------------------------------------------------


def test_get_session_messages_returns_ordered(mtca_db: Path) -> None:
    """get_session_messages 应按 seq 升序返回，且 dict 包含关键列。"""
    sid = create_session(path=mtca_db)
    write_message(
        sid, "assistant", "tool call",
        tool_calls={"name": "search", "args": {"q": "mtca"}},
        path=mtca_db,
    )
    write_message(
        sid, "tool", "search result",
        tool_results={"hits": 3},
        path=mtca_db,
    )

    rows = get_session_messages(sid, path=mtca_db)
    assert isinstance(rows, list)
    assert len(rows) == 2
    # 顺序检查（seq 升序）
    assert rows[0]["seq"] == 1
    assert rows[1]["seq"] == 2
    # 关键列存在
    for row in rows:
        assert {"message_id", "session_id", "seq", "role",
                "content", "tool_calls", "tool_results",
                "token_count", "created_at"}.issubset(row.keys())
    # 序列化字段被保留为 JSON 字符串
    import json as _json
    assert _json.loads(rows[0]["tool_calls"]) == {"name": "search", "args": {"q": "mtca"}}
    assert _json.loads(rows[1]["tool_results"]) == {"hits": 3}


# ---------------------------------------------------------------------------
# 测试 5：token_count 估算 = len(content) // 4
# ---------------------------------------------------------------------------


def test_token_count_estimated(mtca_db: Path) -> None:
    """token_count 应等于 len(content) // 4。"""
    sid = create_session(path=mtca_db)
    mid = write_message(sid, "user", "a" * 100, path=mtca_db)
    rows = get_session_messages(sid, path=mtca_db)
    assert rows[0]["message_id"] == mid
    assert rows[0]["token_count"] == 25  # 100 // 4

    # 空字符串 → 0
    write_message(sid, "system", "", path=mtca_db)
    rows = get_session_messages(sid, path=mtca_db)
    assert rows[1]["token_count"] == 0


# ---------------------------------------------------------------------------
# 测试 6：end_session 设置 ended_at
# ---------------------------------------------------------------------------


def test_end_session_sets_ended_at(mtca_db: Path) -> None:
    """end_session 后 sessions.ended_at 应非空；未结束前为 None。"""
    sid = create_session(path=mtca_db)
    pre = get_session(sid, path=mtca_db)
    assert pre["ended_at"] is None

    end_session(sid, path=mtca_db)
    after = get_session(sid, path=mtca_db)
    assert isinstance(after["ended_at"], int)
    assert after["ended_at"] > 0


# ---------------------------------------------------------------------------
# 测试 7（边界）：非法 role 应抛 ValueError
# ---------------------------------------------------------------------------


def test_write_message_rejects_invalid_role(mtca_db: Path) -> None:
    """role 不在白名单时应抛 ValueError，且消息未写入。"""
    sid = create_session(path=mtca_db)
    with pytest.raises(ValueError) as exc_info:
        write_message(sid, "god", "oops", path=mtca_db)
    assert "role" in str(exc_info.value)

    rows = query(
        "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
        (sid,), path=mtca_db,
    )
    assert rows[0]["n"] == 0


# ---------------------------------------------------------------------------
# 测试 8（边界）：content 非 str 应抛 ValueError
# ---------------------------------------------------------------------------


def test_write_message_rejects_non_str_content(mtca_db: Path) -> None:
    """content 必须为 str；非 str（None / int / dict）应抛 ValueError。"""
    sid = create_session(path=mtca_db)
    with pytest.raises(ValueError):
        write_message(sid, "user", None, path=mtca_db)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        write_message(sid, "user", 123, path=mtca_db)  # type: ignore[arg-type]
