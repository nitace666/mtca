"""src/store/sqlite.py 测试：8 个核心行为。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.store.sqlite import (
    execute,
    fog_permit,
    get_connection,
    get_stats,
    init_db,
    query,
)


# ---------------------------------------------------------------------------
# 辅助：插入一条 session（messages 的外键依赖）
# ---------------------------------------------------------------------------


def _insert_session(db: Path, sid: str = "s1", started_at: int = 1_700_000_000_000) -> None:
    """向 sessions 表插入一条最小记录，供后续 messages / segments 使用。"""
    execute(
        "INSERT INTO sessions (session_id, started_at) VALUES (?, ?)",
        (sid, started_at),
        path=db,
    )


def _insert_message(
    db: Path,
    mid: str = "m1",
    sid: str = "s1",
    seq: int = 1,
    content: str = "hello",
) -> None:
    """向 messages 表插入一条最小记录。"""
    execute(
        "INSERT INTO messages "
        "(message_id, session_id, seq, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, sid, seq, "user", content, 1_700_000_000_000),
        path=db,
    )


# ---------------------------------------------------------------------------
# 测试 1：init_db 创建全部表
# ---------------------------------------------------------------------------


def test_init_creates_all_tables(mtca_db: Path) -> None:
    """init_db 应创建 6 张主表 + 1 张控制表 + FTS5 虚拟表。"""
    with get_connection(mtca_db) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table') ORDER BY name"
        )
        names = {row["name"] for row in cur.fetchall()}

    expected = {
        "sessions",
        "messages",
        "segments",
        "segment_relations",
        "views",
        "score_events",
        "fog_session",
        "messages_fts",
    }
    assert expected.issubset(names), f"缺少表：{expected - names}"


# ---------------------------------------------------------------------------
# 测试 2：PRAGMA 应用
# ---------------------------------------------------------------------------


def test_pragmas_applied(tmp_db: Path) -> None:
    """init_db 后 journal_mode / synchronous / foreign_keys 应正确配置。"""
    conn = init_db(tmp_db)
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        cache_size = conn.execute("PRAGMA cache_size").fetchone()[0]
        mmap_size = conn.execute("PRAGMA mmap_size").fetchone()[0]
        temp_store = conn.execute("PRAGMA temp_store").fetchone()[0]
    finally:
        conn.close()

    # journal_mode 在 WAL 下返回 "wal"；:memory: 等场景可能回退
    assert str(journal).lower() == "wal"
    assert int(synchronous) == 1  # NORMAL
    assert int(foreign_keys) == 1
    assert int(cache_size) == -64000
    assert int(mmap_size) == 268_435_456
    assert int(temp_store) == 2  # MEMORY


# ---------------------------------------------------------------------------
# 测试 3：AI 通道 UPDATE messages 被禁止
# ---------------------------------------------------------------------------


def test_no_update_messages_blocks_ai(mtca_db: Path) -> None:
    """未授权 fog 通道时，UPDATE messages 应被触发器 RAISE(ABORT)。"""
    _insert_session(mtca_db)
    _insert_message(mtca_db)

    with pytest.raises(RuntimeError) as exc_info:
        execute(
            "UPDATE messages SET content = ? WHERE message_id = ?",
            ("new content", "m1"),
            path=mtca_db,
        )
    assert "L0" in str(exc_info.value) or "禁止" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 测试 4：fog_permit 允许用户 UPDATE messages
# ---------------------------------------------------------------------------


def test_fog_permit_allows_user_update(mtca_db: Path) -> None:
    """fog_permit(user) 后 UPDATE messages 应成功（雾化通道放行）。"""
    _insert_session(mtca_db)
    _insert_message(mtca_db)

    # 授权
    fog_permit(session_id="s1", called_by="user", path=mtca_db)
    # UPDATE 不再抛异常
    execute(
        "UPDATE messages SET content = NULL, token_count = 0 "
        "WHERE session_id = ?",
        ("s1",),
        path=mtca_db,
    )

    rows = query("SELECT content FROM messages WHERE session_id = ?", ("s1",), path=mtca_db)
    assert rows[0]["content"] is None


# ---------------------------------------------------------------------------
# 测试 5：禁止删除消息
# ---------------------------------------------------------------------------


def test_no_delete_messages(mtca_db: Path) -> None:
    """DELETE messages 应被 no_delete_messages 触发器阻止。"""
    _insert_session(mtca_db)
    _insert_message(mtca_db)

    with pytest.raises(RuntimeError) as exc_info:
        execute("DELETE FROM messages WHERE message_id = ?", ("m1",), path=mtca_db)
    assert "L0" in str(exc_info.value) or "禁止" in str(exc_info.value)

    # 数据仍然存在
    rows = query("SELECT COUNT(*) AS n FROM messages", path=mtca_db)
    assert rows[0]["n"] == 1


# ---------------------------------------------------------------------------
# 测试 6：get_stats 返回核心表行数
# ---------------------------------------------------------------------------


def test_get_stats(mtca_db: Path) -> None:
    """get_stats 应返回包含 sessions/messages/segments/views 四个键的 dict。"""
    # 空库全部为 0
    stats = get_stats(mtca_db)
    assert isinstance(stats, dict)
    assert set(stats.keys()) >= {"sessions", "messages", "segments", "views"}
    assert all(v == 0 for v in stats.values())

    # 插入后计数变化
    _insert_session(mtca_db, sid="s1")
    _insert_session(mtca_db, sid="s2")
    _insert_message(mtca_db, mid="m1", sid="s1", seq=1)

    stats = get_stats(mtca_db)
    assert stats["sessions"] == 2
    assert stats["messages"] == 1
    assert stats["segments"] == 0
    assert stats["views"] == 0


# ---------------------------------------------------------------------------
# 测试 7：query 返回 list[dict]
# ---------------------------------------------------------------------------


def test_query_returns_list_of_dict(mtca_db: Path) -> None:
    """query() 必须返回 list[dict]，每个 dict 一行记录。"""
    _insert_session(mtca_db, sid="s1", started_at=1)
    _insert_session(mtca_db, sid="s2", started_at=2)

    rows = query(
        "SELECT session_id, started_at FROM sessions ORDER BY started_at",
        path=mtca_db,
    )

    assert isinstance(rows, list)
    assert len(rows) == 2
    for row in rows:
        assert isinstance(row, dict)
    assert rows[0]["session_id"] == "s1"
    assert rows[1]["session_id"] == "s2"
    assert rows[0]["started_at"] == 1


# ---------------------------------------------------------------------------
# 测试 8：fog_permit 拒绝非 'user' 调用方
# ---------------------------------------------------------------------------


def test_fog_permit_rejects_ai_caller(mtca_db: Path) -> None:
    """called_by != 'user' 应抛出 PermissionError，AI 无权雾化。"""
    with pytest.raises(PermissionError) as exc_info:
        fog_permit(session_id="s1", called_by="ai")
    assert "user" in str(exc_info.value)

    # fog_session 应保持为空
    rows = query("SELECT COUNT(*) AS n FROM fog_session", path=mtca_db)
    assert rows[0]["n"] == 0