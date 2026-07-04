"""src/l0/segment_writer.py 测试：7 个核心行为 + 1 个边界。

覆盖：
- write_segments: 一站式写入 L0-骨架 + L0-B 段落
- write_segments: 幂等性（同 session 重复调用不重复写骨架）
- get_segment: 命中 / 未命中两种情况
- list_segments: 按 session / 全局 + limit 行为
- update_segment: 正常字段更新 + 自动跳过不可变字段
- update_segment: 未知字段与 None 静默丢弃，无 SQL 错误
- update_segment: 触发器保护（topic_label / start_at 不允许绕过白名单）
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.l0.segment_writer import (
    get_segment,
    list_segments,
    update_segment,
    write_segments,
)
from src.l0.session_writer import create_session
from src.store.sqlite import get_connection, init_db, query


# ---------------------------------------------------------------------------
# 工具：构造任意时间戳的消息（与 test_time_segmenter.py 保持一致风格）
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
    """直接 INSERT 一条带指定 created_at 的消息。"""
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


# ---------------------------------------------------------------------------
# 测试 1: write_segments 同时写入骨架 + L0-B 段落
# ---------------------------------------------------------------------------


def test_write_segments_writes_skeleton_and_paragraphs(mtca_db: Path) -> None:
    """write_segments 应：写入 1 条骨架 + 2 条 L0-B 段落，骨架先返回。"""
    sid = create_session(path=mtca_db)
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
        mtca_db,
    )

    ids = write_segments(sid, path=mtca_db)
    assert isinstance(ids, list)
    assert len(ids) == 3  # 1 骨架 + 2 段落
    for i in ids:
        assert isinstance(i, str) and len(i) >= 32

    # 落库确认
    rows = query(
        "SELECT segment_id, start_msg_seq, end_msg_seq, gap_to_next, fog_anchor "
        "FROM segments WHERE session_id = ? ORDER BY start_at ASC",
        (sid,), path=mtca_db,
    )
    assert len(rows) == 3

    # 第一条是骨架（start_msg_seq=1, end_msg_seq=6, fog_anchor 非空）
    skel_row = rows[0]
    assert skel_row["segment_id"] == ids[0]
    assert skel_row["start_msg_seq"] == 1
    assert skel_row["end_msg_seq"] == 6
    assert skel_row["fog_anchor"] is not None and len(skel_row["fog_anchor"]) > 0

    # 第二、三条是 L0-B 段落（fog_anchor 应为 NULL）
    assert rows[1]["segment_id"] == ids[1]
    assert rows[1]["start_msg_seq"] == 1
    assert rows[1]["end_msg_seq"] == 3
    assert rows[1]["gap_to_next"] == 90
    assert rows[2]["start_msg_seq"] == 4
    assert rows[2]["end_msg_seq"] == 6
    assert rows[2]["gap_to_next"] == 0


# ---------------------------------------------------------------------------
# 测试 2: write_segments 幂等（L0-骨架 不可变）
# ---------------------------------------------------------------------------


def test_write_segments_is_idempotent(mtca_db: Path) -> None:
    """二次 write_segments 同一 session 应：骨架 ID 不变、不重复写骨架。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        mtca_db,
    )

    ids1 = write_segments(sid, path=mtca_db)
    ids2 = write_segments(sid, path=mtca_db)

    # 骨架 ID（首条）应相同；段落也会重新 INSERT（每次 new UUID）
    assert ids1[0] == ids2[0]

    # 骨架行（fog_anchor IS NOT NULL）总数应为 1
    rows = query(
        "SELECT COUNT(*) AS n FROM segments WHERE session_id = ? "
        "AND fog_anchor IS NOT NULL",
        (sid,), path=mtca_db,
    )
    assert rows[0]["n"] == 1


# ---------------------------------------------------------------------------
# 测试 3: get_segment 命中与未命中
# ---------------------------------------------------------------------------


