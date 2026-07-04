"""src/l0/time_segmenter.py 测试：8 个核心行为。

覆盖：
- 短会话 / 长会话的边界
- MAX_GAP_MINUTES 强制切分
- MIN_SEGMENT_MSGS 短段过滤
- 弱合并：Jaccard 命中与拒绝
- 夜段合并：跨午夜窗口
- save_segments 落库 + 字段校验
- 端到端 realistic 对话
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.l0.session_writer import create_session
from src.l0.time_segmenter import (
    MAX_GAP_MINUTES,
    MIN_SEGMENT_MSGS,
    NIGHT_MERGE_WINDOW,
    is_night_merge,
    save_segments,
    segment_session,
    weak_merge_check,
)
from src.store.sqlite import get_connection, query


# ---------------------------------------------------------------------------
# 工具：构造任意时间戳的消息（绕过 write_message 的 _now_ms）
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
    """直接 INSERT 一条带指定 created_at 的消息，返回 message_id。"""
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
# 测试 1：短会话（消息数 == MIN_SEGMENT_MSGS）→ 1 段
# ---------------------------------------------------------------------------


def test_short_session_single_segment(mtca_db: Path) -> None:
    """3 条高关键词重叠的紧密消息 → 1 段；< MIN_SEGMENT_MSGS → 0 段。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    # 每条都共享 {MTCA, 项目, 骨架, 开发}，Jaccard 始终 >= 0.6
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
        ],
        mtca_db,
    )

    segs = segment_session(sid, path=mtca_db)
    assert len(segs) == 1
    s = segs[0]
    assert s["session_id"] == sid
    assert s["start_msg_seq"] == 1
    assert s["end_msg_seq"] == 3
    assert s["message_count"] == 3
    assert s["gap_to_next"] == 0

    # < MIN_SEGMENT_MSGS 应被过滤掉
    sid2 = create_session(path=mtca_db)
    _seed_messages(
        sid2,
        [
            (1, "user", "MTCA 项目第一条骨架", base),
            (2, "assistant", "MTCA 项目第二条骨架", base + 60_000),
        ],
        mtca_db,
    )
    assert segment_session(sid2, path=mtca_db) == []


# ---------------------------------------------------------------------------
# 测试 2：MAX_GAP_MINUTES 强制切分
# ---------------------------------------------------------------------------


def test_max_gap_splits(mtca_db: Path) -> None:
    """单个 gap > MAX_GAP_MINUTES → 切成 2 段。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    # 第一组：seq 1-3，MTCA 话题
    # gap = 90 分钟（> MAX_GAP_MINUTES=60）
    # 第二组：seq 4-6，关键词高度重叠
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 10 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 20 * 60_000),
            (4, "user", "天气下雨出行计划", base + 110 * 60_000),
            (5, "assistant", "天气下雨带伞防雨", base + 115 * 60_000),
            (6, "user", "天气下雨影响出行", base + 120 * 60_000),
        ],
        mtca_db,
    )

    segs = segment_session(sid, path=mtca_db)
    assert len(segs) == 2
    assert segs[0]["start_msg_seq"] == 1
    assert segs[0]["end_msg_seq"] == 3
    assert segs[0]["message_count"] == 3
    # gap_to_next = 110 - 20 = 90 min
    assert segs[0]["gap_to_next"] == 90
    assert segs[1]["start_msg_seq"] == 4
    assert segs[1]["end_msg_seq"] == 6
    assert segs[1]["gap_to_next"] == 0


# ---------------------------------------------------------------------------
# 测试 3：MIN_SEGMENT_MSGS 过滤短段
# ---------------------------------------------------------------------------


def test_min_segment_filter(mtca_db: Path) -> None:
    """中间出现 < MIN_SEGMENT_MSGS 短段 → 被过滤，前后长段保留。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    # 段 1：seq 1-3（MTCA 话题）
    # 90 min gap → 切
    # 段 2：seq 4（1 条，午餐杂问）→ 应被过滤
    # 90 min gap → 切
    # 段 3：seq 5-7（3 条，编程话题，关键词高度重叠）
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
            (4, "user", "中午随便问个问题", base + 100 * 60_000),
            (5, "user", "下午编写 pytest 代码", base + 190 * 60_000),
            (6, "assistant", "Python 编写 pytest 代码", base + 195 * 60_000),
            (7, "user", "pytest 编写测试代码", base + 200 * 60_000),
        ],
        mtca_db,
    )

    segs = segment_session(sid, path=mtca_db)
    assert len(segs) == 2
    # 第一段：seq 1-3
    assert segs[0]["start_msg_seq"] == 1
    assert segs[0]["end_msg_seq"] == 3
    assert segs[0]["message_count"] == 3
    # 第二段：seq 5-7（跳过 seq 4）
    assert segs[1]["start_msg_seq"] == 5
    assert segs[1]["end_msg_seq"] == 7
    assert segs[1]["message_count"] == 3
    # 过滤后 gap_to_next = 190 - 10 = 180 min（基于端到端时间戳）
    assert segs[0]["gap_to_next"] == 180
    assert segs[1]["gap_to_next"] == 0


