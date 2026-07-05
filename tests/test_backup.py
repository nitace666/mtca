"""tests/test_backup.py — T33 自动备份测试。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.store.sqlite import init_db
from src.store.backup import backup_now, list_backups, DEFAULT_KEEP_DAYS


def test_backup_creates_file_copy_of_db(tmp_path: Path) -> None:
    """backup_now 复制 db 到 backup_dir，文件内容字节级一致。"""
    db = tmp_path / "mtca.db"
    init_db(db)
    bdir = tmp_path / "backups"
    result = backup_now(path=db, backup_dir=bdir)
    assert result.exists()
    assert result.read_bytes() == db.read_bytes()
    assert result.parent == bdir


def test_backup_filename_includes_timestamp(tmp_path: Path) -> None:
    """文件名形如 mtca-YYYYMMDD-HHMMSS.db。"""
    db = tmp_path / "mtca.db"
    init_db(db)
    result = backup_now(path=db, backup_dir=tmp_path / "b")
    assert result.name.startswith("mtca-")
    # 8 位日期 + - + 6 位时间 + .db
    assert len(result.name) == len("mtca-YYYYMMDD-HHMMSS.db")


def test_backup_cleans_old_files(tmp_path: Path) -> None:
    """> keep_days 天的备份被清掉，新的保留。"""
    db = tmp_path / "mtca.db"
    init_db(db)
    bdir = tmp_path / "backups"
    bdir.mkdir(parents=True, exist_ok=True)

    # 创建"老"备份（修改 mtime 到 10 天前）
    old = bdir / "mtca-20200101-000000.db"
    old.write_bytes(b"old")
    old_time = time.time() - 10 * 86400
    import os
    os.utime(old, (old_time, old_time))

    # 创建"新"备份
    fresh_time = time.time() - 1 * 86400
    fresh = bdir / "mtca-20250101-000000.db"
    fresh.write_bytes(b"fresh")
    os.utime(fresh, (fresh_time, fresh_time))

    backup_now(path=db, backup_dir=bdir, keep_days=7)

    assert not old.exists(), "老备份（10 天前）应被清"
    assert fresh.exists(), "新备份（1 天前）应保留"


def test_list_backups_returns_sorted(tmp_path: Path) -> None:
    """list_backups 按 mtime 降序排。"""
    db = tmp_path / "mtca.db"
    init_db(db)
    bdir = tmp_path / "backups"

    backup_now(path=db, backup_dir=bdir)
    time.sleep(0.05)
    backup_now(path=db, backup_dir=bdir)
    time.sleep(0.05)
    backup_now(path=db, backup_dir=bdir)

    backups = list_backups(backup_dir=bdir)
    assert len(backups) == 3
    # 最新的在最前
    for i in range(len(backups) - 1):
        assert backups[i].stat().st_mtime >= backups[i + 1].stat().st_mtime


def test_backup_raises_for_missing_db(tmp_path: Path) -> None:
    """db 不存在时抛 FileNotFoundError（不静默错）。"""
    with pytest.raises(FileNotFoundError):
        backup_now(path=tmp_path / "nonexistent.db", backup_dir=tmp_path / "b")


def test_backup_creates_backup_dir_if_missing(tmp_path: Path) -> None:
    """backup_dir 不存在时自动创建。"""
    db = tmp_path / "mtca.db"
    init_db(db)
    bdir = tmp_path / "deep" / "nested" / "backups"
    assert not bdir.exists()
    result = backup_now(path=db, backup_dir=bdir)
    assert result.exists()
