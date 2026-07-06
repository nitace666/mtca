"""tests/test_urgent_tracker.py — M2.5.3 urgent_tracker 状态机测试

覆盖：
- track_urgent：写 urgent_state='tracking' / expires_at_ms / urgency_level
- tick_urgent：扫描过期 → 转 'expired'
- handle_urgent_response：completed / postponed / important 三种响应
- list_expired：列当前所有 'expired' 段
- 集成：scoring.tick() 末尾自动跑 tick_urgent()
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from src.store.sqlite import execute, init_db
from src.l0.segment_writer import get_segment
from src.lifecycle.urgent_tracker import (
    handle_urgent_response,
    list_expired,
    tick_urgent,
    track_urgent,
)


def _seed_session(session_id: str, started_at: int, path: Path) -> None:
    execute(
        "INSERT INTO sessions(session_id, started_at, agent_source) "
        "VALUES(?, ?, ?)",
        (session_id, started_at, "test"),
        path=path,
    )


def _seed_segment(
    session_id: str,
    topic: str,
    start_at: int,
    *,
    current_tier: str,
    current_score: float,
    urgency_level: float,
    path: Path,
) -> str:
    sid = str(uuid.uuid4())
    execute(
        "INSERT INTO segments "
        "(segment_id, session_id, topic_label, start_msg_seq, start_at, end_msg_seq, end_at, "
        " current_tier, current_score, urgency_level) "
        "VALUES(?, ?, ?, 1, ?, 1, ?, ?, ?, ?)",
        (sid, session_id, topic, start_at, start_at,
         current_tier, current_score, urgency_level),
        path=path,
    )
    return sid


@pytest.fixture
def db_with_segments(tmp_path: Path):
    db = tmp_path / "urgent_test.db"
    init_db(db).close()
    now = int(time.time() * 1000)
    sids = []
    for i in range(3):
        sess_id = f"sess-{i}-{uuid.uuid4().hex[:8]}"
        _seed_session(sess_id, now, db)
        sid = _seed_segment(
            sess_id,
            topic=f"测试段{i}",
            start_at=now,
            current_tier="L1",
            current_score=100.0,
            urgency_level=0.5,
            path=db,
        )
        sids.append(sid)
    return db, sids


def test_track_urgent_sets_state_expiry_and_urgency(db_with_segments):
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    expires = now + 3600 * 1000
    result = track_urgent(segs[0], expires_at_ms=expires, path=db)
    assert result["segment_id"] == segs[0]
    assert result["urgent_state"] == "tracking"
    assert result["expires_at_ms"] == expires
    assert result["urgency_level"] == 1.0
    seg = get_segment(segs[0], path=db)
    assert seg["urgent_state"] == "tracking"
    assert seg["expires_at_ms"] == expires
    assert float(seg["urgency_level"]) == 1.0


def test_track_urgent_preserves_segment_existence(db_with_segments):
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now + 1000, path=db)
    seg = get_segment(segs[0], path=db)
    assert seg is not None
    assert seg["segment_id"] == segs[0]
    assert seg["topic_label"] == "测试段0"


def test_tick_urgent_moves_only_expired_to_expired_state(db_with_segments):
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now - 1000, path=db)
    track_urgent(segs[1], expires_at_ms=now + 3600 * 1000, path=db)
    expired_ids = tick_urgent(path=db, now_ms=now)
    assert expired_ids == [segs[0]]
    seg0 = get_segment(segs[0], path=db)
    seg1 = get_segment(segs[1], path=db)
    assert seg0["urgent_state"] == "expired"
    assert seg1["urgent_state"] == "tracking"


def test_tick_urgent_returns_empty_when_nothing_expired(db_with_segments):
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now + 3600 * 1000, path=db)
    assert tick_urgent(path=db, now_ms=now) == []


def test_tick_urgent_skips_non_tracking_segments(tmp_path):
    """expired/completed/NULL urgent_state 的段 tick_urgent 不动。"""
    db = tmp_path / "t_skip.db"
    init_db(db).close()
    now = int(time.time() * 1000)
    # 1) 直接 INSERT 段，expires_at_ms 设但 urgent_state 不动
    sess = f"sess-skip-{uuid.uuid4().hex[:8]}"
    _seed_session(sess, now, db)
    sid_null = _seed_segment(
        sess, "NULL 状态", now,
        current_tier="L1", current_score=100.0, urgency_level=0.5, path=db,
    )
    execute(
        "UPDATE segments SET expires_at_ms = ? WHERE segment_id = ?",
        (now - 1000, sid_null), path=db,
    )
    expired = tick_urgent(path=db, now_ms=now)
    assert sid_null not in expired


def test_handle_urgent_response_completed_to_l2(db_with_segments):
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now - 1000, path=db)
    tick_urgent(path=db, now_ms=now)
    result = handle_urgent_response(segs[0], "completed", path=db)
    assert result["new_state"] == "completed"
    assert result["tier"] == "L2"
    seg = get_segment(segs[0], path=db)
    assert seg["urgent_state"] == "completed"
    assert seg["current_tier"] == "L2"


def test_handle_urgent_response_postponed_extends_7d(db_with_segments):
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now - 1000, path=db)
    tick_urgent(path=db, now_ms=now)
    result = handle_urgent_response(
        segs[0], "postponed", path=db, now_ms=now,
    )
    assert result["new_state"] == "tracking"
    assert "expires_at_ms" in result
    delta_ms = result["expires_at_ms"] - now
    assert 6.5 * 86400 * 1000 < delta_ms < 7.5 * 86400 * 1000
    seg = get_segment(segs[0], path=db)
    assert seg["urgent_state"] == "tracking"
    assert abs(seg["expires_at_ms"] - result["expires_at_ms"]) < 1


def test_handle_urgent_response_important_locks_score_10000(db_with_segments):
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now - 1000, path=db)
    tick_urgent(path=db, now_ms=now)
    result = handle_urgent_response(segs[0], "important", path=db)
    assert result["new_state"] == "important"
    seg = get_segment(segs[0], path=db)
    assert seg["urgent_state"] == "important"
    assert float(seg["current_score"]) == 10000.0


def test_handle_urgent_response_invalid_response_raises(db_with_segments):
    db, segs = db_with_segments
    with pytest.raises(ValueError):
        handle_urgent_response(segs[0], "garbage", path=db)
    with pytest.raises(ValueError):
        handle_urgent_response(segs[0], "Completed".upper(), path=db)


def test_handle_urgent_response_only_on_expired_or_tracking(db_with_segments):
    db, segs = db_with_segments
    with pytest.raises(ValueError):
        handle_urgent_response(segs[1], "completed", path=db)


def test_handle_urgent_response_not_found_raises(db_with_segments):
    db, _ = db_with_segments
    with pytest.raises(ValueError):
        handle_urgent_response("seg-not-exist-xxx", "completed", path=db)


def test_list_expired_returns_only_expired_segments(db_with_segments):
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now - 1000, path=db)
    track_urgent(segs[1], expires_at_ms=now + 3600 * 1000, path=db)
    tick_urgent(path=db, now_ms=now)
    handle_urgent_response(segs[0], "completed", path=db)
    rows = list_expired(path=db)
    sids = {r["segment_id"] for r in rows}
    assert segs[0] not in sids
    assert segs[1] not in sids
    assert segs[2] not in sids
    track_urgent(segs[0], expires_at_ms=now - 500, path=db)
    tick_urgent(path=db, now_ms=now)
    rows = list_expired(path=db)
    sids = {r["segment_id"] for r in rows}
    assert segs[0] in sids


def test_scoring_tick_invokes_urgent_ticker(monkeypatch, db_with_segments):
    """scoring.tick() 调用时必须触发 tick_urgent。"""
    from src.compress import scoring as scoring_mod
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now - 1000, path=db)
    called = {"n": 0}
    real_tick_urgent = tick_urgent
    def spy_tick_urgent(*, path=None, now_ms=None):
        called["n"] += 1
        return real_tick_urgent(path=path, now_ms=now_ms)
    monkeypatch.setattr(scoring_mod, "tick_urgent", spy_tick_urgent)
    try:
        scoring_mod.tick(path=db, now_ms=now)
    except TypeError:
        scoring_mod.tick(path=db)
    assert called["n"] == 1
    seg0 = get_segment(segs[0], path=db)
    assert seg0["urgent_state"] == "expired"


def test_scoring_tick_returns_int_count(db_with_segments):
    """scoring.tick() 返回值 = n_scored + len(n_expired)。"""
    db, segs = db_with_segments
    now = int(time.time() * 1000)
    track_urgent(segs[0], expires_at_ms=now - 1000, path=db)
    track_urgent(segs[1], expires_at_ms=now + 3600 * 1000, path=db)
    tick_mod = __import__("src.compress.scoring", fromlist=["tick"])
    try:
        n = tick_mod.tick(path=db, now_ms=now)
    except TypeError:
        n = tick_mod.tick(path=db)
    assert isinstance(n, int)
    assert n >= 1
    seg0 = get_segment(segs[0], path=db)
    assert seg0["urgent_state"] == "expired"