# ---------------------------------------------------------------------------
# 测试 4：弱合并（关键词重叠 → 跨 gap 合并）
# ---------------------------------------------------------------------------


def test_weak_merge_with_keywords(mtca_db: Path) -> None:
    """高重叠关键词集 + gap 在 WEAK_MERGE_MAX_GAP 内 → 合并。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    # seq 1-3 + 50 min gap + seq 4-6，全部围绕 MTCA / 骨架 / 项目
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
            # 50 min gap（< MAX_GAP，且 < WEAK_MERGE_MAX_GAP=120）
            (4, "user", "MTCA 项目骨架时间分段", base + 60 * 60_000),
            (5, "assistant", "MTCA 项目骨架分段规则", base + 65 * 60_000),
            (6, "user", "MTCA 项目骨架继续讨论", base + 70 * 60_000),
        ],
        mtca_db,
    )

    segs = segment_session(sid, path=mtca_db)
    assert len(segs) == 1
    assert segs[0]["start_msg_seq"] == 1
    assert segs[0]["end_msg_seq"] == 6
    assert segs[0]["message_count"] == 6
    assert segs[0]["weak_merged"] is True
    assert segs[0]["gap_to_next"] == 0


# ---------------------------------------------------------------------------
# 测试 5：弱合并（关键词低重叠 → 不合并）
# ---------------------------------------------------------------------------


def test_weak_merge_rejects_low_overlap(mtca_db: Path) -> None:
    """两组关键词跨组不重叠 → 跨 gap 不合并,各成一段(段内各自合并)。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    # 第一组：MTCA 话题（内部高重叠 → 段内合并）
    # 第二组：午餐话题（内部高重叠 → 段内合并，但和 MTCA 零重叠 → 不跨 gap 合并）
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 10 * 60_000),
            # 30 min gap（远小于 MAX_GAP，也不夜段）
            (4, "user", "中午外卖午餐食谱", base + 40 * 60_000),
            (5, "assistant", "中午外卖清淡饮食", base + 45 * 60_000),
            (6, "user", "中午外卖牛肉面午餐", base + 50 * 60_000),
        ],
        mtca_db,
    )

    segs = segment_session(sid, path=mtca_db)
    assert len(segs) == 2
    # 第一段：seq 1-3
    assert segs[0]["start_msg_seq"] == 1
    assert segs[0]["end_msg_seq"] == 3
    assert segs[0]["message_count"] == 3
    # 第二段：seq 4-6（不与第一段跨 gap 合并）
    assert segs[1]["start_msg_seq"] == 4
    assert segs[1]["end_msg_seq"] == 6
    assert segs[1]["message_count"] == 3
    # gap_to_next = 40 - 10 = 30 min（端到端时间戳差）
    assert segs[0]["gap_to_next"] == 30
    assert segs[1]["gap_to_next"] == 0


# ---------------------------------------------------------------------------
# 测试 6：夜段合并（23:00–07:00 跨午夜 → 1 段）
# ---------------------------------------------------------------------------