def test_get_segment_hit_and_miss(mtca_db: Path) -> None:
    """get_segment 应：命中返回完整 dict，未命中返回 None。"""
    sid = create_session(path=mtca_db)
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
        mtca_db,
    )
    ids = write_segments(sid, path=mtca_db)

    # 命中
    seg = get_segment(ids[1], path=mtca_db)
    assert isinstance(seg, dict)
    assert seg["segment_id"] == ids[1]
    assert seg["session_id"] == sid
    assert seg["start_msg_seq"] == 1
    assert seg["end_msg_seq"] == 3
    assert seg["gap_to_next"] == 90

    # 未命中（不存在的 UUID）
    assert get_segment("does-not-exist-uuid-0000", path=mtca_db) is None
    assert get_segment("", path=mtca_db) is None


# ---------------------------------------------------------------------------
# 测试 4: list_segments 按 session 过滤 / 全局查询 / limit 行为
# ---------------------------------------------------------------------------


def test_list_segments_filter_and_limit(mtca_db: Path) -> None:
    """list_segments 应：按 session 过滤、全局查询、limit 上限生效。"""
    # 准备 2 个 session
    sid_a = create_session(path=mtca_db)
    sid_b = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)

    # session A：6 条消息 → 1 骨架 + 2 段落
    _seed_messages(
        sid_a,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
            (4, "user", "天气下雨出行计划", base + 100 * 60_000),
            (5, "assistant", "天气下雨带伞防雨", base + 105 * 60_000),
            (6, "user", "天气下雨影响出行", base + 110 * 60_000),
        ],
        mtca_db,
    )
    write_segments(sid_a, path=mtca_db)

    # session B：3 条消息 → 1 骨架 + 1 段落
    _seed_messages(
        sid_b,
        [
            (1, "user", "下午编写 pytest 代码", base + 1000 * 60_000),
            (2, "assistant", "Python 编写 pytest 代码", base + 1005 * 60_000),
            (3, "user", "pytest 编写测试代码", base + 1010 * 60_000),
        ],
        mtca_db,
    )
    write_segments(sid_b, path=mtca_db)

    # 按 session 过滤
    rows_a = list_segments(session_id=sid_a, path=mtca_db)
    assert len(rows_a) == 3
    for r in rows_a:
        assert r["session_id"] == sid_a

    rows_b = list_segments(session_id=sid_b, path=mtca_db)
    assert len(rows_b) == 2
    for r in rows_b:
        assert r["session_id"] == sid_b

    # 全局查询（不指定 session）
    rows_all = list_segments(path=mtca_db)
    assert len(rows_all) == 5  # 3 + 2

    # limit 限制
    rows_lim1 = list_segments(session_id=sid_a, limit=1, path=mtca_db)
    assert len(rows_lim1) == 1

    # limit 非法值回落到默认（20）
    rows_neg = list_segments(limit=-5, path=mtca_db)
    assert len(rows_neg) <= 20

    # limit 字符串应回落
    rows_str = list_segments(limit="abc", path=mtca_db)
    assert len(rows_str) == 5  # 5 < 20，所以全返回


# ---------------------------------------------------------------------------
# 测试 5: update_segment 正常字段更新
# ---------------------------------------------------------------------------


