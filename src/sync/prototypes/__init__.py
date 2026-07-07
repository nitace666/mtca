"""M3-0 sync 选型 spike —— 3 方案原型（仅 spike 用，不进生产）。

公共接口：
    LwwAdapter             方案 A：Last-Write-Wins（按 segment 行 + updated_at_ms）
    LitestreamLikeAdapter  方案 B：Litestream 思路（不真做 daemon，WAL frame 验证）
    CrdtSimpleAdapter      方案 C：简化 CRDT（字段级 timestamp merge）

    get_adapter(name)      工厂，按名字 'lww' | 'litestream' | 'crdt' 取类

设计约束：
    - 每个 adapter 都有 local_db + peer_db 两端视角
    - push 写 local；pull 从 peer 读再写 local
    - sidecar 表（sync_meta / sync_wal / sync_field_ts）首次 init() 时建
    - 不污染生产 segments 表（不在 segments 上加列）
    - 仅 spike 用；生产同步请走 src/sync/{local_only_sync,registry}.py

参考：
    - src/sync/sync_adapter.py Protocol（push/pull/status 三方法）
    - MANAGER_HANDOFF §12 M3-0 spike 任务
"""

from src.sync.prototypes.lww import LwwAdapter
from src.sync.prototypes.litestream_like import LitestreamLikeAdapter
from src.sync.prototypes.crdt_simple import CrdtSimpleAdapter


_ADAPTER_TABLE = {
    "lww": LwwAdapter,
    "litestream": LitestreamLikeAdapter,
    "crdt": CrdtSimpleAdapter,
}


def get_adapter(name: str):
    """按名字取 adapter 类（不实例化，spike 脚本传 db path 自行构造）。"""
    if name not in _ADAPTER_TABLE:
        raise ValueError(
            f"未知 adapter 名 '{name}'，可选：{sorted(_ADAPTER_TABLE.keys())}"
        )
    return _ADAPTER_TABLE[name]


__all__ = [
    "LwwAdapter",
    "LitestreamLikeAdapter",
    "CrdtSimpleAdapter",
    "get_adapter",
]