def test_night_merge(mtca_db: Path) -> None:
    """is_night_merge + segment_session 跨夜窗口合并行为。"""
    # ---- 6a：is_night_merge 单元判定 ----
    t_2330_d4 = _ms(2026, 7, 4, 23, 30)
    t_0100_d5 = _ms(2026, 7, 5, 1, 0)
    t_0600_d5 = _ms(2026, 7, 5, 6, 0)
    t_1000_d5 = _ms(2026, 7, 5, 10, 0)
    t_2200_d4 = _ms(2026, 7, 4, 22, 0)

    # 夜段内：23:30 → 01:00（1.5h）
    assert is_night_merge(t_2330_d4, t_0100_d5) is True
    # 夜段内：23:30 → 06:00（6.5h，仍 ≤ 8h）
    assert is_night_merge(t_2330_d4, t_0600_d5) is True
    # 跨越白天 → 10:00 不在夜段 → False
    assert is_night_merge(t_2330_d4, t_1000_d5) is False
    # 22:00 不在夜段 → False（即便对端 23:30 在夜段）
    assert is_night_merge(t_2200_d4, t_2330_d4) is False
    # 间隔 > 8h → False
    t_far = _ms(2026, 7, 5, 8, 0)  # 23:30 → 08:00 next day = 8.5h
    assert is_night_merge(t_2330_d4, t_far) is False
    # 相等 → False
    assert is_night_merge(t_2330_d4, t_2330_d4) is False
    # 非法 → False
    assert is_night_merge(0, t_0100_d5) is False
    assert is_night_merge(t_2330_d4, -1) is False

    # ---- 6b：segment_session 跨夜合并 ----
    sid = create_session(path=mtca_db)
    _seed_messages(
        sid,
        [
            (1, "user", "深夜回忆 MTCA 项目", t_2330_d4),
            (2, "assistant", "MTCA 项目时间分段", t_2330_d4 + 30 * 60_000),
            (3, "user", "梦到 MTCA 项目灵感", t_0100_d5),
            (4, "assistant", "MTCA 项目记录下来", t_0100_d5 + 30 * 60_000),
            (5, "user", "早晨整理 MTCA 项目", t_0600_d5),
            (6, "assistant", "MTCA 项目继续推进", t_0600_d5 + 30 * 60_000),
        ],
        mtca_db,
    )

    segs = segment_session(sid, path=mtca_db)
    assert len(segs) == 1
    assert segs[0]["start_msg_seq"] == 1
    assert segs[0]["end_msg_seq"] == 6
    assert segs[0]["message_count"] == 6
    assert segs[0]["weak_merged"] is True  # 因夜段合并跨了 gap


# ---------------------------------------------------------------------------
# 测试 7：save_segments 落库（L0-B 写入 + 字段一致性）
# ---------------------------------------------------------------------------


