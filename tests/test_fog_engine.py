"""src/fog/fog_engine.py 测试：8 个核心行为。

覆盖：
1. test_fog_segment_erases_content  —— 物理擦除 messages.content
2. test_fog_segment_preserves_skeleton —— L0-骨架字段（topic_label /
   start_at / fog_anchor）经雾化操作后保持不变
3. test_fog_ai_caller_rejected —— called_by != 'user' 立即抛
   PermissionError，user 仍可正常调用
4. test_fog_writes_audit_event —— score_events 写一行 user_fog
   审计记录，reason 含锚点句
5. test_fog_idempotent —— 重复雾化相同段落不重复擦除、不重复写
   审计，幂等返回 True
6. test_fog_cascade_messages —— 段内全部 messages（seq 范围内）
   全部 content=NULL + token_count=0，段外消息不受影响
7. test_is_fogged_and_get_fog_anchor —— 状态查询辅助函数
8. test_fog_session_tear_down —— fog_session 清理入口

风格与 test_segment_writer.py / test_skeleton.py 保持一致：
- 直接构造临时数据库 + 种子消息 + write_segments
- 用 conftest.py 提供的 mtca_db fixture 隔离
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.fog.fog_engine import (
    ANCHOR_MAX_LEN,
    fog_segment,
    fog_session_tear_down,
    get_fog_anchor,
    is_fogged,
)
from src.l0.segment_writer import write_segments
from src.l0.session_writer import create_session, get_session_messages
from src.store.sqlite import get_connection, query


# ---------------------------------------------------------------------------
# 工具：构造会话 + 消息 + 段落
# ---------------------------------------------------------------------------


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    """生成指定本地时间的毫秒时间戳。"""
    dt = datetime(year, month, day, hour, minute)
    return int(dt.timestamp() * 1000)


def _seed_message(
    session_id: str,
    seq: int,
    role: str,
    content: str,
    created_at: int,
    path: Path,
) -> str:
    """直接 INSERT 一条带指定 created_at 的消息（绕过 write_message 的 now_ms）。"""
    message_id = str(uuid.uuid4())
    token_count = max(0, len(content) // 4)
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO messages "
            "(message_id, session_id, seq, role, content, "
            "tool_calls, tool_results, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                message_id,
                session_id,
                seq,
                role,
                content,
                token_count,
                created_at,
            ),
        )
    return message_id


def _seed_messages(
    session_id: str,
    items: list[tuple[int, str, str, int]],
    path: Path,
) -> None:
    """批量写入：(seq, role, content, created_at_ms)。"""
    for seq, role, content, ts in items:
        _seed_message(session_id, seq, role, content, ts, path)


def _setup_session_with_segments(path: Path) -> tuple[str, str, str, str]:
    """创建一个含 1 骨架 + 2 段落的会话。

    会话内容设计：
    - 段 1（seq 1-3，gap=100min）：MTCA 项目骨架设计开发（高相似）
    - 段 2（seq 4-6，gap=0）：天气下雨出行计划（不同话题）

    返回：(session_id, skeleton_id, paragraph1_id, paragraph2_id)
    """
    sid = create_session(path=path)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
            (4, "user", "天气下雨出行计划", base + 100 * 60_000),
            (5, "assistant", "天气下雨带伞防雨", base + 105 * 60_000),
            (6, "user", "天气下雨影响出行", base + 110 * 60_000),
        ],
        path,
    )
    ids = write_segments(sid, path=path)
    # ids[0]=骨架(整个会话); ids[1]=段1(seq1-3); ids[2]=段2(seq4-6)
    return sid, ids[0], ids[1], ids[2]


# ---------------------------------------------------------------------------
# 测试 1: 物理擦除 messages.content
# ---------------------------------------------------------------------------


def test_fog_segment_erases_content(mtca_db: Path) -> None:
    """雾化应将段内每条消息的 content 置 NULL、token_count 置 0。"""
    _, _, para1_id, _ = _setup_session_with_segments(mtca_db)

    # 雾化前段内有非空 content
    rows_before = query(
        "SELECT seq, content, token_count FROM messages "
        "WHERE seq BETWEEN 1 AND 3 ORDER BY seq",
        path=mtca_db,
    )
    for row in rows_before:
        assert row["content"] is not None
        assert int(row["token_count"]) > 0

    # 执行雾化
    assert fog_segment(para1_id, "骨架雾化测试锚点", path=mtca_db) is True

    # 雾化后段内全部清零
    rows_after = query(
        "SELECT seq, content, token_count FROM messages "
        "WHERE seq BETWEEN 1 AND 3 ORDER BY seq",
        path=mtca_db,
    )
    assert len(rows_after) == 3
    for row in rows_after:
        assert row["content"] is None
        assert int(row["token_count"]) == 0


# ---------------------------------------------------------------------------
# 测试 2: 保留 L0-骨架（topic_label / start_at / fog_anchor 不变）
# ---------------------------------------------------------------------------


def test_fog_segment_preserves_skeleton(mtca_db: Path) -> None:
    """雾化段落不应改写 L0-骨架的三字段：topic_label / start_at / fog_anchor。"""
    _, skel_id, para1_id, _ = _setup_session_with_segments(mtca_db)

    # 记录雾化前的骨架字段
    rows = query(
        "SELECT topic_label, start_at, fog_anchor "
        "FROM segments WHERE segment_id = ?",
        (skel_id,), path=mtca_db,
    )
    assert rows, "骨架应已存在"
    orig = rows[0]
    orig_topic = orig["topic_label"]
    orig_start_at = orig["start_at"]
    orig_fog_anchor = orig["fog_anchor"]

    # 触发器保护：直接尝试 UPDATE fog_anchor 应被 RAISE(ABORT) 拒掉
    # （get_connection 直连不会包装错误，抛 sqlite3.IntegrityError）
    with pytest.raises(sqlite3.IntegrityError, match="L0-骨架 不可变"):
        with get_connection(mtca_db) as conn:
            conn.execute(
                "UPDATE segments SET fog_anchor = ? WHERE segment_id = ?",
                ("绕过", skel_id),
            )

    # 执行雾化（不应触发任何 RAISE(ABORT)）
    assert fog_segment(para1_id, "骨架保留测试", path=mtca_db) is True

    # 雾化后骨架三字段应原样保留
    after = query(
        "SELECT topic_label, start_at, fog_anchor "
        "FROM segments WHERE segment_id = ?",
        (skel_id,), path=mtca_db,
    )[0]
    assert after["topic_label"] == orig_topic
    assert int(after["start_at"]) == int(orig_start_at)
    assert after["fog_anchor"] == orig_fog_anchor

    # 被擦段落标记为 fogged_once
    para_row = query(
        "SELECT fog_state, fog_at FROM segments WHERE segment_id = ?",
        (para1_id,), path=mtca_db,
    )[0]
    assert para_row["fog_state"] == "fogged_once"
    assert int(para_row["fog_at"]) > 0


# ---------------------------------------------------------------------------
# 测试 3: AI 调用方被权限闸门拒掉
# ---------------------------------------------------------------------------


def test_fog_ai_caller_rejected(mtca_db: Path) -> None:
    """called_by 非 'user' 应抛 PermissionError；'user' 正常调用。"""
    _, _, para1_id, _ = _setup_session_with_segments(mtca_db)

    # 多种非 user 调用方：全部被拒
    for bad_caller in ("ai", "system", "assistant", "openclaw", ""):
        with pytest.raises(PermissionError, match="AI 无 /雾化 权限"):
            fog_segment(para1_id, "锚点", called_by=bad_caller, path=mtca_db)

    # 雾化前：段未被擦，消息 content 非空
    pre_rows = query(
        "SELECT content FROM messages WHERE seq = 1",
        path=mtca_db,
    )
    assert pre_rows[0]["content"] is not None

    # 显式 'user' 调用应正常工作
    assert fog_segment(para1_id, "用户锚点", called_by="user", path=mtca_db) is True

    # 调用后：段被擦
    post_rows = query(
        "SELECT content FROM messages WHERE seq = 1",
        path=mtca_db,
    )
    assert post_rows[0]["content"] is None


# ---------------------------------------------------------------------------
# 测试 4: 审计事件写入 score_events
# ---------------------------------------------------------------------------


def test_fog_writes_audit_event(mtca_db: Path) -> None:
    """雾化应写一行 event_type='user_fog' 的 score_events，reason 含锚点。"""
    _, _, para1_id, _ = _setup_session_with_segments(mtca_db)

    anchor_text = "审计事件测试锚点句"
    assert fog_segment(para1_id, anchor_text, path=mtca_db) is True

    rows = query(
        "SELECT segment_id, event_type, delta, reason, created_at "
        "FROM score_events "
        "WHERE segment_id = ? AND event_type = 'user_fog'",
        (para1_id,), path=mtca_db,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["segment_id"] == para1_id
    assert row["event_type"] == "user_fog"
    assert row["delta"] == 0
    assert "不可逆" in (row["reason"] or "")
    assert anchor_text in (row["reason"] or "")
    assert int(row["created_at"]) > 0


# ---------------------------------------------------------------------------
# 测试 5: 幂等（重复调用安全，无副作用）
# ---------------------------------------------------------------------------


def test_fog_idempotent(mtca_db: Path) -> None:
    """同一段第二次雾化：返回 True、不写新审计、不重复擦除。"""
    _, _, para1_id, _ = _setup_session_with_segments(mtca_db)

    # 第一次
    assert fog_segment(para1_id, "幂等测试锚点", path=mtca_db) is True
    # 第二次（幂等）
    assert fog_segment(para1_id, "幂等测试锚点", path=mtca_db) is True
    # 第三次（不同 anchor，幂等依然成立）
    assert fog_segment(para1_id, "另一个锚点", path=mtca_db) is True

    # 审计行应只有 1 条
    rows = query(
        "SELECT COUNT(*) AS n FROM score_events "
        "WHERE segment_id = ? AND event_type = 'user_fog'",
        (para1_id,), path=mtca_db,
    )
    assert rows[0]["n"] == 1

    # 段落状态应稳定在 fogged_once
    assert is_fogged(para1_id, path=mtca_db) is True

    # 内容仍为 NULL（幂等执行不会"恢复"已擦内容）
    msgs = query(
        "SELECT content FROM messages WHERE seq BETWEEN 1 AND 3",
        path=mtca_db,
    )
    for m in msgs:
        assert m["content"] is None


# ---------------------------------------------------------------------------
# 测试 6: 级联擦除段内全部 messages（且不影响段外）
# ---------------------------------------------------------------------------


def test_fog_cascade_messages(mtca_db: Path) -> None:
    """雾化段 1 应擦除 seq 1-3 全部 3 条消息，段 2（seq 4-6）保持原状。"""
    sid, _, para1_id, para2_id = _setup_session_with_segments(mtca_db)

    # 雾化段 1
    assert fog_segment(para1_id, "级联测试", path=mtca_db) is True

    # 段 1：全部 content=NULL + token_count=0
    seg1_rows = get_session_messages(sid, path=mtca_db)
    seg1_msgs = [m for m in seg1_rows if 1 <= m["seq"] <= 3]
    assert len(seg1_msgs) == 3, "段 1 应覆盖 seq 1-3 共 3 条"
    for m in seg1_msgs:
        assert m["content"] is None
        assert int(m["token_count"]) == 0

    # 段 2：全部保持原文，content 非空 + token_count>0
    seg2_msgs = [m for m in seg1_rows if 4 <= m["seq"] <= 6]
    assert len(seg2_msgs) == 3, "段 2 应覆盖 seq 4-6 共 3 条"
    for m in seg2_msgs:
        assert m["content"] is not None
        assert int(m["token_count"]) > 0

    # 段 2 仍处于未雾化状态
    assert is_fogged(para2_id, path=mtca_db) is False

    # 后续再雾化段 2 也不应影响段 1 已擦状态
    assert fog_segment(para2_id, "段 2 锚点", path=mtca_db) is True
    final_rows = get_session_messages(sid, path=mtca_db)
    for m in final_rows:
        # 全部 6 条消息都被擦除（段 1 先 + 段 2 后）
        assert m["content"] is None
        assert int(m["token_count"]) == 0


# ---------------------------------------------------------------------------
# 测试 7: is_fogged / get_fog_anchor / fog_session_tear_down
# ---------------------------------------------------------------------------


def test_is_fogged_and_get_fog_anchor(mtca_db: Path) -> None:
    """状态查询与 anchor 读取在雾化前/后应保持稳定。"""
    sid, skel_id, para1_id, para2_id = _setup_session_with_segments(mtca_db)

    # 雾化前：is_fogged=False、get_fog_anchor 返回 save_skeleton 写入值
    assert is_fogged(para1_id, path=mtca_db) is False
    assert is_fogged("non-existent-uuid", path=mtca_db) is False

    # fog_session_tear_down 在无授权记录时应返回 0
    assert fog_session_tear_down(sid, path=mtca_db) == 0

    # 锚点读取：骨架上 fog_anchor 由 save_skeleton 写入（非空）
    skel_anchor = get_fog_anchor(skel_id, path=mtca_db)
    assert skel_anchor is not None and len(skel_anchor) > 0

    # 段落上 fog_anchor 由 time_segmenter 写入时为 NULL（L0-B 段）
    para1_anchor = get_fog_anchor(para1_id, path=mtca_db)
    assert para1_anchor is None or para1_anchor == ""

    # 执行雾化
    assert fog_segment(para1_id, "查询测试", path=mtca_db) is True

    # 雾化后：is_fogged=True、骨架 fog_anchor 保持不变
    assert is_fogged(para1_id, path=mtca_db) is True
    assert is_fogged(para2_id, path=mtca_db) is False

    # 骨架 fog_anchor 未被改写（与雾化前一致）
    assert get_fog_anchor(skel_id, path=mtca_db) == skel_anchor


# ---------------------------------------------------------------------------
# 测试 8: 参数校验 + 锚点截断 + 不存在段落
# ---------------------------------------------------------------------------


def test_input_validation_and_anchor_truncation(mtca_db: Path) -> None:
    """非法参数应抛 ValueError；超长 anchor 应截断到 ANCHOR_MAX_LEN。"""
    sid, _, para1_id, _ = _setup_session_with_segments(mtca_db)

    # 空 segment_id / 空 anchor
    with pytest.raises(ValueError, match="segment_id"):
        fog_segment("", "锚点", path=mtca_db)
    with pytest.raises(ValueError, match="anchor"):
        fog_segment(para1_id, "", path=mtca_db)
    with pytest.raises(ValueError, match="anchor"):
        fog_segment(para1_id, "   ", path=mtca_db)  # 纯空白截断后为空

    # 非字符串 anchor
    with pytest.raises(ValueError, match="anchor"):
        fog_segment(para1_id, 12345, path=mtca_db)  # type: ignore[arg-type]

    # 不存在的 segment_id → ValueError
    with pytest.raises(ValueError, match="段落不存在"):
        fog_segment("non-existent-uuid", "锚点", path=mtca_db)

    # 超长 anchor：截断到 ANCHOR_MAX_LEN 后写入审计
    long_anchor = "X" * 100  # 100 字
    assert fog_segment(para1_id, long_anchor, path=mtca_db) is True

    rows = query(
        "SELECT reason FROM score_events "
        "WHERE segment_id = ? AND event_type = 'user_fog'",
        (para1_id,), path=mtca_db,
    )
    assert len(rows) == 1
    # reason 形如 "不可逆: XXXXX..."；锚点部分 <= ANCHOR_MAX_LEN
    reason = rows[0]["reason"] or ""
    _, _, anchor_in_reason = reason.partition(": ")
    assert len(anchor_in_reason) == ANCHOR_MAX_LEN
    assert anchor_in_reason == "X" * ANCHOR_MAX_LEN
