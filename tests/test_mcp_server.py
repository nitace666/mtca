"""tests/test_mcp_server.py — T31 MCP Server 测试。

覆盖：
- 6 个 tool 各 2-3 个基本行为 / 参数处理测试
- handle_request 错误路径（unknown tool / missing param / bad JSON）
- handle_list_tools / cmd=list_tools 两路径
- tool 内部抛错不应让 handler 崩（包成 error dict）
- main() stdio 主循环：mock stdin/stdout 测试坏 JSON / 空行
"""
import io
import json
import sys

import pytest

from src.adapters.mcp_server import (
    TOOLS,
    TOOL_SCHEMAS,
    _read_request,
    handle_list_tools,
    handle_request,
    main,
)


# === list_tools / handle_list_tools ===

def test_handle_list_tools_returns_six_schemas():
    """handle_list_tools 返回 6 个 schema。"""
    resp = handle_list_tools()
    assert "tools" in resp
    assert isinstance(resp["tools"], list)
    assert len(resp["tools"]) == 6


def test_handle_list_tools_names_match_tools_dict():
    """schema 里的 name 集合 == TOOLS 字典的键集合。"""
    resp = handle_list_tools()
    schema_names = sorted(t["name"] for t in resp["tools"])
    actual_names = sorted(TOOLS.keys())
    assert schema_names == actual_names


def test_handle_request_cmd_list_tools_via_main_loop():
    """{cmd: list_tools} 也走 list_tools 路径。"""
    resp = handle_request({"cmd": "list_tools"})
    assert "tools" in resp
    assert len(resp["tools"]) == 6


def test_handle_request_unknown_cmd_returns_error():
    """未知 cmd 应该包成 error，不崩溃。"""
    resp = handle_request({"cmd": "fake_cmd"})
    assert resp.get("status") == "error"
    assert "fake_cmd" in resp.get("error", "")


# === recall_segments tool（3 用例） ===

def test_recall_segments_basic(mtca_db):
    """recall_segments 在空库上跑通，返回 count>=0 + segments 列表。"""
    db = mtca_db
    resp = handle_request({
        "tool": "recall_segments",
        "params": {"query": "x", "top_k": 3, "path": str(db)},
    })
    assert resp["status"] == "ok"
    assert resp["result"]["count"] >= 0
    assert "segments" in resp["result"]


def test_recall_segments_default_top_k(mtca_db):
    """不传 top_k 应使用默认 5。"""
    db = mtca_db
    resp = handle_request({
        "tool": "recall_segments",
        "params": {"query": "x", "path": str(db)},
    })
    assert resp["status"] == "ok"
    assert resp["result"]["count"] >= 0


def test_recall_segments_missing_query_returns_error():
    """recall_segments 缺 query 必须返回 error，而不是 raise。"""
    resp = handle_request({"tool": "recall_segments", "params": {}})
    assert resp["status"] == "error"


# === search_facts tool（2 用例） ===

def test_search_facts_basic(mtca_db):
    """search_facts 在空库上跑通，count>=0。"""
    db = mtca_db
    resp = handle_request({
        "tool": "search_facts",
        "params": {"query": "foo", "limit": 5, "path": str(db)},
    })
    assert resp["status"] == "ok"
    assert resp["result"]["count"] >= 0
    assert "facts" in resp["result"]


def test_search_facts_missing_query():
    """缺 query 必须返回 error。"""
    resp = handle_request({"tool": "search_facts", "params": {}})
    assert resp["status"] == "error"


# === mark_important tool（2 用例） ===

def test_mark_important_missing_segment_returns_error():
    """不存在的 segment_id 应该返回 error（来自底层 mark_important）。"""
    resp = handle_request({
        "tool": "mark_important",
        "params": {"segment_id": "non-existent-id"},
    })
    assert resp["status"] == "error"


def test_mark_important_empty_segment_id_returns_error():
    """空 segment_id 应该返回 error（参数校验）。"""
    resp = handle_request({
        "tool": "mark_important",
        "params": {"segment_id": ""},
    })
    assert resp["status"] == "error"


# === fog_segment tool（2 用例） ===

def test_fog_segment_missing_segment_returns_error():
    """不存在的 segment_id 应该返回 error。"""
    resp = handle_request({
        "tool": "fog_segment",
        "params": {"segment_id": "non-existent-id", "anchor": "test anchor"},
    })
    assert resp["status"] == "error"


def test_fog_segment_empty_segment_id_returns_error():
    """空 segment_id 应该返回 error。"""
    resp = handle_request({
        "tool": "fog_segment",
        "params": {"segment_id": "", "anchor": "x"},
    })
    assert resp["status"] == "error"


# === get_recent_sessions tool（2 用例） ===

def test_get_recent_sessions_basic(mtca_db):
    """get_recent_sessions 在空库上跑通，count>=0。"""
    db = mtca_db
    resp = handle_request({
        "tool": "get_recent_sessions",
        "params": {"limit": 3, "path": str(db)},
    })
    assert resp["status"] == "ok"
    assert resp["result"]["count"] >= 0
    assert "sessions" in resp["result"]


def test_get_recent_sessions_default_limit(mtca_db):
    """不传 limit 应使用默认 10。"""
    db = mtca_db
    resp = handle_request({
        "tool": "get_recent_sessions",
        "params": {"path": str(db)},
    })
    assert resp["status"] == "ok"
    assert resp["result"]["count"] >= 0