def test_save_segments(mtca_db: Path) -> None:
    """save_segments 应把段落写入 segments 表，返回对应 segment_id 列表。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    _seed_messages(
        sid,
        [
            (1, "user", "MTCA 项目骨架设计", base),
            (2, "assistant", "MTCA 项目骨架生成", base + 5 * 60_000),
            (3, "user", "MTCA 项目骨架开发", base + 10 * 60_000),
            (4, "user", "天气下雨出行计划", base + 100 * 60_000),
            (5, "assistant", "天气下雨带伞防雨", base + 105 * 60_000),
            (6, "user", "天气下雨影响出行", base + 110 * 60_000),
        ],
        mtca_db,
    )

    segs = segment_session(sid, path=mtca_db)
    assert len(segs) == 2

    inserted_ids = save_segments(sid, segs, path=mtca_db)
    assert isinstance(inserted_ids, list)
    assert len(inserted_ids) == 2
    for sid_str in inserted_ids:
        assert isinstance(sid_str, str) and len(sid_str) >= 32

    # 落库校验
    rows = query(
        "SELECT segment_id, session_id, start_msg_seq, end_msg_seq, "
        "start_at, end_at, gap_to_next, weak_merged "
        "FROM segments WHERE session_id = ? ORDER BY start_msg_seq",
        (sid,), path=mtca_db,
    )
    assert len(rows) == 2
    # 第一段：seq 1-3，weak_merged=False
    assert rows[0]["segment_id"] == inserted_ids[0]
    assert rows[0]["start_msg_seq"] == 1
    assert rows[0]["end_msg_seq"] == 3
    assert rows[0]["gap_to_next"] == 90
    # 第二段：seq 4-6
    assert rows[1]["segment_id"] == inserted_ids[1]
    assert rows[1]["start_msg_seq"] == 4
    assert rows[1]["end_msg_seq"] == 6
    assert rows[1]["gap_to_next"] == 0

    # 字段缺失应抛 ValueError
    with pytest.raises(ValueError):
        save_segments(sid, [{"start_msg_seq": 1, "end_msg_seq": 2}], path=mtca_db)


# ---------------------------------------------------------------------------
# 测试 8：端到端 realistic 对话（3 段，关键词全不重叠）
# ---------------------------------------------------------------------------


def test_segment_realistic_conversation(mtca_db: Path) -> None:
    """3 个话题组（完全不重叠的关键词），用 90 分钟大 gap 切开 → 3 段。"""
    sid = create_session(path=mtca_db)
    base = _ms(2026, 7, 4, 10, 0)
    # 段 1：MTCA 开发（10:00-10:30）— 高重叠核心词
    # 段 2：午饭（11:30-11:50）— 外卖 / 午餐 关键词高重叠
    # 段 3：下午编程（13:50-14:10）— 代码 / Python 关键词高重叠
    _seed_messages(
        sid,
        [
            # 段 1：MTCA 项目讨论
            (1, "user", "MTCA 项目骨架设计开发", base),
            (2, "assistant", "MTCA 项目骨架生成开发", base + 10 * 60_000),
            (3, "user", "MTCA 项目骨架继续开发", base + 20 * 60_000),
            (4, "user", "MTCA 项目骨架完成开发", base + 30 * 60_000),
            # 段 2：午饭话题（11:30-11:50）
            (5, "user", "中午外卖午餐食谱", base + 120 * 60_000),
            (6, "assistant", "中午外卖清淡饮食", base + 130 * 60_000),
            (7, "user", "中午外卖牛肉面午餐", base + 140 * 60_000),
            # 段 3：下午编程（13:50-14:10）
            (8, "user", "下午编写 pytest 代码", base + 230 * 60_000),
            (9, "assistant", "Python 编写 pytest 代码", base + 240 * 60_000),
            (10, "user", "pytest 编写测试代码", base + 250 * 60_000),
        ],
        mtca_db,
    )

    segs = segment_session(sid, path=mtca_db)
    assert len(segs) == 3

    # 段 1：seq 1-4
    assert segs[0]["start_msg_seq"] == 1
    assert segs[0]["end_msg_seq"] == 4
    assert segs[0]["message_count"] == 4

    # 段 2：seq 5-7
    assert segs[1]["start_msg_seq"] == 5
    assert segs[1]["end_msg_seq"] == 7
    assert segs[1]["message_count"] == 3

    # 段 3：seq 8-10
    assert segs[2]["start_msg_seq"] == 8
    assert segs[2]["end_msg_seq"] == 10
    assert segs[2]["message_count"] == 3

    # gap_to_next：seq 4 @ 10:30 → seq 5 @ 12:00 = 90 min
    assert segs[0]["gap_to_next"] == 90
    # seq 7 @ 12:20 → seq 8 @ 13:50 = 90 min
    assert segs[1]["gap_to_next"] == 90
    assert segs[2]["gap_to_next"] == 0

    # 端到端 save_segments 验证
    ids = save_segments(sid, segs, path=mtca_db)
    assert len(ids) == 3
    rows = query(
        "SELECT COUNT(*) AS n FROM segments WHERE session_id = ?",
        (sid,), path=mtca_db,
    )
    assert rows[0]["n"] == 3