"""src/compress/regen_engine.py 测试（T24）

测试覆盖（用户 prompt 列出的 7 项 + 边界）：

1. test_schedule_marks_stale         schedule_regen 标 is_stale=1 + 入队
2. test_regen_view_writes_new         regen_view 写新 view，regen_count+1
3. test_regen_view_expires_old        旧 view expires_at 被设置
4. test_regen_failure_keeps_old       provider 抛错后旧 view 未变
5. test_process_queue_drains          process_queue 清空队列
6. test_manual_refresh                manual_refresh 同步完成
7. test_reason_affects_prompt         不同 reason → prompt 含不同 reason
"""

from __future__ import annotations

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
