# -*- coding: utf-8 -*-
"""tests/test_vector_recall.py — M3-5 向量召回测试（红）

约束：
- 全 mock Ollama（自定义 ollama_fn）+ 全 mock ChromaDB（unittest.mock.MagicMock）
- 函数内 import vector_index（让 pytest 收集到 13 个测试名,运行时 import 失败 → 13 FAIL）
- 不依赖 src/store/sqlite.py 的 v3 migration（测试自己建 vector_cache 表）
- 用 conftest.py 既有的 tmp_db / mtca_db fixture
- 中文 docstring + 中文测试名

预期状态：Step 2 时 vector_index.py 不存在 → 13 个测试运行时 ModuleNotFoundError → 全 FAIL（红）
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


# === 常量（从 vector_index 期望签名） ===

_VECTOR_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS vector_cache (
    segment_id TEXT,
    chunk_idx INTEGER,
    embedding BLOB,
    model_hash TEXT,
    ts INTEGER,
    PRIMARY KEY (segment_id, chunk_idx)
);
"""


# === Mock helpers ===

def _fake_ollama_factory(seed: int = 42):
    """返回一个 ollama_fn,生成确定性的 768 维伪向量（不调真 Ollama）"""
    import random
    rng = random.Random(seed)

    def _fn(text):
        return [rng.uniform(-1.0, 1.0) for _ in range(768)]

    return _fn


def _failing_ollama(text):
    """Ollama 不可达的 mock（抛 ConnectionError）"""
    raise ConnectionError("Ollama 不可达（mock）")


def _make_fake_chroma_collection():
    """伪造一个 chromadb collection,支持 upsert/query,内存 cosine"""
    coll = MagicMock(name="fake_chroma_collection")
    coll._store = {}  # id -> {embedding, document, metadata}

    def upsert(ids, embeddings, documents, metadatas):
        for i, eid in enumerate(ids):
            coll._store[eid] = {
                "embedding": embeddings[i],
                "document": documents[i],
                "metadata": metadatas[i],
            }

    coll.upsert.side_effect = upsert

    def query(query_embeddings, n_results=10):
        q = query_embeddings[0]
        scored = []
        for eid, data in coll._store.items():
            emb = data["embedding"]
            dot = sum(a * b for a, b in zip(q, emb))
            qn = (sum(a * a for a in q)) ** 0.5
            en = (sum(b * b for b in emb)) ** 0.5
            cos = dot / (qn * en + 1e-9)
            dist = 1.0 - cos
            scored.append((eid, dist, data["metadata"], data["document"]))
        scored.sort(key=lambda x: x[1])
        top = scored[:n_results]
        return {
            "ids": [[t[0] for t in top]],
            "distances": [[t[1] for t in top]],
            "metadatas": [[t[2] for t in top]],
            "documents": [[t[3] for t in top]],
        }

    coll.query.side_effect = query
    return coll


def _make_fake_chroma_client(collection=None):
    """伪造一个 chromadb PersistentClient"""
    client = MagicMock(name="fake_chroma_client")
    client._collection = collection or _make_fake_chroma_collection()

    def get_or_create_collection(name):
        return client._collection

    client.get_or_create_collection.side_effect = get_or_create_collection
    return client


# === Setup helper（不用 fixture,测试内联调用） ===

def _setup(mtca_db, *, ollama_fn=None, chroma_collection=None):
    """开 conn + 建 vector_cache 表 + 构造 VectorIndex + 注入 fake chroma"""
    conn = sqlite3.connect(mtca_db)
    conn.row_factory = sqlite3.Row
    conn.executescript(_VECTOR_CACHE_DDL)
    conn.commit()
    chroma_dir = Path(tempfile.mkdtemp(prefix="chroma_"))
    # 函数内 import vector_index（Step 2 时 ModuleNotFoundError → 测试 FAIL）
    from src.recall.vector_index import VectorIndex  # noqa: F401  # 预期红
    idx = VectorIndex(
        db_path=chroma_dir,
        mtca_conn=conn,
        ollama_fn=ollama_fn or _fake_ollama_factory(),
    )
    fake_client = _make_fake_chroma_client(
        collection=chroma_collection or _make_fake_chroma_collection()
    )
    idx._client = fake_client
    idx._collection = fake_client._collection
    return conn, idx


