"""src/compress/regen_engine.py 测试（T24 + T27）

测试覆盖（用户 prompt 列出的 7 项 + 边界 + T27 补全）：

1.  test_schedule_marks_stale             schedule_regen 标 is_stale=1 + 入队
2.  test_regen_view_writes_new            regen_view 写新 view，regen_count+1
3.  test_regen_view_expires_old           旧 view expires_at 被设置
4.  test_regen_failure_keeps_old          provider 抛错后旧 view 未变
5.  test_process_queue_drains             process_queue 清空队列
6.  test_manual_refresh                   manual_refresh 同步完成
7.  test_reason_affects_prompt            不同 reason → prompt 含不同 reason
8.  test_schedule_validates               schedule 参数校验
9.  test_regen_view_validates             regen 参数校验
10. test_default_max_retries_is_three     常量
11. test_regen_view_retries_then_succeeds 重试后成功
-- T27 补全 --
12. test_reason_path_fog                  reason='fog' 路径
13. test_reason_path_supersede            reason='supersede' 路径
14. test_reason_path_promote              reason='promote' 路径
15. test_reason_path_manual               reason='manual' 路径
16. test_concurrent_enqueue               多线程并发入队
17. test_retry_three_times_then_give_up   max_retries=3 全失败后放弃
18. test_failure_old_view_intact          失败后旧 view 完整保留
19. test_regen_count_accumulates          regen_count 多次累加
20. test_new_view_clears_is_stale         新 view is_stale=0
21. test_regen_nonexistent_view_id        view_id 不存在 → ValueError
22. test_background_worker_drains_queue   后台 worker 处理队列
23. test_stop_worker_without_start        无线程时 stop 返回 True
24. test_process_queue_empty_returns_zero 空队列 process_queue 返回 0
25. test_schedule_skips_expired_views     过期 view 不入队
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from src.compress.regen_engine import (
    DEFAULT_MAX_RETRIES,
    VALID_REASONS,
    get_queue_size,
    manual_refresh,
    process_queue,
    regen_view,
    reset_for_tests,
    schedule_regen,
    start_background_worker,
    stop_background_worker,
)
from src.l0.session_writer import create_session
from src.store.sqlite import execute, query


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _insert_segment(
    path: Path,
    topic_label: str = "漫剧方案",
    fog_anchor: str = "漫剧方案 制作 计划",
    start_at_ms: int | None = None,
) -> str:
    """直接 INSERT 一条 segments 记录 + 关联 session。"""
    sid = create_session(path=path)
    seg_id = str(uuid.uuid4())
    start_at = (
        int(start_at_ms) if start_at_ms is not None
        else _now_ms() - 60 * 1000
    )
    end_at = start_at + 60_000
    execute(
        "INSERT INTO segments "
        "(segment_id, session_id, start_msg_seq, end_msg_seq, "
        "start_at, end_at, topic_label, fog_anchor, silence_state) "
        "VALUES (?, ?, 1, 1, ?, ?, ?, ?, 'active')",
        (seg_id, sid, start_at, end_at, topic_label, fog_anchor),
        path=path,
    )
    return seg_id


def _insert_view(
    path: Path,
    segment_id: str,
    tier: str = "L1",
    content: str = "旧摘要内容",
    is_stale: int = 0,
    stale_reason: str | None = None,
    regen_count: int = 0,
) -> str:
    """直接 INSERT 一条 views 记录。"""
    view_id = str(uuid.uuid4())
    execute(
        "INSERT INTO views "
        "(view_id, segment_id, tier, content, created_at, "
        "expires_at, is_stale, stale_reason, regen_count) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        (
            view_id, segment_id, tier, content, _now_ms(),
            int(is_stale), stale_reason, int(regen_count),
        ),
        path=path,
    )
    return view_id


class _MockProvider:
    """可配置 mock LLM provider：捕获 prompt / 控制成功失败。"""

    def __init__(
        self,
        response: str = "new summarized content",
        fail_until_attempt: int = 0,
        fail_message: str = "mock error",
    ) -> None:
        self.response = response
        self.fail_until_attempt = int(fail_until_attempt)
        self.fail_message = fail_message
        self.captured: list[str] = []
        self.calls: int = 0

    def generate(self, prompt: str, **kwargs: Any) -> str:
        self.calls += 1
        self.captured.append(prompt)
        if self.calls <= self.fail_until_attempt:
            raise RuntimeError(self.fail_message)
        return self.response


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """每个测试前后清空队列 + 停止 worker，避免跨测试污染。"""
    reset_for_tests()
    yield
    reset_for_tests()


# ---------------------------------------------------------------------------
# 测试 1：schedule_regen 标 stale + 入队
# ---------------------------------------------------------------------------


def test_schedule_marks_stale(mtca_db: Path) -> None:
    seg_id = _insert_segment(mtca_db)
    _insert_view(mtca_db, seg_id, tier="L1", is_stale=0)
    _insert_view(mtca_db, seg_id, tier="L2", is_stale=0)
    _insert_view(mtca_db, seg_id, tier="L1", is_stale=0)

    # 入队 3 个视图
    n = schedule_regen(seg_id, reason="fog", path=mtca_db)
    assert n == 3

    # 全部标 stale + 原因写入
    rows = query(
        "SELECT view_id, is_stale, stale_reason FROM views "
        "WHERE segment_id = ? ORDER BY created_at DESC",
        (seg_id,),
        path=mtca_db,
    )
    assert len(rows) == 3
    for r in rows:
        assert int(r["is_stale"]) == 1
        assert r["stale_reason"] == "fog"

    # 队列应有 3 项
    assert get_queue_size() == 3


# ---------------------------------------------------------------------------
# 测试 2：regen_view 写新 view（regen_count += 1）
# ---------------------------------------------------------------------------


def test_regen_view_writes_new(mtca_db: Path) -> None:
    seg_id = _insert_segment(mtca_db)
    old_view_id = _insert_view(
        mtca_db, seg_id, tier="L1",
        content="old summary",
        is_stale=1, stale_reason="fog", regen_count=2,
    )

    provider = _MockProvider(response="brand new summary")
    result = regen_view(
        old_view_id, reason="fog", provider=provider, path=mtca_db,
    )

    # 返回 dict 结构
    assert result["view_id"] == old_view_id
    assert result["tier"] == "L1"
    assert result["regen_count"] == 3  # 2 + 1
    assert result["reason"] == "fog"
    assert result["status"] == "ok"
    assert result["new_view_id"] and result["new_view_id"] != old_view_id

    # DB：新行存在且内容为新摘要
    new_rows = query(
        "SELECT * FROM views WHERE view_id = ?",
        (result["new_view_id"],),
        path=mtca_db,
    )
    assert len(new_rows) == 1
    new_row = new_rows[0]
    assert new_row["content"] == "brand new summary"
    assert int(new_row["regen_count"]) == 3
    assert int(new_row["is_stale"]) == 0
    assert new_row["stale_reason"] is None
    assert new_row["expires_at"] is None
    assert new_row["segment_id"] == seg_id
    assert new_row["tier"] == "L1"

    # 调用 1 次
    assert provider.calls == 1


# ---------------------------------------------------------------------------
# 测试 3：旧 view expires_at 被设为 now
# ---------------------------------------------------------------------------


def test_regen_view_expires_old(mtca_db: Path) -> None:
    seg_id = _insert_segment(mtca_db)
    old_view_id = _insert_view(
        mtca_db, seg_id, tier="L1",
        content="old", is_stale=1, regen_count=0,
    )

    before_ms = _now_ms()
    provider = _MockProvider(response="new")
    regen_view(old_view_id, reason="manual", provider=provider, path=mtca_db)
    after_ms = _now_ms()

    old_rows = query(
        "SELECT * FROM views WHERE view_id = ?",
        (old_view_id,),
        path=mtca_db,
    )
    assert len(old_rows) == 1
    old_row = old_rows[0]
    assert old_row["expires_at"] is not None
    expires_at = int(old_row["expires_at"])
    # expires_at 应在 before/after 之间
    assert before_ms <= expires_at <= after_ms + 100
    # 内容 / is_stale 保留不动
    assert old_row["content"] == "old"
    assert int(old_row["regen_count"]) == 0


# ---------------------------------------------------------------------------
# 测试 4：失败保留旧 view
# ---------------------------------------------------------------------------


def test_regen_failure_keeps_old(mtca_db: Path) -> None:
    seg_id = _insert_segment(mtca_db)
    old_view_id = _insert_view(
        mtca_db, seg_id, tier="L1",
        content="keep me", is_stale=1, regen_count=5,
    )

    # max_retries=0 → 第 1 次失败立即抛
    provider = _MockProvider(fail_until_attempt=999)
    with pytest.raises(RuntimeError):
        regen_view(
            old_view_id, reason="fog",
            provider=provider, path=mtca_db,
            max_retries=0,
        )

    # 旧 view 未变：expires_at 仍是 NULL、content 未变、regen_count 未变
    rows = query(
        "SELECT * FROM views WHERE view_id = ?",
        (old_view_id,),
        path=mtca_db,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["content"] == "keep me"
    assert row["expires_at"] is None
    assert int(row["regen_count"]) == 5

    # 没产生新 view
    n_views = query(
        "SELECT COUNT(*) AS n FROM views WHERE segment_id = ?",
        (seg_id,),
        path=mtca_db,
    )
    assert int(n_views[0]["n"]) == 1

    # 但调用了 1 次
    assert provider.calls == 1


# ---------------------------------------------------------------------------
# 测试 5：process_queue 清空队列
# ---------------------------------------------------------------------------


def test_process_queue_drains(mtca_db: Path) -> None:
    seg_id = _insert_segment(mtca_db)
    v1 = _insert_view(mtca_db, seg_id, tier="L1")
    v2 = _insert_view(mtca_db, seg_id, tier="L2")
    v3 = _insert_view(mtca_db, seg_id, tier="L3")

    # schedule_regen 同时入队
    n_sched = schedule_regen(seg_id, reason="manual", path=mtca_db)
    assert n_sched == 3
    assert get_queue_size() == 3

    # process_queue 处理
    provider = _MockProvider(response="queue-new")
    n_done = process_queue(provider=provider, path=mtca_db)
    assert n_done == 3
    assert get_queue_size() == 0

    # 3 个旧 view 均已 expire
    old_rows = query(
        "SELECT view_id, expires_at FROM views "
        "WHERE view_id IN (?, ?, ?)",
        (v1, v2, v3),
        path=mtca_db,
    )
    for r in old_rows:
        assert r["expires_at"] is not None

    # 3 个新 view 已生成（regen_count=1）
    new_rows = query(
        "SELECT view_id, regen_count, expires_at, content "
        "FROM views WHERE segment_id = ? "
        "AND expires_at IS NULL",
        (seg_id,),
        path=mtca_db,
    )
    assert len(new_rows) == 3
    for r in new_rows:
        assert int(r["regen_count"]) == 1
        assert r["content"] == "queue-new"


# ---------------------------------------------------------------------------
# 测试 6：manual_refresh 同步完成
# ---------------------------------------------------------------------------


def test_manual_refresh(mtca_db: Path) -> None:
    seg_id = _insert_segment(mtca_db)
    v1 = _insert_view(mtca_db, seg_id, tier="L1")
    v2 = _insert_view(mtca_db, seg_id, tier="L2")

    provider = _MockProvider(response="manual-new")
    n = manual_refresh(seg_id, provider=provider, path=mtca_db)
    assert n == 2
    assert get_queue_size() == 0

    # 旧 view 全 expire
    old_rows = query(
        "SELECT view_id, expires_at FROM views "
        "WHERE view_id IN (?, ?)",
        (v1, v2),
        path=mtca_db,
    )
    assert len(old_rows) == 2
    for r in old_rows:
        assert r["expires_at"] is not None

    # 新 view 内容 = "manual-new"
    new_rows = query(
        "SELECT content FROM views WHERE segment_id = ? "
        "AND expires_at IS NULL",
        (seg_id,),
        path=mtca_db,
    )
    assert len(new_rows) == 2
    for r in new_rows:
        assert r["content"] == "manual-new"


# ---------------------------------------------------------------------------
# 测试 7：reason 改变 prompt
# ---------------------------------------------------------------------------


def test_reason_affects_prompt(mtca_db: Path) -> None:
    seg_id = _insert_segment(
        mtca_db,
        topic_label="漫剧方案",
        fog_anchor="漫剧 制作 计划",
    )

    provider = _MockProvider(response="reason-new")
    # 4 个 reason 依次调用
    view_ids = []
    for reason in VALID_REASONS:
        vid = _insert_view(
            mtca_db, seg_id, tier="L1",
            content=f"old for {reason}",
            is_stale=1, stale_reason=reason,
        )
        view_ids.append(vid)
        regen_view(vid, reason=reason, provider=provider, path=mtca_db)

    assert provider.calls == len(VALID_REASONS)
    assert len(provider.captured) == len(VALID_REASONS)

    # 每个 prompt 必须包含对应 reason 标签
    for i, reason in enumerate(VALID_REASONS):
        prompt = provider.captured[i]
        assert f"[重写原因] {reason}" in prompt, (
            f"prompt {i} 缺少 [重写原因] {reason}"
        )
        # 还可以验证 reason_hint 区分
        if reason == "fog":
            assert "物理擦除" in prompt
        if reason == "supersede":
            assert "取代" in prompt

    # 同时验证 L0-骨架被填入
    last_prompt = provider.captured[-1]
    assert "漫剧方案" in last_prompt or "漫剧" in last_prompt


# ---------------------------------------------------------------------------
# 测试 8（边界）：校验抛错
# ---------------------------------------------------------------------------


def test_schedule_validates(mtca_db: Path) -> None:
    with pytest.raises(ValueError):
        schedule_regen("", reason="manual", path=mtca_db)
    with pytest.raises(ValueError):
        schedule_regen("nonexistent", reason="manual", path=mtca_db)
    with pytest.raises(ValueError):
        schedule_regen(_insert_segment(mtca_db), reason="weird", path=mtca_db)


def test_regen_view_validates(mtca_db: Path) -> None:
    seg_id = _insert_segment(mtca_db)
    vid = _insert_view(mtca_db, seg_id, is_stale=1)
    provider = _MockProvider()

    with pytest.raises(ValueError):
        regen_view("", path=mtca_db)
    with pytest.raises(ValueError):
        regen_view(vid, reason="bad", provider=provider, path=mtca_db)
    with pytest.raises(ValueError):
        regen_view("nope", provider=provider, path=mtca_db)


def test_default_max_retries_is_three() -> None:
    assert DEFAULT_MAX_RETRIES == 3
    assert VALID_REASONS == ("fog", "supersede", "promote", "manual")


def test_regen_view_retries_then_succeeds(mtca_db: Path) -> None:
    """provider 前 2 次抛错，第 3 次成功；旧 view 仍被 expire。"""
    seg_id = _insert_segment(mtca_db)
    old_view_id = _insert_view(
        mtca_db, seg_id, tier="L1",
        content="retry-me", is_stale=1, regen_count=0,
    )
    # fail_until_attempt=2：前 2 次失败，第 3 次（calls=3）成功
    provider = _MockProvider(
        response="finally-new", fail_until_attempt=2,
    )
    result = regen_view(
        old_view_id, reason="fog",
        provider=provider, path=mtca_db,
        max_retries=3,  # 1+3=4 次尝试容差
    )
    assert result["status"] == "ok"
    assert result["regen_count"] == 1
    assert provider.calls == 3  # 失败 2 次 + 成功 1 次

    # 旧 view 已 expire
    rows = query(
        "SELECT * FROM views WHERE view_id = ?",
        (old_view_id,),
        path=mtca_db,
    )
    assert rows[0]["expires_at"] is not None


# ===========================================================================
# T27：重写测试补全
# ===========================================================================


# ---------------------------------------------------------------------------
# 测试 12-15：4 种 reason 各路径
# ---------------------------------------------------------------------------


def test_reason_path_fog(mtca_db: Path) -> None:
    """reason='fog'：schedule 标 stale + 入队；provider 收到 fog hint。"""
    seg_id = _insert_segment(
        mtca_db, topic_label="漫剧", fog_anchor="漫剧 制作",
    )
    vid = _insert_view(
        mtca_db, seg_id, tier="L1", is_stale=0,
        content="旧版漫剧摘要",
    )

    provider = _MockProvider(response="fog-new")
    n = schedule_regen(seg_id, reason="fog", path=mtca_db)
    assert n == 1
    assert get_queue_size() == 1

    rows = query(
        "SELECT is_stale, stale_reason FROM views WHERE view_id = ?",
        (vid,), path=mtca_db,
    )
    assert int(rows[0]["is_stale"]) == 1
    assert rows[0]["stale_reason"] == "fog"

    # 队列消费后 prompt 应含 fog 提示
    n_done = process_queue(provider=provider, path=mtca_db)
    assert n_done == 1
    assert "物理擦除" in provider.captured[0]
    assert "[重写原因] fog" in provider.captured[0]


def test_reason_path_supersede(mtca_db: Path) -> None:
    """reason='supersede'：标 stale + 入队；prompt 含 supersede 提示。"""
    seg_id = _insert_segment(mtca_db, topic_label="方案A", fog_anchor="")
    vid = _insert_view(mtca_db, seg_id, tier="L2", is_stale=0)

    provider = _MockProvider(response="sup-new")
    n = schedule_regen(seg_id, reason="supersede", path=mtca_db)
    assert n == 1

    rows = query(
        "SELECT is_stale, stale_reason FROM views WHERE view_id = ?",
        (vid,), path=mtca_db,
    )
    assert int(rows[0]["is_stale"]) == 1
    assert rows[0]["stale_reason"] == "supersede"

    process_queue(provider=provider, path=mtca_db)
    assert "取代" in provider.captured[0]
    assert "[重写原因] supersede" in provider.captured[0]


def test_reason_path_promote(mtca_db: Path) -> None:
    """reason='promote'：标 stale + 入队；prompt 含 promote 提示。"""
    seg_id = _insert_segment(mtca_db)
    vid = _insert_view(mtca_db, seg_id, tier="L3", is_stale=0)

    provider = _MockProvider(response="promote-new")
    n = schedule_regen(seg_id, reason="promote", path=mtca_db)
    assert n == 1

    rows = query(
        "SELECT is_stale, stale_reason FROM views WHERE view_id = ?",
        (vid,), path=mtca_db,
    )
    assert int(rows[0]["is_stale"]) == 1
    assert rows[0]["stale_reason"] == "promote"

    process_queue(provider=provider, path=mtca_db)
    assert "升级" in provider.captured[0]
    assert "[重写原因] promote" in provider.captured[0]


def test_reason_path_manual(mtca_db: Path) -> None:
    """reason='manual'：标 stale + 入队；prompt 含 manual 提示。"""
    seg_id = _insert_segment(mtca_db)
    vid = _insert_view(mtca_db, seg_id, tier="L1", is_stale=0)

    provider = _MockProvider(response="manual-new")
    n = schedule_regen(seg_id, reason="manual", path=mtca_db)
    assert n == 1

    rows = query(
        "SELECT is_stale, stale_reason FROM views WHERE view_id = ?",
        (vid,), path=mtca_db,
    )
    assert int(rows[0]["is_stale"]) == 1
    assert rows[0]["stale_reason"] == "manual"

    process_queue(provider=provider, path=mtca_db)
    assert "手动触发" in provider.captured[0]
    assert "[重写原因] manual" in provider.captured[0]


# ---------------------------------------------------------------------------
# 测试 16：队列并发（多线程同时入队）
# ---------------------------------------------------------------------------


def test_concurrent_enqueue(mtca_db: Path) -> None:
    """4 个线程同时 schedule_regen 各自段落，每个段落 3 个 view = 12 个任务。"""
    seg_ids: list[str] = []
    for i in range(4):
        sid = _insert_segment(mtca_db, topic_label=f"主题-{i}")
        for tier in ("L1", "L2", "L3"):
            _insert_view(mtca_db, sid, tier=tier)
        seg_ids.append(sid)

    results: list[int] = []
    errors: list[Exception] = []

    def worker(sid: str) -> None:
        try:
            results.append(schedule_regen(sid, reason="manual", path=mtca_db))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in seg_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    # 无异常
    assert errors == []
    # 4 线程各入队 3 个 view = 12
    assert sorted(results) == [3, 3, 3, 3]
    assert get_queue_size() == 12

    # DB 中 12 个 view 全部标 stale
    rows = query(
        "SELECT COUNT(*) AS n FROM views WHERE is_stale = 1",
        path=mtca_db,
    )
    assert int(rows[0]["n"]) == 12

    # 队列全部消费成功
    provider = _MockProvider(response="concurrent-new")
    n_done = process_queue(provider=provider, path=mtca_db)
    assert n_done == 12
    assert provider.calls == 12
    assert get_queue_size() == 0


# ---------------------------------------------------------------------------
# 测试 17：重试 3 次后放弃（max_retries=3 → 共 4 次尝试后放弃）
# ---------------------------------------------------------------------------


def test_retry_three_times_then_give_up(mtca_db: Path) -> None:
    """provider 永远失败；max_retries=3 → 1+3=4 次后抛 RuntimeError 放弃。"""
    seg_id = _insert_segment(mtca_db)
    vid = _insert_view(
        mtca_db, seg_id, tier="L1", content="keep",
        is_stale=1, regen_count=0, stale_reason="fog",
    )

    provider = _MockProvider(fail_until_attempt=999)
    with pytest.raises(RuntimeError) as excinfo:
        regen_view(
            vid, reason="fog",
            provider=provider, path=mtca_db,
            max_retries=3,
        )

    # 异常消息含 reason / retries 信息
    msg = str(excinfo.value)
    assert "fog" in msg
    assert "retries=3" in msg

    # 共调用 4 次（1 次 + 3 次重试）
    assert provider.calls == 4

    # 旧 view 完全不变
    rows = query(
        "SELECT * FROM views WHERE view_id = ?",
        (vid,), path=mtca_db,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["expires_at"] is None
    assert row["content"] == "keep"
    assert int(row["regen_count"]) == 0
    assert int(row["is_stale"]) == 1
    assert row["stale_reason"] == "fog"

    # 没有新 view 写入
    n_views = query(
        "SELECT COUNT(*) AS n FROM views WHERE segment_id = ?",
        (seg_id,), path=mtca_db,
    )
    assert int(n_views[0]["n"]) == 1


# ---------------------------------------------------------------------------
# 测试 18：失败后旧 view 完整保留（保留时间）
# ---------------------------------------------------------------------------


def test_failure_old_view_intact(mtca_db: Path) -> None:
    """失败后旧 view：expires_at 仍为 NULL（保留可见）、is_stale 保持、
    content 不变、regen_count 不变、stale_reason 保持原值。"""
    seg_id = _insert_segment(mtca_db)
    original = {
        "content": "原始摘要",
        "tier": "L2",
        "regen_count": 7,
        "is_stale": 1,
        "stale_reason": "fog",
    }
    vid = _insert_view(mtca_db, seg_id, **original)

    before_ms = _now_ms()
    provider = _MockProvider(fail_until_attempt=10)
    with pytest.raises(RuntimeError):
        regen_view(
            vid, reason="fog",
            provider=provider, path=mtca_db,
            max_retries=2,
        )
    after_ms = _now_ms()

    rows = query(
        "SELECT * FROM views WHERE view_id = ?",
        (vid,), path=mtca_db,
    )
    row = rows[0]

    # expires_at 必须仍为 NULL（视图未过期，用户仍可读）
    assert row["expires_at"] is None

    # 所有原始字段完整保留
    assert row["content"] == original["content"]
    assert row["tier"] == original["tier"]
    assert int(row["regen_count"]) == original["regen_count"]
    assert int(row["is_stale"]) == original["is_stale"]
    assert row["stale_reason"] == original["stale_reason"]

    # created_at 应在 before/after 之前（未被改写）
    created_at = int(row["created_at"])
    assert created_at <= before_ms

    # 该段落下不应有任何新 view（expired_at IS NULL 即为当前可见）
    n_visible = query(
        "SELECT COUNT(*) AS n FROM views "
        "WHERE segment_id = ? AND expires_at IS NULL",
        (seg_id,), path=mtca_db,
    )
    assert int(n_visible[0]["n"]) == 1


# ---------------------------------------------------------------------------
# 测试 19：regen_count 多次累加
# ---------------------------------------------------------------------------


def test_regen_count_accumulates(mtca_db: Path) -> None:
    """同一段落连续 regen 3 次：regen_count 链式累加 0→1→2→3。"""
    seg_id = _insert_segment(mtca_db)
    current_vid = _insert_view(
        mtca_db, seg_id, tier="L1",
        is_stale=1, regen_count=0, content="v0",
    )
    provider = _MockProvider(response="evolved")

    expected_counts = [1, 2, 3]
    for i, expected in enumerate(expected_counts):
        result = regen_view(
            current_vid, reason="manual",
            provider=provider, path=mtca_db,
        )
        assert result["status"] == "ok"
        assert result["regen_count"] == expected
        assert result["reason"] == "manual"

        # 新 view 在 DB 中的 regen_count 也对得上
        new_rows = query(
            "SELECT regen_count, content, is_stale "
            "FROM views WHERE view_id = ?",
            (result["new_view_id"],), path=mtca_db,
        )
        assert int(new_rows[0]["regen_count"]) == expected
        assert new_rows[0]["content"] == "evolved"
        assert int(new_rows[0]["is_stale"]) == 0

        # 标当前 view 为 stale 以便下次 regen
        execute(
            "UPDATE views SET is_stale = 1, stale_reason = 'manual' "
            "WHERE view_id = ?",
            (result["new_view_id"],), path=mtca_db,
        )
        current_vid = result["new_view_id"]

    # 链路：3 个过期 + 1 个最终存活 = 4 行
    all_rows = query(
        "SELECT regen_count, expires_at FROM views "
        "WHERE segment_id = ? ORDER BY created_at",
        (seg_id,), path=mtca_db,
    )
    assert len(all_rows) == 4
    # 最后一个 view 仍可见且 regen_count = 3
    assert all_rows[-1]["expires_at"] is None
    assert int(all_rows[-1]["regen_count"]) == 3
    # 前 3 个已过期
    for r in all_rows[:3]:
        assert r["expires_at"] is not None


# ---------------------------------------------------------------------------
# 测试 20：新 view 正确清除 is_stale 标志位
# ---------------------------------------------------------------------------


def test_new_view_clears_is_stale(mtca_db: Path) -> None:
    """regen_view 写入新行时：is_stale=0、stale_reason=NULL、expires_at=NULL。"""
    seg_id = _insert_segment(mtca_db)
    old_vid = _insert_view(
        mtca_db, seg_id, tier="L1",
        is_stale=1, stale_reason="fog",
        content="stale old", regen_count=4,
    )

    provider = _MockProvider(response="fresh-clean")
    result = regen_view(
        old_vid, reason="fog",
        provider=provider, path=mtca_db,
    )
    new_vid = result["new_view_id"]

    # 新 view 全字段
    new_rows = query(
        "SELECT is_stale, stale_reason, expires_at, content, "
        "regen_count, tier, segment_id "
        "FROM views WHERE view_id = ?",
        (new_vid,), path=mtca_db,
    )
    assert len(new_rows) == 1
    nr = new_rows[0]
    assert int(nr["is_stale"]) == 0
    assert nr["stale_reason"] is None
    assert nr["expires_at"] is None
    assert nr["content"] == "fresh-clean"
    assert int(nr["regen_count"]) == 5  # 4 + 1
    assert nr["tier"] == "L1"
    assert nr["segment_id"] == seg_id

    # 旧 view 仍 is_stale=1（保留 stale 标记供审计）
    old_rows = query(
        "SELECT is_stale, stale_reason, expires_at FROM views "
        "WHERE view_id = ?",
        (old_vid,), path=mtca_db,
    )
    assert int(old_rows[0]["is_stale"]) == 1
    assert old_rows[0]["stale_reason"] == "fog"
    assert old_rows[0]["expires_at"] is not None


# ---------------------------------------------------------------------------
# 测试 21：view_id 不存在时的边界
# ---------------------------------------------------------------------------


def test_regen_nonexistent_view_id(mtca_db: Path) -> None:
    """view_id 合法但 DB 中不存在 → ValueError；不应调用 provider。"""
    provider = _MockProvider(response="unused")
    bogus_id = "this-id-does-not-exist-12345"

    with pytest.raises(ValueError) as excinfo:
        regen_view(bogus_id, reason="manual", provider=provider, path=mtca_db)
    assert "视图不存在" in str(excinfo.value)
    assert provider.calls == 0

    # 空字符串 → 同样 ValueError（参数校验先于 DB 查询）
    with pytest.raises(ValueError):
        regen_view("", reason="manual", provider=provider, path=mtca_db)
    assert provider.calls == 0


def test_regen_view_with_missing_segment(mtca_db: Path) -> None:
    """view 存在但关联 segment 已不存在 → ValueError。

    通过 PRAGMA defer_foreign_keys 临时禁用 FK 检查来构造孤儿 view。
    """
    seg_id = _insert_segment(mtca_db)
    vid = _insert_view(mtca_db, seg_id, is_stale=1)

    # 在同一连接内临时禁用 FK（PRAGMA 仅作用于当前连接）
    from src.store.sqlite import get_connection
    with get_connection(path=mtca_db) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM segments WHERE segment_id = ?", (seg_id,))
        conn.commit()

    provider = _MockProvider()
    with pytest.raises(ValueError) as excinfo:
        regen_view(vid, reason="manual", provider=provider, path=mtca_db)
    assert "段落" in str(excinfo.value)
    assert provider.calls == 0


# ---------------------------------------------------------------------------
# 测试 22：后台 worker 处理队列
# ---------------------------------------------------------------------------


def test_background_worker_drains_queue(mtca_db: Path) -> None:
    """启动后台 worker → 入队 → worker 自动消费 → 停止。"""
    seg_id = _insert_segment(mtca_db)
    _insert_view(mtca_db, seg_id, tier="L1")
    _insert_view(mtca_db, seg_id, tier="L2")

    provider = _MockProvider(response="bg-fresh")
    thread = start_background_worker(provider=provider, path=mtca_db)
    assert thread is not None
    assert thread.is_alive()
    assert thread.daemon is True

    try:
        n = schedule_regen(seg_id, reason="manual", path=mtca_db)
        assert n == 2

        # 等 worker 消费完（最多 3 秒）
        deadline = _now_ms() + 3000
        while get_queue_size() > 0 and _now_ms() < deadline:
            time.sleep(0.05)

        assert get_queue_size() == 0
        assert provider.calls == 2
    finally:
        stopped = stop_background_worker(timeout=2.0)
        assert stopped is True

    # 2 个新 view 已生成
    new_rows = query(
        "SELECT content, expires_at, regen_count, is_stale FROM views "
        "WHERE segment_id = ? AND expires_at IS NULL",
        (seg_id,), path=mtca_db,
    )
    assert len(new_rows) == 2
    for r in new_rows:
        assert r["content"] == "bg-fresh"
        assert int(r["regen_count"]) == 1
        assert int(r["is_stale"]) == 0


def test_start_worker_returns_existing_when_alive(mtca_db: Path) -> None:
    """worker 已启动时再次 start_background_worker 返回原线程句柄。"""
    provider = _MockProvider()
    thread1 = start_background_worker(provider=provider, path=mtca_db)
    try:
        thread2 = start_background_worker(provider=provider, path=mtca_db)
        assert thread2 is thread1
    finally:
        stop_background_worker(timeout=2.0)


# ---------------------------------------------------------------------------
# 测试 23：stop_background_worker 无线程时
# ---------------------------------------------------------------------------


def test_stop_worker_without_start(mtca_db: Path) -> None:
    """未启动 worker 就 stop → True（无线程可 join，不抛错）。"""
    assert stop_background_worker() is True
    # 幂等：多次调用仍 True
    assert stop_background_worker() is True
    assert stop_background_worker(timeout=5.0) is True


# ---------------------------------------------------------------------------
# 测试 24：空队列 process_queue 返回 0
# ---------------------------------------------------------------------------


def test_process_queue_empty_returns_zero(mtca_db: Path) -> None:
    """空队列 process_queue 立即返回 0；不应调 provider。"""
    provider = _MockProvider()
    n = process_queue(provider=provider, path=mtca_db)
    assert n == 0
    assert get_queue_size() == 0
    assert provider.calls == 0


# ---------------------------------------------------------------------------
# 测试 25：过期 view 不被 schedule_regen 入队
# ---------------------------------------------------------------------------


def test_schedule_skips_expired_views(mtca_db: Path) -> None:
    """schedule_regen 跳过已 expire 的视图（不复活）。"""
    seg_id = _insert_segment(mtca_db)
    # 1 个未过期 + 1 个已过期
    alive_vid = _insert_view(mtca_db, seg_id, is_stale=0)
    expired_vid = str(uuid.uuid4())
    execute(
        "INSERT INTO views "
        "(view_id, segment_id, tier, content, created_at, "
        "expires_at, is_stale, stale_reason, regen_count) "
        "VALUES (?, ?, 'L1', 'expired', ?, ?, 0, NULL, 0)",
        (expired_vid, seg_id, _now_ms() - 60_000, _now_ms() - 1),
        path=mtca_db,
    )

    n = schedule_regen(seg_id, reason="fog", path=mtca_db)
    # 仅入队未过期的 1 个
    assert n == 1
    assert get_queue_size() == 1

    # 过期 view 仍 is_stale=0（未被改动）
    rows = query(
        "SELECT is_stale, expires_at FROM views WHERE view_id = ?",
        (expired_vid,), path=mtca_db,
    )
    assert int(rows[0]["is_stale"]) == 0
    assert rows[0]["expires_at"] is not None

    # 未过期 view 已被标 stale
    rows2 = query(
        "SELECT is_stale, stale_reason FROM views WHERE view_id = ?",
        (alive_vid,), path=mtca_db,
    )
    assert int(rows2[0]["is_stale"]) == 1
    assert rows2[0]["stale_reason"] == "fog"


# ---------------------------------------------------------------------------
# 测试 26（额外）：process_queue 失败项不阻塞队列
# ---------------------------------------------------------------------------


def test_process_queue_continues_after_failure(mtca_db: Path) -> None:
    """队列中第 1 个 regen 失败，第 2 个仍被处理。"""
    seg_id = _insert_segment(mtca_db)
    bad_vid = _insert_view(mtca_db, seg_id, is_stale=1, content="bad")
    good_vid = _insert_view(mtca_db, seg_id, is_stale=1, content="good")

    # 直接入队两条任务
    _queue_put_for_test(bad_vid, seg_id)
    _queue_put_for_test(good_vid, seg_id)

    # provider：对 bad_vid 抛错，对 good_vid 返回成功
    class _SelectiveProvider(_MockProvider):
        def generate(self, prompt: str, **kwargs: Any) -> str:  # type: ignore[override]
            self.calls += 1
            self.captured.append(prompt)
            if "bad" in prompt:
                raise RuntimeError("intentional fail")
            return self.response

    provider = _SelectiveProvider(response="good-new")
    n_done = process_queue(provider=provider, path=mtca_db)

    # 仅 good_vid 成功
    assert n_done == 1
    # bad_vid 重试 1+3=4 次都失败，good_vid 1 次成功 → 总 5 次
    assert provider.calls == 5
    assert get_queue_size() == 0

    # good_vid 已 expire + 新 view 存在
    rows = query(
        "SELECT expires_at FROM views WHERE view_id = ?",
        (good_vid,), path=mtca_db,
    )
    assert rows[0]["expires_at"] is not None

    # bad_vid 仍 is_stale=1, expires_at=NULL（保留旧视图）
    rows2 = query(
        "SELECT is_stale, expires_at FROM views WHERE view_id = ?",
        (bad_vid,), path=mtca_db,
    )
    assert int(rows2[0]["is_stale"]) == 1
    assert rows2[0]["expires_at"] is None


def _queue_put_for_test(view_id: str, segment_id: str) -> None:
    """直接向模块队列注入测试任务。"""
    from src.compress.regen_engine import _queue
    _queue.put({
        "view_id": view_id,
        "segment_id": segment_id,
        "reason": "manual",
        "scheduled_at": _now_ms(),
    })
