"""src.sync：MTCA 云同步接口预留（M2.5.8 B）。

本模块是**未来云同步的插座**：当前只暴露 ``SyncAdapter`` 协议 + 
``LocalOnlySync`` 默认 no-op + ``registry`` 全局注册表。
云同步实现（Dropbox / iCloud / 自建 CRDT）只需实现 ``SyncAdapter`` 协议，
调用 ``set_adapter()`` 即可接入，核心代码无需改动。

设计原则：
- **本地最权威**：默认 ``LocalOnlySync`` 永远不发数据，仅占位。
- **失败不抛**：`sync_X_to_adapter()` 系列函数异常时仅 warning + 返回 ""，
  绝不阻塞本地操作。
- **线程安全**：注册表用 ``threading.Lock`` 保护，懒初始化。
"""
from __future__ import annotations

from .local_only_sync import LocalOnlySync
from .registry import get_adapter, reset_adapter, set_adapter
from .sync_adapter import SyncAdapter

__all__ = [
    "SyncAdapter",
    "LocalOnlySync",
    "get_adapter",
    "set_adapter",
    "reset_adapter",
]

# ---------------------------------------------------------------------------
# M3-0 spike：3 方案 sync 选型原型（仅 spike 用，不进生产）
# ---------------------------------------------------------------------------
# - 路径：src.sync.prototypes.{lww, litestream_like, crdt_simple}
# - 公共入口：src.sync.prototypes.get_adapter(name)
# - 注意：prototypes 仅 spike 验证用，生产同步实现请走 SyncAdapter 协议