# === list_llm_backends tool（2 用例） ===

def test_list_llm_backends_returns_availability_map(mtca_db):
    """返回 current_backend (可空) + 4 个 backend availability 字典。"""
    db = mtca_db
    resp = handle_request({
        "tool": "list_llm_backends",
        "params": {"path": str(db)},
    })
    assert resp["status"] == "ok"
    res = resp["result"]
    assert "current_backend" in res
    assert "available" in res
    # 4 个预期 backend：ollama / lmstudio / llamacpp / cloud
    available_keys = set(res["available"].keys())
    assert {"ollama", "lmstudio", "llamacpp", "cloud"}.issubset(available_keys)


def test_list_llm_backends_with_empty_db_path(mtca_db):
    """传一个已初始化的 DB 路径，应能列出 backend 状态。

    不测默认 MTCA_DB_PATH 因为默认 DB 可能没初始化 settings 表。
    """
    resp = handle_request({"tool": "list_llm_backends", "params": {"path": str(mtca_db)}})
    assert resp["status"] == "ok"
    assert "available" in resp["result"]


# === handler 错误处理 ===

def test_unknown_tool_returns_error_with_available_list():
    """未知 tool 返回 error，且给出可用的 tool 列表。"""
    resp = handle_request({"tool": "fake_tool", "params": {}})
    assert resp["status"] == "error"
    assert "fake_tool" in resp["error"]
    assert "available" in resp
    assert set(resp["available"]) == set(TOOLS.keys())


def test_handle_request_rejects_non_dict():
    """非 dict 请求（None / list / str）应返回 error。"""
    for bad in [None, [], "string", 42]:
        resp = handle_request(bad)
        assert resp.get("status") == "error"


def test_handle_request_rejects_non_dict_params():
    """params 不是 dict 应返回 error。"""
    resp = handle_request({"tool": "list_llm_backends", "params": "not-a-dict"})
    assert resp["status"] == "error"


def test_tool_exception_does_not_crash_handler(tmp_path):
    """tool 内部抛错（如 DB 路径无效）应被包成 error dict，不让 server 崩。"""
    resp = handle_request({
        "tool": "search_facts",
        "params": {"query": "foo", "path": "Z:/__definitely_not_exists__/nope.db"},
    })
    # 不管最终是 ok 还是 error，关键是 handler 没把异常抛出来
    assert isinstance(resp, dict)
    assert "status" in resp


# === _read_request 单元测试 ===

def test_read_request_empty_line_returns_none():
    """空行（whitespace-only）应该返回 None 让 main 跳过。"""
    assert _read_request("") is None
    assert _read_request("   \n") is None


def test_read_request_bad_json_returns_parse_error_marker():
    """坏 JSON 应该返回带 _parse_error 键的 dict，便于 main 区分。"""
    result = _read_request("{not valid json")
    assert isinstance(result, dict)
    assert "_parse_error" in result
    assert "_raw" in result


def test_read_request_valid_json():
    """合法 JSON 直接返回解析后的 dict。"""
    result = _read_request('{"cmd": "list_tools"}')
    assert result == {"cmd": "list_tools"}


# === main() stdio 主循环：用 io.StringIO 替换 stdin/stdout ===

def _run_main_with_input(stdin_text: str) -> str:
    """跑 main()，stdin = stdin_text，收集 stdout 返回。"""
    saved_stdin = sys.stdin
    saved_stdout = sys.stdout
    try:
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        main()
        return sys.stdout.getvalue()
    finally:
        sys.stdin = saved_stdin
        sys.stdout = saved_stdout


def test_main_processes_list_tools_request():
    """单行 list_tools 请求应回写一行 JSON（含 tools 字段）。"""
    out = _run_main_with_input('{"cmd":"list_tools"}\n')
    lines = [l for l in out.split("\n") if l]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert "tools" in parsed
    assert len(parsed["tools"]) == 6


def test_main_handles_bad_json_without_crashing():
    """坏 JSON 行应回写 error，但不退出循环（后续行继续处理）。"""
    out = _run_main_with_input(
        'this is not json\n'
        '{"cmd":"list_tools"}\n'
    )
    lines = [l for l in out.split("\n") if l]
    assert len(lines) == 2
    err = json.loads(lines[0])
    assert err["status"] == "error"
    ok = json.loads(lines[1])
    assert "tools" in ok


def test_main_skips_empty_lines():
    """空行（含只有 whitespace 的行）应该被跳过，不回写。"""
    out = _run_main_with_input('\n\n{"cmd":"list_tools"}\n\n')
    lines = [l for l in out.split("\n") if l]
    assert len(lines) == 1


def test_main_unknown_tool_returns_error_dict():
    """未知 tool 请求应回写含 error + available 的 dict。"""
    out = _run_main_with_input('{"tool":"nope","params":{}}\n')
    lines = [l for l in out.split("\n") if l]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["status"] == "error"
    assert "available" in parsed


def test_main_single_line_json_round_trip():
    """真实工具调用：list_tools 通过 stdin 回写完整 schema。"""
    out = _run_main_with_input('{"cmd":"list_tools"}\n')
    lines = [l for l in out.split("\n") if l]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert "tools" in parsed
    tool_names = {t["name"] for t in parsed["tools"]}
    assert "recall_segments" in tool_names
    assert "fog_segment" in tool_names