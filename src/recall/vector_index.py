# -*- coding: utf-8 -*-
"""src/recall/vector_index.py -- M3-5 vector recall layer (Ollama nomic-embed + ChromaDB).

設设原则:
- 单例模式（模块级 get_default_index()）
- 懒加载 ChromaDB client + collection
- 默认 vector_recall_enabled = False
- 任意异常 -> fallback FTS5 不中断主流程
- Sidecar SQLite 缓存 (vector_cache 表)
- 单文件 <= 800 行

依赖:
- Ollama http://localhost:11434 (nomic-embed-text, 768 维)
- ChromaDB PersistentClient, path = ~/.mtca/vector_db
- SQLite vector_cache 表（v3 migration）
"""

from __future__ import annotations

import json
import sqlite3
import struct
import time
import urllib.error
import urllib.request

import chromadb
from pathlib import Path
from typing import Any, Callable, Optional


# === 常量 ===

EMBED_MODEL: str = "nomic-embed-text"
EMBED_DIM: int = 768
MAX_CHARS_PER_CHUNK: int = 1500
OLLAMA_URL: str = "http://localhost:11434/api/embeddings"
OLLAMA_TIMEOUT_SEC: float = 5.0
COLLECTION_NAME: str = "mtca_segments"
SETTING_KEY: str = "vector_recall_enabled"
DEFAULT_MODEL_HASH: str = "nomic-embed-text-v1"
DEFAULT_DB_PATH: Path = Path.home() / ".mtca" / "vector_db"


# === HTTP 工具 ===

