"""全局 SyncAdapter 注册表（懒初始化 + 线程安全）。

提供 3 个函数：
- ``get_adapter()``：首次调用时实例化 ``LocalOnlySync()``，之后复用。
- ``set_adapter(adapter)``：替换全局适配器（用于接入真云同步实现）。
- ``reset_adapter()``：清空全局适配器（测试 / 热重载场景）。

并发：用 ``threading.Lock`` 保护读写。``get_adapter`` 的快路径
（已初始化）只需要读锁；当前实现用同一把锁，简单优先。

注意：本模块不导入 ``LocalOnlySync`` 在模块顶层——``set_adapter`` 调用方
负责确保传入对象满足 ``SyncAdapter`` 协议（``LocalOnlySync`` 是默认）。
但为方便 ``get_adapter`` 的懒初始化，本模块确实 import 了它。
"""
from __future__ import annotations

import threading
from typing import Optional

from .local_only_sync import LocalOnlySync
from .sync_adapter import SyncAdapter

_lock = threading.Lock()
_global_adapter: Optional[SyncAdapter] = None


def get_adapter() -> SyncAdapter:
    """获取全局 SyncAdapter 实例。未初始化时自动建一个 ``LocalOnlySync``。"""
    with _lock:
        global _global_adapter
        if _global_adapter is None:
            _global_adapter = LocalOnlySync()
        return _global_adapter


def set_adapter(adapter: SyncAdapter) -> None:
    """替换全局 SyncAdapter。调用方负责确保 adapter 满足 ``SyncAdapter`` 协议。"""
    with _lock:
        global _global_adapter
        _global_adapter = adapter


def reset_adapter() -> None:
    """清空全局 SyncAdapter。下次 ``get_adapter()`` 会重新建一个 ``LocalOnlySync``。"""
    with _lock:
        global _global_adapter
        _global_adapter = None