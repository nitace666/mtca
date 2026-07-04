"""pytest 全局 fixture：临时数据库 + MTCA 初始化连接。

设计：
- ``tmp_db``：每次测试返回 tmp_path 下独立的文件数据库路径，测试间隔离。
- ``mtca_db``：基于 ``tmp_db`` 初始化 schema，返回路径供被测函数使用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.store.sqlite import init_db


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """返回 tmp_path 下唯一的临时数据库路径（文件 DB，跨连接共享状态）。"""
    db_path = tmp_path / "mtca_test.db"
    return db_path


@pytest.fixture
def mtca_db(tmp_db: Path) -> Path:
    """已应用 PRAGMA + 已创建 schema 的临时数据库路径。"""
    conn = init_db(tmp_db)
    conn.close()
    return tmp_db