# MTCA 架构(高层)

> 简版。完整设计在代码注释 + 内部文档里。

## 整体架构

```
   任意 Agent 平台(Claude Code / Cursor / OpenClaw / Hermes / ...)
                  │
                  │ MCP / REST
                  ▼
        MTCA Server(独立进程)
   ┌──────────────────────────────┐
   │  GUI(MVP) + MCP Server + REST │
   │  召回主链路 + 异步打分/压缩    │
   │  SQLite (WAL) + FTS5          │
   └──────────────────────────────┘
```

## 关键设计原则

1. **L0 不可变 + 用户主权** — L0 原文层由用户掌控,AI 不能改
2. **多档压缩视图** — L1/L2/L3 是给 LLM 看的"快速回忆",可损失
3. **用户主动遗忘** — `/雾化` 物理擦除细节,保留骨架
4. **永不遗忘重要** — Q1 / URGENT / `/重要` 类段永不丢失
5. **Agent 无关** — 通过 MCP / REST 接入,与现有 AI 平台不冲突

## 接入方式

- **GUI**(本地优先,新手向):`python gui/run.py`
- **MCP Server**(Anthropic 标准):`mtca-mcp` 命令,Claude Code / Cursor 5 行配置
- **REST API**(通用):HTTP 端点,任何语言可调
- **CLI**(开发期辅助):`python -m src.cli.timeline` 等

详见:
- [docs/ADAPTERS.md](ADAPTERS.md) — 各平台适配
- [docs/MCP_INTEGRATION.md](MCP_INTEGRATION.md) — MCP 接入指南

## 8+1 铁律

见 [README.md §设计哲学](../README.md#设计哲学8-1-铁律)。