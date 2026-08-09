# MTCA M2 性能+MCP+向量召回 方案（v0.4 同步）

> **副标题**：从 MVP 升级到"真能用"
> **状态**：M2 启动（设计锁定）
> **位置**：`./`
> **依赖**：M1 闭环（21/28 + 8 个静默完成 + P0 修复）
> **下一步**：M2.5 动态评分 → M3 跨设备

---

## 0. 为什么 M2

| 里程碑 | 内容 | 工时 | 你的核心收益 |
|---|---|---|---|
| ✅ M1（已完成 75%）| L0 写入 + 打分 + 雾化 + GUI MVP | ~95h | 跑通本地存对话 |
| **M2（本文件）** | 召回性能 + MCP + 向量召回 + CLI 测试 + 备份 + 量化 | ~60h | 接入 Claude Code / Cursor + 准确率可量化 |
| M2.5（已 plan）| 4 象限 + 紧急追踪 + 情感 | ~120h | "懂你忙什么" |
| M3 | CRDT 同步 + 主动智能 + Web | ~80h | 跨设备 + 主动 |

**M2 的核心赌注**：从"CLI 自己用"升级到"任何 Agent 都能 5 行接入"。MCP Server 是这个跳跃的载体。

---

## 1. M2 Goals & Non-Goals

### ✅ Goals（7 项）

1. **召回 p95 < 50ms @ 10k 段**（M1 实测 p95=145ms @ 1k 段，超目标 3 倍）
2. **向量召回通道**（bge-small-zh 30MB）—— 长尾查询准确率 +30%
3. **MCP Server 接入** —— Claude Code / Cursor 5 行配置即可调
4. **召回准确率量化**（hit_rate@1 / hit_rate@3 / MRR）
5. **CLI 测试从 0% → 60%**（M1 报告 B1 系列）
6. **自动备份** —— 每天 1 份，保留 7 天
7. **GUI 自动化截图**（playwright，可选）

### ❌ Non-Goals（M2 不做）

- ❌ 跨设备同步（M3 才做）
- ❌ L1-L3 压缩视图（M3 才做——M2.5 是 4 象限，不是 L1-L3）
- ❌ 主动智能（M3）
- ❌ Web 时间线（M3）
- ❌ 多用户隔离（M4+）
- ❌ 加密 SQLite（M3）

---

## 2. M2 依赖变化（新 pip 包）

| 包 | 用途 | 强制？ | 备选 |
|---|---|---|---|
| `sqlite-vss` | 向量检索 | ⚠️ 看 T29 决策 | 纯 sqlite + 朴素 cosine |
| `sentence-transformers` | bge-small-zh 加载 | ⚠️ 同上 | ONNX runtime（更轻量）|
| `httpx` | 已装（T10）| — | — |
| `rich` | 已装 | — | — |
| `click` | 已装 | — | — |
| `pytest-benchmark` | 已有（dev 依赖）| — | — |
| `pytest-playwright` | 仅 T34 GUI 可选用 | 🟡 可选 | 手动截图 |

**决策**：bge-small-zh 30MB，可接受；M2 阶段引入 sentence-transformers。sqlite-vss 需要 sqlite-extensions，**Windows 编译麻烦**，先评估；如果 24h 内跑不通，回退"sqlite + 朴素 cosine 计算"方案。

---

## 3. M2 架构变化

### 3.1 召回主链路（v2）

```
              query
                ↓
       ┌──── query_router ────┐
       │  (按长度/字符类别分发) │
       ↓                       ↓
   ┌───────┐              ┌──────────┐
   │ FTS5  │              │ bge 向量  │   ← M2 新增
   │ (L0)  │              │ (L0 嵌 入) │
   └───────┘              └──────────┘
       ↓                       ↓
   rerank(Jaccard)        rerank(余弦)
       ↓                       ↓
       └─────── merge ─────────┘
                ↓
       recall_with_fallback
                ↓
       返回 L0 原文
```

### 3.2 MCP Server 接口

```
tool: recall_segments
  params: { query: str, top_k?: int = 5, time_window?: tuple}
  returns: list[ {segment_id, session_id, score, messages} ]

tool: mark_important
  params: { segment_id: str }
  returns: { status: "ok", rows_updated: int }

tool: fog_segment
  params: { segment_id: str, anchor: str }
  returns: { status: "ok", fog_state: "fogged_once" }

tool: get_recent_sessions
  params: { limit?: int = 10 }
  returns: list[ {session_id, topic_label, started_at, ...} ]
```

### 3.3 备份流程

```
MTCA 启动
    ↓
检测 ~/.mtca/backups/ 目录
    ↓
复制当前 mtca.db → backups/mtca-YYYYMMDD.db
    ↓
清理 backups/ 中 > 7 天的文件
```

---

## 4. 任务清单 T28-T34