def _ollama_post(prompt: str) -> list[float]:
    """调 Ollama HTTP API 返回 768 维向量。失败抛出。"""
    body = json.dumps({"model": EMBED_MODEL, "prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SEC) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    emb = payload.get("embedding")
    if not isinstance(emb, list) or len(emb) != EMBED_DIM:
        raise RuntimeError(
            f"Ollama embedding 维度异常：期望 {EMBED_DIM}, 实际 "
            f"{len(emb) if isinstance(emb, list) else type(emb).__name__}"
        )
    return [float(x) for x in emb]


# === 切块工具 ===

def _chunk_long_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    """长文本按段落切块，每块 <= max_chars。"""
    if len(text) <= max_chars:
        return [text]
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return [text]
    chunks: list[str] = []
    cur = ""
    for p in paragraphs:
        if len(p) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(p), max_chars):
                chunks.append(p[i:i + max_chars])
            continue
        if len(cur) + len(p) + 2 > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks


# === Sidecar cache 序列化 ===

def _pack_embedding(emb: list[float]) -> bytes:
    """768 floats -> BLOB (little-endian float32)"""
    return struct.pack(f"<{len(emb)}f", *emb)


def _unpack_embedding(blob: bytes) -> list[float]:
    """BLOB -> list[float]"""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# === settings 表读取 ===

def _read_setting(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """读 settings 表某 key 的 value；无 key 返 None。"""
    cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None:
        return None
    return row["value"] if hasattr(row, "keys") else row[0]


# === VectorIndex 主类 ===

class VectorIndex:
    """M3-5 向量召回层。

    用法:
        idx = VectorIndex(mtca_conn=conn)
        idx.is_enabled()
        idx.index_segment("seg-1", text)
        hits = idx.search(query, k=10)

    异常策略：search / index_segment 内部 catch 所有异常并 fallback。
    """

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        mtca_conn: Optional[sqlite3.Connection] = None,
        ollama_fn: Callable[[str], list[float]] = _ollama_post,
    ) -> None:
        """构造 VectorIndex。"""
        self._db_path = Path(db_path)
        self._mtca_conn = mtca_conn
        self._ollama = ollama_fn
        self._client: Optional[Any] = None
        self._collection: Optional[Any] = None
        self._ollama_calls: int = 0

    def ollama_call_count(self) -> int:
        """返回本实例累计 Ollama 调用次数。"""
        return self._ollama_calls

    def is_enabled(self) -> bool:
        """读 settings 表 vector_recall_enabled。"1" = True，其他 = False。"""
        if self._mtca_conn is None:
            return False
        v = _read_setting(self._mtca_conn, SETTING_KEY)
        return v == "1"

    def _ensure_client(self) -> None:
        """懒加载 ChromaDB PersistentClient + collection。"""
        if self._client is not None:
            return
        self._db_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._db_path))
        self._collection = self._client.get_or_create_collection(COLLECTION_NAME)

    def encode(self, text: str) -> list[float]:
        """调 ollama_fn 返回 768 维向量。失败 raise。成功后才 +1 _ollama_calls。"""
        emb = self._ollama(text)
        if not isinstance(emb, list) or len(emb) != EMBED_DIM:
            raise RuntimeError(
                f"embedding 维度异常：期望 {EMBED_DIM}, 实际 "
                f"{len(emb) if isinstance(emb, list) else type(emb).__name__}"
            )
        self._ollama_calls += 1
        return emb

    @staticmethod
    def _chunk_long_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
        """实例方法版切块（测试用 idx._chunk_long_text 调用）。"""
        return _chunk_long_text(text, max_chars)

    def _cache_get(
        self, segment_id: str, chunk_idx: int, model_hash: str
    ) -> Optional[list[float]]:
        """读 sidecar cache；无 conn 或无 row -> None。"""
        if self._mtca_conn is None:
            return None
        cur = self._mtca_conn.execute(
            "SELECT embedding FROM vector_cache "
            "WHERE segment_id = ? AND chunk_idx = ? AND model_hash = ?",
            (segment_id, chunk_idx, model_hash),
        )
        row = cur.fetchone()
        if row is None:
            return None
        blob = row["embedding"] if hasattr(row, "keys") else row[0]
        return _unpack_embedding(blob)

    def _cache_put(
        self, segment_id: str, chunk_idx: int, model_hash: str, emb: list[float]
    ) -> None:
        """写 sidecar cache（INSERT OR REPLACE 幂等）。"""
        if self._mtca_conn is None:
            return
        blob = _pack_embedding(emb)
        ts = int(time.time() * 1000)
        self._mtca_conn.execute(
            "INSERT OR REPLACE INTO vector_cache"
            "(segment_id, chunk_idx, embedding, model_hash, ts) "
            "VALUES (?,?,?,?,?)",
            (segment_id, chunk_idx, blob, model_hash, ts),
        )

    def index_segment(
        self,
        segment_id: str,
        text: str,
        model_hash: str = DEFAULT_MODEL_HASH,
    ) -> int:
        """索引一段文本：切块 + encode + cache + ChromaDB upsert。返回 chunk 数。"""
        chunks = _chunk_long_text(text)
        embeddings: list[list[float]] = []
        for i, chunk in enumerate(chunks):
            emb = self._cache_get(segment_id, i, model_hash)
            if emb is None:
                emb = self.encode(chunk)
                self._cache_put(segment_id, i, model_hash, emb)
            embeddings.append(emb)
        self._ensure_client()
        ids = [f"{segment_id}_{i}" for i in range(len(chunks))]
        metadatas = [{"segment_id": segment_id, "chunk_idx": i} for i in range(len(chunks))]
        self._collection.upsert(
            ids=ids, embeddings=embeddings, documents=chunks, metadatas=metadatas
        )
        return len(chunks)

    def search(self, query: str, k: int = 10, path=None) -> list[dict]:
        """语义搜索。空 query / 未启用 -> []；异常 -> fallback FTS5。"""
        if not query or not query.strip():
            return []
        if not self.is_enabled():
            return []
        try:
            q_emb = self.encode(query)
            self._ensure_client()
            res = self._collection.query(query_embeddings=[q_emb], n_results=k)
        except Exception:
            return self._fallback_fts5(query, k, path)
        ids_list = res.get("ids", [[]])[0]
        dists_list = res.get("distances", [[]])[0]
        metas_list = res.get("metadatas", [[]])[0]
        best: dict[str, dict] = {}
        for cid, dist, meta in zip(ids_list, dists_list, metas_list):
            seg_id = (meta or {}).get("segment_id")
            if seg_id is None and isinstance(cid, str) and "_" in cid:
                seg_id = cid.rsplit("_", 1)[0]
            elif seg_id is None:
                seg_id = cid if isinstance(cid, str) else str(cid)
            score = 1.0 - float(dist)
            cur = best.get(seg_id)
            if cur is None or score > cur["score"]:
                best[seg_id] = {
                    "segment_id": seg_id,
                    "score": score,
                    "chunk_idx": -1,
                }
        return list(best.values())

    def _fallback_fts5(self, query: str, k: int, path=None) -> list[dict]:
        """兜底走现有 src.recall.recall_engine.search_segments。任何异常返 []。"""
        try:
            from src.recall.recall_engine import search_segments
            if path is None:
                return []
            rows = search_segments(query, top_k=k, path=path)
        except Exception:
            return []
        out: list[dict] = []
        for r in rows or []:
            sid = r.get("segment_id", "")
            score = r.get("current_score", 0.0)
            out.append(
                {"segment_id": sid, "score": float(score), "chunk_idx": -1}
            )
        return out


# === 模块级单例 helper ===

_default_index: Optional[VectorIndex] = None


def get_default_index() -> Optional[VectorIndex]:
    """返回模块级单例（懒加载）。"""
    global _default_index
    if _default_index is not None:
        return _default_index
    _default_index = VectorIndex()
    return _default_index
