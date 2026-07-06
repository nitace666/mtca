"""src/adapters/mcp_server.py — T31 MCP Server（stdio JSON-RPC）。

让外部 AI（Claude Code / Cursor）通过 stdio 调用 MTCA 的核心能力。
本文件是「自建 stdio JSON-RPC 路线」，不依赖 mcp-python-sdk 运行时
（开发环境 pip install mcp 已安装，但本 server 用纯 Python 标准库实现）。

设计要点：
- 6 个 tool：recall_segments / search_facts / mark_important /
  fog_segment / get_recent_sessions / list_llm_backends
- 每个 tool 是无状态的纯函数包装，返回 dict
- handle_request 处理单个 JSON 请求体（tool/params 或 cmd=list_tools）
- main() 跑 stdio 主循环：读 stdin JSON 行，回写 stdout JSON 行
- 不抛异常逃出函数：所有错误包成 {"status":"error",...} dict
- fog_segment 固定 called_by="user"（防止 AI 滥用 /雾化）

JSON 协议（极简版）：
    {"cmd":"list_tools"} → {"tools": [...]}
    {"tool":"<name>","params":{...}} → {"status":"ok","result":{...}}
                                          或 {"status":"error","error":"..."}
"""
import json
import sys
from pathlib import Path
from typing import Any, Optional

from src.recall.recall_engine import recall, get_recent_sessions
from src.compress.scoring import mark_important
from src.fog.fog_engine import fog_segment
from src.llm.facts_store import search_facts as facts_search
from src.llm.provider import probe_all as probe_llm_backends
from src.llm.config_store import db_get_setting
from src.store.sqlite import MTCA_DB_PATH


# === tool 实现（每个 tool 一个 _tool_* 函数，返回 dict；不抛异常） ===

def _tool_recall_segments(
    query: str,
    top_k: int = 5,
    time_window: Optional[list] = None,
    path: Optional[str] = None,
) -> dict:
    """按 query 召回段落（包含 C1 facts 通道 + L0 原文）。"""
    tw: Optional[tuple] = tuple(time_window) if time_window else None
    results = recall(
        query=query,
        top_k=top_k,
        time_window=tw,
        path=path or MTCA_DB_PATH,
    )
    return {"status": "ok", "count": len(results), "segments": results}


def _tool_search_facts(
    query: str,
    limit: int = 5,
    path: Optional[str] = None,
) -> dict:
    """FTS5 全文搜索 facts（FTS5 + LIKE 兜底）。"""
    # facts_search 的真实签名是 (query_text, top_k, path)，与 recall 不一致
    facts = facts_search(
        query_text=query,
        top_k=limit,
        path=path or MTCA_DB_PATH,
    )
    return {"status": "ok", "count": len(facts), "facts": facts}


def _tool_mark_important(
    segment_id: str,
    path: Optional[str] = None,
) -> dict:
    """把段落标记为 /重要（score=IMPORTANT_SCORE，tier 升级）。"""
    rows = mark_important(segment_id, path=path or MTCA_DB_PATH)
    return {"status": "ok", "rows_updated": rows}


def _tool_fog_segment(
    segment_id: str,
    anchor: str,
    path: Optional[str] = None,
) -> dict:
    """雾化指定段落（物理擦除 L0-细节，不可逆）。

    MCP 调用固定 called_by='user'（permission check 在 fog_segment 内）。
    """
    ok = fog_segment(
        segment_id=segment_id,
        anchor=anchor,
        called_by="user",
        path=path or MTCA_DB_PATH,
    )
    return {"status": "ok" if ok else "fail", "anchor": anchor}


def _tool_get_recent_sessions(
    limit: int = 10,
    path: Optional[str] = None,
) -> dict:
    """获取最近的会话列表（按 started_at DESC）。"""
    sessions = get_recent_sessions(
        limit=limit,
        path=path or MTCA_DB_PATH,
    )
    return {"status": "ok", "count": len(sessions), "sessions": sessions}


def _tool_list_llm_backends(path: Optional[str] = None) -> dict:
    """探测 4 个 LLM 后端的可用性 + 列出当前 active backend。

    DB 未初始化时（settings 表缺失）返回 ``current_backend=None``
    + 空 ``available={}``，而不是让 server 抛错 —— 这是为了容错：
    用户首次 ``mtca-mcp`` 调用时可能还没 ``mtca init`` 过 DB。
    """
    try:
        available = probe_llm_backends()
    except Exception as e:
        return {"status": "error", "error": f"探测 LLM 后端失败：{e}"}
    try:
        current = db_get_setting("llm.backend", path=path or MTCA_DB_PATH)
    except Exception:
        # settings 表缺失（DB 未 init）—— 视为未配置
        current = None
    return {
        "status": "ok",
        "current_backend": current,
        "available": {k: bool(v) for k, v in available.items()},
    }