def _set_enabled(conn, enabled):
    """写 settings 表的 vector_recall_enabled"""
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?,?,?)",
        ("vector_recall_enabled", "1" if enabled else "0", 0),
    )
    conn.commit()


# === 13 个测试 ===

def test_01_空查询返回空列表(mtca_db):
    """空 query / 纯空格 query → []（不调 Ollama,不调 Chroma）"""
    conn, idx = _setup(mtca_db)
    try:
        hits = idx.search("", k=10)
        assert hits == []
        hits2 = idx.search("   \n\t  ", k=10)
        assert hits2 == []
    finally:
        conn.close()


def test_02_单段命中返回非空(mtca_db):
    """先 index_segment 一段,再 search,验证返回非空且包含该段"""
    conn, idx = _setup(mtca_db)
    try:
        _set_enabled(conn, True)
        idx.index_segment("seg1", "今天搬家真累,腰酸背痛")
        hits = idx.search("搬家 累", k=5)
        assert len(hits) >= 1, f"应至少 1 命中,实际 {len(hits)}"
        assert any(h["segment_id"] == "seg1" for h in hits), (
            f"应命中 seg1,实际 ids={[h['segment_id'] for h in hits]}"
        )
    finally:
        conn.close()


def test_03_缓存命中不重复调Ollama(mtca_db):
    """重复 index 同一段 → Ollama 只调 1 次（第二次走 cache）"""
    conn, idx = _setup(mtca_db)
    try:
        before = idx.ollama_call_count()
        idx.index_segment("seg-cache", "测试段一内容")
        mid = idx.ollama_call_count()
        idx.index_segment("seg-cache", "测试段一内容")  # 同段 + 同文 → cache hit
        after = idx.ollama_call_count()
        assert mid - before == 1, f"首次应调 1 次,实际 {mid - before}"
        assert after - mid == 0, f"重复应 0 次,实际 {after - mid}"
    finally:
        conn.close()


def test_04_缓存未命中重算(mtca_db):
    """不同 segment_id → Ollama 重新调用（各自独立）"""
    conn, idx = _setup(mtca_db)
    try:
        before = idx.ollama_call_count()
        idx.index_segment("seg-a", "文本 A 内容")
        idx.index_segment("seg-b", "文本 B 内容")
        after = idx.ollama_call_count()
        assert after - before == 2, f"两个不同段应调 2 次,实际 {after - before}"
    finally:
        conn.close()


def test_05_encode返回768维(mtca_db):
    """encode 返回 list 长度 == 768"""
    conn, idx = _setup(mtca_db)
    try:
        emb = idx.encode("测试 768 维度验证")
        assert isinstance(emb, list), f"应返 list,实际 {type(emb)}"
        assert len(emb) == 768, f"维度必须 768,实际 {len(emb)}"
    finally:
        conn.close()


def test_06_Ollama不可达回退FTS5(mtca_db):
    """Ollama 抛异常 → search 不中断,返回 list（fallback）+ Ollama 调用计数不增"""
    conn, idx = _setup(mtca_db, ollama_fn=_failing_ollama)
    try:
        _set_enabled(conn, True)
        hits = idx.search("随便查一下")
        assert isinstance(hits, list), f"应返 list（fallback）,实际 {type(hits)}"
        # Ollama 抛错时调用计数不应增加（encode 内部未到 +=1）
        assert idx.ollama_call_count() == 0, (
            f"Ollama 失败时计数应=0,实际 {idx.ollama_call_count()}"
        )
    finally:
        conn.close()


def test_07_Chroma不可达回退FTS5(mtca_db):
    """ChromaDB query 抛异常 → search 不中断,返回 fallback list"""
    conn, idx = _setup(mtca_db)
    try:
        _set_enabled(conn, True)
        idx._collection.query.side_effect = RuntimeError("Chroma 不可达（mock）")
        hits = idx.search("查询")
        assert isinstance(hits, list), f"应返 list（fallback）,实际 {type(hits)}"
    finally:
        conn.close()


