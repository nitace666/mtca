# MTCA 开发操作手册（v0.4 Vibe Coding 版）

> **给老板（PM）用的剧本**
> 你**不写代码**，只做 3 件事：**复制提示词 → 看 AI 输出 → 验收**
> 这份文档里有 28 个任务的完整提示词，复制喂给 AI 开发者即可

---

## 目录

- [0. 你的角色](#0-你的角色)
- [1. 项目当前状态](#1-项目当前状态)
- [2. 工具链](#2-工具链)
- [3. 5 步工作流](#3-5-步工作流)
- [4. M1 任务总表](#4-m1-任务总表)
- [5. T0–T28 详细提示词](#5-t0t28-详细提示词)
- [6. 测试流程](#6-测试流程)
- [7. 验收标准总表](#7-验收标准总表)
- [8. 故障处理](#8-故障处理)
- [9. 进度跟踪模板](#9-进度跟踪模板)
- [10. 常用命令速查](#10-常用命令速查)
- [11. 通用提示词模板](#11-通用提示词模板)

---

## 0. 你的角色

### 你是 PM（产品经理 + 任务派发员）

**做**：
- ✅ 读设计文档，理解每个 T 任务
- ✅ 复制 T 任务的"提示词"喂给 AI
- ✅ 跑测试命令看结果
- ✅ 用验收清单逐项打勾
- ✅ 通过 → 下一个 T；不通过 → 退回 AI 重做
- ✅ 每日更新进度跟踪表

**不做**：
- ❌ 写代码（除非修测试 fixture）
- ❌ 调试 AI 写错的代码（直接退回重写）
- ❌ 一次性派发多个 T（串行）

### AI 开发者角色

**AI 应该**：
- 用 Read 工具读必读文档
- 用 Write 工具写代码到指定路径
- 用 Bash 跑测试
- 输出文件清单 + 行数 + 测试结果
- 中文 commit message

**AI 不应该**：
- 改你没让它改的目录
- 跳任务
- 写超 1000 行的单文件（拆）
- 跳过测试

---

## 1. 项目当前状态

| 项 | 值 |
|---|---|
| 项目路径 | `I:\PROJECTS\MTCA\` |
| 协议 | MIT → 建议改 AGPL-3.0（M5 时）|
| 版本 | v0.4（设计锁定）|
| 代码行数 | 0（设计阶段）|
| 设计文档 | 完整（9 个 .md）|
| M1 任务 | T0–T28（~110h）|
| 启动命令 | `mtca-server start`（M1 末可用）|

**9 份必读设计文档**（你应该至少读 1-2-3-5）：

1. [README.md](file:///i:/PROJECTS/MTCA/README.md) — 项目导览 + 8 条铁律
2. [DEVELOPER_PLAN.md](file:///i:/PROJECTS/MTCA/DEVELOPER_PLAN.md) — 完整开发文档
3. [HANDOVER_XIAOBA.md](file:///i:/PROJECTS/MTCA/HANDOVER_XIAOBA.md) — AI 任务清单
4. [docs/DATA_MODEL.md](file:///i:/PROJECTS/MTCA/docs/DATA_MODEL.md) — schema 详解
5. [docs/ARCHITECTURE.md](file:///i:/PROJECTS/MTCA/docs/ARCHITECTURE.md) — 架构图
6. [docs/USER_CONTROLS.md](file:///i:/PROJECTS/MTCA/docs/USER_CONTROLS.md) — 命令 + GUI
7. [docs/V0.4_PIVOT.md](file:///i:/PROJECTS/MTCA/docs/V0.4_PIVOT.md) — v0.4 方向调整
8. [docs/COMPETITOR_ANALYSIS.md](file:///i:/PROJECTS/MTCA/docs/COMPETITOR_ANALYSIS.md) — 6 家竞品对比
9. 本文件 — 操作手册

---

## 2. 工具链

### 必备工具

| 工具 | 用途 | 备注 |
|---|---|---|
| **Cursor / Claude Code / Trae** | AI 开发 | 任选一个 |
| **VSCode** | 写测试、看代码 | 轻量 |
| **Python 3.10+** | 运行环境 | 系统装好 |
| **pytest** | 测试 | `pip install pytest` |
| **git** | 版本控制 | 系统装好 |
| **DB Browser for SQLite** | 看数据库 | 可选 |
| **Total Commander** | 文件管理 | Windows 推荐 |

### 推荐 AI 工具配置

| AI 工具 | 上下文窗口 | 适合 |
|---|---|---|
| **Claude Sonnet 4** | 200K | 主力（推荐）|
| **GPT-4o** | 128K | 备选 |
| **本地 Qwen3.6 35B A3B** | 32K | 大任务拆小后用 |
| **Cursor + Claude** | 200K | 边写边问 |

**推荐组合**：Cursor（编辑器）+ Claude Sonnet 4（模型）

### Python 环境初始化（一次性）

```bash
cd I:\PROJECTS\MTCA
python -m venv venv
venv\Scripts\activate
pip install -e ".[dev]"
```

---

## 3. 5 步工作流

### 完整循环

```
┌─────────────────────────────────────┐
│ 1. 读懂任务                          │
│    读 HANDOVER + 本文件对应 T 章节   │
└─────────────┬───────────────────────┘
              ▼
┌─────────────────────────────────────┐
│ 2. 派发任务                          │
│    复制 T 提示词 → 喂给 AI           │
└─────────────┬───────────────────────┘
              ▼
┌─────────────────────────────────────┐
│ 3. AI 输出                          │
│    等待 AI 在指定路径写代码          │
└─────────────┬───────────────────────┘
              ▼
┌─────────────────────────────────────┐
│ 4. 测试                              │
│    跑 pytest，看输出                  │
└─────────────┬───────────────────────┘
              ▼
┌─────────────────────────────────────┐
│ 5. 验收 / 退回                       │
│    验收清单全勾 → git commit          │
│    任一不勾 → 退回 AI 重做            │
└─────────────────────────────────────┘
              │
              ▼ 下一个 T
```

### 单 T 任务时间分配

| 步骤 | 你花的时间 | AI 花的时间 |
|---|---|---|
| 1. 读任务 | 5 min | 0 |
| 2. 派发 | 2 min | 0 |
| 3. AI 输出 | 0 | 30-180 min |
| 4. 测试 | 5 min | 0 |
| 5. 验收 | 5 min | 0 |
| **单 T 总** | **~17 min** | **~30-180 min** |

**你每天可推进 3-5 个 T 任务**（视 AI 速度）

---

## 4. M1 任务总表

### 任务依赖图

```
T0 ─┬─► T1 ─┬─► T2 ─┬─► T3
    │       │       │
    │       │       ├─► T4 ─► T5
    │       │       │       │
    │       │       │       ├─► T6 (fog)
    │       │       │       │
    │       │       │       └─► T7 (scoring) ─► T8 (recall) ─► T9
    │       │       │                                       │
    │       │       │                                       └─► T10 (LLM)
    │       │       │
    │       │       └─► T11 (CLI) ─► T12 (timeline) ─► T13 (GUI)
    │       │
    │       └─► T20 (retention) ─► T21 (ask_restore)
    │                                             
    ├─► T22 (contradiction) ─┐
    │                        ├─► T23 (relations graph)
    ├─► T24 (regen) ─────────┘
    │                       
    └─► T25 (GUI graph) ──► T26/T27 (tests)
                           
    └─► T17/T18/T19/T28 (docs)
```

### 任务工时表

| T | 任务 | 预计 | 依赖 | 关键产出 |
|---|---|---|---|---|
| T0 | 仓库初始化 | 0.5h | 无 | 目录结构 + 1 commit |
| T1 | SQLite 存储层 | 3h | T0 | `src/store/sqlite.py` |
| T2 | L0-细节 写入 | 3h | T1 | `src/l0/session_writer.py` |
| T3 | L0-骨架 生成 | 2h | T1 | `src/l0/skeleton.py` |
| T4 | 时间分段器 | 4h | T2 | `src/l0/time_segmenter.py` |
| T5 | 段落写入器 | 3h | T4 | `src/l0/segment_writer.py` |
| T6 | 雾化引擎 | 3h | T5 | `src/fog/fog_engine.py` |
| T7 | 动态打分 | 6h | T5 | `src/compress/scoring.py` |
| T8 | 召回引擎 | 6h | T7 | `src/recall/recall_engine.py` |
| T9 | 雾化召回协议 | 2h | T6, T8 | `src/recall/fog_protocol.py` |
| T10 | LLM 抽象 | 4h | T1 | `src/llm/provider.py` |
| T11 | CLI 用户控制 | 3h | T5 | `src/cli/user_controls.py` |
| T12 | CLI 时间线 | 4h | T5 | `src/cli/timeline.py` |
| T13 | GUI MVP | 12h | T11, T12 | `gui/` 目录 |
| T14 | 召回基础测试 | 5h | T8 | `tests/test_recall_basic.py` |
| T15 | 雾化测试 | 3h | T9 | `tests/test_fog_protocol.py` |
| T16 | 测试数据集 | 4h | T5 | `benchmarks/test_set.json` |
| T17 | QUICKSTART | 2h | T13 | `docs/QUICKSTART.md` |
| T18 | LLM 接入指南 | 2h | T10 | `docs/LLM_PROVIDERS.md` |
| T19 | M1 验收报告 | 3h | T14–T18 | `benchmarks/M1_report.md` |
| T20 | retention 引擎 | 4h | T1 | `src/lifecycle/retention_engine.py` |
| T21 | AI 询问升级 | 3h | T20 | `src/lifecycle/ask_restore.py` |
| T22 | 矛盾检测 | 4h | T10, T5 | `src/lifecycle/contradiction_detector.py` |
| T23 | 关系图谱 | 5h | T22, T5 | `src/relations/graph.py` |
| T24 | 摘要重写 | 4h | T6, T22 | `src/compress/regen_engine.py` |
| T25 | GUI 知识图谱 | 8h | T13, T23 | `gui/graph_view.py` |
| T26 | 关系图测试 | 4h | T23 | `tests/test_relations_graph.py` |
| T27 | 重写测试 | 3h | T24 | `tests/test_regen_engine.py` |
| T28 | 竞品分析 | 3h | 无 | `docs/COMPETITOR_ANALYSIS.md` ✅ 已完成 |

**总计**：~110h（1 人全时 2.5 周，Vibe coding 60-90h）

---

## 5. T0–T28 详细提示词

> **使用说明**：每个 T 章节包含 4 段
> 1. **任务信息**（你必读）
> 2. **提示词**（复制喂给 AI）
> 3. **测试命令**（验收用）
> 4. **验收清单**（逐项打勾）

---

### T0: 仓库初始化

**预计工时**：0.5h
**依赖**：无
**关键产出**：
- `I:\PROJECTS\MTCA\.gitignore`（已存在，确认）
- 9 个 `src/<module>/` 目录
- 1 个 git commit

**提示词（复制这段）**：

```
你是一个 Python 项目初始化 AI。

工作目录：I:\PROJECTS\MTCA\

任务：初始化 MTCA 项目仓库骨架。

步骤：
1. cd 到工作目录
2. 跑 `git init`（如果已 init 跳过）
3. 创建以下子目录（每个目录下创建空 __init__.py）：
   - src/store/
   - src/l0/
   - src/l1/
   - src/l2/
   - src/l3/
   - src/recall/
   - src/compress/
   - src/llm/
   - src/adapters/
   - src/cli/
   - src/fog/
   - src/lifecycle/
   - src/relations/
   - tests/
   - benchmarks/
   - examples/
4. 创建 tests/conftest.py 空文件
5. 跑 `git add . && git commit -m "T0: 初始化项目骨架"`
6. 跑 `git log --oneline` 确认 1 个 commit
7. 跑 `ls src/` 确认 13 个子目录

要求：
- 目录结构与 DEVELOPER_PLAN.md 第 3.1 节一致
- 不创建任何 Python 源文件，只创建空 __init__.py
- commit 用中文
- 如果任何步骤失败，明确报告

完成后输出：
- 跑了哪些命令（顺序列出）
- 目录列表
- git log 输出
```

**测试命令**：

```bash
cd I:\PROJECTS\MTCA
git log --oneline
ls src/
ls tests/
```

**验收清单**：
- [ ] `git log` 看到 1 个 commit
- [ ] `src/` 下有 13 个子目录
- [ ] `tests/conftest.py` 存在
- [ ] commit message 中文

**回退信号**：
- 漏建目录
- 多个 commit
- 创建了不该有的源文件

---

### T1: SQLite 存储层

**预计工时**：3h
**依赖**：T0
**关键产出**：
- `src/store/sqlite.py`（~600 行）
- `tests/test_store_sqlite.py`（~200 行）
- `tests/conftest.py`（pytest fixture）

**AI 必读**：
- [DEVELOPER_PLAN.md](file:///i:/PROJECTS/MTCA/DEVELOPER_PLAN.md) 第 2.2 节
- [docs/DATA_MODEL.md](file:///i:/PROJECTS/MTCA/docs/DATA_MODEL.md) 第 2 节

**提示词（复制这段）**：

```
你是一个 Python SQLite 架构 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 MTCA 存储层（src/store/sqlite.py）。

必读（用 Read 工具读这些文件）：
1. I:\PROJECTS\MTCA\DEVELOPER_PLAN.md 第 2.2 节 - 完整 schema
2. I:\PROJECTS\MTCA\docs\DATA_MODEL.md 第 2 节 - 不可变约束

实现文件 1：src/store/sqlite.py
- 6 张表 CREATE TABLE：sessions, messages, segments, segment_relations, views, score_events, fog_session
- 包含 v0.4 所有新字段：fog_state, silence_state, superseded_by, is_stale 等
- 3 个触发器：no_update_messages_ai, no_delete_messages, no_update_skeleton
- 5 个 PRAGMA：journal_mode=WAL, cache_size=-64000, mmap_size=268435456, temp_store=MEMORY, synchronous=NORMAL
- 公共函数：
  - MTCA_DB_PATH = Path.home() / ".mtca" / "mtca.db"
  - init_db(path=None) - 初始化或打开数据库
  - get_connection() - 上下文管理器（with get_connection() as conn:）
  - get_stats() - 返回 dict {sessions, messages, segments, views}
  - execute(sql, params) - 写操作
  - query(sql, params) - 读操作，返回 list[dict]
  - 触发器白名单：fog_permit(session_id, called_by='user') - 校验 called_by == 'user'，否则 PermissionError

实现文件 2：tests/conftest.py
- pytest fixture: tmp_db (tmp_path + :memory:)
- pytest fixture: mtca_db (用 tmp_db fixture 初始化)

实现文件 3：tests/test_store_sqlite.py
- 至少 8 个测试：
  - test_init_creates_all_tables
  - test_pragmas_applied
  - test_no_update_messages_blocks_ai
  - test_fog_permit_allows_user_update
  - test_no_delete_messages
  - test_get_stats
  - test_query_returns_list_of_dict
  - test_fog_permit_rejects_ai_caller

技术要求：
- 用 sqlite3 标准库，不用 SQLAlchemy
- 所有 SQL 用 ? 占位符
- 函数 docstring 中文
- 错误处理：IntegrityError, OperationalError 转 RuntimeError
- 文件 ≤ 700 行（多就拆 helper）
- pytest 全通过

完成后输出：
- 创建的文件清单
- 每个文件行数
- pytest 输出
```

**测试命令**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_store_sqlite.py -v
```

**验收清单**：
- [ ] 6 张表都创建成功
- [ ] 3 个触发器生效
- [ ] 5 个 PRAGMA 应用
- [ ] 8+ 个测试全通过
- [ ] `init_db()` 重复调用不报错
- [ ] `fog_permit(called_by='ai')` 抛 PermissionError

**回退信号**：
- 用了 SQLAlchemy
- 写错字段名（v0.4 新字段漏了）
- 测试用真实文件（必须用 tmp_path）
- 触发器不生效

---

### T2: L0-细节 消息写入

**预计工时**：3h
**依赖**：T1
**关键产出**：
- `src/l0/session_writer.py`（~350 行）
- `tests/test_session_writer.py`（~150 行）

**AI 必读**：
- [DEVELOPER_PLAN.md](file:///i:/PROJECTS/MTCA/DEVELOPER_PLAN.md) 第 4.1 节
- `src/store/sqlite.py`（T1 刚写的）

**提示词**：

```
你是一个 Python 数据库写入 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 L0-细节 消息写入（src/l0/session_writer.py）。

必读：
1. DEVELOPER_PLAN.md 第 2.2 节 - sessions 和 messages 表结构
2. src/store/sqlite.py - 刚写的存储层（理解 get_connection / execute / query）

实现文件 1：src/l0/session_writer.py
- 函数：create_session(agent_source='user', topic_label=None) -> session_id
- 函数：write_message(session_id, role, content, tool_calls=None, tool_results=None) -> message_id
  - role ∈ {'user', 'assistant', 'tool', 'system'}
  - 自动算 seq（session 内序号）
  - 自动算 token_count（len(content) // 4 估算）
  - 写完更新 sessions.message_count 和 token_estimate
- 函数：get_session_messages(session_id) -> list[dict]
- 函数：end_session(session_id) - 设 ended_at

实现文件 2：tests/test_session_writer.py
- 至少 6 个测试：
  - test_create_session
  - test_write_message_increments_seq
  - test_write_message_updates_session_stats
  - test_get_session_messages_returns_ordered
  - test_token_count_estimated
  - test_end_session_sets_ended_at

技术：
- 复用 src.store.sqlite.get_connection
- message_id 用 uuid.uuid4()
- seq 用 `SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id=?`
- ≤ 400 行
- pytest 全通过

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_session_writer.py -v
```

**验收清单**：
- [ ] create_session 返回 UUID
- [ ] write_message seq 自动递增
- [ ] session 统计实时更新
- [ ] 6+ 测试全通过

---

### T3: L0-骨架 锚点生成

**预计工时**：2h
**依赖**：T1
**关键产出**：
- `src/l0/skeleton.py`（~250 行）
- `tests/test_skeleton.py`（~100 行）

**AI 必读**：
- [docs/V0.4_PIVOT.md](file:///i:/PROJECTS/MTCA/docs/V0.4_PIVOT.md) 第 3.1 节
- `src/l0/session_writer.py`

**提示词**：

```
你是一个 Python NLP 工具 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 L0-骨架 锚点生成（src/l0/skeleton.py）。

必读：
1. docs/V0.4_PIVOT.md 第 3.1 节 - 锚点句定义（≤20 字）
2. src/store/sqlite.py - schema 中 segments 字段
3. src/l0/session_writer.py - get_session_messages 用法

实现文件 1：src/l0/skeleton.py
- 函数：generate_anchor(messages: list[dict]) -> str
  - 从 messages 提取第一句用户消息的关键内容
  - 截断到 ≤ 20 字（中英文都算字数）
  - 用 jieba 切词（可选），fallback 用空格分
- 函数：extract_keywords(messages: list[dict], top_k=5) -> list[str]
  - 简单词频统计 + 停用词过滤
  - 停用词表用内置 set（中文常见 100 个停用词）
- 函数：build_skeleton(session_id) -> dict
  - 返回 {anchor, keywords, message_count, time_range}
- 函数：save_skeleton(session_id, skeleton: dict)
  - UPDATE segments SET topic_label=?, fog_anchor=?（用 anchor 作 topic_label fallback）
  - 创建 segments 记录（如果还没有）

实现文件 2：tests/test_skeleton.py
- 至少 5 个测试：
  - test_generate_anchor_short
  - test_generate_anchor_chinese
  - test_extract_keywords_filters_stopwords
  - test_build_skeleton_returns_dict
  - test_save_skeleton_writes_to_db

技术：
- 用 jieba 库（pip install jieba）
- 停用词表写在文件头（10-20 个常见词即可）
- ≤ 300 行
- pytest 全通过

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_skeleton.py -v
```

**验收清单**：
- [ ] generate_anchor 返回 ≤ 20 字
- [ ] extract_keywords 过滤停用词
- [ ] save_skeleton 写库
- [ ] 5+ 测试全通过

---

### T4: 时间分段器

**预计工时**：4h
**依赖**：T2
**关键产出**：
- `src/l0/time_segmenter.py`（~450 行）
- `tests/test_time_segmenter.py`（~200 行）

**AI 必读**：
- [DEVELOPER_PLAN.md](file:///i:/PROJECTS/MTCA/DEVELOPER_PLAN.md) 第 4.1 节
- `src/l0/session_writer.py`

**提示词**：

```
你是一个 Python 时间序列分析 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现时间分段器（src/l0/time_segmenter.py）。

必读：
1. DEVELOPER_PLAN.md 第 4.1 节 - 3 规则：MAX_GAP=60min, MIN_SEG=3 msgs, NIGHT_MERGE=(23,7)
2. src/l0/session_writer.py - get_session_messages

实现文件 1：src/l0/time_segmenter.py
- 常量：
  - MAX_GAP_MINUTES = 60
  - MIN_SEGMENT_MSGS = 3
  - NIGHT_MERGE_WINDOW = (23, 7)  # 23:00-07:00 跨天合并
- 函数：segment_session(session_id) -> list[dict]
  - 返回 [{start_msg_seq, end_msg_seq, start_at, end_at, gap_to_next, weak_merged, topic_label}]
- 函数：weak_merge_check(current_msgs, new_msg, gap_min) -> bool
  - 60 < gap < 240 分钟 + 关键词重叠 ≥ 30% → True
- 函数：is_night_merge(ts_a, ts_b) -> bool
  - ts_a 在 23:00-07:00 且 ts_b 也在该窗口 → True
- 函数：save_segments(session_id, segments: list[dict])
  - 批量 INSERT INTO segments

实现文件 2：tests/test_time_segmenter.py
- 至少 8 个测试：
  - test_short_session_single_segment
  - test_max_gap_splits
  - test_min_segment_filter
  - test_weak_merge_with_keywords
  - test_weak_merge_rejects_low_overlap
  - test_night_merge
  - test_save_segments
  - test_segment_realistic_conversation

技术：
- datetime 处理时间戳
- 关键词重叠用 Jaccard 相似度
- ≤ 500 行
- pytest 全通过

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_time_segmenter.py -v
```

**验收清单**：
- [ ] MAX_GAP=60 切分生效
- [ ] MIN_SEGMENT=3 过滤
- [ ] 弱合并触发
- [ ] 夜段合并
- [ ] 8+ 测试全通过

---

### T5: 段落写入器

**预计工时**：3h
**依赖**：T3, T4
**关键产出**：
- `src/l0/segment_writer.py`（~300 行）
- `tests/test_segment_writer.py`（~120 行）

**AI 必读**：
- `src/l0/time_segmenter.py`
- `src/l0/skeleton.py`

**提示词**：

```
你是一个 Python 集成 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现段落写入器（src/l0/segment_writer.py）。

必读：
1. src/l0/time_segmenter.py - segment_session 返回格式
2. src/l0/skeleton.py - build_skeleton / save_skeleton
3. src/store/sqlite.py - schema

实现文件 1：src/l0/segment_writer.py
- 函数：write_segments(session_id) -> list[segment_id]
  - 调用 segment_session + build_skeleton
  - 为每个段落生成 segment_id（uuid）
  - INSERT INTO segments（含 topic_label, fog_anchor, start_at 等）
  - 关联 messages（通过 session_id + seq 范围）
  - 返回所有 segment_id
- 函数：get_segment(segment_id) -> dict | None
- 函数：list_segments(session_id=None, limit=20) -> list[dict]
  - 按 start_at DESC
- 函数：update_segment(segment_id, **fields)
  - 只允许更新非 L0-骨架字段

实现文件 2：tests/test_segment_writer.py
- 至少 5 个测试

技术：
- 复用 time_segmenter + skeleton
- ≤ 350 行
- pytest 全通过

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_segment_writer.py -v
```

---

### T6: 雾化引擎

**预计工时**：3h
**依赖**：T5
**关键产出**：
- `src/fog/fog_engine.py`（~400 行）
- `tests/test_fog_engine.py`（~150 行）

**AI 必读**：
- [docs/DATA_MODEL.md](file:///i:/PROJECTS/MTCA/docs/DATA_MODEL.md) 第 4 节
- [docs/USER_CONTROLS.md](file:///i:/PROJECTS/MTCA/docs/USER_CONTROLS.md) 第 2.4 节

**提示词**：

```
你是一个 Python 安全操作 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现雾化引擎（src/fog/fog_engine.py）。

必读：
1. docs/DATA_MODEL.md 第 4 节 - 雾化物理操作
2. docs/USER_CONTROLS.md 第 2.4 节 - 业务逻辑

实现文件 1：src/fog/fog_engine.py
- 函数：fog_segment(segment_id, anchor: str, called_by='user') -> bool
  - 校验 called_by == 'user'，否则 raise PermissionError("AI 无 /雾化 权限")
  - 事务内：
    1. INSERT INTO fog_session VALUES (session_id, now, 'user')
    2. UPDATE messages SET content=NULL, token_count=0 WHERE session_id=? AND seq BETWEEN ? AND ?
    3. UPDATE segments SET fog_state='fogged_once', fog_at=?, fog_anchor=? WHERE segment_id=?
    4. INSERT INTO score_events (segment_id, event_type, reason) VALUES (?, 'user_fog', '不可逆')
    5. DELETE FROM fog_session WHERE session_id=?
  - 返回 True
- 函数：is_fogged(segment_id) -> bool
  - 返回 fog_state != 'clear'
- 函数：get_fog_anchor(segment_id) -> str | None
- 函数：fog_session_tear_down(session_id) - 异常时清理

实现文件 2：tests/test_fog_engine.py
- 至少 6 个测试：
  - test_fog_segment_erases_content
  - test_fog_segment_preserves_skeleton
  - test_fog_ai_caller_rejected
  - test_fog_writes_audit_event
  - test_fog_idempotent
  - test_fog_cascade_messages

技术：
- 严格事务（try/except/finally）
- 物理擦除用 UPDATE messages SET content=NULL
- ≤ 450 行
- pytest 全通过

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_fog_engine.py -v
```

**验收清单**：
- [ ] fog_segment 物理擦除 messages.content
- [ ] fog_segment 保留 L0-骨架
- [ ] AI 调用被拒绝（PermissionError）
- [ ] 写 score_events 审计
- [ ] 6+ 测试全通过

---

### T7: 动态打分

**预计工时**：6h
**依赖**：T5
**关键产出**：
- `src/compress/scoring.py`（~700 行）
- `tests/test_scoring.py`（~250 行）

**AI 必读**：
- [DEVELOPER_PLAN.md](file:///i:/PROJECTS/MTCA/DEVELOPER_PLAN.md) 第 4.2 节
- [docs/DATA_MODEL.md](file:///i:/PROJECTS/MTCA/docs/DATA_MODEL.md) 第 3 节

**提示词**：

```
你是一个 Python 调度算法 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现动态打分引擎（src/compress/scoring.py）。

必读：
1. DEVELOPER_PLAN.md 第 4.2 节 - tick() 算法
2. docs/DATA_MODEL.md 第 3 节 - 阈值

实现文件 1：src/compress/scoring.py
- 常量：
  - INITIAL_SCORE = 100.0
  - PER_TICK_DECAY = 1.0
  - REFERENCE_BOOST = 10.0
  - L1_THRESHOLD = 70.0
  - L2_THRESHOLD = 50.0
  - L3_THRESHOLD = 30.0
  - IMPORTANT_SCORE = 10000.0
- 函数：tick() - 异步打分（每轮对话后调用）
  - 遍历所有 silence_state='active' 的 segments
  - cycle_tag 非空 → 跳过衰减
  - score -= PER_TICK_DECAY
  - 引用检测（has_been_referenced）→ score += REFERENCE_BOOST
  - 阈值判断 → 更新 current_tier
  - 写 score_events
- 函数：has_been_referenced(segment, recent_msgs=None) -> bool
  - 用 Jaccard 词袋相似度（≥ 0.3 算引用）
  - 比较 segment.anchor + keywords vs recent 10 messages
- 函数：determine_tier(score) -> str
  - score >= 70 → 'L1'
  - score >= 50 → 'L2'
  - score >= 30 → 'L3'
  - score < 30 → 'L3_hidden'
- 函数：mark_important(segment_id) - score=10000
- 函数：mark_cycle(segment_id, cycle_tag)
- 函数：archive(segment_id) - tier='L3_hidden'

实现文件 2：tests/test_scoring.py
- 至少 10 个测试：
  - test_initial_score_100
  - test_tick_decrements_by_1
  - test_tick_skips_cycle_tag
  - test_reference_boost_10
  - test_threshold_l1
  - test_threshold_l2
  - test_threshold_l3
  - test_threshold_hidden
  - test_mark_important_locks_score
  - test_archive_hides
  - test_score_events_logged

技术：
- 纯 Python，不依赖外部库
- tick() 设计为可异步（但 M1 同步实现）
- ≤ 750 行
- pytest 全通过

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_scoring.py -v
```

---

### T8: 召回引擎

**预计工时**：6h
**依赖**：T7
**关键产出**：
- `src/recall/recall_engine.py`（~700 行）
- `tests/test_recall_basic.py`（~500 行，部分功能）

**AI 必读**：
- [DEVELOPER_PLAN.md](file:///i:/PROJECTS/MTCA/DEVELOPER_PLAN.md) 第 4.4 节
- [docs/ARCHITECTURE.md](file:///i:/PROJECTS/MTCA/docs/ARCHITECTURE.md) 第 2 节

**提示词**：

```
你是一个 Python 检索 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现召回引擎（src/recall/recall_engine.py）。

必读：
1. DEVELOPER_PLAN.md 第 4.4 节 - 双通道召回
2. docs/ARCHITECTURE.md 第 2 节 - 召回流程
3. src/store/sqlite.py - messages_fts FTS5 索引

实现文件 1：src/recall/recall_engine.py
- 函数：recall(query, time_window=None, topics=None, top_k=5) -> list[dict]
  - 主通道 1：FTS5 搜索 messages_fts
  - 主通道 2：搜索 segments.topic_label
  - 合并去重
  - 加段内 messages（L0-细节 全文）
  - 返回 [{segment_id, session_id, tier, score, messages}]
- 函数：search_sessions(query, time_window, topics, limit) -> list[dict]
- 函数：search_segments(query, time_window, topics, limit) -> list[dict]
- 函数：expand_neighbors(segment_ids, window=1) -> list[dict]
  - 前后各 1 段
- 函数：rerank(query, candidates) -> list[dict]
  - 简单 Jaccard + recency 加权
- 函数：recall_with_fallback(query, **kwargs) -> list[dict]
  - 如果 recall() 空 → get_recent_sessions(limit=10)
  - 兜底逻辑

实现文件 2：tests/test_recall_basic.py（部分）
- 至少 8 个测试（占 50 段测试的一半，剩下 T14 补）：
  - test_recall_finds_by_keyword
  - test_recall_finds_by_topic
  - test_recall_combines_channels
  - test_recall_expands_neighbors
  - test_recall_falls_back_to_recent
  - test_recall_respects_time_window
  - test_recall_rerank_by_relevance
  - test_recall_returns_l0_messages

技术：
- 用 sqlite3 FTS5 (messages_fts MATCH)
- Jaccard rerank
- ≤ 750 行
- pytest 全通过

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_recall_basic.py -v
```

---

### T9: 雾化召回协议

**预计工时**：2h
**依赖**：T6, T8
**关键产出**：
- `src/recall/fog_protocol.py`（~200 行）
- `tests/test_fog_protocol.py`（~300 行）

**AI 必读**：
- [docs/V0.4_PIVOT.md](file:///i:/PROJECTS/MTCA/docs/V0.4_PIVOT.md) 第 3.1 节（雾化 3 态）
- `src/recall/recall_engine.py`

**提示词**：

```
你是一个 Python 状态机 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现雾化召回协议（src/recall/fog_protocol.py）。

必读：
1. docs/V0.4_PIVOT.md 第 3.1 节 - 雾化 3 态
2. src/recall/recall_engine.py - 召回返回格式

实现文件 1：src/recall/fog_protocol.py
- 函数：apply_fog_protocol(results: list[dict]) -> list[dict]
  - 处理每个 segment：
    - fog_state='clear' → 正常
    - fog_state='fogged_once' → 替换 messages 为 [{role: 'system', content: '片段已删除（[fog_anchor]）'}]
      同时 UPDATE fog_state='archived'
    - fog_state='archived' → 从结果移除
- 函数：filter_silent_segments(segments, include_dormant=True) -> list[dict]
  - silence_state='active' → 保留
  - silence_state='dormant' → score × 0.5
  - silence_state='silent' → 移除
  - include_dormant=False → 也移除 dormant
- 函数：apply_supersede(results) -> list[dict]
  - 如果 A 被 B supersede，移除 A，加注：「A 已被 B 取代（[date]）」

实现文件 2：tests/test_fog_protocol.py
- 至少 6 个测试：
  - test_clear_passes_through
  - test_fogged_once_returns_skeleton
  - test_fogged_once_transitions_to_archived
  - test_archived_excluded
  - test_silent_excluded
  - test_dormant_score_halved
  - test_supersede_announces

技术：
- 纯函数（输入 list[dict]，输出 list[dict]）
- ≤ 250 行
- pytest 全通过

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_fog_protocol.py -v
```

---

### T10: LLM 抽象层

**预计工时**：4h
**依赖**：T1
**关键产出**：
- `src/llm/provider.py`（~600 行）
- `tests/test_llm_provider.py`（~200 行）

**AI 必读**：
- [DEVELOPER_PLAN.md](file:///i:/PROJECTS/MTCA/DEVELOPER_PLAN.md) 第 7 节

**提示词**：

```
你是一个 Python 抽象工厂 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 LLM 抽象层（src/llm/provider.py）。

必读：
1. DEVELOPER_PLAN.md 第 7 节 - 4 种后端选型
2. src/store/sqlite.py

实现文件 1：src/llm/provider.py
- 抽象基类：LLMProvider (Protocol)
  - generate(prompt: str, **kwargs) -> str
  - embed(text: str) -> list[float]
  - is_available() -> bool
- 4 个实现：
  1. OllamaProvider - HTTP 调用 http://localhost:11434
     - generate: POST /api/generate
     - embed: POST /api/embeddings
  2. LMStudioProvider - OpenAI 兼容端点 http://localhost:1234
     - generate: POST /v1/chat/completions
     - embed: POST /v1/embeddings
  3. LlamaCppProvider - 直接调用 llama-cpp-python（可选 import）
  4. CloudProvider - OpenAI/Anthropic API
- 工厂函数：get_provider(name='auto') -> LLMProvider
  - auto: 探测所有后端，返回第一个可用的
- 配置读取：~/.mtca/config.toml
  ```toml
  [llm]
  default = "ollama"
  fallback = "cloud"
  [ollama]
  base_url = "http://localhost:11434"
  model = "qwen3.6:35b-a3b"
  ```

实现文件 2：tests/test_llm_provider.py
- mock 测试（不实际调 LLM）
- 至少 5 个测试：
  - test_provider_protocol
  - test_ollama_generate
  - test_lmstudio_generate
  - test_get_provider_auto_detect
  - test_config_parsing

技术：
- 用 httpx 做 HTTP（轻量）
- 工厂模式
- 失败时 fallback
- ≤ 700 行
- pytest 全通过（mock）

完成后输出文件清单 + 行数 + pytest 结果。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
pytest tests/test_llm_provider.py -v
```

---

### T11: CLI 用户控制

**预计工时**：3h
**依赖**：T5
**关键产出**：
- `src/cli/user_controls.py`（~350 行）

**AI 必读**：
- [docs/USER_CONTROLS.md](file:///i:/PROJECTS/MTCA/docs/USER_CONTROLS.md) 第 2 节

**提示词**：

```
你是一个 Python CLI AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 CLI 用户控制命令（src/cli/user_controls.py）。

必读：
1. docs/USER_CONTROLS.md 第 2 节 - 4 个命令实现

实现文件 1：src/cli/user_controls.py
- 4 个命令：
  - mark_important(segment_id) - 调 scoring.mark_important
  - mark_cycle(segment_id, cycle_tag) - 调 scoring.mark_cycle
  - archive(segment_id) - 调 scoring.archive
  - fog_segment(segment_id, anchor) - 调 fog.fog_segment（带用户校验）
- 命令解析：parse_command(text: str) -> (cmd, args) | None
  - 识别 /重要 /循环 /归档 /雾化 前缀
  - 返回 (command, [args])
- CLI 入口：main() - click 装饰
  - `mtca important <segment_id>`
  - `mtca cycle <segment_id> <tag>`
  - `mtca archive <segment_id>`
  - `mtca fog <segment_id> --anchor "..."`
- 自然语言前缀：parse_natural(text) - 支持 "把这段标记为重要"

技术：
- 用 click 库
- 简单 argparse 风格
- ≤ 400 行

完成后输出文件清单 + 行数。
```

---

### T12: CLI 时间线

**预计工时**：4h
**依赖**：T5
**关键产出**：
- `src/cli/timeline.py`（~450 行）

**AI 必读**：
- [docs/USER_CONTROLS.md](file:///i:/PROJECTS/MTCA/docs/USER_CONTROLS.md) 第 6 节

**提示词**：

```
你是一个 Python 终端 UI AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 CLI 时间线（src/cli/timeline.py）。

必读：
1. docs/USER_CONTROLS.md 第 6 节 - 时间线 / 树状视图

实现文件 1：src/cli/timeline.py
- 用 rich 库实现
- 函数：render_timeline(period='day', project=None) -> str
  - 返回 rich 渲染的字符串
  - 用 Tree 控件
  - 颜色规则：active=亮色, dormant=灰, silent=暗, fogged=红框, superseded=删除线
- 函数：render_tree(project=None) -> str
  - 项目级树状视图
- CLI 入口：main()
  - `mtca timeline` - 显示今天
  - `mtca timeline --period=week`
  - `mtca tree --project=MTCA`
- 支持：搜索（`mtca timeline --query "漫剧"`）

技术：
- rich 库（pip install rich）
- click 集成
- ≤ 500 行

完成后输出文件清单 + 行数。
```

---

### T13: GUI MVP

**预计工时**：12h
**依赖**：T11, T12
**关键产出**：
- `gui/main_window.py`
- `gui/timeline_view.py`
- `gui/segment_detail.py`
- `gui/fog_dialog.py`
- `gui/requirements.txt`（PySide6 / Tauri / Electron 三选一）

**AI 必读**：
- [docs/USER_CONTROLS.md](file:///i:/PROJECTS/MTCA/docs/USER_CONTROLS.md) 第 3 节

**提示词**：

```
你是一个 Python GUI AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 GUI MVP（时间线 + 话题树 + 雾化按钮）。

技术栈：PySide6（推荐，最快出 MVP）
- 如果 PySide6 安装失败，回退 Tauri（Rust+Web）

必读：
1. docs/USER_CONTROLS.md 第 3.1-3.3 节 - 3 个主界面 + 雾化对话框
2. src/l0/segment_writer.py - 段数据
3. src/recall/recall_engine.py - 召回接口

实现文件：
1. gui/main_window.py
   - QMainWindow
   - 顶部工具栏：搜索 / 新建 / 刷新 / 设置
   - 左侧：时间线列表（QListView）
   - 中间：段落详情
   - 右侧：操作按钮（雾化 / 归档 / 重要 / 循环）
2. gui/timeline_view.py
   - 树状 / 时间线 切换
   - 颜色按 silence_state / fog_state 渲染
3. gui/segment_detail.py
   - 显示 L0-骨架 / L1 摘要 / L2 / L3
   - 展开 L0 全文按钮
4. gui/fog_dialog.py
   - 强警告对话框
   - 锚点句输入框（≤20 字校验）
   - 取消 + 我确定要雾化 按钮
5. gui/requirements.txt
   - PySide6>=6.5
6. gui/run.py
   - 启动入口：`python gui/run.py`

技术：
- PySide6（Qt for Python）
- 简单的 MVP（不上 MVC 框架）
- 不要堆 UI 美观，先跑通功能
- ≤ 1500 行

完成后输出文件清单 + 行数 + 启动截图（playwright 或手动）。
```

**测试**：

```bash
cd I:\PROJECTS\MTCA
python gui/run.py
# 应看到主窗口，列出当前所有 segments
```

**验收清单**：
- [ ] GUI 启动 < 5 秒
- [ ] 时间线显示
- [ ] 点击段落到详情
- [ ] 雾化按钮 + 对话框工作
- [ ] 截图清晰

---

### T14: 召回基础测试

**预计工时**：5h
**依赖**：T8
**关键产出**：
- `tests/test_recall_basic.py`（补全到 50 段用例）
- `benchmarks/test_set.json`（T16 同时完成）

**提示词**：

```
你是一个 Python 测试 AI。

工作目录：I:\PROJECTS\MTCA\

任务：补充召回测试到 50 段用例。

必读：
1. src/recall/recall_engine.py - 召回 API
2. tests/test_recall_basic.py - 已有部分

实现：
- 已有 8 个测试，补充到 50 个
- 30 段老板真历史场景（漫剧 / 编程 / 工作 / 生活）
- 20 段自造边界 case
- 每个测试：
  - 准备数据（write_message + write_segments）
  - 调 recall(query)
  - 断言（top-1 segment_id 正确 / 召回率）

完成后输出测试统计 + pytest 全通过证明。
```

---

### T15: 雾化测试

**预计工时**：3h
**依赖**：T9

**提示词**：

```
你是一个 Python 测试 AI。

工作目录：I:\PROJECTS\MTCA\

任务：补全雾化测试。

必读：src/recall/fog_protocol.py

实现：
- 20+ 段测试：
  - clear → fogged_once → archived 转移
  - AI 调用被拒
  - 7 天防骚扰
  - 矛盾 supersede 反馈格式
  - 边界 case（空 query / 多重 fog / 同段多次召回）

完成后输出 pytest 通过证明。
```

---

### T16: 测试数据集

**预计工时**：4h
**依赖**：T5
**关键产出**：
- `benchmarks/test_set.json`（50 段对话数据）

**提示词**：

```
你是一个 JSON 数据生成 AI。

工作目录：I:\PROJECTS\MTCA\

任务：生成测试数据集（benchmarks/test_set.json）。

格式：
```json
[
  {
    "id": "test_001",
    "topic": "漫剧方案 A",
    "user_messages": ["我想做漫剧...", "用 Python + AI", "..."],
    "expected_recall_query": "漫剧 Python",
    "expected_top_segment": "test_001"
  },
  ...
]
```

要求：
- 30 段老板真历史场景（编程 / 工作 / 生活 / 学习）
- 20 段边界 case（短对话 / 长对话 / 多话题 / 含工具调用）
- 每段 3-20 条消息
- 每段有 expected_recall_query 和 expected_top_segment

完成后报告文件大小 + 段数。
```

---

### T17–T19: 文档

**T17 (QUICKSTART) / T18 (LLM_PROVIDERS) / T19 (M1_report)** 都是文档类，提示词模板：

```
你是一个技术文档 AI。

工作目录：I:\PROJECTS\MTCA\

任务：写 [文档名]。

必读：
- [相关设计文档]
- 现有 src/ 代码

实现 docs/[文件名].md
- 章节：[列章节]
- 包含：复制粘贴的命令
- 包含：截图占位（描述）
- 中英双语（M1 阶段先中文，M2 加英文）

完成后输出文件路径 + 行数。
```

---

### T20: retention 引擎

**预计工时**：4h
**依赖**：T1
**关键产出**：
- `src/lifecycle/retention_engine.py`（~500 行）

**AI 必读**：
- [docs/V0.4_PIVOT.md](file:///i:/PROJECTS/MTCA/docs/V0.4_PIVOT.md) 第 3.6 节

**提示词**：

```
你是一个 Python 调度 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 retention 引擎（src/lifecycle/retention_engine.py）。

必读：
1. docs/V0.4_PIVOT.md 第 3.6 节 - 静默态 5 条铁律

实现：
- 默认 retention 策略表（编程 365d, 工作 180d, 娱乐 30d, 健康 永久）
- 函数：tick() - 每轮检查
  - active 段：检查 promoted_at + retention_days < now → 降 dormant
  - dormant 段：检查 promoted_at + retention_days * 2 < now → 降 silent
- 函数：demote(segment_id) - silence_state 下沉
- 函数：promote(segment_id, target='dormant' or 'active') - 上浮
  - promoted_at = now
  - ref_count += 1 (如果来自 ask_restore)
- 函数：lock_permanent(segment_id) - 用户标"永久活跃"
- 函数：force_silent(segment_id) - 跳 dormant 直接 silent
- 函数：get_retention_for(topic_category) -> int (天数)

技术：
- 后台 tick 用 threading.Timer 或 APScheduler
- ≤ 600 行
- pytest 覆盖

完成后输出文件清单 + 行数。
```

---

### T21: AI 询问升级

**预计工时**：3h
**依赖**：T20

**提示词**：

```
你是一个 Python 交互 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 AI 询问升级（src/lifecycle/ask_restore.py）。

必读：
1. docs/V0.4_PIVOT.md 第 3.6 节 - AI 询问机制
2. src/lifecycle/retention_engine.py

实现：
- 函数：should_ask(segment_id) -> bool
  - silence_state='silent' AND now - last_ask_at > 7 days AND ref_count < threshold
- 函数：make_ask_prompt(segment_id) -> str
  - "你 X 月前做过 [topic_label]（[fog_anchor]），当前话题相关吗？"
- 函数：handle_user_response(segment_id, response: 'yes'|'no'|'skip_7d')
  - 'yes' → ref_count += 1，promote 到 dormant，if ref_count >= 3 → active
  - 'no' → last_ask_at = now
  - 'skip_7d' → long_silent = true

技术：
- 纯函数
- ≤ 400 行
- pytest 覆盖 5+ 场景

完成后输出文件清单 + 行数。
```

---

### T22: 矛盾检测

**预计工时**：4h
**依赖**：T10, T5

**AI 必读**：
- [docs/V0.4_PIVOT.md](file:///i:/PROJECTS/MTCA/docs/V0.4_PIVOT.md) 第 3.7 节
- [docs/DATA_MODEL.md](file:///i:/PROJECTS/MTCA/docs/DATA_MODEL.md) 第 5 节

**提示词**：

```
你是一个 Python NLP AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现矛盾检测（src/lifecycle/contradiction_detector.py）。

必读：
1. docs/V0.4_PIVOT.md 第 3.7 节
2. docs/DATA_MODEL.md 第 5 节
3. src/llm/provider.py - generate()

实现：
- 函数：detect_contradiction(new_segment_id) -> list[dict]
  - 找候选：同 topic_label + active + 90 天内 + ≤ 10 段
  - 对每对调 LLM 判断：
    prompt = f"段落 A 说 {a.content[:500]}\n段落 B 说 {b.content[:500]}\n是否矛盾？级别？"
  - 返回 [{a_id, b_id, level, reason}]
- 函数：apply_supersede(a_id, b_id)
  - INSERT INTO segment_relations (b_id, a_id, 'supersedes', auto_created=1)
  - UPDATE segments SET superseded_by=b_id, supersedes_count += 1 WHERE segment_id=a_id
- 函数：user_override(a_id, action='accept'|'branch'|'revoke')
  - 'accept' → 应用 supersede
  - 'branch' → 改 related_to
  - 'revoke' → 删关系

技术：
- LLM 调用走 src/llm/provider.py
- 异步（threading 或 asyncio）
- ≤ 550 行
- pytest 覆盖

完成后输出文件清单 + 行数。
```

---

### T23: 关系图谱

**预计工时**：5h
**依赖**：T22, T5

**AI 必读**：
- [docs/V0.4_PIVOT.md](file:///i:/PROJECTS/MTCA/docs/V0.4_PIVOT.md) 第 3.8 节

**提示词**：

```
你是一个 Python 图算法 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现关系图谱（src/relations/graph.py）。

必读：
1. docs/V0.4_PIVOT.md 第 3.8 节 - 4 种关系

实现：
- 函数：create_relation(seg_a, seg_b, relation_type, weight=1.0, auto=False)
- 函数：get_related(segment_id, types=['related_to']) -> list[segment]
- 函数：get_topic_cluster(segment_id, depth=2) -> list[segment]
  - BFS/DFS 遍历
- 函数：get_supersede_chain(segment_id) -> list[segment]
  - 递归查 superseded_by
- 函数：auto_create_relations(segment_id)
  - 同 topic → related_to
  - 时间相邻 + 关键词重叠 → derived_from
  - 矛盾检测调用 T22
- 函数：export_to_json(segment_id, depth=2) -> dict
  - 给 GUI 用

技术：
- 邻接表 + BFS
- ≤ 700 行
- pytest 覆盖 4 种关系 + 遍历

完成后输出文件清单 + 行数。
```

---

### T24: 摘要重写

**预计工时**：4h
**依赖**：T6, T22

**AI 必读**：
- [docs/V0.4_PIVOT.md](file:///i:/PROJECTS/MTCA/docs/V0.4_PIVOT.md) 第 3.9 节
- [docs/DATA_MODEL.md](file:///i:/PROJECTS/MTCA/docs/DATA_MODEL.md) 第 7 节

**提示词**：

```
你是一个 Python 异步任务 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现摘要重写（src/compress/regen_engine.py）。

必读：
1. docs/V0.4_PIVOT.md 第 3.9 节
2. docs/DATA_MODEL.md 第 7 节

实现：
- 函数：schedule_regen(segment_id, reason='fog'|'supersede'|'promote'|'manual')
  - UPDATE views SET is_stale=1, stale_reason=? WHERE segment_id=?
  - 加入异步队列
- 函数：regen_view(view_id)
  - 加载 L0-骨架 + 旧 view
  - 调 LLM 重新生成（prompt 含 reason）
  - 写新 view，regen_count += 1
  - 旧 view expires_at = now
  - 失败时保留旧 view，重试 3 次
- 函数：process_queue() - 后台 worker
- 函数：manual_refresh(segment_id) - GUI 触发

技术：
- 队列用 threading.Queue 或 asyncio.Queue
- 失败回退（保留旧 view）
- ≤ 500 行
- pytest 覆盖 4 种触发

完成后输出文件清单 + 行数。
```

---

### T25: GUI 知识图谱

**预计工时**：8h
**依赖**：T13, T23

**AI 必读**：
- [docs/USER_CONTROLS.md](file:///i:/PROJECTS/MTCA/docs/USER_CONTROLS.md) 第 3.4 节

**提示词**：

```
你是一个 Python GUI 可视化 AI。

工作目录：I:\PROJECTS\MTCA\

任务：实现 GUI 知识图谱视图（gui/graph_view.py）。

必读：
1. docs/USER_CONTROLS.md 第 3.4 节
2. src/relations/graph.py - export_to_json
3. gui/main_window.py - 主窗口集成

实现：
- 用 PySide6 + QGraphicsView / QGraphicsScene
- 节点：圆形，按重要性调整半径，按 silence_state 着色
- 边：箭头，4 种关系 4 种颜色
- 控件：
  - 时间倒带滑块（QSlider）
  - 话题过滤下拉
  - 静默态过滤多选
- 交互：
  - 点击节点 → 高亮 + 显示详情
  - 双击 → 跳到段落详情
  - 滚轮缩放
  - 拖拽移动
- 集成到 main_window.py 作为第 4 个 Tab

技术：
- 简单力导向布局（手写或用 networkx + spring_layout）
- ≤ 1000 行
- 截图证明工作

完成后输出文件清单 + 启动截图。
```

---

### T26: 关系图测试

**预计工时**：4h
**依赖**：T23

**提示词**：

```
你是一个 Python 测试 AI。

工作目录：I:\PROJECTS\MTCA\

任务：关系图测试（tests/test_relations_graph.py）。

实现：
- 15+ 段测试：
  - 4 种关系创建
  - 遍历（topic_cluster）
  - supersede 链
  - 边界（空 / 单节点 / 环）
  - 性能（100 段 < 100ms）
  - export_to_json 格式

完成后 pytest 全通过。
```

---

### T27: 重写测试

**预计工时**：3h
**依赖**：T24

**提示词**：

```
你是一个 Python 测试 AI。

工作目录：I:\PROJECTS\MTCA\

任务：重写测试（tests/test_regen_engine.py）。

实现：
- 10+ 段测试：
  - 4 种触发（fog/supersede/promote/manual）
  - 失败回退
  - 队列处理
  - is_stale 标记

完成后 pytest 全通过。
```

---

### T28: 竞品分析 ✅ 已完成

详见 [docs/COMPETITOR_ANALYSIS.md](file:///i:/PROJECTS/MTCA/docs/COMPETITOR_ANALYSIS.md)

---

## 6. 测试流程

### 6.1 跑全部测试

```bash
cd I:\PROJECTS\MTCA
pytest -v
```

**期望**：所有测试通过，耗时 < 30 秒

### 6.2 跑单个测试

```bash
pytest tests/test_xxx.py::test_yyy -v
```

### 6.3 看测试覆盖率

```bash
pytest --cov=src --cov-report=html
# 打开 htmlcov/index.html
```

**目标**：核心模块覆盖率 ≥ 80%

### 6.4 解读 pytest 输出

| 输出 | 含义 | 行动 |
|---|---|---|
| `5 passed in 0.12s` | 全过 | ✅ commit |
| `1 failed` | 1 个测试挂了 | ❌ 看 traceback，复制给 AI 重做 |
| `2 errors` | 2 个 setup 错 | ❌ 看 import 错误，给 AI |
| `4 warnings` | 警告（非错）| 🟡 可忽略，也可让 AI 修 |

### 6.5 失败时怎么写退回提示词

```
你的上一段代码有问题：

失败测试：tests/test_xxx.py::test_yyy

错误输出（粘贴 pytest 输出）：
[paste]

期望行为：[你写的]

请重做。提示：
- [看错误给的建议]
- 不要改我没让你改的文件
```

---

## 7. 验收标准总表

每完成 1 个 T，用这个表打勾：

| T | 产物 | pytest | 行数符合 | 文档 | commit |
|---|---|---|---|---|---|
| T0 | 9 子目录 | n/a | n/a | n/a | ☐ |
| T1 | sqlite.py | ☐ 8+ | ☐ ≤700 | n/a | ☐ |
| T2 | session_writer.py | ☐ 6+ | ☐ ≤400 | n/a | ☐ |
| T3 | skeleton.py | ☐ 5+ | ☐ ≤300 | n/a | ☐ |
| T4 | time_segmenter.py | ☐ 8+ | ☐ ≤500 | n/a | ☐ |
| T5 | segment_writer.py | ☐ 5+ | ☐ ≤350 | n/a | ☐ |
| T6 | fog_engine.py | ☐ 6+ | ☐ ≤450 | n/a | ☐ |
| T7 | scoring.py | ☐ 10+ | ☐ ≤750 | n/a | ☐ |
| T8 | recall_engine.py | ☐ 8+ | ☐ ≤750 | n/a | ☐ |
| T9 | fog_protocol.py | ☐ 6+ | ☐ ≤250 | n/a | ☐ |
| T10 | llm/provider.py | ☐ 5+ | ☐ ≤700 | n/a | ☐ |
| T11 | cli/user_controls.py | 手动测 | ☐ ≤400 | n/a | ☐ |
| T12 | cli/timeline.py | 手动测 | ☐ ≤500 | n/a | ☐ |
| T13 | gui/ | 截图 | ☐ ≤1500 | n/a | ☐ |
| T14 | test_recall_basic.py | ☐ 50 | n/a | n/a | ☐ |
| T15 | test_fog_protocol.py | ☐ 20 | n/a | n/a | ☐ |
| T16 | test_set.json | n/a | ☐ 50 段 | n/a | ☐ |
| T17 | QUICKSTART.md | n/a | ☐ ≤300 | ☐ | ☐ |
| T18 | LLM_PROVIDERS.md | n/a | ☐ ≤400 | ☐ | ☐ |
| T19 | M1_report.md | n/a | ☐ ≤500 | ☐ | ☐ |
| T20 | retention_engine.py | ☐ | ☐ ≤600 | n/a | ☐ |
| T21 | ask_restore.py | ☐ 5+ | ☐ ≤400 | n/a | ☐ |
| T22 | contradiction_detector.py | ☐ | ☐ ≤550 | n/a | ☐ |
| T23 | graph.py | ☐ | ☐ ≤700 | n/a | ☐ |
| T24 | regen_engine.py | ☐ | ☐ ≤500 | n/a | ☐ |
| T25 | gui/graph_view.py | 截图 | ☐ ≤1000 | n/a | ☐ |
| T26 | test_relations_graph.py | ☐ 15+ | n/a | n/a | ☐ |
| T27 | test_regen_engine.py | ☐ 10+ | n/a | n/a | ☐ |
| T28 | COMPETITOR_ANALYSIS.md | n/a | n/a | ✅ 完成 | ✅ |

---

## 8. 故障处理

### 8.1 单 T 失败 1-2 次

**重试**：
- 复制 pytest 输出给 AI
- 加提示："重做，不要改其他文件"

### 8.2 单 T 失败 ≥ 2 次

**降级 1 次**：
- 砍非核心功能
- 保留最小可用版本

降级优先级（保留顺序）：
1. L0-细节 SQLite 入库（不能砍）
2. 静默/雾化基本态转移（不能砍）
3. L0 兜底召回（不能砍）
4. 用户控制接口（不能砍）
5. 时间分段（可改成每会话单段）
6. 矛盾检测（可降级为 LLM 调用留 stub）
7. 关系图谱（可降级为 references 单类型）
8. 自动摘要重写（可降级为手动 GUI 按钮）
9. 知识图谱 GUI（可降级为 CLI 输出）
10. FTS5 全文索引（可降级到 LIKE）

### 8.3 整体混乱

**暂停 + 汇报老板（你自己）**：
- 写一段总结："M1 暂停于 T{id}，原因：xxx"
- 重新评估技术栈 / 设计

### 8.4 AI 写出超长文件

**强制拆分**：
- 给 AI："这个文件 > 1000 行，拆成 3 个：xxx.py / yyy.py / zzz.py"

### 8.5 AI 改了不该改的文件

**git 回退**：
```bash
git checkout HEAD -- path/to/wrong_file
```
然后让 AI 重做只动它该动的。

---

## 9. 进度跟踪模板

### 每日复制这个填

```markdown
## YYYY-MM-DD 进度

### 今日完成
- [ ] T0 ✅
- [ ] T1 ✅
- [ ] T2 ✅

### 今日产出
- src/store/sqlite.py (600 行, 8 测试)
- src/l0/session_writer.py (350 行, 6 测试)

### 遇到问题
- T4 失败 2 次，原因是：[原因]
- 解决：[方法]

### 明日计划
- T5 / T6 / T7

### 总进度
- M1 总：28 个 T
- 已完成：3/28（10.7%）
- 累计工时：5.5h
- 预计剩余：104.5h
```

### 每周复盘

```markdown
## YYYY-Wxx 周复盘

### 本周 T 数
- 计划：X 个
- 实际：Y 个
- 差异：Z

### 主要阻塞
- [列出]

### 下周调整
- [策略调整]
```

---

## 10. 常用命令速查

### 10.1 git

```bash
git status                          # 看状态
git add .                           # 暂存所有
git commit -m "T1: 实现 SQLite 存储"  # 提交
git log --oneline                   # 看历史
git checkout HEAD -- path/to/file   # 撤销单文件
git diff                            # 看修改
```

### 10.2 pytest

```bash
pytest -v                           # 跑全部
pytest tests/test_xxx.py -v         # 跑单个文件
pytest -k "test_name" -v            # 跑单个测试
pytest --cov=src                    # 覆盖率
pytest -x                           # 失败即停
```

### 10.3 数据库

```bash
# 用 Python 看
python -c "import sqlite3; conn=sqlite3.connect('~/.mtca/mtca.db'); print(conn.execute('SELECT count(*) FROM segments').fetchone())"

# 用 DB Browser
# 拖 .db 文件到 DB Browser for SQLite

# 备份
cp ~/.mtca/mtca.db ~/.mtca/mtca.db.bak
```

### 10.4 启动 MTCA

```bash
# 初始化 DB
python -m mtca_server init

# CLI 时间线
mtca timeline

# GUI（如果 T13 完成）
python gui/run.py
```

### 10.5 环境问题

```bash
# Python 版本
python --version    # 应 ≥ 3.10

# 重新装依赖
pip install -e ".[dev]"

# 清理缓存
rm -rf src/__pycache__ src/*/__pycache__ tests/__pycache__
```

---

## 11. 通用提示词模板

> 当你不知道该怎么写 prompt 时，复制这个填空：

```
你是一个 [角色：Python 后端 / 测试 / 文档 / GUI] AI。

工作目录：I:\PROJECTS\MTCA\

任务：[一句话说明]

必读（用 Read 工具读）：
1. [文件 1] - [用途]
2. [文件 2] - [用途]

实现文件：
- [路径 1] - [说明]
- [路径 2] - [说明]

技术要求：
- [约束 1]
- [约束 2]
- [约束 3]

测试要求：
- pytest 全通过
- 至少 N 个测试覆盖
- 关键边界：[列出]

输出：
- 创建的文件清单
- 每个文件行数
- pytest 输出
- 不要输出完整代码

预计工时：[X]h
```

---

## 附录 A: 文件路径速查

```
I:\PROJECTS\MTCA\
├── README.md
├── DEVELOPER_PLAN.md
├── HANDOVER_XIAOBA.md
├── PLAYBOOK.md                       ← 你正在看
├── pyproject.toml
├── mtca-server.py
├── .gitignore
│
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DATA_MODEL.md
│   ├── USER_CONTROLS.md
│   ├── V0.4_PIVOT.md
│   ├── COMPETITOR_ANALYSIS.md
│   ├── QUICKSTART.md                 (T17)
│   └── LLM_PROVIDERS.md              (T18)
│
├── src/
│   ├── store/sqlite.py                (T1)
│   ├── l0/
│   │   ├── session_writer.py          (T2)
│   │   ├── skeleton.py               (T3)
│   │   ├── time_segmenter.py         (T4)
│   │   └── segment_writer.py         (T5)
│   ├── fog/fog_engine.py             (T6)
│   ├── compress/
│   │   ├── scoring.py                (T7)
│   │   └── regen_engine.py           (T24)
│   ├── recall/
│   │   ├── recall_engine.py          (T8)
│   │   └── fog_protocol.py           (T9)
│   ├── llm/provider.py               (T10)
│   ├── cli/
│   │   ├── user_controls.py          (T11)
│   │   └── timeline.py               (T12)
│   ├── lifecycle/
│   │   ├── retention_engine.py       (T20)
│   │   ├── ask_restore.py            (T21)
│   │   └── contradiction_detector.py (T22)
│   ├── relations/graph.py            (T23)
│   ├── l1/, l2/, l3/                 (M3 才用)
│   └── adapters/                     (M4 才用)
│
├── gui/                              (T13)
│   ├── main_window.py
│   ├── timeline_view.py
│   ├── segment_detail.py
│   ├── fog_dialog.py
│   ├── graph_view.py                 (T25)
│   ├── run.py
│   └── requirements.txt
│
├── tests/
│   ├── conftest.py
│   ├── test_store_sqlite.py          (T1)
│   ├── test_session_writer.py        (T2)
│   ├── test_skeleton.py              (T3)
│   ├── test_time_segmenter.py        (T4)
│   ├── test_segment_writer.py        (T5)
│   ├── test_fog_engine.py            (T6)
│   ├── test_scoring.py               (T7)
│   ├── test_recall_basic.py          (T8/T14)
│   ├── test_fog_protocol.py          (T9/T15)
│   ├── test_llm_provider.py          (T10)
│   ├── test_relations_graph.py       (T23/T26)
│   └── test_regen_engine.py          (T24/T27)
│
└── benchmarks/
    ├── test_set.json                 (T16)
    └── M1_report.md                  (T19)
```

---

## 附录 B: 第一次跑通的最少任务

如果你时间紧，**先做这 8 个 T** 就能跑通最小可用：

```
T0 (0.5h) → T1 (3h) → T2 (3h) → T4 (4h) → T5 (3h) →
T7 (6h) → T8 (6h) → T13 GUI MVP (12h)
                    = 37.5h
```

**这 8 个做完**：MTCA 能存对话 → 切段 → 打分 → 召回 → GUI 看时间线

**剩下 20 个 T** 是增强（雾化 / 静默 / 矛盾 / 关系图 / 重写 / 测试）—— 用户用了觉得爽再回来加

---

## 结语

**老板你要做的事**：
1. 读这份文档
2. 选 T0 开始
3. 复制 prompt 喂 AI
4. 跑 pytest
5. 打勾
6. 下一个 T

**就这么简单**。每个 T 17 分钟你的时间 + AI 自己跑。

**关键习惯**：
- ✅ 串行派发（不要并发 T1-T5 一起）
- ✅ 每次跑 pytest 验收
- ✅ 失败的 T 退回 AI，不要自己修
- ✅ 每个 T 一次 commit
- ✅ 每日更新进度表

**开始吧**。从 T0 开始复制 prompt 给 AI。