| T | 任务 | 工时 | 依赖 | 产出 |
|---|---|---|---|---|
| **T28** | 召回性能优化 | 6h | 0 | `src/recall/recall_engine.py` 加 query 缓存 + 预编译 + 单 SQL union |
| **T29** | 向量召回通道 | 12h | T30 | `src/recall/vector_engine.py` 新建 + sqlite-vss 集成 + bge-small-zh 加载 |
| **T30** | 召回准确率量化 | 6h | T28 | `benchmarks/test_set.json` 加 expected_top1 + `benchmarks/accuracy_report.py` |
| **T31** | MCP Server | 16h | T29 | `src/adapters/mcp_server.py` 新建 + stdio transport |
| **T32** | CLI 测试补全 | 4h | 0 | `tests/test_user_controls_cli.py` + `tests/test_timeline_cli.py` ≥30 测试 |
| **T33** | 自动备份 | 2h | 0 | `src/store/backup.py` 新建 + 启动 hook |
| **T34** | GUI 自动化截图（可选）| 4h | 0 | `tests/gui/test_*.py` + playwright 集成 |
| **M2 总** | | **~50h** | | 7 个新/改文件 + 30+ 新测试 + 量化基线报告 |

---

## 5. 任务详情（按执行顺序）

### T28 — 召回性能优化（基础）

**问题**：当前 `recall()` 走 2 通道（FTS5 + segments LIKE）+ Jaccard rerank + 邻居展开，1k 段就 145ms。

**3 个优化**：
1. **预编译 SQL**：用 `conn.execute()` 缓存 prepared statement
2. **FTS5 + segments LIKE 合并为单 SQL**（UNION ALL 一次扫）
3. **query 缓存**：LRU 100 条，按 query string hash

**验收**：p95 < 50ms @ 10k 段 + 准确率不变（hit_rate@1 ≥ 现有值）

### T29 — 向量召回通道（核心差异化）

**新模块**：`src/recall/vector_engine.py`
- `embed(text: str) -> list[float]`：bge-small-zh 出 384 维向量
- `search_similar(query_vec, top_k) -> list[dict]`：sqlite-vss 或 朴素 cosine

**两条路径**：
- **A 推荐**：sqlite-vss（快但 Windows 编译麻烦，linux/macOS 一键）
- **B 兜底**：纯 sqlite + Python numpy.cosine（性能差，但零依赖）

**24h spike**：先试 A，A 通过就锁；不通就回退 B，M2 末尾再考虑 Qdrant 替代

**验收**：长尾查询"3 年前那次旅行"等 FTS5 关键词命中差的，bge 通道有命中

### T30 — 召回准确率量化（度量）

**改 test_set.json**：每条加 `expected_top1: segment_id`（已有人工标注）

**新文件 `benchmarks/accuracy_report.py`**：
- 跑 50 段测试集
- 输出 hit_rate@1 / hit_rate@3 / MRR
- 与 baseline（T28 前的 v1 召回）对比
- 生成 markdown 报告存到 `benchmarks/M2_accuracy_report.md`

**验收**：report 自动生成 + 数字可信 + 提升幅度可见

### T31 — MCP Server（M2 关键产出）

**新模块**：`src/adapters/mcp_server.py`
- stdio transport（与 Claude Code / Cursor 集成）
- 4 个 tool：recall_segments / mark_important / fog_segment / get_recent_sessions
- 复用现有 `src.recall` / `src.compress.scoring` / `src.fog.fog_engine`

**集成点**：`pyproject.toml`
```toml
[project.scripts]
mtca-server = "src.cli.server:main"
mtca-mcp = "src.adapters.mcp_server:main"   # ← 新
```

**测试**：`tests/test_mcp_server.py`
- mock stdin/stdout
- 跑 4 tool 各 3 个 case
- 验证 prompt 输入 → JSON 输出

**验收**：`mtca-mcp` 启动后可用 `echo '{"tool":"recall_segments","params":{"query":"x"}}' | mtca-mcp` 返回 JSON

### T32 — CLI 测试补全（M1 报告 P1）

**2 个新文件**：
- `tests/test_user_controls_cli.py`（≥ 20 测试）
  - cmd_important / cmd_cycle / cmd_archive / cmd_fog 各 3 个
  - parse_command 5 个（前缀 / 行中 / 中文 / 错误）
  - parse_natural 5 个（fog / cycle / 重要 / 归档）
  - argparse fallback 5 个
- `tests/test_timeline_cli.py`（≥ 10 测试）
  - render_timeline / render_tree
  - 4 状态色映射
  - 空 DB / 多 session 边界

**验收**：CLI 覆盖率从 0% → ≥ 60%

### T33 — 自动备份（M1 报告 P1 / 数据安全）

**新模块**：`src/store/backup.py`
- `backup_now(path, keep_days=7) -> str` 返回备份文件路径
- `start_backup_hook()` 注册为 mtca-server init 后

**集成**：`src/cli/server.py init` 命令末尾调 `start_backup_hook()`

**测试**：
- mock time.time 跑 3 次确认保留 7 天
- 损坏 db 检测 + 自动还原到上次备份

**验收**：每天启动至少 1 份备份，> 7 天的自动清理