TOOLS = {
    "recall_segments": _tool_recall_segments,
    "search_facts": _tool_search_facts,
    "mark_important": _tool_mark_important,
    "fog_segment": _tool_fog_segment,
    "get_recent_sessions": _tool_get_recent_sessions,
    "list_llm_backends": _tool_list_llm_backends,
}


# === tool schema（给外部 LLM 看的 JSON Schema 描述） ===

TOOL_SCHEMAS = [
    {
        "name": "recall_segments",
        "description": "按 query 召回段落（含 C1 facts 优先通道 + L0 原文）",
        "params": [
            {"name": "query", "type": "string", "required": True},
            {"name": "top_k", "type": "int", "default": 5},
            {"name": "time_window", "type": "list[int]", "default": None},
            {"name": "path", "type": "string", "default": None},
        ],
    },
    {
        "name": "search_facts",
        "description": "FTS5 全文搜索 facts（FTS5 + LIKE 兜底）",
        "params": [
            {"name": "query", "type": "string", "required": True},
            {"name": "limit", "type": "int", "default": 5},
            {"name": "path", "type": "string", "default": None},
        ],
    },
    {
        "name": "mark_important",
        "description": "/重要 命令直达 L0（score=IMPORTANT_SCORE，tier 升级）",
        "params": [
            {"name": "segment_id", "type": "string", "required": True},
            {"name": "path", "type": "string", "default": None},
        ],
    },
    {
        "name": "fog_segment",
        "description": "雾化段落（物理擦除 L0-细节，不可逆；MCP 调用固定 called_by='user'）",
        "params": [
            {"name": "segment_id", "type": "string", "required": True},
            {"name": "anchor", "type": "string", "required": True},
            {"name": "path", "type": "string", "default": None},
        ],
    },
    {
        "name": "get_recent_sessions",
        "description": "获取最近 N 个会话列表（按 started_at DESC）",
        "params": [
            {"name": "limit", "type": "int", "default": 10},
            {"name": "path", "type": "string", "default": None},
        ],
    },
    {
        "name": "list_llm_backends",
        "description": "探测 4 个 LLM 后端的可用性 + 列出当前 active backend",
        "params": [
            {"name": "path", "type": "string", "default": None},
        ],
    },
]


# === handler（公开 API：handle_request / handle_list_tools） ===

def handle_list_tools() -> dict:
    """返回 6 个 tool 的 schema 列表。"""
    return {"tools": list(TOOL_SCHEMAS)}


def handle_request(req: dict) -> dict:
    """处理 JSON-RPC 风格请求。

    支持两种请求形态：
      1. {"cmd": "list_tools"} → list_tools 响应
      2. {"tool": "<name>", "params": {...}} → 调对应 tool

    返回 dict：
      {"status": "ok", "result": <tool 返回值>}    成功
      {"status": "error", "error": "...", ...}     失败（含 available 列表）
    """
    if not isinstance(req, dict):
        return {"status": "error", "error": "请求必须是 JSON object"}

    # 命令形态：列出 tools
    cmd = req.get("cmd")
    if cmd == "list_tools":
        return handle_list_tools()
    if cmd is not None:
        return {"status": "error", "error": f"unknown cmd: {cmd!r}"}

    # 工具调用形态
    tool = req.get("tool")
    if tool not in TOOLS:
        return {
            "status": "error",
            "error": f"unknown tool: {tool!r}",
            "available": list(TOOLS.keys()),
        }

    params = req.get("params", {}) or {}
    if not isinstance(params, dict):
        return {"status": "error", "error": "params 必须是 JSON object"}

    try:
        result = TOOLS[tool](**params)
        return {"status": "ok", "result": result}
    except TypeError as e:
        # 参数缺失或不匹配
        return {"status": "error", "error": f"bad params: {e}"}
    except Exception as e:
        # tool 内部错误（如 DB 路径无效）—— 包成 error dict，不要让 server 崩
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }


# === stdio 主循环 ===

def _read_request(line: str):
    """把单行字符串解析成 dict；解析失败返回 None。

    返回：
        None —— 空行
        {"_parse_error": str, "_raw": str} —— JSON 解析失败
        dict —— 解析成功
    """
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        return {"_parse_error": str(e), "_raw": line}


def main() -> None:
    """stdio 主循环：读 stdin JSON 行，回写 stdout JSON 行。

    异常处理：
      - 空行：跳过
      - JSON 解析失败：回写 {"status":"error","error":"bad JSON..."}
      - 单条请求异常：包成 error dict，不退出循环
    """
    for line in sys.stdin:
        parsed = _read_request(line)
        if parsed is None:
            continue
        if "_parse_error" in parsed:
            resp = {
                "status": "error",
                "error": f"bad JSON: {parsed['_parse_error']}",
            }
        else:
            resp = handle_request(parsed)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()