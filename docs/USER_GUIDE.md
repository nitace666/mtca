# MTCA 用户指南

> **版本**：MTCA v0.5（M2.5 收尾版）  
> **写给**：完全不懂代码、装了 MTCA 想用起来的真实用户  
> **预计读完**：5 – 30 分钟

---

## 1. 欢迎：你打开了 MTCA

**MTCA 是什么？一句话：它是给你和 AI 用的「永不丢失的笔记本」。**

你平时用 ChatGPT、Claude、Cursor 这些 AI 工具，最大的痛点是什么？是不是聊过的内容过几天就找不到了？三年前的某段对话突然想起来很有用，结果翻遍所有聊天记录都搜不到？

MTCA 解决的就是这个问题。它是一个装在你电脑本地的"长期记忆盒"：

- 所有 AI 对话会被悄悄持久化存到本地的 SQLite 数据库
- 不管是一周前还是三年前，你搜关键词它都能找到原文
- 数据**永远在你电脑里**，不联网也能用，AI 公司倒闭了也不影响

**和"AI 记事本"有什么区别？**

| 普通 AI 记事本 | MTCA |
|---|---|
| 记了就要自己整理 | AI 帮你自动记，你不用动手 |
| 关键字搜索经常找不到 | FTS5 全文索引 + 段落级匹配 |
| 时间久了就被覆盖 | 重要的内容永远不会"下沉"丢失 |
| 云端，你看不到摸不着 | 本地一个 `.db` 文件，你随时能拷走 |

**最关键的一条规矩（第 9 铁律）**：

> **MTCA 永远不会忘记重要的事。**  
> 你标了"重要"的内容、紧急的事项、强调过的截止日期——它们会一直在那里，哪怕过去 1 年、3 年。

---

## 2. 快速安装

