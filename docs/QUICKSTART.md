# MTCA 快速开始

> 10 分钟跑通 GUI + CLI。

## 1. 系统要求

| 项 | 要求 |
|----|------|
| Python | 3.10 或更高 |
| 操作系统 | Windows 10+ / macOS 12+ / Ubuntu 20.04+ |
| 磁盘 | ≥ 2 GB |
| 内存 | ≥ 4 GB |

## 2. 安装

```bash
git clone https://github.com/nitace666/mtca.git
cd mtca
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
pip install -e .
```

## 3. 启动 GUI

```bash
python gui/run.py
```

首次启动会自动创建 `~/.mtca/` 目录和 `mtca.db` 数据库。

GUI 主窗口:左侧时间线 / 右侧段落详情 / 顶部工具栏(搜索 / 刷新 / 雾化 / 归档)。

完整 GUI 使用见 [docs/USER_GUIDE.md](USER_GUIDE.md)。

## 4. CLI 时间线

```bash
# 今天
python -m src.cli.timeline timeline

# 最近 7 天 / 30 天
python -m src.cli.timeline timeline --period week
python -m src.cli.timeline timeline --period month

# 关键词搜索
python -m src.cli.timeline timeline --query "雾化"

# 树状视图
python -m src.cli.timeline tree --project "MTCA"
```

## 5. 4 个用户控制命令

完整文档见 [docs/USER_CONTROLS.md](USER_CONTROLS.md)。简版:

```bash
# 标记重要
python -m src.cli.user_controls important <seg_id>

# 标记循环
python -m src.cli.user_controls cycle <seg_id> "每周一"

# 归档
python -m src.cli.user_controls archive <seg_id>

# 雾化(不可逆!)
python -m src.cli.user_controls fog <seg_id> --anchor "≤20字锚点"
```

## 6. 配置 LLM

LLM 配置在 `~/.mtca/config.toml`,首次启动自动生成。详见 [docs/LLM_PROVIDERS.md](LLM_PROVIDERS.md)。

## 7. 接 MCP(Claude Code / Cursor)

详见 [docs/MCP_INTEGRATION.md](MCP_INTEGRATION.md)。最简 5 行配置即可。

## FAQ

**Q1: GUI 启动报 `ModuleNotFoundError: No module named "PySide6"`?**
A:`pip install -e ".[gui]"` 装齐 GUI 依赖。

**Q2: 时间线空白?**
A:数据库为空。可以通过 `examples/` 灌测试数据,或让 Agent 接入开始记对话。

**Q3: 雾化能撤销吗?**
A:**不能**。雾化是物理擦除,只保留骨架。先打 `/重要` 再雾化,骨架会一直在。

**Q4: 跨设备同步?**
A:MTCA 不内置同步。`~/.mtca/mtca.db` 是单文件,可以拷到云盘 / U 盘,但不要同时在两台机器打开(文件锁冲突)。

## 下一步

| 想了解 | 看哪 |
|--------|------|
| 完整用户视角 | [docs/USER_GUIDE.md](USER_GUIDE.md) |
| 命令规范 | [docs/USER_CONTROLS.md](USER_CONTROLS.md) |
| 接 Agent 平台 | [docs/ADAPTERS.md](ADAPTERS.md) |
| 配 LLM | [docs/LLM_PROVIDERS.md](LLM_PROVIDERS.md) |
| 接 MCP | [docs/MCP_INTEGRATION.md](MCP_INTEGRATION.md) |
| 架构(高层) | [docs/ARCHITECTURE.md](ARCHITECTURE.md) |