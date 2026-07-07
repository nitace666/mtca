# -*- coding: utf-8 -*-
"""tests/test_fts5_trigram.py -- Step 8 bugfix：B2 FTS5 中文 token 化失效

Bug 2 验证：
- 当前 messages_fts 用 tokenize='unicode61'，中文不分词，整段当 1 token，query 必须完全匹配
- Step 8 修复：v4 migration 把 messages_fts 改 tokenize='trigram'（FTS5 内置三字符索引）
- 验证：中文 query ≥ 3 字符命中 ≥ 1，重复 init_db 幂等（不重复 rebuild）

测试设计：
- mtca_db fixture 已调 init_db 一次，自动触发 v4 migration
- 直接验证 messages_fts tokenize 参数 + 中文 MATCH 行为
- 不依赖任何 FTS5 unicode61 行为（即便 baseline 测试用过 unicode61，mtca_db fresh 时已是 trigram）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _read_tokenize(db_path: Path) -> str:
    """读 messages_fts 的 CREATE TABLE SQL，提取 tokenize 参数。

    简化策略：直接搜 'unicode61' 或 'trigram' 字符串。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone()
        if row is None or not row[0]:
            return "<none>"
        sql = row[0]
        if "trigram" in sql:
            return "trigram"
        if "unicode61" in sql:
            return "unicode61"
        return "<unknown>"
    finally:
        conn.close()


def test_messages_fts_用trigram_tokenize(mtca_db: Path) -> None:
    """B2 验证：init_db 后 messages_fts tokenize='trigram'（v4 migration 已触发）。

    当前实现（修前）：tokenize='unicode61'，中文不分词，整段当 1 token，命中率 ~0
    修后：tokenize='trigram'（FTS5 内置），中文按 3 字符 trigram 索引，query ≥ 3 字符命中
    """
    tokenize = _read_tokenize(mtca_db)
    assert tokenize == "trigram", (
        f"messages_fts 应已升级到 tokenize='trigram'（Step 8 B2 修复），"
        f" 实际 tokenize={tokenize!r}"
    )


def test_中文query_trigram命中(mtca_db: Path) -> None:
    """B2 验证：中文 query ≥ 3 字符 MATCH 命中 ≥ 1。

    unicode61 时代："搬家经验上次三箱衣服" 整段当 1 token，query "搬家经验上次"
    必须完全匹配整段 content 才命中，命中率 ~0。
    trigram 时代：content 按 3 字符 trigrams 索引，query "搬家经验上次" 切 trigrams
    含 content 的 trigrams，命中 ≥ 1。
    """
    conn = sqlite3.connect(str(mtca_db))
    try:
        # 灌 1 条消息（trigger 自动同步 messages_fts）
        conn.execute(
            "INSERT INTO messages(message_id, session_id, seq, role, content, created_at) "
            "VALUES (?, ?, 1, 'user', ?, ?)",
            ("m-trigram-1", "s1", "搬家经验上次三箱衣服", 1700000000000),
        )
        conn.commit()

        # query ≥ 3 字符（trigram 下限）
        n = conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
            ("搬家经验",),
        ).fetchone()[0]
        assert n >= 1, (
            f"trigram 下中文 query '搬家经验'（4 字符 ≥ 3）应命中 ≥ 1，实际 {n}。"
            f" 若 tokenize='unicode61' 则整段当 1 token 不会命中。"
        )
    finally:
        conn.close()


def test_v4_migration_幂等(mtca_db: Path) -> None:
    """B2 验证：重复 init_db 不重复 rebuild（幂等）。

    init_db 多次调用：第二次应检测到 messages_fts 已是 trigram，不重复 DROP+CREATE+rebuild。
    """
    from src.store.sqlite import init_db
    # 灌点数据
    conn = sqlite3.connect(str(mtca_db))
    conn.execute(
        "INSERT INTO messages(message_id, session_id, seq, role, content, created_at) "
        "VALUES (?, ?, 1, 'user', ?, ?)",
        ("m-idem-1", "s1", "项目状态汇报测试数据", 1700000000000),
    )
    conn.commit()
    conn.close()

    # 第二次 init_db（应幂等）
    conn = init_db(str(mtca_db))
    conn.close()

    # 验证：内容仍在 + trigram 仍命中
    conn = sqlite3.connect(str(mtca_db))
    try:
        tokenize = _read_tokenize(mtca_db)
        assert tokenize == "trigram", (
            f"第二次 init_db 后 tokenize 应仍是 trigram（幂等），实际 {tokenize!r}"
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
            ("项目状态",),
        ).fetchone()[0]
        assert n >= 1, f"幂等重建后 query 应仍命中，实际 {n}"
    finally:
        conn.close()


def test_PRAGMA_user_version升级到4(mtca_db: Path) -> None:
    """B2 验证：init_db 后 PRAGMA user_version = 4（schema 版本标记）。

    v0~v3 migration 不动 user_version，v4 修后置 4。
    """
    conn = sqlite3.connect(str(mtca_db))
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 4, f"PRAGMA user_version 应 ≥ 4（v4 migration 已跑），实际 {ver}"
    finally:
        conn.close()
