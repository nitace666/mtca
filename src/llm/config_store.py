"""LLM 配置持久化（DB settings 表 ↔ TOML 双向同步）。

设计：
- SQLite 是 source of truth（db_set_setting 写入立刻生效）
- TOML 文件（``~/.mtca/config.toml``）是缓存/导出，可手工编辑
- 嵌套字典在 DB 里用点分键存储（如 ``lmstudio.base_url``）
- 同步策略：DB 优先 → TOML 备份

公共 API：
- ``db_set_setting(key, value, path=None)``
- ``db_get_setting(key, path=None) -> str | None``
- ``db_list_settings(path=None) -> list[tuple[str, str]]``
- ``db_delete_setting(key, path=None) -> bool``
- ``toml_load(path=None) -> dict``
- ``toml_save(cfg, path=None) -> None``
- ``sync_db_to_toml(path=None, toml_path=None)``
- ``sync_toml_to_db(path=None, toml_path=None)``
- ``load_config(path=None, toml_path=None) -> dict``
- ``get_active_config(path=None, toml_path=None) -> (dict, source)``
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional, Union

from src.store.sqlite import get_connection, query

try:
    import tomllib as _toml_read
except ImportError:  # pragma: no cover
    import tomli as _toml_read  # type: ignore[import-untyped]

try:
    import tomllib as _toml_write
except ImportError:
    try:
        import tomli_w as _toml_write  # type: ignore[import-untyped]
    except ImportError:
        _toml_write = None  # type: ignore[assignment]


MTCA_CONFIG_PATH: Path = Path.home() / ".mtca" / "config.toml"


# ---------------------------------------------------------------------------
# DB settings 表 CRUD
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def db_set_setting(
    key: str,
    value: str,
    path: Optional[Union[Path, str]] = None,
) -> None:
    """写入一条 setting（key 存在则覆盖）。

    value 必须是字符串；调用方负责序列化（int/bool/dict → str）。
    """
    if not isinstance(key, str) or not key:
        raise ValueError("key 必须是非空字符串")
    if not isinstance(value, str):
        raise ValueError("value 必须是字符串（调用方负责序列化）")
    now = _now_ms()
    with get_connection(path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, now),
        )


def db_get_setting(
    key: str,
    path: Optional[Union[Path, str]] = None,
) -> Optional[str]:
    """按 key 读取 setting；不存在返回 None。"""
    rows = query(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
        path=path,
    )
    return rows[0]["value"] if rows else None


def db_list_settings(
    path: Optional[Union[Path, str]] = None,
) -> list[tuple[str, str]]:
    """返回全部 ``[(key, value), ...]``，按 key 排序。"""
    rows = query(
        "SELECT key, value FROM settings ORDER BY key ASC",
        path=path,
    )
    return [(r["key"], r["value"]) for r in rows]


def db_delete_setting(
    key: str,
    path: Optional[Union[Path, str]] = None,
) -> bool:
    """删除一条 setting；存在并删除返回 True，否则 False。"""
    with get_connection(path) as conn:
        cur = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return int(cur.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# TOML 读写
# ---------------------------------------------------------------------------


def toml_load(path: Optional[Union[Path, str]] = None) -> dict:
    """加载 TOML 配置；文件不存在或为空返回 {}。"""
    cfg_path = Path(path) if path is not None else MTCA_CONFIG_PATH
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "rb") as f:
            return _toml_read.load(f)
    except (OSError, ValueError):
        return {}


def toml_save(
    cfg: dict,
    path: Optional[Union[Path, str]] = None,
) -> None:
    """写 TOML 配置。"""
    cfg_path = Path(path) if path is not None else MTCA_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if _toml_write is None:
        raise RuntimeError("TOML 写入需要 Python 3.11+ 或安装 tomli_w")
    with open(cfg_path, "wb") as f:
        _toml_write.dump(cfg, f)


# ---------------------------------------------------------------------------
# 点分键 ↔ 嵌套字典
# ---------------------------------------------------------------------------


def _flatten(d: dict, prefix: str = "") -> dict[str, str]:
    """嵌套 dict → 点分键 dict（值统一 str）。"""
    out: dict[str, str] = {}
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, full))
        else:
            out[full] = "" if v is None else str(v)
    return out


def _unflatten(d: dict[str, str]) -> dict:
    """点分键 dict → 嵌套 dict（值仍是 str）。"""
    out: dict = {}
    for key, val in d.items():
        parts = key.split(".")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
            if not isinstance(cur, dict):
                cur = {}
        cur[parts[-1]] = val
    return out


# ---------------------------------------------------------------------------
# 同步
# ---------------------------------------------------------------------------


def sync_db_to_toml(
    path: Optional[Union[Path, str]] = None,
    toml_path: Optional[Union[Path, str]] = None,
) -> dict:
    """把 DB 全部 settings 写成 TOML；返回写入的 cfg。"""
    rows = db_list_settings(path=path)
    flat = {k: v for k, v in rows}
    cfg = _unflatten(flat)
    toml_save(cfg, path=toml_path)
    return cfg


def sync_toml_to_db(
    path: Optional[Union[Path, str]] = None,
    toml_path: Optional[Union[Path, str]] = None,
) -> int:
    """从 TOML 加载嵌套 cfg 并写入 DB；返回写入的条目数。

    DB 已存在的 key 会被 TOML 覆盖（sync_toml_to_db 假设 DB 为空或接受覆盖）。
    """
    cfg = toml_load(path=toml_path)
    flat = _flatten(cfg)
    for k, v in flat.items():
        db_set_setting(k, v, path=path)
    return len(flat)


# ---------------------------------------------------------------------------
# 高级：load_config（DB 优先 + TOML 兜底 + DEFAULT 兜底）
# ---------------------------------------------------------------------------


def load_config(
    path: Optional[Union[Path, str]] = None,
    toml_path: Optional[Union[Path, str]] = None,
) -> dict:
    """加载最终生效配置。

    优先级：DB settings（点分键展开成嵌套 dict） > TOML 文件 > {}。

    调用方负责把结果与 DEFAULT_CONFIG 做 merge（保留 provider.py 的现有行为）。
    """
    from src.llm.provider import DEFAULT_CONFIG

    rows = db_list_settings(path=path)
    if rows:
        flat = {k: v for k, v in rows}
        cfg = _unflatten(flat)
        # 与 DEFAULT_CONFIG 深合并：DB/TOML 值优先
        return _deep_merge(DEFAULT_CONFIG, cfg)

    cfg = toml_load(path=toml_path)
    if cfg:
        return _deep_merge(DEFAULT_CONFIG, cfg)
    return {k: dict(v) for k, v in DEFAULT_CONFIG.items()}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """deep merge：overlay 覆盖 base（递归字典层级）。"""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for k, v in overlay.items():
        if (
            k in out
            and isinstance(out[k], dict)
            and isinstance(v, dict)
        ):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = dict(v) if isinstance(v, dict) else v
    return out


def get_active_config(
    path: Optional[Union[Path, str]] = None,
    toml_path: Optional[Union[Path, str]] = None,
) -> tuple[dict, str]:
    """返回 ``(cfg, source)``，source ∈ {``"db"``, ``"toml"``, ``"default"``}。

    供 GUI / CLI 提示用户"配置从哪里来"。
    """
    from src.llm.provider import DEFAULT_CONFIG

    rows = db_list_settings(path=path)
    if rows:
        flat = {k: v for k, v in rows}
        cfg = _unflatten(flat)
        return _deep_merge(DEFAULT_CONFIG, cfg), "db"

    cfg = toml_load(path=toml_path)
    if cfg:
        return _deep_merge(DEFAULT_CONFIG, cfg), "toml"

    return {k: dict(v) for k, v in DEFAULT_CONFIG.items()}, "default"


__all__ = [
    "MTCA_CONFIG_PATH",
    "db_set_setting",
    "db_get_setting",
    "db_list_settings",
    "db_delete_setting",
    "toml_load",
    "toml_save",
    "sync_db_to_toml",
    "sync_toml_to_db",
    "load_config",
    "get_active_config",
]
