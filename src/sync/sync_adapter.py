"""SyncAdapter 协议：所有云同步实现的统一接口。

任何想接入 MTCA 同步管道的实现（Dropbox / iCloud / 自建 CRDT / S3），
只需实现以下 3 个方法，并通过 ``src.sync.set_adapter()`` 注册即可。

设计意图：
- ``push(data)``：把本地数据推到云端；返回远端 id（用于追溯）。
- ``pull(since_ms)``：拉取远端数据；since_ms=0 表示全量。
- ``status()``：健康检查；返回 dict 含 mode / last_sync_ms / pending / errors。

用 ``@runtime_checkable`` 装饰后，第三方实现可用 ``isinstance(obj, SyncAdapter)`` 
做结构检查（无需显式注册）。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SyncAdapter(Protocol):
    """同步适配器接口。

    所有云同步实现（Dropbox / iCloud / 自建 CRDT）都实现这个接口。
    ``LocalOnlySync`` 是默认 no-op 实现，本地最权威。
    """

    def push(self, data: dict) -> str:
        """把一条数据推到云端；返回远端 id。"""
        ...

    def pull(self, since_ms: int = 0) -> list[dict]:
        """拉取 ``since_ms`` 之后的远端数据。since_ms=0 表示全量。"""
        ...

    def status(self) -> dict:
        """返回同步状态 dict，至少含 mode/last_sync_ms/pending/errors 4 字段。"""
        ...