def test_update_segment_updates_field(mtca_db: Path) -> None:
    """更新 current_score / current_tier / ref_count 应生效并返回 1。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        mtca_db,
    )
    ids = write_segments(sid, path=mtca_db)
    para_id = ids[1]  # 取一条段落

    # 更新多个字段
    n = update_segment(
        para_id,
        path=mtca_db,
        current_tier="L1",
        current_score=87.5,
        ref_count=3,
        silence_state="active",
    )
    assert n == 1

    # 回查确认
    row = get_segment(para_id, path=mtca_db)
    assert row is not None
    assert row["current_tier"] == "L1"
    assert abs(float(row["current_score"]) - 87.5) < 1e-6
    assert int(row["ref_count"]) == 3
    assert row["silence_state"] == "active"


# ---------------------------------------------------------------------------
# 测试 6: update_segment 静默丢弃不可变 / 未知 / None 字段
# ---------------------------------------------------------------------------


def test_update_segment_skips_invalid_fields(mtca_db: Path) -> None:
    """不可变 / 未知 / None 字段应被丢弃，合法字段仍能生效；返回行数 1。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        mtca_db,
    )
    ids = write_segments(sid, path=mtca_db)
    para_id = ids[1]

    # 同时传入：合法 + 不可变 + 未知 + None
    # （注意：segment_id / path 由签名定义，不能放进 fields，否则会触发
    # TypeError: got multiple values for argument 'segment_id'，验证实现层保护）
    n = update_segment(
        para_id,
        path=mtca_db,
        current_score=42.0,        # 合法
        topic_label="恶意改写",     # 不可变（触发器保护）应被应用层挡掉
        start_at=0,                # 不可变（触发器保护）应被挡掉
        fog_anchor="新锚点",        # 不可变（触发器保护）应被挡掉
        session_id="OTHER",        # FK / 不可变
        start_msg_seq=99,          # 不可变
        fake_column="xxx",         # 未知字段
        current_tier=None,         # None 字段
    )
    assert n == 1

    # 合法字段已生效
    row = get_segment(para_id, path=mtca_db)
    assert row is not None
    assert abs(float(row["current_score"]) - 42.0) < 1e-6

    # 不可变 / 未知 / None 字段未被改写
    assert row["topic_label"] != "恶意改写"
    assert row["fog_anchor"] != "新锚点"
    assert row["segment_id"] == para_id
    assert row["session_id"] == sid

    # 空 fields 应直接返回 0，不发 SQL
    assert update_segment(para_id, path=mtca_db) == 0
    assert update_segment(para_id, unknown_only=1, path=mtca_db) == 0
    assert update_segment(para_id, topic_label=None, path=mtca_db) == 0

    # 不存在的 segment_id → 受影响行数 0
    assert update_segment(
        "no-such-segment-uuid-xxx",
        path=mtca_db,
        current_score=10.0,
    ) == 0


# ---------------------------------------------------------------------------
# 测试 7: 无段落落库时 write_segments / list_segments 行为
# ---------------------------------------------------------------------------


def test_write_segments_empty_messages(mtca_db: Path) -> None:
    """空 session：写 1 条骨架，list_segments 应返回 1 条。"""
    sid = create_session(path=mtca_db)
    # 不写任何消息

    ids = write_segments(sid, path=mtca_db)
    # 骨架仍然写入（even on empty，end_msg_seq=1）
    assert len(ids) == 1

    # list_segments 应按该 session_id 返回 1 条
    rows = list_segments(session_id=sid, path=mtca_db)
    assert len(rows) == 1
    assert rows[0]["segment_id"] == ids[0]
    assert rows[0]["start_msg_seq"] == 1


# ---------------------------------------------------------------------------
# 测试 8: list_segments 默认参数 (session_id=None, limit=20) 必须显式传 path
#           避免污染真实 ~/.mtca/mtca.db
# ---------------------------------------------------------------------------


def test_list_segments_does_not_touch_global_db(mtca_db: Path, tmp_path: Path) -> None:
    """显式传 path 时不应触碰默认 MTCA_DB_PATH（用户目录）。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        mtca_db,
    )
    write_segments(sid, path=mtca_db)

    # 用 mtca_db 路径显式调用，验证行为
    rows = list_segments(path=mtca_db)
    assert len(rows) >= 2

    # 用一个明确空白路径（其它 DB）调用 → 应当返回 0
    other_db = tmp_path / "other.db"
    init_conn = init_db(other_db)
    init_conn.close()
    assert list_segments(session_id=sid, path=other_db) == []
