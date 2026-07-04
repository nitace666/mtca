# MTCA 快速开始（QUICKSTART）

> 适用版本：v0.4（M1 阶段）
> 预计用时：10 分钟跑通 GUI + CLI

---

## 1. 项目简介

**MTCA**（Multi-Tier Context Architecture）是一个 **本地优先（local-first）** 的 AI 长期记忆中间件：把全部对话原文持久化到本地 SQLite，对外按 L0 / L1 / L2 / L3 多档压缩视图供 Agent 召回，**用户主权 + L0 不可变 + 雾化不可逆 + GUI 优先**。MTCA 不接管 Agent 平台（如 OpenClaw / Hermes / 扣子），而是通过 MCP Server 或 CLI 旁路接入，按需召回 L0 原文。

---

## 2. 安装（pip install -e .）

### 2.1 系统要求

| 项 | 要求 |
|---|---|
| Python | 3.10 或更高（启用 `match` 语句与 `X \| Y` 类型联合） |
| 操作系统 | Windows 10+ / macOS 12+ / Ubuntu 20.04+ |
| 磁盘 | ≥ 2 GB 可用（数据库 + 日志） |
| 内存 | ≥ 4 GB（GUI + 本地 LLM 推理） |

### 2.2 克隆与安装（开发模式）

```bash
# 1. 克隆仓库
git clone https://github.com/nitace666/mtca.git
cd mtca

# 2. 创建并激活虚拟环境
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. 以 editable 模式安装（含 src/ 路径、所有依赖）
pip install -e .

# 4. 验证安装
python -c "import src; print('MTCA OK')"
```

> 如果 `pip install -e .` 报 `pyproject.toml not found`，说明仓库还未发布打包元数据，请改用 `python -m pip install -r requirements.txt`（M1 阶段暂未生成 pyproject，开发分支直接 import `src.*` 模块即可）。

---

## 3. 启动 GUI（python gui/run.py）

### 3.1 一键启动

```bash
python gui/run.py
```

首次启动会自动创建 `~/.mtca/` 目录与 `mtca.db` 数据库文件（SQLite + WAL + FTS5）。

### 3.2 GUI 主窗口布局

```
┌──────────────────────────────────────────────────────────┐
│ [DB: ~/.mtca/mtca.db]              [刷新] [雾化] [归档]   │  ← 工具栏
├────────────────────┬─────────────────────────────────────┤
│ 时间线（左侧 30%）  │ 段落详情（右侧 70%）                 │
│                    │                                     │
│ ● 2026-07-05 14:22 │ 话题：MTCA 启动优化                  │
│   ├─ L0 骨架       │ 时间：2026-07-05 14:22              │
│   ├─ 关键词：GUI…  │ 关键词：GUI, sqlite, 启动           │
│   └─ 锚点句…       │ 锚点句：第一次启动 GUI 用了 4.2s    │
│                    │ ─────────────────────────────────── │
│ ● 2026-07-04 09:11 │ [完整对话正文]                      │
│   └─ (dormant)     │ （长文本 / 工具调用 / 代码块）       │
└────────────────────┴─────────────────────────────────────┘
```

> **截图占位**：启动后主窗口截图保存为 `docs/screenshots/gui-main.png`，包含工具栏 + 时间线 + 详情三栏。

### 3.3 自定义数据库路径

```bash
# 使用自定义 DB（多环境隔离）
python gui/run.py --db /path/to/test.db
```

---

## 4. 启动 CLI 时间线（python -m src.cli.timeline timeline）