def test_08_默认关走原FTS5(mtca_db):
    """settings 表无 vector_recall_enabled key → is_enabled False → search 返 []"""
    conn, idx = _setup(mtca_db)
    try:
        # 不写 settings,默认 False
        assert idx.is_enabled() is False, "默认（settings 无 key）应 False"
        hits = idx.search("随便")
        assert hits == [], f"默认关时 search 应 [],实际 {hits}"
    finally:
        conn.close()


def test_09_启用后调向量(mtca_db):
    """settings.vector_recall_enabled='1' → is_enabled True → search 走向量"""
    conn, idx = _setup(mtca_db)
    try:
        _set_enabled(conn, True)
        assert idx.is_enabled() is True, "设置后应 True"
        idx.index_segment("seg-enabled", "今天测试启用走向量")
        hits = idx.search("启用", k=5)
        assert any(h["segment_id"] == "seg-enabled" for h in hits), (
            f"启用后应命中 seg-enabled,实际 {[h['segment_id'] for h in hits]}"
        )
    finally:
        conn.close()


def test_10_top_k限制(mtca_db):
    """index 5 段,search k=3 → 返回 ≤ 3"""
    conn, idx = _setup(mtca_db)
    try:
        _set_enabled(conn, True)
        for i in range(5):
            idx.index_segment(f"seg-{i}", f"段 {i} 内容不同 keyword-{i} test")
        hits = idx.search("keyword", k=3)
        assert len(hits) <= 3, f"top-k=3 应返 ≤ 3,实际 {len(hits)}"
    finally:
        conn.close()


def test_11_同段多块合并去重(mtca_db):
    """同一段切成多块,搜到时合并成 1 个 segment_id + score 取 max"""
    conn, idx = _setup(mtca_db)
    try:
        _set_enabled(conn, True)
        # 长文本,> 1500 字,必然切成 ≥ 2 块
        long_text = ("段落A。" * 500) + "\n\n" + ("段落B。" * 500)
        assert len(long_text) > 1500
        idx.index_segment("seg-multi", long_text)
        hits = idx.search("段落", k=10)
        seg_ids = [h["segment_id"] for h in hits]
        assert seg_ids.count("seg-multi") == 1, (
            f"同段多块应合并去重为 1 个,实际 seg_ids={seg_ids}"
        )
    finally:
        conn.close()


def test_12_中英混合查询(mtca_db):
    """中英混合 query 不抛,正常返回"""
    conn, idx = _setup(mtca_db)
    try:
        _set_enabled(conn, True)
        idx.index_segment("seg-cn-en", "AI 模型 training 调试 testing")
        hits = idx.search("AI 训练 model testing", k=5)
        assert isinstance(hits, list)
        assert any(h["segment_id"] == "seg-cn-en" for h in hits), (
            f"中英混合 query 应命中 seg-cn-en,实际 {[h['segment_id'] for h in hits]}"
        )
    finally:
        conn.close()


def test_13_长段切块每块独立encode(mtca_db):
    """长文本(> 1500 中文字)按段落切,每块独立 encode → Ollama 调 ≥ 2 次"""
    conn, idx = _setup(mtca_db)
    try:
        before = idx.ollama_call_count()
        # 构造 ~3200 字的长文本（确保 > 1500）
        long_text = ("今天天气不错,我们讨论一下搬家的事情安排。" * 100)
        chunks = idx._chunk_long_text(long_text, max_chars=1500)
        assert len(chunks) > 1, f"长段应切多块（>1）,实际 {len(chunks)}"
        for i, c in enumerate(chunks):
            assert len(c) <= 1500, f"第 {i} 块超长 {len(c)} > 1500"
        idx.index_segment("seg-long", long_text)
        after = idx.ollama_call_count()
        assert after - before >= len(chunks), (
            f"Ollama 调用应 ≥ {len(chunks)},实际 {after - before}"
        )
    finally:
        conn.close()