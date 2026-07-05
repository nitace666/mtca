"""src/lifecycle/ask_restore.py 测试（T21）

测试覆盖（用户 prompt 要求的 8 条 + 边界）：

1.  test_should_ask_silent                 silent + 无 last_ask_at + ref=0 → True
2.  test_should_not_ask_non_silent         active / dormant → False
3.  test_should_not_ask_7d_within          last_ask_at 在 7 天内 → False
4.  test_should_not_ask_long_silent        long_silent=1 → False
5.  test_should_not_ask_threshold_met      ref_count >= 阈值 → False
6.  test_make_ask_prompt_format            prompt 含 topic_label + fog_anchor
7.  test_handle_yes_promote                yes → ref_count++ + dormant + promoted_at
8.  test_handle_yes_threshold_to_active    yes ×3 → active
9.  test_handle_no_updates_ask_at          no → last_ask_at 更新；ref_count 不变
10. test_handle_skip_7d                    skip_7d → long_silent=1 + last_ask_at
11. test_handle_invalid_response           非法 response 抛 ValueError
12. test_constants_export                  PROMOTE_THRESHOLD / COOLDOWN 常量
13. test_response_case_insensitive         YES / No / SKIP_7D 大小写归一
14. test_missing_segment_raises            不存在段落应抛 ValueError
15. test_prompt_age_phrase_branches        time_phrase 5 月前 / 5 天前分支
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from src.lifecycle.ask_restore import (
    ASK_COOLDOWN_DAYS,
    ASK_COOLDOWN_MS,
    PROMOTE_THRESHOLD,
    VALID_RESPONSES,
    handle_user_response,
    make_ask_prompt,
    should_ask,
)
from src.l0.segment_writer import get_segment, update_segment
from src.l0.session_writer import create_session
from src.store.sqlite import execute, query


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _days_ago_ms(days: float) -> int:
    """N 天前的毫秒时间戳。"""
    return _now_ms() - int(days * 24 * 60 * 60 * 1000)


def _insert_segment(
    path: Path,
    silence_state: str = "silent",
    ref_count: int = 0,
    last_ask_at: int = 0,
    long_silent: int = 0,
    promoted_at: int = 0,
    topic_label: str = "MTCA 项目复盘",
    fog_anchor: str = "MTCA 项目复盘",
    start_days_ago: int = 90,
) -> str:
    """直接 INSERT 一条 segments 记录（绕过 L0-骨架触发器）。

    返回 segment_id。一次性设置所有需要的字段，避免后续 UPDATE 触发器冲突。
    """
    sid = create_session(path=path)
    seg_id = str(uuid.uuid4())
    start_at = _days_ago_ms(start_days_ago)
    end_at = start_at + 60_000

    execute(
        "INSERT INTO segments "
        "(segment_id, session_id, start_msg_seq, end_msg_seq, "
        "start_at, end_at, topic_label, fog_anchor, "
        "silence_state, ref_count, last_ask_at, promoted_at, long_silent) "
        "VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            seg_id, sid,
            start_at, end_at,
            topic_label, fog_anchor,
            silence_state, ref_count,
            last_ask_at,
            promoted_at if promoted_at > 0 else _days_ago_ms(60),
            long_silent,
        ),
        path=path,
    )
    return seg_id


# ---------------------------------------------------------------------------
# 测试 1：should_ask silent + 无冷却 → True
# ---------------------------------------------------------------------------


def test_should_ask_silent(mtca_db: Path) -> None:
    """silent 段 + last_ask_at=0 + ref_count=0 + long_silent=0 → True。"""
    seg_id = _insert_segment(
        mtca_db,
        silence_state="silent",
        ref_count=0,
        last_ask_at=0,
        long_silent=0,
    )

    assert should_ask(seg_id, path=mtca_db) is True


# ---------------------------------------------------------------------------
# 测试 2：非 silent 段不询问
# ---------------------------------------------------------------------------


def test_should_not_ask_non_silent(mtca_db: Path) -> None:
    """active / dormant 段 should_ask 必须返回 False。"""
    seg_active = _insert_segment(mtca_db, silence_state="active")
    seg_dormant = _insert_segment(mtca_db, silence_state="dormant")

    assert should_ask(seg_active, path=mtca_db) is False
    assert should_ask(seg_dormant, path=mtca_db) is False


# ---------------------------------------------------------------------------
# 测试 3：7 天内不重复问
# ---------------------------------------------------------------------------


def test_should_not_ask_7d_within(mtca_db: Path) -> None:
    """silent 段 last_ask_at = 3 天前 → should_ask=False。"""
    seg_id = _insert_segment(
        mtca_db,
        silence_state="silent",
        last_ask_at=_days_ago_ms(3),
    )

    assert should_ask(seg_id, path=mtca_db) is False

    # 8 天前 → 通过
    update_segment(seg_id, path=mtca_db, last_ask_at=_days_ago_ms(8))
    assert should_ask(seg_id, path=mtca_db) is True


# ---------------------------------------------------------------------------
# 测试 4：long_silent 跳过询问
# ---------------------------------------------------------------------------


def test_should_not_ask_long_silent(mtca_db: Path) -> None:
    """long_silent=1 的 silent 段不应被询问。"""
    seg_id = _insert_segment(mtca_db, silence_state="silent", long_silent=1)

    assert should_ask(seg_id, path=mtca_db) is False


# ---------------------------------------------------------------------------
# 测试 5：ref_count >= 阈值不询问
# ---------------------------------------------------------------------------


def test_should_not_ask_threshold_met(mtca_db: Path) -> None:
    """ref_count >= PROMOTE_THRESHOLD → 不再询问（应已升 active）。"""
    seg_id = _insert_segment(
        mtca_db,
        silence_state="silent",
        ref_count=PROMOTE_THRESHOLD,
    )

    assert should_ask(seg_id, path=mtca_db) is False

    # ref_count < 阈值 → 通过
    update_segment(seg_id, path=mtca_db, ref_count=PROMOTE_THRESHOLD - 1)
    assert should_ask(seg_id, path=mtca_db) is True


# ---------------------------------------------------------------------------
# 测试 6：prompt 格式
# ---------------------------------------------------------------------------


def test_make_ask_prompt_format(mtca_db: Path) -> None:
    """make_ask_prompt 返回含 topic_label + fog_anchor 的中文 prompt。"""
    seg_id = _insert_segment(
        mtca_db,
        topic_label="MTCA 项目复盘",
        fog_anchor="讨论 T20 升级路径",
        start_days_ago=90,
    )

    prompt = make_ask_prompt(seg_id, path=mtca_db)

    # 必含字段
    assert "MTCA 项目复盘" in prompt
    assert "讨论 T20 升级路径" in prompt
    assert "当前话题相关吗" in prompt
    # 时间短语（90 天 ≈ 3 个月）
    assert "个月前" in prompt

    # 字段缺失时的降级：直接覆盖 topic_label/fog_anchor 会触发器
    # 但 INSERT 时是 NULL → 直接走 INSERT 路径
    seg_id2 = _insert_segment(
        mtca_db,
        topic_label="",  # 空字符串视为缺失
        fog_anchor="",
    )
    prompt2 = make_ask_prompt(seg_id2, path=mtca_db)
    assert "某个话题" in prompt2
    assert "已雾化" in prompt2


# ---------------------------------------------------------------------------
# 测试 7：handle 'yes' → ref_count++ + promote dormant
# ---------------------------------------------------------------------------


def test_handle_yes_promote(mtca_db: Path) -> None:
    """'yes' 响应：ref_count=0→1，silence_state silent→dormant，promoted_at 刷新。"""
    seg_id = _insert_segment(
        mtca_db,
        silence_state="silent",
        ref_count=0,
    )
    fixed_now = _now_ms()

    result = handle_user_response(
        seg_id, "yes", path=mtca_db, now_ms=fixed_now,
    )

    assert result["response"] == "yes"
    assert result["ref_count"] == 1
    assert result["silence_state"] == "dormant"
    assert result["promoted_to_active"] is False

    # DB 验证
    row = get_segment(seg_id, path=mtca_db)
    assert int(row["ref_count"]) == 1
    assert row["silence_state"] == "dormant"
    assert int(row["promoted_at"]) == fixed_now

    # 审计
    events = query(
        "SELECT event_type, reason FROM score_events "
        "WHERE segment_id = ? AND event_type = 'ask_yes'",
        (seg_id,),
        path=mtca_db,
    )
    assert len(events) == 1
    assert "ref_count 0->1" in events[0]["reason"]


# ---------------------------------------------------------------------------
# 测试 8：handle 'yes' ×3 → active
# ---------------------------------------------------------------------------


def test_handle_yes_threshold_to_active(mtca_db: Path) -> None:
    """连续 3 次 'yes'：第 3 次把 silence_state 升级到 active。"""
    seg_id = _insert_segment(mtca_db, silence_state="silent", ref_count=0)
    fixed_now = _now_ms()

    # 第 1 次 yes
    r1 = handle_user_response(
        seg_id, "yes", path=mtca_db, now_ms=fixed_now,
    )
    assert r1["ref_count"] == 1
    assert r1["silence_state"] == "dormant"

    # 第 2 次 yes
    r2 = handle_user_response(
        seg_id, "yes", path=mtca_db, now_ms=fixed_now + 1000,
    )
    assert r2["ref_count"] == 2
    assert r2["silence_state"] == "dormant"

    # 第 3 次 yes → 触发阈值
    r3 = handle_user_response(
        seg_id, "yes", path=mtca_db, now_ms=fixed_now + 2000,
    )
    assert r3["ref_count"] == 3
    assert r3["silence_state"] == "active"
    assert r3["promoted_to_active"] is True

    # DB 验证 + 不再询问
    row = get_segment(seg_id, path=mtca_db)
    assert int(row["ref_count"]) == 3
    assert row["silence_state"] == "active"
    assert should_ask(seg_id, path=mtca_db) is False


# ---------------------------------------------------------------------------
# 测试 9：handle 'no' → last_ask_at 更新，ref_count 不变
# ---------------------------------------------------------------------------


def test_handle_no_updates_ask_at(mtca_db: Path) -> None:
    """'no' 响应：last_ask_at 更新为 now；ref_count / silence_state 不变。"""
    seg_id = _insert_segment(mtca_db, silence_state="silent", ref_count=1)
    fixed_now = _now_ms()

    result = handle_user_response(
        seg_id, "no", path=mtca_db, now_ms=fixed_now,
    )

    assert result["response"] == "no"
    assert result["ref_count"] == 1
    assert result["silence_state"] == "silent"
    assert result["last_ask_at"] == fixed_now

    # DB
    row = get_segment(seg_id, path=mtca_db)
    assert int(row["last_ask_at"]) == fixed_now
    assert int(row["ref_count"]) == 1
    assert row["silence_state"] == "silent"

    # 7 天内不再询问
    assert should_ask(seg_id, path=mtca_db) is False

    # 审计
    events = query(
        "SELECT event_type FROM score_events "
        "WHERE segment_id = ? AND event_type = 'ask_no'",
        (seg_id,),
        path=mtca_db,
    )
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 测试 10：handle 'skip_7d' → long_silent = 1
# ---------------------------------------------------------------------------


def test_handle_skip_7d(mtca_db: Path) -> None:
    """'skip_7d' 响应：long_silent=1，last_ask_at=now；ref_count 不变。"""
    seg_id = _insert_segment(
        mtca_db,
        silence_state="silent",
        ref_count=2,
        long_silent=0,
    )
    fixed_now = _now_ms()

    result = handle_user_response(
        seg_id, "skip_7d", path=mtca_db, now_ms=fixed_now,
    )

    assert result["response"] == "skip_7d"
    assert result["long_silent"] == 1
    assert result["last_ask_at"] == fixed_now
    assert result["ref_count"] == 2
    assert result["silence_state"] == "silent"

    # DB
    row = get_segment(seg_id, path=mtca_db)
    assert int(row["long_silent"]) == 1
    assert int(row["last_ask_at"]) == fixed_now

    # 立即不再询问（long_silent=1）
    assert should_ask(seg_id, path=mtca_db) is False

    # 审计
    events = query(
        "SELECT event_type, reason FROM score_events "
        "WHERE segment_id = ? AND event_type = 'ask_skip'",
        (seg_id,),
        path=mtca_db,
    )
    assert len(events) == 1
    assert "long_silent=1" in events[0]["reason"]


# ---------------------------------------------------------------------------
# 测试 11：非法 response 抛 ValueError
# ---------------------------------------------------------------------------


def test_handle_invalid_response(mtca_db: Path) -> None:
    """非法 response 值应抛 ValueError，不写入任何字段。"""
    seg_id = _insert_segment(mtca_db)

    with pytest.raises(ValueError):
        handle_user_response(seg_id, "maybe", path=mtca_db)

    with pytest.raises(ValueError):
        handle_user_response(seg_id, "", path=mtca_db)

    with pytest.raises(ValueError):
        handle_user_response(seg_id, None, path=mtca_db)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        handle_user_response("", "yes", path=mtca_db)


# ---------------------------------------------------------------------------
# 测试 12：常量导出
# ---------------------------------------------------------------------------


def test_constants_export() -> None:
    """PROMOTE_THRESHOLD=3, ASK_COOLDOWN_DAYS=7 与 V0.4_PIVOT.md §3.6 对齐。"""
    assert PROMOTE_THRESHOLD == 3
    assert ASK_COOLDOWN_DAYS == 7
    assert ASK_COOLDOWN_MS == 7 * 24 * 60 * 60 * 1000
    assert set(VALID_RESPONSES) == {"yes", "no", "skip_7d"}


# ---------------------------------------------------------------------------
# 测试 13：response 大小写 + 空格不敏感
# ---------------------------------------------------------------------------


def test_handle_response_case_insensitive(mtca_db: Path) -> None:
    """'YES' / ' No ' / 'SKIP_7D' 大小写 + 首尾空格归一为 'yes' / 'no' / 'skip_7d'。"""
    seg_id = _insert_segment(mtca_db, silence_state="silent", ref_count=0)

    r1 = handle_user_response(seg_id, "YES", path=mtca_db)
    assert r1["response"] == "yes"
    assert r1["ref_count"] == 1

    r2 = handle_user_response(seg_id, " No ", path=mtca_db)
    assert r2["response"] == "no"

    r3 = handle_user_response(seg_id, "SKIP_7D", path=mtca_db)
    assert r3["response"] == "skip_7d"
    assert r3["long_silent"] == 1


# ---------------------------------------------------------------------------
# 测试 14：不存在的段落抛 ValueError
# ---------------------------------------------------------------------------


def test_missing_segment_raises(mtca_db: Path) -> None:
    """不存在的 segment_id 在 should_ask / make_ask_prompt / handle 中都抛 ValueError。"""
    fake = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ValueError):
        should_ask(fake, path=mtca_db)
    with pytest.raises(ValueError):
        make_ask_prompt(fake, path=mtca_db)
    with pytest.raises(ValueError):
        handle_user_response(fake, "yes", path=mtca_db)


# ---------------------------------------------------------------------------
# 测试 15：prompt 时间短语分支
# ---------------------------------------------------------------------------


def test_prompt_age_phrase_branches(mtca_db: Path) -> None:
    """time_phrase 覆盖「5 个月前」/「5 天前」/「曾经」三种。"""
    # 5 个月前
    seg_far = _insert_segment(
        mtca_db, topic_label="远期话题", fog_anchor="远锚点",
        start_days_ago=5 * 30,
    )
    prompt_far = make_ask_prompt(seg_far, path=mtca_db)
    assert "5 个月前" in prompt_far

    # 5 天前（< 1 月）→ "5 天前"
    seg_near = _insert_segment(
        mtca_db, topic_label="近期话题", fog_anchor="近锚点",
        start_days_ago=5,
    )
    prompt_near = make_ask_prompt(seg_near, path=mtca_db)
    assert "天前" in prompt_near
    assert "个月前" not in prompt_near

    # start_at=0 → "曾经"
    sid = create_session(path=mtca_db)
    seg_zero_id = str(uuid.uuid4())
    execute(
        "INSERT INTO segments "
        "(segment_id, session_id, start_msg_seq, end_msg_seq, "
        "start_at, end_at, topic_label, fog_anchor, silence_state) "
        "VALUES (?, ?, 1, 1, 0, 0, '零起点', '零锚点', 'silent')",
        (seg_zero_id, sid),
        path=mtca_db,
    )
    prompt_zero = make_ask_prompt(seg_zero_id, path=mtca_db)
    assert "曾经" in prompt_zero