GUI 之外的开发期辅助工具，基于 [rich](https://github.com/Textualize/rich) 渲染。

```bash
# 默认：今天的时间线（day = 1 天窗口）
python -m src.cli.timeline timeline

# 最近 7 天
python -m src.cli.timeline timeline --period week

# 最近 30 天
python -m src.cli.timeline timeline --period month

# 按项目过滤（topic_label 模糊匹配）
python -m src.cli.timeline timeline --project "MTCA"

# 关键词搜索（FTS5 + topic_label）
python -m src.cli.timeline timeline --query "雾化"

# 树状视图（按话题聚合）
python -m src.cli.timeline tree --project "MTCA" --limit 200
```

输出示例（节选）：

```
2026-07-05  ● MTCA 启动优化            [active]
2026-07-05  ● GUI 时间线组件            [active]
2026-07-04  ○ SQLite 存储层             [dormant]
2026-07-03  ■ 已删除片段                [fogged_once]  关键词：sqlite, WAL
2026-07-01  ─ 历史对话                  [archived]
```

---

## 5. 4 个用户控制命令示例

> 实现位置：[`src/cli/user_controls.py`](../src/cli/user_controls.py)（T11 / USER_CONTROLS.md §2）。
> 所有命令 **直达 L0 层**，跳过任何压缩视图；前缀 `/xxx` 打在句中任意位置都能识别。

### 5.1 `/重要` — 段落 score → 10000，永远 L1

```bash
# 把段落 seg-uuid 标记为"永久重要"，永远进入 L1 视图
python -m src.cli.user_controls important seg-uuid-xxxx
```

或自然语言：

```bash
python -c "from src.cli.user_controls import parse_natural; print(parse_natural('把 seg-uuid-xxxx 标记为重要'))"
# 输出: ('important', ['seg-uuid-xxxx'])
```

### 5.2 `/循环` — 打 cycle_tag，跳过时间衰减

```bash
# 标记为"每周一循环"任务，时间打分不再衰减
python -m src.cli.user_controls cycle seg-uuid-xxxx "每周一"
```

### 5.3 `/归档` — tier → L3_hidden，不删 L0 全文

```bash
# 归档段落：摘要视图不再展示，但 L0 原文 + 骨架仍在（可被关键词搜索召回）
python -m src.cli.user_controls archive seg-uuid-xxxx
```

### 5.4 `/雾化` — 物理擦除 L0-细节，保留骨架 + 锚点句（不可逆）

> ⚠️ **警告：雾化不可逆！**
> - 物理擦除 L0-细节（完整对话、工具调用、长文本），**无法恢复**；
> - 仅保留 L0-骨架（话题标题、时间、关键词、锚点句）；
> - AI 召回时第一次返回「片段已删除，关键词：xxx」+ 锚点句，第二次后进入 `archived` 状态；
> - **AI 无 /雾化 权限**（API 拒绝），只能由用户主动触发；
> - 锚点句 ≤ 20 字，用于兜底召回。

```bash
# 必须显式提供 --anchor（≤ 20 字）
python -m src.cli.user_controls fog seg-uuid-xxxx --anchor "项目立项决定"

# 雾化前请三思：先在 GUI 时间线右键段落 → 「预览雾化效果」
```

---

## 6. 配置文件（`~/.mtca/config.toml`）

### 6.1 默认位置

| 平台 | 路径 |
|---|---|
| Windows | `C:\Users\<用户>\.mtca\config.toml` |
| macOS / Linux | `~/.mtca/config.toml` |

### 6.2 示例配置

```toml
# ~/.mtca/config.toml
# M1 阶段由 src/llm/provider.py 加载；缺文件时使用内置默认值。

[llm]
default = "ollama"        # auto / ollama / lmstudio / llamacpp / cloud
fallback = "cloud"         # 主 provider 不可用时的备选

[ollama]
base_url = "http://localhost:11434"
model = "qwen3.6:35b-a3b"

[cloud]
# M1 仅保留接口，不真实调用（T10 占位）
api_base = "https://api.example.com/v1"
api_key_env = "MTCA_CLOUD_KEY"   # 读取环境变量，不写入文件
```

### 6.3 修改生效

```bash
# 1. 编辑 config.toml
# 2. GUI：重启 GUI（Ctrl+C → python gui/run.py）
# 3. CLI：下一条命令自动重新加载（单进程内不热更新）
```

---

## 7. 常见问题（FAQ）

### Q1：GUI 启动报 `ModuleNotFoundError: No module named 'PySide6'`？

A：T13 依赖未装齐。执行：

```bash
pip install -e ".[gui]"      # 完整 GUI 依赖
# 或
pip install PySide6 rich click jieba httpx
```

### Q2：CLI 时间线输出空白，没有任何段落？

A：可能是首次启动数据库为空，或 `--period` 窗口太短。先尝试拉大窗口：

```bash
python -m src.cli.timeline timeline --period month --limit 200
```

仍为空则说明还没有任何对话被写入 L0；可用 `python -m src.cli.user_controls` 命令手动写入测试段，或通过 `examples/` 下的示例脚本灌数据。

### Q3：`/雾化` 之后能撤销吗？

A：**不能**。雾化是物理擦除 L0-细节，骨架 + 锚点句 + 关键词是 AI 仅存的提示。`/重要` 和 `/循环` 可叠加使用——雾化前先把"重要标记"打好，雾化后骨架仍在，`/重要` 优先级最高会兜底。

### Q4：数据库能跨设备同步吗？

A：M1 阶段 **不内置同步**。`~/.mtca/mtca.db` 是单文件 SQLite，可以直接拷贝到另一台机器的 `~/.mtca/` 目录下。注意：
- 不要在两台机器同时写同一个 DB（SQLite 文件锁）；
- 推荐用云盘（OneDrive / iCloud / Syncthing）做目录级同步，关闭后重启。

### Q5：能接 OpenClaw / Hermes / 扣子 吗？

A：M2 才出 MCP Server 与平台适配器；M1 阶段只能通过 CLI 控制 L0，或把 MTCA 作为 Python 库 import 到自己的脚本里。详细接入见 [`docs/ADAPTERS.md`](ADAPTERS.md)（M2 启用）。

---

## 8. 下一步

| 想做什么 | 看哪里 |
|---|---|
| 理解整体架构 | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| 查 L0 / L1 / L2 / L3 数据结构 | [`DATA_MODEL.md`](DATA_MODEL.md) |
| 看用户控制接口完整规范 | [`USER_CONTROLS.md`](USER_CONTROLS.md) |
| 接入 Agent 平台 | [`ADAPTERS.md`](ADAPTERS.md)（M2） |
| 了解设计决策 | [`V0.4_PIVOT.md`](V0.4_PIVOT.md) |

---

**反馈**：规则不合理 → 改 `AI_RULES.md`；文档错误 → 改 `docs/QUICKSTART.md`；BUG → 在 GitHub 提 Issue。