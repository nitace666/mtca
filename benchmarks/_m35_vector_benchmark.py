"""M3-5 向量召回 benchmark
# -*- coding: utf-8 -*-

- 5 场景（老板日常）
- Jaccard(A, B) = |A∩B| / |A∪B|
- 不算 P@3，算 Jaccard 召回多样性
- 真连 Ollama + ChromaDB，测实 p95
- 通过 settings.vector_recall_enabled 临时开 / 运行后恢复 0
- benchmark 脚本本身不进 pytest（下划线开头 pytest 不收）

"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# 加入 worktree 路径，便于 import src.recall.recall_engine
WORKTREE = Path(r"I:\\PROJECTS\\MTCA-m3-5-vector")
sys.path.insert(0, str(WORKTREE))

from src.recall.recall_engine import recall
from src.recall.vector_index import VectorIndex
from src.recall.vector_index import EMBED_DIM

# === 自定义 Ollama 调用（timeout=60s，benchmark 用） ===
import urllib.request
import urllib.error

def _slow_ollama_post(prompt: str) -> list:
    """同 vector_index._ollama_post，但 timeout=60s（Chroma 冷启 + Ollama 慢时）。"""
    body = json.dumps({"model": "nomic-embed-text", "prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/embeddings",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60.0) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    emb = payload.get("embedding")
    if not isinstance(emb, list) or len(emb) != 768:
        raise RuntimeError(f"embedding 维度异常: {len(emb) if isinstance(emb, list) else type(emb).__name__}")
    return [float(x) for x in emb]


from src.store.sqlite import init_db


# === 5 场景 ===

SCENARIOS = [
    ("上次搬家咋整的", "seg-move-001"),
    ("项目咋样了", "seg-proj-001"),
    ("X 朋友最近", "seg-friend-001"),
    ("紧急的事", "seg-urgent-001"),
    ("老朋友", "seg-oldfriend-001"),
]


# === Ground truth: 5 段对应 5 场景 ===

GROUND_TRUTH = [
    {
        "segment_id": "seg-move-001",
        "session_id": "s1",
        "start_msg_seq": 1,
        "end_msg_seq": 5,
        "start_at": 1700000000000,
        "end_at": 1700000005000,
        "topic_label": "搬家经验 上次运了三箱子衣服担活干部分来乐助",
        "fog_anchor": "搬家",
        "current_tier": "L0",
        "current_score": 100.0,
        "fog_state": "clear",
        "silence_state": "active",
        "long_silent": 0,
        "supersedes_count": 0,
    },
    {
        "segment_id": "seg-proj-001",
        "session_id": "s2",
        "start_msg_seq": 1,
        "end_msg_seq": 5,
        "start_at": 1700100000000,
        "end_at": 1700100005000,
        "topic_label": "M3-5 向量召回项目进度已完成 80% TDD 流程",
        "fog_anchor": "项目",
        "current_tier": "L0",
        "current_score": 100.0,
        "fog_state": "clear",
        "silence_state": "active",
        "long_silent": 0,
        "supersedes_count": 0,
    },
    {
        "segment_id": "seg-friend-001",
        "session_id": "s3",
        "start_msg_seq": 1,
        "end_msg_seq": 5,
        "start_at": 1700200000000,
        "end_at": 1700200005000,
        "topic_label": "Alice 朋友最近在看新剧曾经去山西",
        "fog_anchor": "Alice",
        "current_tier": "L0",
        "current_score": 100.0,
        "fog_state": "clear",
        "silence_state": "active",
        "long_silent": 0,
        "supersedes_count": 0,
    },
    {
        "segment_id": "seg-urgent-001",
        "session_id": "s4",
        "start_msg_seq": 1,
        "end_msg_seq": 5,
        "start_at": 1700300000000,
        "end_at": 1700300005000,
        "topic_label": "紧急事项昌都地铁管漏水需要紧急用钢缴材料",
        "fog_anchor": "紧急",
        "current_tier": "L0",
        "current_score": 100.0,
        "fog_state": "clear",
        "silence_state": "active",
        "long_silent": 0,
        "supersedes_count": 0,
    },
    {
        "segment_id": "seg-oldfriend-001",
        "session_id": "s5",
        "start_msg_seq": 1,
        "end_msg_seq": 5,
        "start_at": 1700400000000,
        "end_at": 1700400005000,
        "topic_label": "老朋友张大帅还是原来那个伙子吃长寿青英镇大接",
        "fog_anchor": "张大帅",
        "current_tier": "L0",
        "current_score": 100.0,
        "fog_state": "clear",
        "silence_state": "active",
        "long_silent": 0,
        "supersedes_count": 0,
    },
]


def _setup_db(tmp_dir: Path) -> Path:
    """建隔离 DB + 灌 ground truth segments + 默认 vector_recall_enabled=0。"""
    db_path = tmp_dir / "bench.db"
    conn = init_db(db_path)
    conn.close()
    # 灌 segments + 其它表
    conn = sqlite3.connect(db_path)
    for gt in GROUND_TRUTH:
        conn.execute(
            "INSERT INTO sessions(session_id, started_at, ended_at, topic_label) VALUES (?, ?, ?, ?)",
            (gt['session_id'], gt['start_at'], gt['end_at'], gt['topic_label']),
        )
        conn.execute(
            "INSERT INTO segments(segment_id, session_id, start_msg_seq, end_msg_seq, "
            "start_at, end_at, topic_label, fog_anchor, current_tier, current_score, "
            "fog_state, silence_state, long_silent, supersedes_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gt['segment_id'], gt['session_id'], gt['start_msg_seq'], gt['end_msg_seq'],
             gt['start_at'], gt['end_at'], gt['topic_label'], gt['fog_anchor'],
             gt['current_tier'], gt['current_score'],
             gt['fog_state'], gt['silence_state'], gt['long_silent'], gt['supersedes_count']),
        )
    conn.commit()
    # 默认关 vector_recall_enabled
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
        ('vector_recall_enabled', '0', int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()
    return db_path


def _set_enabled(db_path: Path, enabled: bool) -> None:
    """写 settings.vector_recall_enabled = '1' or '0'。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
        ('vector_recall_enabled', '1' if enabled else '0', int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()


def _read_enabled(db_path: Path) -> str:
    """读 settings.vector_recall_enabled 当前值（运行前 / 运行后监控用）。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            ('vector_recall_enabled',),
        ).fetchone()
        return row[0] if row else '<none>'
    finally:
        conn.close()


def _index_to_chroma(db_path: Path, chroma_dir: Path) -> tuple:
    """索引 5 段 ground truth 到 ChromaDB（真连 Ollama）。返回 (VectorIndex, total_ms)。"""
    conn = sqlite3.connect(db_path)
    try:
        idx = VectorIndex(db_path=chroma_dir, mtca_conn=conn, ollama_fn=_slow_ollama_post)
        t0 = time.perf_counter()
        for gt in GROUND_TRUTH:
            idx.index_segment(gt['segment_id'], gt['topic_label'] + ' ' + gt['fog_anchor'])
        total_ms = (time.perf_counter() - t0) * 1000
        return idx, total_ms
    finally:
        conn.close()


def _jaccard(a_ids, b_ids) -> tuple:
    """计算 Jaccard(A,B) 和标注。"""
    a_set, b_set = set(a_ids), set(b_ids)
    if not a_set and not b_set:
        return 0.0, '空'
    inter = a_set & b_set
    union = a_set | b_set
    return len(inter) / len(union), f'交={len(inter)} 并={len(union)}'


def _run_scenario(query: str, enabled: bool, db_path: Path, idx):
    """运行一个场景，返回 (results, median_ms, perf_breakdown)。"""
    _set_enabled(db_path, enabled)
    times = []
    results = None
    for _ in range(3):
        t0 = time.perf_counter()
        results = recall(query, top_k=5, path=str(db_path))
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    median_ms = times[1]
    return results or [], median_ms, {'runs': 3, 'times_ms': times}


def main() -> int:
    """M3-5 benchmark 主流程。"""
    print('=' * 70)
    print('M3-5 向量召回 Benchmark (实连 Ollama + ChromaDB)')
    print('=' * 70)
    print()
    tmp = Path(tempfile.mkdtemp(prefix='m35_bench_'))
    db_path = _setup_db(tmp)
    print(f'[setup] tmp dir: {tmp}')
    print(f'[setup] db: {db_path}')
    print(f'[setup] segments count: {len(GROUND_TRUTH)}')
    print(f'[settings] BEFORE: vector_recall_enabled = {_read_enabled(db_path)!r}')
    print()
    # ChromaDB 索引
    chroma_dir = tmp / 'chroma'
    print(f'[chroma] indexing to {chroma_dir}...')
    try:
        idx, index_ms = _index_to_chroma(db_path, chroma_dir)
        print(f'[chroma] indexed {len(GROUND_TRUTH)} segments in {index_ms:.1f}ms')
    except Exception as e:
        print(f'[chroma] FAILED: {type(e).__name__}: {e}')
        print('[chroma] Ollama 或 ChromaDB 不可达，中止 benchmark')
        _set_enabled(db_path, False)
        print(f'[cleanup] settings AFTER (restored 0): {_read_enabled(db_path)!r}')
        return 1
    print()
    # 5 场景圈
    print('-' * 100)
    print(f'{"场景":<20} | {"baseline top-3":<35} | {"+vector top-3":<35} | {"Jaccard":<14} | {"p50 ms":<8}')
    print('-' * 100)
    print()
    p50_samples = []
    for query, expected_seg_id in SCENARIOS:
        # baseline (vector_recall_enabled=0)
        base_results, base_ms, _ = _run_scenario(query, False, db_path, None)
        base_ids = [r.get('segment_id') for r in base_results]
        # +vector (vector_recall_enabled=1)
        vec_results, vec_ms, _ = _run_scenario(query, True, db_path, idx)
        vec_ids = [r.get('segment_id') for r in vec_results]
        # Jaccard
        j, note = _jaccard(base_ids, vec_ids)
        p50_samples.append(vec_ms)
        base_top3 = ','.join(str(i) for i in base_ids[:3])
        vec_top3 = ','.join(str(i) for i in vec_ids[:3])
        print(f'{query:<20} | {base_top3:<35} | {vec_top3:<35} | {j:.3f} {note:<8} | {vec_ms:.1f}')
    # p50 近似 p95（5 场景 sample 太少）
    p_max_ms = max(p50_samples) if p50_samples else 0.0
    print()
    print('-' * 100)
    print(f'p_max (5 samples, 近似 p95): {p_max_ms:.1f}ms')
    print(f'benchmark 标等: < 100ms -> {"✓" if p_max_ms < 100 else "✗"}')
    print()
    # 恢复 settings
    _set_enabled(db_path, False)
    print(f'[settings] AFTER (restored): vector_recall_enabled = {_read_enabled(db_path)!r}')
    print(f'[cleanup] tmp dir: {tmp} (自动在 OS 时间刷新)')
    return 0


if __name__ == "__main__":
    sys.exit(main())