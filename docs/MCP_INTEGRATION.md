# MCP Server 接入指南

> 让外部 AI（Claude Code / Cursor）通过 **stdio JSON-RPC** 调用 MTCA 的核心记忆能力。
> 实现位于 `src/adapters/mcp_server.py`（T31，无第三方运行时依赖）。

---

## 快速接入：5 行配置

### Claude Code

编辑 Claude Code 的 MCP 配置（`~/.claude.json` 或 IDE settings）：

```json
{
  "mcpServers": {
    "mtca": {
      "command": "mtca-mcp",
      "args": []
    }
  }
}
```

安装 MTCA 后 `mtca-mcp` 命令会自动可用（见下文"安装"）。

### Cursor

`~/.cursor/mcp.json`（Cursor 1.x 同名）：

```json
{
  "mcpServers": {
    "mtca": {
      "command": "mtca-mcp",
      "args": []
    }
  }
}
```

### 其他支持 stdio MCP 的客户端

任何遵循 [MCP 协议](https://modelcontextprotocol.io/) 的客户端都可以接入，
关键是定义一个指向 `mtca-mcp` 命令的 stdio server。

---

## 安装

```bash
# 装 MTCA（MCP server 入口是 mtca-mcp）
pip install -e .

# 首次使用前必须 init 数据库
mtca init
```

> ⚠️ **必须**先跑 `mtca init`，否则 list_llm_backends 会返回空 `current_backend`，
> recall 会返回空 list（DB 没有 schema）。

---

## 6 个 tool 清单

跑 `echo '{"cmd":"list_tools"}' | mtca-mcp` 可以查看完整 schema。

| Tool | 功能 | 关键参数 |
|---|---|---|
| `recall_segments` | 按 query 召回段落（C1 facts 通道 + L0 原文） | `query` (str, 必填), `top_k` (int, 默认 5), `time_window` ([start_ms, end_ms], 可选) |
| `search_facts` | FTS5 全文搜索 facts | `query` (str, 必填), `limit` (int, 默认 5) |
| `mark_important` | `/重要` 直达 L0 | `segment_id` (str, 必填) |
| `fog_segment` | 雾化段落（物理擦除 L0） | `segment_id` (str), `anchor` (str) |
| `get_recent_sessions` | 获取最近会话列表 | `limit` (int, 默认 10) |
| `list_llm_backends` | 探测 4 个 LLM 后端可用性 | （无必填参数） |

---

## 调用示例

### recall：问"上月跟张三的项目讨论"

```json
{"tool": "recall_segments", "params": {"query": "张三 项目", "top_k": 5}}
```

返回：
```json
{
  "status": "ok",
  "result": {
    "status": "ok",
    "count": 3,
    "segments": [{"segment_id": "...", "session_id": "...", "score": 0.85, "messages": [...]}]
  }
}
```

### search_facts：问"张三住哪"

```json
{"tool": "search_facts", "params": {"query": "张三 地址", "limit": 5}}
```

返回 `facts` 列表（直接匹配的事实优先）。

### mark_important：标关键段

```json
{"tool": "mark_important", "params": {"segment_id": "seg-uuid-here"}}
```

### fog_segment：永久擦除一段（用户语义）

```json
{"tool": "fog_segment", "params": {"segment_id": "seg-uuid-here", "anchor": "已整理到 Notion"}}
```

> ⚠️ 雾化**不可逆**。MCP 调用固定走 `called_by='user'`（permission check 在 `fog_segment` 内），
> 外部 AI 不能滥用——即便 tool 被外部 prompt 注入攻击，`fog_segment` 也会保护。

---

## JSON 协议

本 server 用极简版 JSON-RPC（请求/响应都是单行 JSON）：

```
请求：{"cmd":"list_tools"}                              → 列出 6 个 tool schema
请求：{"tool":"<name>","params":{...}}                 → 调对应 tool
响应：{"status":"ok","result":{...}}                   → 成功
响应：{"status":"error","error":"..."}                 → 失败
```

所有错误都包成 `{"status":"error"}` dict —— server 不会因为单条坏请求挂掉。

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `no such table: settings` | DB 没 init | 跑 `mtca init` |
| `no such table: messages_fts` | DB 没 init | 跑 `mtca init` |
| `mtca-mcp: command not found` | MTCA 没装 | `pip install -e .` |
| recall 总是空 | 还没有任何会话 | 先用 `mtca write` 写几段对话 |
| LLM 后端都 unavailable | Ollama 没起 / Cloud 没配 key | 用 mtca-mcp 的 `list_llm_backends` 探测 |

---

## 安全约束

- **雾化操作需要 user 显式确认**（MCP 调用 fixed `called_by='user'`，
  但 fog_segment 还会校验 called_by 必须在 `{'user'}` 集合中）
- **mark_important 直达 L0**：等价于 `/重要` 命令，需要谨慎
- **所有 SQL 走 `?` 占位符**（防注入），但 MCP 接受任意 JSON——请确保客户端不接受不可信输入

---

## 开发与测试

```bash
# 跑测试
python -m pytest tests/test_mcp_server.py -v

# 手动跑 stdio
echo '{"cmd":"list_tools"}' | python -m src.adapters.mcp_server
echo '{"tool":"recall_segments","params":{"query":"foo","top_k":1}}' | python -m src.adapters.mcp_server
```

29 个测试覆盖：6 个 tool 基本 + 错误处理 + stdio 主循环。

---

**Refs**: T31 (M2 rollout), `src/adapters/mcp_server.py`