### T34 — GUI 自动化截图（可选）

**新依赖**：playwright（仅 dev） + Chrome/Edge

**新文件**：`tests/gui/test_screenshots.py`
- 用 QTimer 把 QWidget render 到 offscreen image
- 截 4 个 main page：main_window / timeline_view / fog_dialog / 4 象限
- 存到 `tests/gui/screenshots/`

**验收**：4 张图 + pytest fixture 可重跑

---

## 6. 测试要求

| 类别 | 新增测试数 | 累计目标 |
|---|---|---|
| T28 性能 benchmark | 5 | 301 |
| T29 向量召回 | 15 | 316 |
| T30 准确率量化 | 5 | 321 |
| T31 MCP Server | 12 | 333 |
| T32 CLI | 30 | 363 |
| T33 备份 | 8 | 371 |
| T34 GUI 截图 | 4 | 375 |
| **M2 总** | **~79 新增** | **375 passed** |

加上 M2.5 计划，T29-T35 还会再加 ~60，**M2 + M2.5 后到 ~435 测试，~85% 覆盖率**。

---

## 7. 验收清单（M2 收尾）

| 项 | 指标 | 验收方式 |
|---|---|---|
| 召回 p95 < 50ms | 10k 段 | pytest-benchmark |
| 准确率提升 | hit_rate@1 ≥ baseline +10% | accuracy_report.py 自动生成 |
| MCP Server 工作 | Claude Code 配置后能调通 | 手动 + tests/test_mcp_server.py |
| CLI 覆盖 ≥ 60% | pytest --cov=src/cli | pytest-cov |
| 自动备份 | 启动后存在 backups/*.db | 手动 + test_backup.py |
| 旧测试 0 回归 | 296 + 30 = 326 passed | pytest |
| M2_report.md | benchmark + accuracy + acceptance | 类 M1_report 写 |

---

## 8. 风险与兜底

| 风险 | 兜底 |
|---|---|
| sqlite-vss Windows 编译失败 | 24h spike 不通就回退到 sqlite+numpy 方案 |
| bge-small-zh 模型文件太大 | 30MB 接受；用户不想用可 disable 向量通道 |
| MCP Server 协议演进 | 锁 mcp-python-sdk 到具体版本（≥ 0.5）|
| 召回性能优化破坏正确性 | T30 准确率量化在前；优化后必须 ≥ baseline |
| 自动备份占太多磁盘 | 默认保留 7 天；可配 |
| playwright 在 Windows 装不稳 | 标 T34 为可选，跳过不影响其他 |

---

## 9. 任务依赖图

```
T28 (perf) ──────────────────┐
                             ↓
T30 (accuracy 量化) ←────────┤
                             ↓
T29 (vector 通道) ───────────┤
                             ↓
T31 (MCP Server) ←───────────┘

T32 (CLI tests) ── 独立，可并行
T33 (backup)    ── 独立，可并行
T34 (GUI 测试)  ── 独立，可并行
```

**推荐执行顺序**：
1. T28 (perf) — 立即可做，无依赖
2. T30 (accuracy) — 跑 baseline + 验证 T28 不回归
3. T32 (CLI tests) — 并行做，4h
4. T33 (backup) — 并行做，2h
5. T29 (vector) — spike 后决定 A/B 路径
6. T31 (MCP Server) — 锁最关键的对外接口
7. T34 (GUI) — 锦上添花，最后做或不做

---

## 10. 与 M1 / 现有 8 铁律的兼容性

| M2 改动 | 8 铁律影响 |
|---|---|
| 预编译 SQL + 缓存 | 不影响（只读性能优化）|
| sqlite-vss 向量索引 | 新 schema，**不在 L0 骨架列**，合规 |
| MCP Server 4 tool | AI 通过 MCP 调 = 4 个命令的延伸，**铁律 6 合规** |
| 自动备份 | 只读 .mtca/，不碰骨架列，合规 |
| CLI 测试 | 不改源码，纯加测试，合规 |
| GUI 截图 | 只读 GUI，不改数据，合规 |

**全绿，0 违反**。

---

## 11. 不在 M2 范围（与 M2.5 / M3 区分）

- ❌ 4 象限评分（M2.5 → T29-T35）
- ❌ 紧急追踪 / 情感属性（M2.5 → T31/T32）
- ❌ 4 象限 GUI（M2.5 → T34）
- ❌ 跨设备 CRDT（M3）
- ❌ 主动智能（M3）
- ❌ Web 时间线（M3）

---

## 12. 下一步

老板你选：

1. **开 T28**（召回性能优化）—— 我给第一段 prompt，你新对话跑
2. **开 T32**（CLI 测试补全）—— 比 T28 简单，4h 完事
3. **开 T33**（自动备份）—— 2h 完事，最简单
4. **保留** —— 等你想开工再说

我推荐 **T33 → T32 → T28 → T30 → T29 → T31**（从最简单到最难，从独立到有依赖）。

---

**文档结束**。下次开工从这里继续。