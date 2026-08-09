# MTCA Agent 平台适配器

> 设计原则：**MTCA 不替代 Agent，只做外挂记忆中间件。Agent 跑它的，MTCA 跑 MTCA 的，互不干扰，按需召回。**

---

## 0. 总协议

无论哪个平台，都通过统一接口对接 MTCA：

```
MCP Server  (推荐，Anthropic 标准)
REST API    (兼容任何语言)
SQLite 直接读 (本地调试 / 高级用)
```

接 MTCA 的 Agent 配置示例：

```json
{
  "mtca": {
    "server": "http://localhost:7777",
    "mode": "mcp",
    "session_id_strategy": "platform_native",
    "auto_recall": true,
    "topic_inject": true
  }
}
```

---

## 1. OpenClaw 适配

- 接入方式：订阅内部事件流 `session.message.*`
- 协议：MTCA 启动时连 OpenClaw 的事件总线
- 配置：在 `openclaw.config.json` 加：

```json
{
  "memory": {
    "backend": "mtca",
    "mtca_server": "http://localhost:7777",
    "session_id_source": "openclaw.session_id"
  }
}
```

---

## 2. Hermes 适配

- 接入方式：MCP Client 标准集成
- 配置：在 Hermes 启动参数中加：

```yaml
mcp_servers:
  mtca:
    command: "mtca-server"
    args: ["mcp"]
    env:
      MTCA_DB: "/home/user/mtca.db"
```

---

## 3. 扣子（Coze）适配

- 接入方式：通过 OpenAPI 插件
- 配置：上传 Coze 插件时填入：

```
MTCA Server URL: http://localhost:7777
认证: 本地 mTLS（无）
```

---

## 4. Claude Code 适配

- 接入方式：Claude Code 支持 MCP Server
- 配置：

```bash
claude --mcp-config mtca.json
```

`mtca.json`:

```json
{
  "mcpServers": {
    "mtca": {
      "command": "mtca-server",
      "args": ["mcp"]
    }
  }
}
```

---

## 5. Cursor / Devin / Cline 适配

- 这些 IDE 类 Agent 通常用 REST 兼容接口
- 用 MTCA 的 REST API 直接对接：

```bash
curl -X POST http://localhost:7777/api/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "上次讨论 MTCA v0.3 的设计哲学", "top_k": 5}'
```

---

## 6. 其他平台（ChatGPT / Gemini / 豆包 等）

不在 MTCA 自带适配器范围。但用户可以用：
- **本地代理**：起个 MITM 抓网页内容写 L0
- **手工 + REST**：用 OpenAI Function Calling 调 MTCA

这部分 v1.0 之后再考虑。

---

## 7. 隔离与权限

- MTCA 默认本地跑，无网络监听
- 数据存 `~/.mtca/` 或 `./mtca.db`
- **加密**：依赖系统自带磁盘加密（BitLocker / FileVault）

---

## 8. 不冲突保证

> 当 MTCA 后台压缩某个 Agent 的历史时，Agent 自身的工作不受影响。

机制：
- MTCA 独立进程，跑自己的 CPU / RAM
- Agent 不依赖 MTCA 实时响应（即使 MTCA 挂了，Agent 仍能继续工作，只是少了长期记忆）
- MTCA 不侵入 Agent 的 UI / 输入 / 输出
- Agent 可以关闭 MTCA（直接 stop `mtca-server`），无副作用
