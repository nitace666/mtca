"""tests/test_demo.py — A-2 demo 一键跑脚本 smoke test。

5 分钟体验脚本（``examples/demo_run.py``）的端到端冒烟测试。

覆盖：
- demo 在隔离 DB 上跑完不抛错
- demo 写出至少 3 个 segments
- demo 标记了至少 1 个 URGENT 段 + 至少 1 个 /重要 段
- demo 召回 ('MTCA 演示') 至少返 1 段
- demo 中的 URGENT 段 100 tick 后 score 不衰减（第 9 铁律）

每个 case 用 ``tmp_path`` 隔离 DB，不污染 ``examples/__demo_db/``。

约束：spec 白名单内新增文件，仅追加 ``tests/test_demo.py``，不动其它测试。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# 让 ``from src...`` 能找到项目根
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from examples import demo_run  # noqa: E402  examples/demo_run.py


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DAY_MS: int = 86_400_000
_IMPORTANT_SCORE: float = 10000.0  # 与 src/compress/scoring.py IMPORTANT_SCORE 对齐


# ---------------------------------------------------------------------------
# 1. demo 跑完不抛错
# ---------------------------------------------------------------------------


def test_demo_runs_without_error(tmp_path: Path) -> None:
    """demo 在 tmp_path 隔离的 DB 上跑完，断言无异常。"""
    db_path = tmp_path / "demo.db"
    demo_run.main(db=str(db_path), keep_db=True)
    assert db_path.exists(), f"demo 跑完应生成 DB 文件：{db_path}"


# ---------------------------------------------------------------------------
# 2. demo 写出 ≥3 个 segments
# ---------------------------------------------------------------------------


def test_demo_creates_3_segments(tmp_path: Path) -> None:
    """demo 跑完 DB 里至少 3 个 segments（L0-B 段落：3 段对话）。"""
    db_path = tmp_path / "demo.db"
    demo_run.main(db=str(db_path), keep_db=True)

    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    finally:
        conn.close()
    assert n >= 3, f"期望 ≥3 segments，实际 {n}"


# ---------------------------------------------------------------------------
# 3. demo 标记了 URGENT + /重要 段
# ---------------------------------------------------------------------------


def test_demo_marks_urgent_and_important(tmp_path: Path) -> None:
    """demo 跑完 DB 里至少 1 个 urgent_state='tracking' 段 + 至少 1 个 current_score ≥ 10000 段。"""
    db_path = tmp_path / "demo.db"
    demo_run.main(db=str(db_path), keep_db=True)

    conn = sqlite3.connect(str(db_path))
    try:
        urgent_n = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE urgent_state = 'tracking'"
        ).fetchone()[0]
        important_n = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE current_score >= ?",
            (_IMPORTANT_SCORE,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert urgent_n >= 1, (
        f"demo 后应至少 1 段是 urgent_state='tracking'，实际 {urgent_n}"
    )
    assert important_n >= 1, (
        f"demo 后应至少 1 段 current_score ≥ {_IMPORTANT_SCORE}，实际 {important_n}"
    )


# ---------------------------------------------------------------------------
# 4. demo 召回至少返 1 段
# ---------------------------------------------------------------------------


def test_demo_recall_returns_results(tmp_path: Path) -> None:
    """demo 跑完 recall('MTCA 项目') 至少返 1 段（unicode61 tokenize quirks 选定的可工作查询）。"""
    db_path = tmp_path / "demo.db"
    demo_run.main(db=str(db_path), keep_db=True)

    from src.recall.recall_engine import recall

    results = recall(query="MTCA 项目", path=str(db_path))
    assert len(results) >= 1, f"召回 'MTCA 项目' 期望 ≥1 段，实际 {len(results)}"


# ---------------------------------------------------------------------------
# 5. 第 9 铁律：URGENT 段 100 tick 模拟不衰减
# ---------------------------------------------------------------------------


def test_demo_iron_rule_9_passes(tmp_path: Path) -> None:
    """demo 中的 URGENT 段，promoted_at 前进 100 天后 score 仍 ≥ 初始值 95%。

    第 9 铁律：URGENT 段 base 不衰减 + f_time=1.0（多因子打分公式加固）。
    """
    db_path = tmp_path / "demo.db"
    demo_run.main(db=str(db_path), keep_db=True)

    from src.compress.multi_factor_score import calculate_score  # noqa: E402
    from src.store.sqlite import query  # noqa: E402

    rows = query(
        "SELECT * FROM segments WHERE urgent_state = 'tracking' LIMIT 1",
        path=str(db_path),
    )
    assert rows, "demo 后应该至少有 1 段是 urgent_state='tracking'"

    seg = dict(rows[0])
    promoted_at = int(seg.get("promoted_at") or 0)

    # 初始：promoted_at == now（age=0，f_time=1.0）
    score_initial, _ = calculate_score(seg, promoted_at)
    # 100 tick 后：age 增加 100 天，对普通段半衰期会显著衰减
    score_after, _ = calculate_score(seg, promoted_at + 100 * _DAY_MS)

    assert score_after >= score_initial * 0.95, (
        f"URGENT 段 100 tick 衰减违反铁律 9："
        f"initial={score_initial:.2f}, after={score_after:.2f}"
    )