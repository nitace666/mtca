# -*- coding: utf-8 -*-
"""tests/test_openclaw_http_integration.py -- Step 10 接 openclaw HTTP transport"""
from __future__ import annotations
import json, socket, time, urllib.error, urllib.request
from contextlib import contextmanager
from unittest.mock import patch
import pytest

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def _http_post_json(url, body, timeout=5.0):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _http_post_json_expect_error(url, body, timeout=5.0):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

@contextmanager
def _adapter_ctx(port):
    from src.adapters.openclaw_http_adapter import OpenClawHTTPAdapter
    adapter = OpenClawHTTPAdapter(host="127.0.0.1", port=port)
    adapter.start()
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    try:
        yield adapter
    finally:
        adapter.stop()

@pytest.fixture
def mcp_server():
    port = _find_free_port()
    with _adapter_ctx(port) as adapter:
        yield f"http://127.0.0.1:{port}/mcp", adapter

def _mock_tools():
    fake = {
        "recall_segments": {"status": "ok", "hits": [{"segment_id": "s1"}]},
        "search_facts": {"status": "ok", "facts": [{"fact_id": "f1"}]},
        "mark_important": {"status": "ok", "marked": True},
        "fog_segment": {"status": "ok", "fogged": True},
        "get_recent_sessions": {"status": "ok", "sessions": []},
        "list_llm_backends": {"status": "ok", "current_backend": "ollama", "available": {"ollama": True}},
    }
    def _mk(name):
        def fn(**kwargs): return fake[name]
        return fn
    return {n: _mk(n) for n in fake}

def test_http_handshake_OK(mcp_server):
    url, _ = mcp_server
    resp = _http_post_json(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "openclaw", "version": "1.0"}},
    })
    assert resp.get("jsonrpc") == "2.0"
    assert resp.get("id") == 1
    assert "result" in resp
    result = resp["result"]
    assert result.get("protocolVersion") == "2025-03-26"
    server = result.get("serverInfo", {})
    assert server.get("name"), f"缺 serverInfo.name: {server!r}"

def test_recall_segments_via_http(mcp_server):
    url, _ = mcp_server
    with patch("src.adapters.openclaw_http_adapter.TOOLS", _mock_tools()):
        resp = _http_post_json(url, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "recall_segments", "arguments": {"query": "搬家", "top_k": 5}},
        })
    assert "result" in resp
    content = resp["result"]["content"][0]
    data = content["data"]
    assert data.get("status") == "ok"
    assert data["hits"][0]["segment_id"] == "s1"

def test_search_facts_via_http(mcp_server):
    url, _ = mcp_server
    with patch("src.adapters.openclaw_http_adapter.TOOLS", _mock_tools()):
        resp = _http_post_json(url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_facts", "arguments": {"query": "老朋友", "limit": 3}},
        })
    assert "result" in resp
    data = resp["result"]["content"][0]["data"]
    assert data["status"] == "ok"
    assert data["facts"][0]["fact_id"] == "f1"

def test_mark_important_via_http(mcp_server):
    url, _ = mcp_server
    with patch("src.adapters.openclaw_http_adapter.TOOLS", _mock_tools()):
        resp = _http_post_json(url, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "mark_important", "arguments": {"segment_id": "seg-x"}},
        })
    assert "result" in resp
    data = resp["result"]["content"][0]["data"]
    assert data["status"] == "ok"
    assert data["marked"] is True

def test_fog_segment_via_http(mcp_server):
    url, _ = mcp_server
    with patch("src.adapters.openclaw_http_adapter.TOOLS", _mock_tools()):
        resp = _http_post_json(url, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "fog_segment", "arguments": {"segment_id": "seg-y", "anchor": "搬家"}},
        })
    assert "result" in resp
    data = resp["result"]["content"][0]["data"]
    assert data["status"] == "ok"
    assert data["fogged"] is True

def test_get_recent_sessions_via_http(mcp_server):
    url, _ = mcp_server
    with patch("src.adapters.openclaw_http_adapter.TOOLS", _mock_tools()):
        resp = _http_post_json(url, {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "get_recent_sessions", "arguments": {"limit": 5}},
        })
    assert "result" in resp
    data = resp["result"]["content"][0]["data"]
    assert data["status"] == "ok"
    assert data["sessions"] == []

def test_list_llm_backends_via_http(mcp_server):
    url, _ = mcp_server
    with patch("src.adapters.openclaw_http_adapter.TOOLS", _mock_tools()):
        resp = _http_post_json(url, {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "list_llm_backends", "arguments": {}},
        })
    assert "result" in resp
    data = resp["result"]["content"][0]["data"]
    assert data["status"] == "ok"
    assert data["current_backend"] == "ollama"
    assert data["available"]["ollama"] is True

def test_http_error_handling_404(mcp_server):
    url, _ = mcp_server
    base = url.rsplit("/", 1)[0]
    wrong_url = base + "/wrong-path"
    status, body = _http_post_json_expect_error(
        wrong_url,
        {"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {}},
    )
    assert status == 404, f"POST /wrong-path 应返 404, 实际 {status}"
