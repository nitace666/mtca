"""LocalOnlySync：本地唯一同步 no-op（默认实现）。

设计意图：
- **本地最权威**：永远不发数据，也不期待云端数据。
- push 是 no-op：仅原样返回 ``data["id"]``（便于调试：返回值 = 已知本地 id）。
- pull 永远返回 ``[]``（本地没有"远端数据"概念）。
- status 报告模式 ``local_only``，方便运维区分。

为什么不是空类？因为 ``@runtime_checkable`` Protocol 要求结构完整，
空类无法通过 ``isinstance()`` 检查。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from src.store.sqlite import MTCA_DB_PATH


class LocalOnlySync:
    """本地唯一同步 no-op：永远不发数据，本地已是最权威。"""

    def __init__(self, db_path: Optional[Union[Path, str]] = None) -> None:
        # db_path 字段保留以便未来实现真同步时复用 LocalOnlySync 作 fallback
        self.db_path: Path = Path(db_path) if db_path is not None else MTCA_DB_PATH

    def push(self, data: dict) -> str:
        """no-op：原样返回 ``data["id"]``（缺失则返回 ``""``）。"""
        return data.get("id", "")

    def pull(self, since_ms: int = 0) -> list[dict]:
        """永远返回 ``[]``（本地无远端数据概念）。"""
        return []

    def status(self) -> dict:
        """返回 4 字段状态 dict。"""
        return {
            "mode": "local_only",
            "last_sync_ms": None,
            "pending": 0,
            "errors": [],
        }