### 2.1 你需要先准备什么

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10+ / macOS 12+ / Ubuntu 20.04+ |
| Python | 3.10 或更高（[python.org](https://www.python.org/downloads/) 下载） |
| 磁盘空间 | ≥ 2 GB 可用 |
| 内存 | ≥ 4 GB |

### 2.2 一行命令装好

打开终端（Windows 用 PowerShell，Mac/Linux 用 Terminal），输入：

```bash
pip install -e .
```

> 这一步需要在 MTCA 的源码目录下执行。如果你下载的是源码包，进入解压后的文件夹再跑这条命令。

### 2.3 验证装好了

```bash
python -c "import src; print('MTCA OK')"
```

如果屏幕上打出 `MTCA OK`，说明装好了。

---

## 3. 第一次使用：5 分钟跑起来

### 3.1 打开 GUI

```bash
python gui/run.py
```

你会看到一个标题为 "MTCA — 长期记忆中间件" 的窗口。窗口刚启动是空的（你还没有任何对话被记录）。

![MTCA 主窗口](docs/assets/user_guide/cn_main_window.png)

主窗口分两部分：

- **顶部**：5 个标签页（时间线 / 详情 / 知识图谱 / 操作 / 4 象限）
- **左侧**：按时间倒序排列的段落列表
- **底部状态栏**：当前数据库路径

### 3.2 第一次写一段记忆

MTCA 自己没有"输入框"——它的工作方式是：**AI 工具的对话会被自动持久化**。

最简单的体验方法：在终端跑这条命令手动写入一条测试段：

```bash
python -m src.cli.timeline timeline --period month
```

如果你看到一段时间线输出（哪怕是空的），说明 CLI 通了。

### 3.3 想看真正的"4 象限"？

打开 "4 象限" 标签页，你会看到：

- **顶部黄色 URGENT 红条**：告诉你有没有过期的紧急事项
- **2 × 2 网格**：四个象限分别装不同重要度 / 紧急度的话题

![MTCA 4 象限视图 + URGENT 红条](docs/assets/user_guide/cn_quadrant_with_urgent.png)

如果你的数据库是空的，4 个象限都显示"空"。下一步（第 4 章）我们讲怎么填满它。

---



### 5. 一键跑 demo（CLI-only 5 分钟体验）

不用 GUI、不用 shell 命令，一条命令跑通 MTCA 全部核心功能：

```bash
python examples/demo_run.py
```

跑完会看到 8 个 step 的中文输出（环境检查 → 隔离 DB → 3 段演示对话 → /紧急 → /重要 → 召回 → 第 9 铁律 → 收尾），全程隔离在 `examples/__demo_db/demo.db`，不会污染你的 `~/.mtca/mtca.db`。

可选参数：

| 选项 | 作用 |
|---|---|
| `--keep-db` | 跑完保留 demo DB，方便事后用 GUI 打开看看 |
| `--db /path/to/isolated.db` | 自定义 DB 路径（测试用） |

退出时默认清理 demo DB；`.bak-<timestamp>` 备份留最近 3 个。

如果只想看代码怎么写、跑哪些 API：

```bash
python examples/demo_run.py --keep-db
python -c "import sqlite3; c = sqlite3.connect('examples/__demo_db/demo.db'); \
  print(c.execute('SELECT segment_id, current_score, urgent_state FROM segments').fetchall())"
```

SMOKE 测试：

```bash
python -m pytest tests/test_demo.py -q
```

5 个测试（端到端：跑 demo / 段数 / URGENT 段 / /重要 段 / 召回 / 铁律 9 不衰减）都过就算 OK。

---
## 4. 日常使用：GUI 篇

MTCA 装好后，最常用的入口就是 GUI。这里重点讲窗口的 5 个标签页怎么用。

### 4.1 时间线（默认页）

按时间倒序列出所有段落，每段显示：

- 时间段（如 `13:00-13:05`）
- 话题（如 `下班顺路取快递`）
- 当前评分（`score=100`）
- 状态标签：`[active]` / `[dormant]` / `[/雾化]` 等

点击某条记录，右侧（详情标签页）会显示这条段的完整内容。

### 4.2 详情

显示选中段的完整信息：

- 话题 + 关键词 + 锚点句
- 完整对话原文
- 关联段（supersedes / superseded by / related_to）

### 4.3 知识图谱

可视化段落之间的关系网络。节点大小 = 重要性，边颜色 = 关系类型。

### 4.4 操作

对当前选中的段落做 4 个动作：

| 按钮 | 行为 |
|---|---|
| `/重要` | 当前段落 score → 10000，永远 L1 |
| `/循环 X` | 打周期标签，跳过时间衰减 |
| `/归档` | tier → L3_hidden，不主动召回 |
| `/雾化` | **物理擦除 L0 细节**（不可逆！） |

> ⚠️ **`/雾化` 是不可逆操作！** 点之前会弹一个强警告对话框，要你输 `我确认雾化` 才执行。一旦雾化，对话原文就被永久删除，只剩话题标题 + 关键词 + 锚点句（≤ 20 字）。  
> 详细的雾化规则见 [第 7 章](#7-紧急事项追踪)。

### 4.5 4 象限

把段落按"重要 + 紧急"两个维度分类，最直观地看到"什么该先做"。第 6 章详细讲。

---

## 5. 日常使用：CLI 篇

不爱用 GUI？MTCA 也提供命令行（CLI）。

### 5.1 时间线 CLI

```bash
# 看今天的时间线
python -m src.cli.timeline timeline

# 看最近 7 天
python -m src.cli.timeline timeline --period week

# 按项目过滤
python -m src.cli.timeline timeline --project "MTCA"

# 关键词搜索
python -m src.cli.timeline timeline --query "演示"

# 树状视图
python -m src.cli.timeline tree --project "MTCA"
```

输出长这样（黑底白字终端风格）：

![CLI 时间线输出](docs/assets/user_guide/cn_cli_timeline.png)

每行格式：时间段 + 话题 + score + 状态。

### 5.2 用户控制 CLI

7 个命令，从对话里直接调（也可以在终端调）：

```bash
python -m src.cli.user_controls --help
```

![CLI 用户控制命令](docs/assets/user_guide/cn_cli_help.png)

命令清单：

| 命令 | 干什么 | 可逆？ |
|---|---|---|
| `important <id>` | `/重要`：score 锁底，永远 L1 | ✅ 可降级 |
| `cycle <id> <tag>` | `/循环`：打周期标签（如"周一"），跳过时间衰减 | ✅ 可取消 |
| `archive <id>` | `/归档`：tier 变 L3_hidden，不主动召回 | ✅ 可恢复 |
| `fog <id> --anchor "..."` | `/雾化`：物理擦除细节（不可逆） | ❌ **不可逆** |
| `urgent <id> <when>` | `/紧急 <段> <截止时间>`：标记段为紧急追踪 | ✅ |
| `done <id>` | `/完成`：把过期 / tracking 段标记 completed | ✅ |
| `postpone <id> [when]` | `/延期 <段> [新时间]`：重置过期时间（默认 +7 天） | ✅ |

时间格式支持：明天 / 后天 / 下周 / 3d / 7d / `2026-07-10` / 毫秒戳。

### 5.3 一个完整例子

把"明天客户演示的 PPT 还没改完"标记为紧急、明天 14:00 前必回：

```bash
# 1. 先找到段 ID（在时间线输出里）
python -m src.cli.timeline timeline --query "PPT"

# 2. 假设返回 ID 是 seg-xxxxxx
python -m src.cli.user_controls urgent seg-xxxxxx "明天" --anchor "演示前 PPT 必改完"

# 3. 输出：[OK] 已 /紧急 seg-xxxxxx → expires=... (state=tracking)
```

![CLI /紧急 命令演示](docs/assets/user_guide/cn_cli_urgent.png)

完成后这条段会出现在 GUI "4 象限" 页的顶部 URGENT 红条里，到了时间就提醒你。

---

## 6. 4 象限怎么理解

MTCA 把所有段落按"重要 + 紧急"两个维度分成 4 类，这是它最直观的"助理大脑"视图。

### 6.1 两个维度

| 维度 | 含义 | 取值范围 |
|---|---|---|
| **urgency（紧急度）** | 时效压力：高 = 急 / 低 = 缓 | 0.0 - 1.0 |
| **importance（重要度）** | 长期价值：高 = 重要 / 低 = 闲聊 | 0.0 - 1.0 |

### 6.2 4 个象限 + 1 个特殊类

| 类别 | 重要度 | 紧急度 | 半衰期 | 例子 |
|---|---|---|---|---|
| **Q1 重要 + 紧急** | ≥ 0.7 | ≥ 0.7 | 180 天 | "明天客户演示"、"月底前必交的方案" |
| **Q2 重要 + 不紧急** | ≥ 0.7 | < 0.7 | 365 天 | "读完《思考，快与慢》第 5 章"、"OKR" |
| **Q3 不重要 + 紧急** | < 0.7 | ≥ 0.7 | 14 天 | "超市要买鸡蛋牛奶"、"下午 3 点开空调" |
| **Q4 都不沾** | < 0.7 | < 0.7 | 3 天 | "今天天气不错"、"听说隔壁组换了咖啡机" |
| **URGENT 已强调时效** | 任意 | 任意 | **永不衰减** | "今天 17:00 前必回邮件"、"周一前完成" |

**关键规矩**：

- **Q1 段 + 标了 `/重要` 的段 + URGENT 段 = 3 类永不遗忘**（第 9 铁律）
- 其他象限的段落会随时间慢慢"下沉"，最终关键词级别搜索还能找到，但不会主动弹出来烦你

### 6.3 URGENT 红条：过期的紧急事项

如果某条 URGENT 段过期了（你强调的截止时间过了），它会从原象限消失，跑到顶部 URGENT 红条里：

> ⚠ N 段紧急过期待处理（双击查看）

这时候你需要做决定：

- **已完成**：跑 `done <id>`
- **要延期**：跑 `postpone <id> 7d`
- **其实很重要**：跑 `important <id>` 把它锁底

### 6.4 4 象限的视觉规则

| 象限 | 颜色 | 含义 |
|---|---|---|
| Q1 | 🔴 红 | 重要 + 紧急，今天 / 这周必须做 |
| Q2 | 🟢 青 | 重要 + 不紧急，长期价值 |
| Q3 | 🟠 橙 | 不重要 + 紧急，琐事 |
| Q4 | ⚫ 灰 | 闲聊 / 心情，可丢弃 |

URGENT tracking 段在象限内显示时会有 `⚠` 前缀，过期段加粗。

---

## 7. 紧急事项追踪（`/紧急` `/完成` `/延期`）

这一章讲 MTCA 最有"助理感"的功能：**紧急事项追踪**。

### 7.1 它解决什么问题

你平时会有这种焦虑："这个月到底有多少事得做？哪些已经过期了？"

MTCA 的紧急追踪就是给你一个**清单 + 自动提醒**。一旦你标了 `/紧急`，系统会：

1. 把这条段放进 4 象限的 Q1（如果它本来就紧急）或新建一个 URGENT tracking 段
2. 顶部 URGENT 红条一直显示剩余 / 过期情况
3. 时间一到，它会从原位置跳到红条里提醒你"该处理了"

### 7.2 4 个场景示例

#### 场景 A：明天必须交方案

```bash
# 找到方案段 ID
python -m src.cli.timeline timeline --query "方案"

# 标记紧急，明天截止
python -m src.cli.user_controls urgent seg-方案-id "明天" --anchor "月底前必交"
```

明天到了，这条段会自动出现在 URGENT 红条里。

#### 场景 B：完成的紧急事项

```bash
# 我已经做完了
python -m src.cli.user_controls done seg-方案-id
```

段状态变 `completed`，从红条消失，从 4 象限里也消失。

#### 场景 C：要延期

```bash
# 顺延 7 天
python -m src.cli.user_controls postpone seg-方案-id "7d"

# 顺延到下周五
python -m src.cli.user_controls postpone seg-方案-id "2026-07-15"
```

#### 场景 D：发现这条很重要，长期跟进

```bash
# 升级为 /重要，永远不丢
python -m src.cli.user_controls important seg-方案-id
```

### 7.3 时间格式速查

`/紧急 <时间>` 和 `/延期 <时间>` 都支持这些格式：

| 写法 | 含义 |
|---|---|
| `明天` / `后天` | 相对 1 / 2 天 |
| `下周` / `下月` | 相对 7 / 30 天 |
| `3d` / `7d` / `30d` | N 天后 |
| `尽快` / `马上` / `立刻` | 今天 |
| `2026-07-10` | ISO 日期 |
| `2026-07-10T15:30` | ISO 日期 + 时间 |
| `1710000000000` | 毫秒时间戳 |

---

## 8. 备份你的数据

MTCA 的数据**只在你电脑本地一个 SQLite 文件**里。这个文件丢了就什么都没了，所以备份很重要。

### 8.1 数据库在哪里

| 平台 | 路径 |
|---|---|
| **Windows** | `C:\Users\<你的用户名>\.mtca\mtca.db` |
| **macOS / Linux** | `~/.mtca/mtca.db` |

数据库旁边会自动建一个 `backups/` 目录，每天自动备份一份。

### 8.2 自动备份

MTCA 启动时会自动：

1. 把当前 `mtca.db` 复制到 `backups/mtca-YYYYMMDD.db`
2. 清理 7 天前的旧备份

你不需要动手。要查看备份：

- Windows 资源管理器打开 `C:\Users\<你>\.mtca\backups\`
- 或终端：`ls ~/.mtca/backups/`

### 8.3 手动备份

保险起见，每隔一段时间手动拷一份：

**Windows**：

```powershell
# 整目录拷到 OneDrive
Copy-Item -Recurse $env:USERPROFILE\.mtca $env:USERPROFILE\OneDrive\mtca-backup-2026-07-06
```

**macOS / Linux**：

```bash
cp -r ~/.mtca ~/iCloud/mtca-backup-$(date +%Y-%m-%d)
```

### 8.4 跨设备同步

MTCA 的 SQLite 是单文件，你可以：

- **云盘同步**：把整个 `~/.mtca/` 目录加进 OneDrive / iCloud / Syncthing / Dropbox  
  ⚠️ **不要同时在两台机器打开同一个 DB**（SQLite 文件锁）。先关 MTCA 再同步。
- **手动 U 盘拷**：每周 / 每月拷一次 DB 文件到另一台机器的 `~/.mtca/` 目录

---

## 9. 常见问题 FAQ

### Q1：装好打开 GUI 是空的，正常吗？

正常。如果你刚装好还没让 AI 工具接入 MTCA，数据库当然是空的。GUI 时间线和 4 象限都显示空白是预期行为。

### Q2：怎么把 AI 对话自动写入 MTCA？

MTCA 本身不直接"接"AI 工具，需要通过：

- **MCP Server**（推荐）：让 Claude Code / Cursor 接入，5 行配置即可。详细见 [docs/MCP_INTEGRATION.md](MCP_INTEGRATION.md)
- **CLI 命令手动写入**：用 `python -m src.cli.user_controls` 系列命令

### Q3：4 象限页面打开报错怎么办？

最常见原因是**数据库没经过 M2.5.1 schema 迁移**。打开 Python 终端跑：

```python
from src.store.sqlite import init_db
init_db()
```

这条命令会触发 schema 迁移，给 segments 表加 5 个新字段（urgency / importance / emotion / expires / urgent_state），然后重启 GUI 就 OK。

### Q4：`/雾化` 之后能撤销吗？

**不能。** 雾化是**物理擦除**对话原文，只保留骨架（话题标题 + 时间 + 关键词 + 锚点句 ≤ 20 字）。AI 召回时第一次返回"片段已删除，关键词：xxx + 锚点句"，第二次后进入 `archived` 状态不再主动召回。

**经验法则**：雾化前先打 `/重要` 标记，骨架层（标题 / 关键词）永远保留，AI 至少能记起"你做过这件事"。

### Q5：数据库可以多人共享吗？

不行。MTCA 是单人本地工具，SQLite 文件锁不支持并发写。如果你想多设备用，建议方案：

- 每人独立 `~/.mtca/` 目录
- 用云盘做目录级**备份级**同步（不是实时），关 MTCA 后再同步

### Q6：占用磁盘多大？

平均**纯文本 1-2 GB / 年**。如果你的对话特别多（每天 1000 条），一年约 5-8 GB。

要清理：删 `~/.mtca/backups/` 里的旧文件即可。

### Q7：怎么知道我有没有"重要"的内容快过期？

GUI 4 象限页面顶部 URGENT 红条会一直显示。CLI 跑：

```bash
python -m src.cli.timeline timeline --query "重要"
```

也可以在时间线里搜 `/重要` 标签的段。

### Q8：我的 LLM provider 配置在哪里？

`~/.mtca/config.toml`，首次启动会自动生成。支持的 provider：

- `ollama`（本地，推荐）
- `lmstudio`
- `llamacpp`（server 模式）
- `cloud`（需要 API key）

详细配置见 [docs/QUICKSTART.md §6](QUICKSTART.md)。

### Q9：MTCA 和我的 ChatGPT / Claude 冲突吗？

**完全不冲突**。MTCA 是旁路工具，不接管任何 AI 平台。你的 ChatGPT / Claude 正常工作，MTCA 在背后默默记录对话。

> 设计原则：**Agent 跑它的，MTCA 跑 MTCA 的，互不干扰，按需召回。**

可以。MTCA 的核心功能（时间线查询 / 段落操作 / 紧急追踪）全部都有 CLI 命令。GUI 只是更直观的可视化包装。

---

## 9.5 常见错误信息

> **重要保证（M2.5.8 A-3）**：从这一版开始，**用户撞错不再看到 Python traceback**。
> CLI 命令会把错误打到 stderr（GUI 操作会弹 QMessageBox 警告框），
> 格式统一为 `❌ 中文一句话 + 💡 建议 + （退出码 N）`。

错误信息由 `src/errors.py` 统一生成。退出码含义：

| 退出码 | 常量名 | 含义 |
|---|---|---|
| 0 | `EXIT_OK` | 成功 |
| 1 | `EXIT_USER_ERROR` | 用户操作错误（参数错 / 文件不存在 / 缺参数） |
| 2 | `EXIT_DB_ERROR` | 数据库错误 + 兼容旧测试的校验错误 |
| 3 | `EXIT_LLM_ERROR` | LLM provider 不可用 / 网络超时 |
| 99 | `EXIT_INTERNAL` | 内部未预期错误（理论上不应该看到） |

⚠️ **退出码 2 是"过载"的**：DB 错误、参数校验、权限不足都共用 2，
这是为了向后兼容既有 581 个测试。用户从 stderr 的 message 文本能区分。
未来 polish 会重整 EXIT 码表。

### 7 个常见错误速查

| # | 触发场景 | 你会看到 | 退出码 | 怎么办 |
|---|---|---|---|---|
| 1 | `--period invalid` 之类参数填错 | `❌ 参数 --period 非法`<br>`💡 建议：检查参数值是否合法。'invalid' is not one of 'day', 'week', 'month'.` | 1 | 看 `--help` 确认合法值；值是否大小写敏感 |
| 2 | `--db /path/that/does/not/exist.db` 路径写错 | `❌ 文件不存在：Z:\`<br>`💡 建议：检查路径是否正确，或文件是否已被删除` | 1 | 检查路径拼写；如果是相对路径，确认当前目录 |
| 3 | 数据库文件 locked（被另一个进程占用） | `❌ 数据库错误`<br>`💡 建议：检查 ~/.mtca/mtca.db 是否存在且可写。原始错误：database is locked` | 2 | 关掉其他 SQLite 客户端；或重启后重试 |
| 4 | `~/.mtca/mtca.db` 没写权限 | `❌ 权限不足：/home/user/.mtca/mtca.db`<br>`💡 建议：检查文件权限，或用管理员/属主身份运行` | 2 | `chmod 644 ~/.mtca/mtca.db`；或换一个有写权限的目录 |
| 5 | LLM provider 没配 / 网络不通 | `❌ LLM 调用失败`<br>`💡 建议：检查 ~/.mtca/config.toml 中 provider 配置，或网络是否通畅` | 3 | 见 `docs/LLM_PROVIDERS.md` §配置 |
| 6 | `cmd_important <不存在段ID>` | `❌ 段落不存在：segment_id=xxx`<br>`💡 建议：用 `timeline` 命令查一下正确的段 ID` | 1 | 用 `python -m src.cli.timeline timeline --query <关键词>` 找段 |
| 7 | GUI 点按钮撞错（如 `/重要`） | QMessageBox 弹窗：<br>标题 `❌ /重要 失败`<br>正文 `文件不存在：/x.db` + 空行 + `💡 建议：...`<br>点 OK 关掉 | （GUI 不退出码） | 按弹窗里的建议操作；或看状态栏 |

### 怎么定位"我刚才到底撞了什么错"

**CLI 用户**：

```powershell
# 跑命令，撞错时看 stderr
python -m src.cli.timeline timeline --period invalid
# ❌ 参数 --period 非法
# 💡 建议：检查参数值是否合法。'invalid' is not one of 'day', 'week', 'month'.
# （退出码 1）

# 检查退出码（PowerShell 用 $LASTEXITCODE，bash 用 $?）
echo $LASTEXITCODE  # 1
```

**GUI 用户**：

QMessageBox 弹窗就是错误信息本身，不需要查日志。

如果弹窗没出现但状态栏显示红色，先把鼠标停在状态栏文本上看完整 tooltip（有时文字会被截断）。

### 出错想报告 bug？

1. 记下完整的 stderr 三行（或 QMessageBox 正文）
2. 记下复现命令（脱敏后）
3. 查 `~/.mtca/mtca.db` 是否完整（`sqlite3 ~/.mtca/mtca.db ".schema"` 应能跑）
4. 发到 issue tracker，附 1+2+3

> ⚠️ **不要把 Python traceback 截图发 issue**——你不会看到 traceback，
> 因为 A-3 已经把 traceback 拦截在内部了。如果真看到了 traceback，
> 那是 bug，请附完整 traceback + 复现命令。

---

## 10. 速查 cheat sheet

| 我想…… | 怎么做 |
|---|---|
| 打开 GUI | `python gui/run.py` |
| 看今天时间线 | `python -m src.cli.timeline timeline` |
| 看最近 7 天 | `python -m src.cli.timeline timeline --period week` |
| 看最近 30 天 | `python -m src.cli.timeline timeline --period month` |
| 关键词搜索 | `python -m src.cli.timeline timeline --query "关键词"` |
| 按项目过滤 | `python -m src.cli.timeline timeline --project "项目名"` |
| 标记重要 | `python -m src.cli.user_controls important <seg_id>` |
| 标紧急 + 截止 | `python -m src.cli.user_controls urgent <seg_id> "明天" --anchor "说明"` |
| 标记完成 | `python -m src.cli.user_controls done <seg_id>` |
| 延期 | `python -m src.cli.user_controls postpone <seg_id> "7d"` |
| 归档 | `python -m src.cli.user_controls archive <seg_id>` |
| 雾化（不可逆！） | `python -m src.cli.user_controls fog <seg_id> --anchor "≤20字锚点"` |
| 数据库位置 | `~/.mtca/mtca.db`（Windows: `%USERPROFILE%\.mtca\mtca.db`） |
| 备份目录 | `~/.mtca/backups/` |

---

## 11. 进阶用法

如果你用顺手了，想进一步深入：

| 你想了解 | 看哪 |
|---|---|
| 开发者速查（pip install / API / 数据结构） | [docs/QUICKSTART.md](QUICKSTART.md) |
| `/重要 /循环 /归档 /雾化` 完整规范 | [docs/USER_CONTROLS.md](USER_CONTROLS.md) |
| 让 Claude Code / Cursor 通过 MCP 接入 | [docs/MCP_INTEGRATION.md](MCP_INTEGRATION.md) |
| 整体架构 + 8+1 条铁律 | [docs/ARCHITECTURE.md](ARCHITECTURE.md) |
| L0 / L1 / L2 / L3 数据模型 | [docs/DATA_MODEL.md](DATA_MODEL.md) |
| 选哪个 LLM provider / 怎么配 | [docs/LLM_PROVIDERS.md](LLM_PROVIDERS.md) |

**9 条铁律（MTCA 不动摇的底线）**：

1. 数据主权在用户（本地 SQLite）
2. L0 双层 + AI 不可变（骨架 / 细节分层，AI 不能改）
3. L1–L3 是视图（可重建、可损失）
4. 雾化 = 用户主动丢弃细节（不可逆）
5. 召回错误自动回 L0-骨架
6. 用户控制接口直达 L0（`/重要 /循环 /归档 /雾化`）
7. GUI 优先（CLI 仅开发期辅助）
8. 存储成本由用户承担（1-2 GB / 年）
9. **重要记忆永不遗忘**（Q1 锁底 / URGENT 不衰减 / `/重要` 冻结）

---

**反馈**：规则不合理 → 改 `AI_RULES.md`；文档错误 → 改本文件或提 GitHub Issue；BUG → 在 GitHub 提 Issue。


## 12. 同步接口预留（M2.5.8 B，未来云同步插座）

**当前默认行为**：MTCA **不发任何数据到云端**。本地 SQLite 是唯一权威。

### 设计目的

为未来的云同步（Dropbox / iCloud / 自建 CRDT）预留标准接口；当前实现仅为 no-op 占位，确保未来接入云同步时**核心代码无需改动**。

### 三个核心组件

1. **`SyncAdapter`**（`src/sync/sync_adapter.py`）：云同步协议。任意第三方实现只需满足 `push(data) / pull(since_ms) / status()` 三个方法。
2. **`LocalOnlySync`**（`src/sync/local_only_sync.py`）：默认 no-op 实现。本地最权威，push 返回原 id，pull 永远空列表，status 报告 `mode=local_only`。
3. **`registry`**（`src/sync/registry.py`）：全局注册表，懒初始化，`threading.Lock` 保护并发安全。

### 三个 sync 钩子

`src/l0/segment_writer.py`、`src/l0/session_writer.py`、`src/llm/facts_store.py` 末尾各加一个 `sync_X_to_adapter(id, adapter=None, path=None)` 函数。失败不抛异常，仅 `warning` 日志 + 返回 `""`。

### 手动验证

```bash
python -c "from src.sync import get_adapter; a = get_adapter(); print(type(a).__name__, a.status())"
# 期望：LocalOnlySync {'mode': 'local_only', 'last_sync_ms': None, 'pending': 0, 'errors': []}
```

### 未来接入云同步

实现一个满足 `SyncAdapter` 协议的类，调用 `src.sync.set_adapter(my_adapter)` 即可切换全局适配器，**核心 writer 代码 0 改动**。