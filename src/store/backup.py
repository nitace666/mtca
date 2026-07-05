"""src/store/backup.py — T33 自动备份模块。

启动时复制 mtca.db → backups/mtca-YYYYMMDD-HHMMSS.db，
清理 > keep_days 天的旧备份。
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from src.store.sqlite import MTCA_DB_PATH


DEFAULT_KEEP_DAYS: int = 7
BACKUP_DIR_NAME: str = "backups"


def backup_now(
    path: Optional[Union[Path, str]] = None,
    backup_dir: Optional[Path] = None,
    keep_days: int = DEFAULT_KEEP_DAYS,
) -> Path:
    """复制 db 到 backup_dir/mtca-YYYYMMDD-HHMMSS.db。
    清理 > keep_days 天的旧备份。
    返回本次备份文件路径。
    """
    src = Path(path) if path else Path(MTCA_DB_PATH)
    if not src.exists():
        raise FileNotFoundError(f"db 不存在：{src}")
    bdir = Path(backup_dir) if backup_dir else src.parent / BACKUP_DIR_NAME
    bdir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"mtca-{ts}.db"
    # T33_fix: 处理同秒内多次调用的文件名冲突（追加 -N 计数器）
    n = 1
    while dest.exists():
        dest = bdir / f"mtca-{ts}-{n}.db"
        n += 1
    shutil.copy2(src, dest)

    cutoff = time.time() - keep_days * 86400
    cleaned = 0
    for old in bdir.glob("mtca-*.db"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
                cleaned += 1
        except OSError:
            pass

    return dest


def list_backups(backup_dir: Optional[Path] = None) -> list[Path]:
    """列出当前所有备份，按 mtime 降序。"""
    src = Path(MTCA_DB_PATH)
    bdir = Path(backup_dir) if backup_dir else src.parent / BACKUP_DIR_NAME
    if not bdir.exists():
        return []
    return sorted(bdir.glob("mtca-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)


__all__ = ["backup_now", "list_backups", "DEFAULT_KEEP_DAYS", "BACKUP_DIR_NAME"]
