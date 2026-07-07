"""sync 接口 stub 测试（M2.5.8 B：未来云同步插座预留）。

12 个测试覆盖：
- SyncAdapter Protocol + runtime_checkable
- LocalOnlySync 行为
- 全局注册表 + reset + 并发 race
- segment / session / fact sync 钩子
- 同步失败不抛异常
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from src.sync import (
    LocalOnlySync,
    SyncAdapter,
    get_adapter,
    reset_adapter,
    set_adapter,
)
from src.sync.local_only_sync import LocalOnlySync as LocalOnlySyncDirect
from src.sync.registry import get_adapter as get_adapter_direct
from src.sync.sync_adapter import SyncAdapter as SyncAdapterDirect


# ---------------------------------------------------------------------------
# 1. Protocol runtime_checkable
# ---------------------------------------------------------------------------


def test_sync_adapter_protocol() -> None:
    """LocalOnlySync 必须 isinstance SyncAdapter（Protocol runtime_checkable）。"""
    a = LocalOnlySync()
    assert isinstance(a, SyncAdapter)
    assert isinstance(a, SyncAdapterDirect)


# ---------------------------------------------------------------------------
# 2-4. LocalOnlySync 行为
# ---------------------------------------------------------------------------


def test_local_only_sync_push_returns_id() -> None:
    """push(data) 是 no-op：原样返回 data['id']。"""
    a = LocalOnlySync()
    assert a.push({"id": "abc-123", "type": "segment"}) == "abc-123"
    # id 缺失则返回 ""（不允许 KeyError）
    assert a.push({"type": "segment"}) == ""


def test_local_only_sync_pull_empty() -> None:
    """pull 默认空列表（本地最权威）。"""
    a = LocalOnlySync()
    assert a.pull() == []
    assert a.pull(since_ms=1234567890) == []


def test_local_only_sync_status() -> None:
    """status 必须含 4 个字段：mode / last_sync_ms / pending / errors。"""
    a = LocalOnlySync()
    s = a.status()
    assert set(s.keys()) == {"mode", "last_sync_ms", "pending", "errors"}
    assert s["mode"] == "local_only"
    assert s["last_sync_ms"] is None
    assert s["pending"] == 0
    assert s["errors"] == []


# ---------------------------------------------------------------------------
# 5-7. Registry：默认 / set / reset
# ---------------------------------------------------------------------------


def test_registry_default_local_only() -> None:
    """未初始化时 get_adapter() 返回 LocalOnlySync 实例。"""
    reset_adapter()
    a = get_adapter()
    assert isinstance(a, LocalOnlySync)


def test_registry_set_custom_adapter() -> None:
    """set_adapter 后 get_adapter() 必须是新实例。"""
    reset_adapter()
    custom = LocalOnlySync()
    set_adapter(custom)
    assert get_adapter() is custom
    reset_adapter()


def test_registry_reset() -> None:
    """reset 后再次 get_adapter 返回新 LocalOnlySync。"""
    set_adapter(LocalOnlySync())
    reset_adapter()
    a = get_adapter()
    assert isinstance(a, LocalOnlySync)


# ---------------------------------------------------------------------------
# 8. Registry 并发 race
# ---------------------------------------------------------------------------


def test_registry_concurrent_set_adapter_no_race() -> None:
    """8 线程并发 set+get 100 次：所有 get 返回非 None，且 set 之间互不破坏。"""
    reset_adapter()

    errors: list[str] = []
    iterations = 100
    barrier = threading.Barrier(8)

    def worker(seed: int) -> None:
        try:
            barrier.wait(timeout=5)
            for i in range(iterations):
                ad = LocalOnlySync()
                set_adapter(ad)
                got = get_adapter()
                if got is None:
                    errors.append(f"seed={seed} i={i} got None")
        except Exception as exc:
            errors.append(f"seed={seed} exc={exc!r}")

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == [], f"race errors: {errors[:5]}"
    # 最终状态：get_adapter 必须仍然可调用且非 None
    assert get_adapter() is not None
    reset_adapter()


# ---------------------------------------------------------------------------
# Mock 适配器（用于 #9-12）
# ---------------------------------------------------------------------------


class _MockAdapter:
    """捕获 push 调用的最小适配器。"""

    def __init__(self, raise_on_push: bool = False) -> None:
        self.calls: list[dict] = []
        self.raise_on_push = raise_on_push

    def push(self, data: dict) -> str:
        self.calls.append(data)
        if self.raise_on_push:
            raise RuntimeError("simulated sync failure")
        return data.get("id", "")

    def pull(self, since_ms: int = 0) -> list[dict]:
        return []

    def status(self) -> dict:
        return {"mode": "mock", "last_sync_ms": None, "pending": 0, "errors": []}


# ---------------------------------------------------------------------------
# 9-11. sync_X_to_adapter 钩子
# ---------------------------------------------------------------------------


def test_segment_sync_to_calls_adapter(mtca_db: Path) -> None:
    """sync_segment_to_adapter：必须 push 一条 type=segment 数据。"""
    from src.l0.segment_writer import write_segments, sync_segment_to_adapter
    from src.l0.session_writer import create_session

    sid = create_session(path=mtca_db)
    seg_ids = write_segments(sid, path=mtca_db)
    seg_id = seg_ids[0]

    mock = _MockAdapter()
    returned = sync_segment_to_adapter(seg_id, adapter=mock, path=mtca_db)

    assert returned == seg_id
    assert len(mock.calls) == 1
    assert mock.calls[0]["type"] == "segment"
    assert mock.calls[0]["id"] == seg_id
    assert isinstance(mock.calls[0]["payload"], dict)
    assert "ts" in mock.calls[0]


def test_session_sync_to_calls_adapter(mtca_db: Path) -> None:
    """sync_session_to_adapter：必须 push 一条 type=session 数据。"""
    from src.l0.session_writer import create_session, sync_session_to_adapter

    sid = create_session(path=mtca_db)
    mock = _MockAdapter()
    returned = sync_session_to_adapter(sid, adapter=mock, path=mtca_db)

    assert returned == sid
    assert len(mock.calls) == 1
    assert mock.calls[0]["type"] == "session"
    assert mock.calls[0]["id"] == sid
    assert isinstance(mock.calls[0]["payload"], dict)
    assert "ts" in mock.calls[0]


def test_fact_sync_to_calls_adapter(mtca_db: Path) -> None:
    """sync_fact_to_adapter：必须 push 一条 type=fact 数据。"""
    from src.llm.facts_store import create_fact, sync_fact_to_adapter
    from src.l0.session_writer import create_session

    sid = create_session(path=mtca_db)
    fid = create_fact(sid, "天是蓝的", path=mtca_db)

    mock = _MockAdapter()
    returned = sync_fact_to_adapter(fid, adapter=mock, path=mtca_db)

    assert returned == fid
    assert len(mock.calls) == 1
    assert mock.calls[0]["type"] == "fact"
    assert mock.calls[0]["id"] == fid
    assert isinstance(mock.calls[0]["payload"], dict)
    assert "ts" in mock.calls[0]


# ---------------------------------------------------------------------------
# 12. 同步失败不抛
# ---------------------------------------------------------------------------


def test_sync_to_failure_does_not_raise(mtca_db: Path) -> None:
    """adapter.push 抛异常时，sync_X_to_adapter 必须返回 "" 且不抛。"""
    from src.l0.session_writer import create_session, sync_session_to_adapter

    sid = create_session(path=mtca_db)
    mock = _MockAdapter(raise_on_push=True)

    # 任何异常都必须在 sync_X_to_adapter 内部消化
    result = sync_session_to_adapter(sid, adapter=mock, path=mtca_db)
    assert result == ""
    assert len(mock.calls) == 1  # push 仍被尝试调用过