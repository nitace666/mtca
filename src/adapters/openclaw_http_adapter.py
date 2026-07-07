# -*- coding: utf-8 -*-
"""src/adapters/openclaw_http_adapter.py -- Step 10 接 openclaw HTTP transport

设计原则：
- 0 新依赖（stdlib http.server.BaseHTTPRequestHandler + HTTPServer）
- 实现 MCP 2025-03-26 Streamable HTTP transport（POST /mcp + JSON-RPC 2.0）
- 路由 6 tool 到现有 src.adapters.mcp_server.TOOLS（不重复实现 tool 逻辑）
- 默认绑 127.0.0.1:8765（openclaw 配置匹配）
- 测试时可指定其他端口（避免 8765 冲突）
- 中文 docstring + 中文错误信息
- 单文件 <= 800 行

支持方法（MCP 2025-03-26 JSON-RPC 2.0）：
  - initialize       -> 返回 serverInfo + capabilities
  - tools/list       -> 返回 6 tool 的 schema 列表
  - tools/call       -> 路由到 TOOLS[tool_name](**arguments) + 包成 MCP 响应
  - 其他              -> -32601 method not found

错误处理：
  - JSON 解析失败     -> -32700 parse error
  - 路径错误（!= /mcp）-> 404 Not Found
  - 未知 tool         -> -32601 method not found
  - tool 内部异常     -> -32603 internal error（含 traceback 摘要）
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

# 复用既有 MCP Server T31 的 6 tool + schema（不复制逻辑，避免双源）
try:
    from src.adapters.mcp_server import TOOLS, TOOL_SCHEMAS
except ImportError as e:  # pragma: no cover
    # 极端情况：mcp_server.py 不可用 -- 此时 adapter 也无法工作
    raise ImportError(f"openclaw_http_adapter 依赖 src.adapters.mcp_server: {e}") from e

logger = logging.getLogger(__name__)

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8765
MCP_PATH: str = "/mcp"
PROTOCOL_VERSION: str = "2025-03-26"
SERVER_NAME: str = "mtca-mcp"
SERVER_VERSION: str = "0.6.0"

# JSON-RPC 2.0 错误码
ERR_PARSE: int = -32700
ERR_INVALID_REQUEST: int = -32600
ERR_METHOD_NOT_FOUND: int = -32601
ERR_INVALID_PARAMS: int = -32602
ERR_INTERNAL: int = -32603


def _jsonrpc_response(req_id: Any, result: Any) -> dict:
    """包装 JSON-RPC 2.0 成功响应。req_id 必须保留。"""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    """包装 JSON-RPC 2.0 错误响应。"""
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


class MCPRequestHandler(BaseHTTPRequestHandler):
    """MCP 2025-03-26 Streamable HTTP request handler。

    通过类属性 adapter 访问 OpenClawHTTPAdapter 实例（避免 closure 捕获）。
    """

    # 静默 BaseHTTPRequestHandler 默认 stderr 日志（避免污染 pytest 输出）
    def log_message(self, format, *args):  # noqa: A002
        logger.debug("MCP HTTP " + format, *args)

    def do_POST(self):  # noqa: N802
        adapter: "OpenClawHTTPAdapter" = self.server.adapter  # type: ignore[attr-defined]
        if self.path != MCP_PATH:
            self._send_json(404, _jsonrpc_error(None, ERR_METHOD_NOT_FOUND, f"path 必须是 {MCP_PATH}, 实际 {self.path}"))
            return
        # 读 body
        length_str = self.headers.get("Content-Length")
        if length_str is None:
            self._send_json(400, _jsonrpc_error(None, ERR_INVALID_REQUEST, "缺 Content-Length header"))
            return
        try:
            length = int(length_str)
        except ValueError:
            self._send_json(400, _jsonrpc_error(None, ERR_INVALID_REQUEST, f"Content-Length 不是整数: {length_str}"))
            return
        if length < 0 or length > 10 * 1024 * 1024:
            self._send_json(400, _jsonrpc_error(None, ERR_INVALID_REQUEST, f"Content-Length 越界: {length}"))
            return
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except Exception as e:
            self._send_json(400, _jsonrpc_error(None, ERR_INVALID_REQUEST, f"读 body 失败: {e}"))
            return
        # 解析 JSON
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json(400, _jsonrpc_error(None, ERR_PARSE, f"JSON 解析失败: {e}"))
            return
        # 分发到 adapter
        resp = adapter.handle_jsonrpc(req)
        # 决定 HTTP 状态码
        status = 200 if "result" in resp else 400 if "error" in resp else 500
        self._send_json(status, resp)

    def _send_json(self, status: int, body: dict) -> None:
        """发 JSON-RPC 响应（Content-Type: application/json）。"""
        try:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            # body 不可序列化（如 tool 返了 datetime）-- 包成 error
            req_id = body.get("id") if isinstance(body, dict) else None
            body = _jsonrpc_error(req_id, ERR_INTERNAL, f"响应序列化失败: {e}")
            payload = json.dumps(body).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_error(self, format, *args):  # noqa: A002
        logger.warning("MCP HTTP error " + format, *args)


class _AdapterHTTPServer(HTTPServer):
    """HTTPServer 子类，绑定 adapter 实例到 server.adapter 属性。"""
    adapter: "OpenClawHTTPAdapter"  # type: ignore[assignment]


class OpenClawHTTPAdapter:
    """OpenClaw HTTP MCP server 适配器（Streamable HTTP transport）。

    用法：
        adapter = OpenClawHTTPAdapter(host="127.0.0.1", port=8765)
        adapter.start()
        # 服务器在后台线程 serve_forever
        adapter.stop()  # 优雅关闭

    测试用法（避免端口冲突）：
        adapter = OpenClawHTTPAdapter(host="127.0.0.1", port=0)  # OS 分配
        port = adapter.start()  # 返回实际绑定端口
        try:
            url = f"http://127.0.0.1:{port}/mcp"
            # 测试请求
        finally:
            adapter.stop()
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self._server: Optional[_AdapterHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        """启动 HTTP server（后台线程）。返回实际绑定的端口（port=0 时由 OS 分配）。"""
        if self._server is not None:
            raise RuntimeError("adapter 已启动,禁止重复 start")
        server = _AdapterHTTPServer((self.host, self.port), MCPRequestHandler)
        server.adapter = self  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"openclaw-mcp-{server.server_address[1]}",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        actual_port = server.server_address[1]
        self.port = actual_port  # 同步 self.port 为实际端口（port=0 时）
        logger.info("OpenClawHTTPAdapter 启动 at http://%s:%d%s", self.host, actual_port, MCP_PATH)
        return actual_port

    def stop(self) -> None:
        """优雅关闭 server + join 线程。幂等。"""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None
        logger.info("OpenClawHTTPAdapter 关闭")

    def handle_jsonrpc(self, req: Any) -> dict:
        """处理单条 JSON-RPC 2.0 请求,返回响应 dict（不直接发 HTTP）。

        支持方法：
          - initialize            -> serverInfo + capabilities
          - tools/list            -> 6 tool schema 列表
          - tools/call            -> 路由到 TOOLS[tool_name](**arguments)
          - notifications/initialized -> 返回 result={}（no-op,MCP 2025 spec）
          - 其他                   -> -32601 method not found

        错误处理：
          - 非 dict 请求            -> -32600 invalid request
          - jsonrpc != "2.0"       -> -32600 invalid request（warning, 但兼容）
          - 缺 method              -> -32600 invalid request
          - tool 内部异常           -> -32603 internal error（traceback 摘要）
        """
        req_id: Any = None
        if isinstance(req, dict):
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}
        else:
            return _jsonrpc_error(None, ERR_INVALID_REQUEST, f"请求必须是 JSON object, 实际 {type(req).__name__}")

        if not method or not isinstance(method, str):
            return _jsonrpc_error(req_id, ERR_INVALID_REQUEST, "缺 method 字段或 method 非字符串")

        if method == "initialize":
            return _jsonrpc_response(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}},
            })

        if method == "notifications/initialized":
            # client 发出的初始化完成通知（no response needed per spec）
            # 我们返 result={} 作友好回执
            return _jsonrpc_response(req_id, {})

        if method == "tools/list":
            return _jsonrpc_response(req_id, {"tools": list(TOOL_SCHEMAS)})

        if method == "tools/call":
            return self._handle_tools_call(req_id, params)

        return _jsonrpc_error(req_id, ERR_METHOD_NOT_FOUND, f"未知 method: {method!r}（已支持: initialize / tools/list / tools/call）")

    def _handle_tools_call(self, req_id: Any, params: Any) -> dict:
        """处理 tools/call 内部逻辑。"""
        if not isinstance(params, dict):
            return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, f"params 必须是 JSON object, 实际 {type(params).__name__}")
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not tool_name or not isinstance(tool_name, str):
            return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, "params.name 必填且必须是字符串")
        if not isinstance(arguments, dict):
            return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, f"params.arguments 必须是 JSON object, 实际 {type(arguments).__name__}")
        if tool_name not in TOOLS:
            return _jsonrpc_error(req_id, ERR_METHOD_NOT_FOUND, f"未知 tool: {tool_name!r}（已支持: {sorted(TOOLS.keys())}）")
        try:
            result = TOOLS[tool_name](**arguments)
        except TypeError as e:
            return _jsonrpc_error(req_id, ERR_INVALID_PARAMS, f"参数错误: {e}")
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            logger.warning("tool %s 内部异常: %s", tool_name, tb)
            return _jsonrpc_error(req_id, ERR_INTERNAL, f"{type(e).__name__}: {e}", data={"traceback": tb})
        # MCP 2025-03-26 响应格式：result.content = [{type: "json", data: ...}]
        return _jsonrpc_response(req_id, {
            "content": [{"type": "json", "data": result}],
            "isError": False,
        })


def main() -> None:
    """CLI 入口：启动 HTTP MCP server 在 127.0.0.1:8765/mcp。

    用法：
        python -m src.adapters.openclaw_http_adapter
        或：
        python src/adapters/openclaw_http_adapter.py
    """
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    adapter = OpenClawHTTPAdapter()
    adapter.start()
    print(f"MTCA MCP HTTP server 启动 at http://{adapter.host}:{adapter.port}{MCP_PATH}")
    print("按 Ctrl+C 停止")
    try:
        if adapter._thread is not None:
            adapter._thread.join()
    except KeyboardInterrupt:
        print("\n停止中...")
    finally:
        adapter.stop()


if __name__ == "__main__":
    main()
