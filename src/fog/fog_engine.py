"""雾化引擎 (v0.4)

实现 ``/雾化`` 命令的事务层入口：物理擦除 L0-细节、保留 L0-骨架、
写入审计事件。设计要点：

- 严格权限闸门：``called_by`` 仅接受 ``'user'``，其他值立即抛
  ``PermissionError``（AI 无 /雾化 权限）。
- 单事务原子性：5 步操作在同一 ``get_connection`` 上下文内执行，
  异常由 ``get_connection`` 自动回滚，确保「开启 fog 通道 → 物理
  擦除 messages → 标记段状态 → 写审计 → 关闭 fog 通道」要么全成功
  要么全失败。
- ``finally`` 防御性兜底：异常路径再次清理 ``fog_session``，防止
  fog 通道残留（即使事务回滚后通常已无残留，仍做二次保险）。
- 幂等：已 ``fog_state='fogged_once'`` 的段落直接返回 True，不重复
  擦除、不重复写审计。
- L0-骨架保留：本模块的 UPDATE **不触碰** ``fog_anchor`` 列，因此
  不会触发 ``no_update_skeleton`` 触发器，骨架的 ``topic_label`` /
  ``start_at`` / ``fog_anchor`` 三字段全部不变。用户提供的 ``anchor``
  锚点句保留至 ``score_events.reason`` 供事后追溯。
- 不可逆：被擦除的 ``messages.content`` 物理置 NULL，没有 /取消雾化
  / /恢复 命令。

公共 API：
- ``fog_segment(segment_id, anchor, called_by='user', path=None) -> bool``
- ``is_fogged(segment_id, path=None) -> bool``
- ``get_fog_anchor(segment_id, path=None) -> str | None``
- ``fog_session_tear_down(session_id, path=None) -> int``
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Union

from src.store.sqlite import get_connection, query


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 用户锚点句最大长度（与 skeleton.ANCHOR_MAX_LEN 对齐；DATA_MODEL §4）
ANCHOR_MAX_LEN: int = 20

# 合法调用方白名单（M1 阶段仅 user 允许调用雾化）
_VALID_CALLERS: frozenset[str] = frozenset({"user"})

# 审计 reason 前缀（写 score_events 时使用）
_AUDIT_REASON_PREFIX: str = "不可逆"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """返回当前毫秒时间戳（用于 fog_at / score_events.created_at）。"""
    return int(time.time() * 1000)


def _truncate_anchor(anchor: str) -> str:
    """截断 anchor 到 ``ANCHOR_MAX_LEN`` 字符；空值返回空串。"""
    if not anchor:
        return ""
    text = str(anchor).strip()
    if len(text) <= ANCHOR_MAX_LEN:
        return text
    return text[:ANCHOR_MAX_LEN]


def _load_segment(
    segment_id: str,
    path: Optional[Union[Path, str]],
) -> Optional[dict]:
    """按 segment_id 加载段落元信息；返回 ``None`` 表示段落不存在。

    选取的字段满足 fog_segment 全流程所需：``session_id``、
    ``start_msg_seq`` / ``end_msg_seq`` 用于限定物理擦除的 messages
    范围；``fog_state`` 用于幂等判定。
    """
    rows = query(
        "SELECT segment_id, session_id, start_msg_seq, end_msg_seq, "
        "fog_state, fog_anchor "
        "FROM segments WHERE segment_id = ?",
        (segment_id,),
        path=path,
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 异常清理入口
# ---------------------------------------------------------------------------


def fog_session_tear_down(
    session_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """强制清理 ``fog_session`` 控制表中的指定会话授权记录。

    用于异常路径的兜底清理；同一事务回滚后通常已无残留，本函数
    做二次保险，避免 fog 通道遗留。

    返回：
        被删除的行数（``>= 0``；失败时抛 ``RuntimeError``）。
    """
    try:
        with get_connection(path) as conn:
            cur = conn.execute(
                "DELETE FROM fog_session WHERE session_id = ?",
                (session_id,),
            )
            return int(cur.rowcount or 0)
    except RuntimeError:
        raise


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------


def is_fogged(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> bool:
    """判断指定段落是否已被雾化（``fog_state == 'fogged_once'``）。

    段落不存在返回 ``False``（与其他查询行为一致）。
    """
    rows = query(
        "SELECT fog_state FROM segments WHERE segment_id = ?",
        (segment_id,),
        path=path,
    )
    if not rows:
        return False
    return str(rows[0]["fog_state"]) == "fogged_once"


def get_fog_anchor(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> Optional[str]:
    """返回段落当前 ``fog_anchor`` 字段值；不存在或未设置返回 ``None``。

    注意：L0-骨架的 ``fog_anchor`` 由 ``no_update_skeleton`` 触发器
    保护，雾化操作不会改写它。本函数仅做只读查询。
    """
    rows = query(
        "SELECT fog_anchor FROM segments WHERE segment_id = ?",
        (segment_id,),
        path=path,
    )
    if not rows:
        return None
    return rows[0]["fog_anchor"]


# ---------------------------------------------------------------------------
# 雾化主入口
# ---------------------------------------------------------------------------


def fog_segment(
    segment_id: str,
    anchor: str,
    called_by: str = "user",
    path: Optional[Union[Path, str]] = None,
) -> bool:
    """对指定段落执行雾化操作（物理擦除 L0-细节，不可逆）。

    参数：
        segment_id: 目标段落 UUID（必须已存在）。
        anchor: 用户提供的锚点句；超过 ``ANCHOR_MAX_LEN`` 自动截断。
            最终写入 ``score_events.reason`` 供事后追溯。
        called_by: 调用方标识；仅 ``'user'`` 允许，其他值抛
            ``PermissionError``（AI 无 /雾化 权限）。
        path: 数据库路径；``None`` 用默认 ``MTCA_DB_PATH``。

    返回：
        ``True`` —— 雾化成功或段落已处于雾化态（幂等）。

    行为（单事务 + finally 清理）：
        1. 权限闸门：``called_by == 'user'``
        2. 参数校验：``segment_id``、``anchor`` 合法
        3. 加载段落（取 ``session_id`` + seq 范围）
        4. 幂等判定：已是 ``fogged_once`` → 直接 ``return True``
        5. 单事务内：
           a. ``INSERT INTO fog_session``（开启 fog 通道）
           b. ``UPDATE messages SET content=NULL, token_count=0``
              ``WHERE session_id=? AND seq BETWEEN ? AND ?``
           c. ``UPDATE segments SET fog_state='fogged_once', fog_at=?``
              （不触碰 ``fog_anchor`` 列，绕开骨架保护触发器）
           d. ``INSERT INTO score_events``（``event_type='user_fog'``）
           e. ``DELETE FROM fog_session``（关闭 fog 通道）
        6. 异常 → ``finally`` 二次清理 fog_session（防御性）

    异常：
        ``PermissionError``：``called_by != 'user'``。
        ``ValueError``：``segment_id`` / ``anchor`` 非法或段落不存在。
        ``RuntimeError``：底层数据库操作失败。
    """
    # ---- 1. 权限闸门 ----
    if called_by not in _VALID_CALLERS:
        raise PermissionError(
            f"AI 无 /雾化 权限（called_by={called_by!r}）"
        )

    # ---- 2. 参数校验 ----
    if not isinstance(segment_id, str) or not segment_id.strip():
        raise ValueError("segment_id 必须是非空字符串")
    if not isinstance(anchor, str):
        raise ValueError("anchor 必须是字符串")
    safe_anchor = _truncate_anchor(anchor)
    if not safe_anchor:
        raise ValueError("anchor 截断后为空，请提供有效内容")

    # ---- 3. 加载段落 ----
    seg = _load_segment(segment_id, path=path)
    if seg is None:
        raise ValueError(f"段落不存在：segment_id={segment_id!r}")

    # ---- 4. 幂等：已雾化直接返回 ----
    if str(seg["fog_state"]) == "fogged_once":
        return True

    session_id = str(seg["session_id"])
    start_seq = int(seg["start_msg_seq"])
    end_seq = int(seg["end_msg_seq"])
    now_ms = _now_ms()

    # ---- 5. 单事务：5 步操作原子完成 ----
    try:
        with get_connection(path) as conn:
            # 5a) 开启 fog 通道（触发器将放行后续 UPDATE messages）
            conn.execute(
                "INSERT INTO fog_session "
                "(session_id, enabled_at, enabled_by) "
                "VALUES (?, ?, ?)",
                (session_id, now_ms, "user"),
            )

            # 5b) 物理擦除 L0-细节（content / token_count 双清零）
            conn.execute(
                "UPDATE messages "
                "SET content = NULL, token_count = 0 "
                "WHERE session_id = ? AND seq BETWEEN ? AND ?",
                (session_id, start_seq, end_seq),
            )

            # 5c) 标记雾化状态。
            #     注意：不触碰 fog_anchor 列，保留 L0-骨架
            #     （no_update_skeleton 触发器保护 topic_label /
            #      start_at / fog_anchor 三字段不可变；本 UPDATE
            #      只 SET fog_state + fog_at，不触发 RAISE(ABORT)）
            conn.execute(
                "UPDATE segments "
                "SET fog_state = 'fogged_once', fog_at = ? "
                "WHERE segment_id = ?",
                (now_ms, segment_id),
            )

            # 5d) 审计：user_fog 事件（reason 含锚点句便于追溯）
            conn.execute(
                "INSERT INTO score_events "
                "(segment_id, event_type, delta, old_score, "
                "new_score, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    segment_id,
                    "user_fog",
                    0.0,
                    None,
                    None,
                    f"{_AUDIT_REASON_PREFIX}: {safe_anchor}",
                    now_ms,
                ),
            )

            # 5e) 关闭 fog 通道（在 commit 前释放权限）
            conn.execute(
                "DELETE FROM fog_session WHERE session_id = ?",
                (session_id,),
            )

            # get_connection 上下文退出时自动 commit
    except Exception:
        # ---- 6. 异常兜底清理 ----
        # get_connection 已回滚事务，fog_session 通常已空；
        # 二次 DELETE 仅作防御，避免通道残留导致后续 UPDATE 误放行
        try:
            fog_session_tear_down(session_id, path=path)
        except RuntimeError:
            # 清理失败不应掩盖原始异常
            pass
        raise

    return True


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = [
    "ANCHOR_MAX_LEN",
    "fog_segment",
    "is_fogged",
    "get_fog_anchor",
    "fog_session_tear_down",
]
