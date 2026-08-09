# MTCA — Multi-Tier Context Architecture

> AI 长期记忆中间件。Local-first. User-controlled. L0 immutable. Active forgetting by design.

## 这是什么

MTCA 是给 AI Agent 的**外挂长期记忆中间件**。它在你电脑本地持久化所有对话,提供 8+1 条铁律保障的存储语义,让任何 Agent 通过 MCP / REST 接入,按需召回任意时段的原始对话。

MTCA **不替代**任何 Agent 平台(OpenClaw / Hermes / Claude Code / Cursor / ...),它是一个旁路工具 —— **Agent 跑它的,MTCA 跑 MTCA 的,互不干扰,按需召回**。

## 快速开始

```bash
pip install mtca-memory
mtca init            # 初始化 SQLite 数据库
mtca-server start    # 启动 MTCA 服务(独立进程)

# 在你的 Agent 平台配置 MCP 地址
# Claude Code / Cursor 等支持 stdio MCP 的客户端可直连
```

详见 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

## 核心特性

| 维度 | MTCA |
|------|------|
| 数据位置 | 本地 SQLite,用户掌控 |
| 长期记忆成本 | 一次性 + 极低(纯文本 1-2 GB / 年) |
| 接入成本 | GUI 零代码 + MCP 5 行配置 |
| 用户对记忆的权 | `/重要 /循环 /归档 /雾化` + GUI 时间线 |
| 与 Agent 关系 | **并存**:Agent 跑它的,MTCA 跑 MTCA 的 |
| 主动遗忘 | 用户主动 `/雾化` = 物理擦除细节 |
| N 年前对话找回率 | 95%+ (vs RAG 默认 3-5%) |

## 设计哲学(8+1 铁律)

1. **数据主权在用户** — 本地 SQLite,系统自带磁盘加密
2. **L0 双层 + AI 不可变** — 骨架(标题/时间/关键词/锚点句)+ 细节(完整对话),AI 无任何层修改权
3. **L1-L3 是视图** — 可重建、可损失,错了回 L0-骨架
4. **雾化 = 用户主动丢弃细节** — 物理擦除 L0-细节,保留骨架
5. **召回错误自动回 L0-骨架** — 摘要丢失的,骨架 + 关键词兜底
6. **用户控制接口直达 L0** — `/重要 /循环 /归档 /雾化` 跳过任何压缩
7. **GUI 优先** — 时间线、树状、雾化按钮全在 GUI
8. **存储成本由用户承担** — 1-2 GB / 年,可忽略
9. **重要记忆永不遗忘** — 标 `/重要`、URGENT、Q1 类段永不丢失

## 文档导览

| 我想... | 看哪 |
|---------|------|
| 装好跑通 | [docs/QUICKSTART.md](docs/QUICKSTART.md) |
| 用起来 / 看懂 GUI | [docs/USER_GUIDE.md](docs/USER_GUIDE.md) |
| 用 `/重要 /循环 /归档 /雾化` | [docs/USER_CONTROLS.md](docs/USER_CONTROLS.md) |
| 接 Claude Code / Cursor | [docs/MCP_INTEGRATION.md](docs/MCP_INTEGRATION.md) |
| 接入其他 Agent 平台 | [docs/ADAPTERS.md](docs/ADAPTERS.md) |
| 配 LLM 后端 | [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md) |
| 看架构(高层) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |

## 协议

MIT License — 开源协议,所有人可商用。

## 反馈

- 产品问题 / Bug / 想法 → [GitHub Issues](https://github.com/nitace666/mtca/issues)
- 接入集成问题 → [GitHub Discussions](https://github.com/nitace666/mtca/discussions)