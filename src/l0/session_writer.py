"""L0-细节 消息写入（v0.4）

本模块负责把 Agent 适配器传来的对话消息写入 ``sessions`` 与 ``messages``
两张 L0-不可变表，并维护 ``sessions.message_count`` 与 ``token_estimate``
聚合统计字段。

设计要点：
- 所有写入通过 ``src.store.sqlite.get_connection`` 上下文管理器，自动
  commit / rollback。
- ``message_id`` 由 ``uuid.uuid4()`` 生成；``seq`` 在 ``get_connection``
  事务内根据当前会话最大 seq + 1 计算，保证并发安全。
- ``token_count`` 用 ``len(content) // 4`` 估算（M1 精度够用；M3 接入
  tokenizer 后替换）。
- ``tool_calls`` / ``tool_results`` 默认 None；若传入字典则序列化为
  JSON 字符串存储（messages 表对应列为 TEXT）。

公共 API：
- ``create_session(agent_source='user', topic_label=None, path=None) -> str``
- ``write_message(session_id, role, content, tool_calls=None,
  tool_results=None, path=None) -> str``
- ``get_session_messages(session_id, path=None) -> list[dict]``
- ``end_session(session_id, path=None) -> None``
- ``get_session(session_id, path=None) -> dict | None``

约束：
- L0 不可变：本模块只写入新行（INSERT），不更新 / 删除已有消息。
- 角色白名单：``role`` 必须是 ``user`` / ``assistant`` / ``tool`` /
  ``system`` 之一，其他值抛 ``ValueError``。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Union

from src.store.sqlite import get_connection, query

# ---------------------------------------------------------------------------
# 常量与类型
# ---------------------------------------------------------------------------

# 角色白名单（与 DEVELOPER_PLAN.md §2.2 messages.role 对齐）
_VALID_ROLES: frozenset[str] = frozenset({"user", "assistant", "tool", "system"})

# token 估算系数：1 token ≈ 4 字符（中英文混合粗估，M1 够用）
_TOKEN_PER_CHAR: int = 4

# 当前毫秒时间戳工具（便于测试 monkeypatch）
def _now_ms() -> int:
    """返回当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _serialize_json(value: Optional[Union[dict, list, str]]) -> Optional[str]:
    """序列化 tool_calls / tool_results 为 JSON 字符串。

    规则：
    - None → None（数据库存 NULL）
    - str → 原样返回（允许调用方已自行序列化）
    - dict / list → json.dumps(ensure_ascii=False)
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无法序列化为 JSON：{exc}") from exc


# ---------------------------------------------------------------------------
# 会话级：创建 / 查询 / 结束
# ---------------------------------------------------------------------------


def create_session(
    agent_source: str = "user",
    topic_label: Optional[str] = None,
    path: Optional[Union[Path, str]] = None,
) -> str:
    """创建一条新会话，返回 ``session_id``（UUID4 字符串）。

    参数：
        agent_source: 调用方标识（如 ``'openclaw'`` / ``'hermes'`` /
            ``'coze'`` / ``'user'``）；存入 ``sessions.agent_source``。
        topic_label: 初始话题标签；可选，``None`` 表示后续由
            LLM 异步生成或用户手动打标。
        path: 数据库路径；``None`` 时用 ``MTCA_DB_PATH``。

    返回：
        新生成的 ``session_id``（字符串形式的 UUID4）。

    异常：
        RuntimeError：底层数据库写入失败时抛出。
    """
    session_id = str(uuid.uuid4())
    started_at = _now_ms()
    try:
        with get_connection(path) as conn:
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, started_at, topic_label, agent_source) "
                "VALUES (?, ?, ?, ?)",
                (session_id, started_at, topic_label, agent_source),
            )
    except RuntimeError:
        # 上下文管理器已 rollback，原样抛出
        raise
    return session_id


def get_session(
    session_id: str,
    path: Optional[Union[Path, str]] = None,
) -> Optional[dict]:
    """按 session_id 查询会话元信息。

    返回 ``None`` 表示不存在；命中则返回包含全部列的 dict。
    """
    rows = query(
        "SELECT * FROM sessions WHERE session_id = ?",
        (session_id,),
        path=path,
    )
    return rows[0] if rows else None


def end_session(
    session_id: str,
    path: Optional[Union[Path, str]] = None,
) -> None:
    """结束会话：将 ``ended_at`` 设为当前毫秒时间戳。

    注意：
    - 本操作会修改 sessions 列，但 sessions 不在 L0-细节 / L0-骨架
      不可变白名单触发器范围内，因此允许 UPDATE。
    - 多次调用安全（幂等更新为最新时间）。
    """
    ended_at = _now_ms()
    with get_connection(path) as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
            (ended_at, session_id),
        )


# ---------------------------------------------------------------------------
# 消息级：写入（事务内完成 seq 计算 + 插入 + 会话聚合更新）
# ---------------------------------------------------------------------------


def write_message(
    session_id: str,
    role: str,
    content: str,
    tool_calls: Optional[Union[dict, list, str]] = None,
    tool_results: Optional[Union[dict, list, str]] = None,
    path: Optional[Union[Path, str]] = None,
) -> str:
    """向指定会话写入一条消息，返回 ``message_id``。

    参数：
        session_id: 目标会话 UUID；必须已存在，否则外键报错。
        role: ``'user'`` / ``'assistant'`` / ``'tool'`` / ``'system'``
            之一；其他值抛 ``ValueError``。
        content: 消息原文；允许空字符串，不允许 None。
        tool_calls: assistant 角色常用的工具调用；dict / list / str /
            None，自动序列化为 JSON 字符串。
        tool_results: tool 角色常用的工具返回值；同上。
        path: 数据库路径；``None`` 时用 ``MTCA_DB_PATH``。

    返回：
        新生成的 ``message_id``（字符串形式的 UUID4）。

    行为：
        1. 在单个事务内：计算 seq = max(seq)+1（同会话内单调递增）；
        2. 计算 token_count = len(content) // 4 估算；
        3. INSERT INTO messages；
        4. UPDATE sessions SET message_count = message_count + 1,
           token_estimate = token_estimate + token_count。

    异常：
        ValueError：role 非法或 content 非字符串。
        RuntimeError：底层数据库错误（外键失败 / 触发器抛错等）。
    """
    # ---- 参数校验 ----
    if role not in _VALID_ROLES:
        raise ValueError(
            f"role 非法：'{role}'，仅允许 {_VALID_ROLES}"
        )
    if not isinstance(content, str):
        raise ValueError("content 必须是字符串")

    message_id = str(uuid.uuid4())
    token_count = max(0, len(content) // _TOKEN_PER_CHAR)
    tool_calls_json = _serialize_json(tool_calls)
    tool_results_json = _serialize_json(tool_results)
    created_at = _now_ms()

    try:
        with get_connection(path) as conn:
            # 1) 计算下一个 seq（同事务内读，避免竞态）
            cur = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq "
                "FROM messages WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            next_seq = int(row["max_seq"]) + 1 if row else 1

            # 2) 插入消息（外键失败时 SQLError 自动转 RuntimeError）
            conn.execute(
                "INSERT INTO messages "
                "(message_id, session_id, seq, role, content, "
                "tool_calls, tool_results, token_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    session_id,
                    next_seq,
                    role,
                    content,
                    tool_calls_json,
                    tool_results_json,
                    token_count,
                    created_at,
                ),
            )

            # 3) 更新会话聚合统计
            conn.execute(
                "UPDATE sessions SET "
                "message_count = message_count + 1, "
                "token_estimate = token_estimate + ? "
                "WHERE session_id = ?",
                (token_count, session_id),
            )
    except RuntimeError:
        raise

    return message_id


# ---------------------------------------------------------------------------
# 消息级：查询
# ---------------------------------------------------------------------------


def get_session_messages(
    session_id: str,
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """获取指定会话的全部消息，按 ``seq`` 升序返回。

    返回 list[dict]，每条 dict 包含 messages 表全部列；空会话返回 []。
    """
    return query(
        "SELECT message_id, session_id, seq, role, content, "
        "tool_calls, tool_results, token_count, created_at "
        "FROM messages WHERE session_id = ? ORDER BY seq ASC",
        (session_id,),
        path=path,
    )


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = [
    "create_session",
    "write_message",
    "get_session_messages",
    "get_session",
    "end_session",
]
