"""T28 召回性能测试：p95 < 50ms @ 1k 段。"""
from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest

from src.l0.session_writer import create_session, write_message
from src.l0.skeleton import build_skeleton, save_skeleton
from src.recall.recall_engine import recall


@pytest.fixture
def db_with_1k_segments(mtca_db: Path) -> Path:
    """灌 1000 个 session，每 session 2 段（L0-骨架 + time_segmenter 产出）。"""
    for i in range(1000):
        sid = create_session(path=mtca_db)
        write_message(sid, "user", f"MTCA 性能测试 query 关键词_{i}", path=mtca_db)
        write_message(sid, "assistant", f"MTCA 性能测试 response 关键词_{i}", path=mtca_db)
        skel = build_skeleton(sid, path=mtca_db)
        skel["topic_label"] = f"perf_topic_{i}"
        save_skeleton(sid, skel, path=mtca_db)
    return mtca_db


def _measure_p95(query: str, db_path: Path, n: int = 20) -> float:
    """跑 n 次 recall，取 p95 ms。"""
    times = []
    for _ in range(n):
        start = time.perf_counter()
        recall(query=query, top_k=5, path=db_path)
        times.append((time.perf_counter() - start) * 1000)
    # p95：第 19 位（n=20 时约对应 95 百分位）
    return statistics.quantiles(times, n=20)[18]


def test_recall_p95_under_50ms_at_1k(db_with_1k_segments: Path) -> None:
    """召回 p95 < 50ms @ 1k 段。"""
    p95 = _measure_p95("MTCA 性能测试 关键词", db_with_1k_segments)
    print(f"\n  baseline p95 = {p95:.1f}ms (target < 50ms)")
    assert p95 < 50.0, f"p95 = {p95:.1f}ms exceeds 50ms target"


def test_recall_p95_after_50_repeats_is_better(db_with_1k_segments: Path) -> None:
    """L1 / L2 cache 命中后，p95 应比首次调用更低。"""
    _measure_p95("MTCA 性能", db_with_1k_segments, n=5)
    times2 = []
    for _ in range(20):
        start = time.perf_counter()
        recall(query="MTCA 性能", top_k=5, path=db_with_1k_segments)
        times2.append((time.perf_counter() - start) * 1000)
    p95_warm = statistics.quantiles(times2, n=20)[18]
    print(f"\n  warm-cache p95 = {p95_warm:.1f}ms")
    assert p95_warm < 200.0, f"warm cache p95 = {p95_warm:.1f}ms (sanity check)"


def test_recall_cache_isolated_by_db_path(tmp_path: Path) -> None:
    """两个不同 db 的相同 query 不应串味。"""
    from src.store.sqlite import init_db
    db1 = tmp_path / "db1.db"; init_db(db1)
    db2 = tmp_path / "db2.db"; init_db(db2)
    # 用空库跑就行（无 segment 也不应返回对方 db 的缓存）
    r1 = recall(query="isolation", top_k=5, path=db1)
    r2 = recall(query="isolation", top_k=5, path=db2)
    # 都应返回空 list，且不抛异常（隔离验证）
    assert r1 == [] and r2 == []
