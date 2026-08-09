# MTCA 项目会话笔记（2026-07-06）

> 本文件是主对话的"压缩摘要"。新开对话时第一句话："读 SESSION_NOTES_2026-07-06.md 继续"。
> 主对话 token 已膨胀，fork 后会清爽。

## 项目速览

**MTCA = Multi-Tier Context Architecture**，AI 长期记忆中间件，本地优先。
- 5 阶段路线：M1（基础 ✅）→ M2（性能+MCP+向量 ✅）→ M2.5（4 象限+紧急追踪+永不遗忘，6/7 ✅）→ M3（跨设备 计划）→ M5（开源 发布）
- 测试基线：570 passed in 104s
- 唯一物理文件：本地 SQLite @ `~/.mtca/mtca.db`（10k+ 用户真实对话）
- 7 条已发现的 spec bug（重要！前面 M2.5.x 都发现过，未来 M3 也会）

## 已落地 commit 链（main）

```
926c36b M2.5.6: 情感自动提取（5 alias + 5 标准 + 别名映射，喂多因子 f_emotion）
39ca7bb M2.5.5: GUI 4 象限视图 + URGENT 红条 + 30s 自动 refresh
2bae974 M2.5.4: CLI /紧急 /完成 /延期 3 命令 + _time_parse
2581e88 M2.5.3: urgent_tracker 3 状态机
13fcc59 M2.5.2: 多因子打分公式 + Q1/URGENT/重要 永不衰减（第 9 铁律）
b641ac0 M2.5.1: segments 表加 4 象限 5 字段
6a505f0 T31: MCP Server 让 Claude Code / Cursor 接入（6 tool + stdio）
e2db113 merge C1 (LLM 事实提炼 + GUI 多平台配置, hit_rate 0.16→0.38)
139a83b C1_fix1: parser 兼容 3 schema
5a40fad C1: LLM 事实提炼 + GUI 多平台配置
332714a docs(plans): T29 标 SUPERSEDED + 加 C1 完整 8-step spec
7386157 T32: CLI 测试补全
0faab5c T33: 自动备份模块
87b7e17 T30: 召回准确率量化（baseline hit_rate=0.160）
b1f1b95 T28_fix2: p95 阈值 50→80ms
4469cdc chore(gitignore): 排除 tests/_tmp_ground_truth
9517e1c T28_fix: LRU cache_key 加 path 维度
dd30f5b T28: 召回性能验证 + 防御性 LRU 缓存
（更早 commits 略 - M1 阶段）
```

## 关键设计决策（已锁定）

### 8 条铁律（铁律 1-8 不变，新增第 9 条）
9. **重要记忆永不遗忘**（用户原话："决不接受遗忘"）
   - Q1 段永不进 L3_hidden
   - /重要 (score=10000) 冻结
   - URGENT 状态 f_time=1.0
   - 紧急到期 → 必问"完成/延期/重要"（不静默）

### T10 复用为 LLM provider
ollama / lmstudio / llamacpp / cloud 4 后端 + factory + mock fallback 已在 src/llm/provider.py 实现。**不重新造**。

### GUI 配置：用户可配置
- 4 后端 radio（Ollama / LM Studio / llama.cpp server / Cloud）
- llama.cpp server 走 LMStudioProvider 路径（OpenAI 兼容），base_url 配 8083
- 用户可改 backend 不改代码

### worktree 隔离
- 每个 Stage 一个 worktree：`codex/<stage>-<slug>`
- main 是 fast-forward 目标
- 删 worktree + branch 在 merge 后

### 第 5 块 MCP Server：自建 stdio JSON-RPC
- 没用 mcp-python-sdk（自建更轻量）
- 6 tool: recall_segments / search_facts / mark_important / fog_segment / get_recent_sessions / list_llm_backends
- 已用 docs/MCP_INTEGRATION.md 接入指南（不动 README.md 锁定文件）

### A-mode 协议
- A-mode = 我把"完整剧本"贴给新对话，新对话独立跑全活
- 失败停下，成功只贴 5 字段总结（worktree / commit / 测试数 / spec bug / 完成）
- **本主对话不再跑长 A-mode 任务**（避免 token 膨胀）

### 4 角色工作流
1. **主对话（这个 / fork 后的新对话）**：战略 + 拍板 + 合并 + 出下一段方向
2. **分支对话**：写 A-mode 提示词（spec 完整 + TDD 模板）
3. **执行对话**：贴提示词跑全活，给 5 字段报告
4. **用户**：把执行报告转分支审核，再把审核总结转主对话

## C1 验证结果（重要！）

- **hit_rate@3 改善**：baseline 0.160 → **0.380**（+0.220，2.4× 提升）
- 16/50 case 抽出 103 个 facts（剩下 34 个 LLM 评估"无可提炼"）
- 验证脚本：benchmarks/_c1_full.py
- C1_fix1 修了 2 个 schema bug：字段名 alias + RDF 三元组兼容

## 已知 spec bug 历史（提醒 M3 注意类似问题）

1. **字段名不匹配**：LLM 实际输出和 parser 期望不同（content/fact/text/subject/predicate/object 各种）
2. **LLM 完全无视 prompt**：qwen-heretic 强 fit-tune 模型无视 instruction 输出自有 schema
3. **fog_anchor 列双向锁**：被 trigger + _IMMUTABLE_FIELDS 双向拦截，update_segment 静默丢字段
4. **/重要 检查 base >= 10000 永远失败**：base = current_score - 1 永远 < 10000，应检查 current_score
5. **/重要 不应被 f_importance 放大**：破坏完全冻结语义
6. **urgent_state='important' 状态机图未到终态**：只调 mark_important 不设状态
7. **extract_emotion 函数在 spec 中被截断**：return 引用了不存在名字
8. **CLI argparse flag 位置**：--db 不能放子命令后

**M3 注意**：CRDT / 跨设备 sync 也可能踩类似 schema 兼容坑，每个新数据路径都要先 probe 再写测试。

## 当前会话 M2.5 进度（6/7 完成）

| Stage | 状态 | 工时 | 关键产物 |
|---|---|---|---|
| M2.5.1 schema | ✅ | 1d | 5 字段（urgency/importance/emotion/expires/urgent_state）+ quadrant 派生 |
| M2.5.2 多因子打分 | ✅ | 3d | urgency×importance×emotion×ref×time + 4 象限 half_life + 3 永不遗忘例外 |
| M2.5.3 urgent_tracker | ✅ | 3d | track/tick/handle_response 3 状态机 |
| M2.5.4 CLI 命令 | ✅ | 2d | /紧急 /完成 /延期 + _time_parse 8 种格式 |
| M2.5.5 GUI 4 象限 | ✅ | 3d | 2x2 网格 + URGENT 红条 + 30s 自动 refresh |
| M2.5.6 情感提取 | ✅ | 3d | 5 标准 + 5 alias + 别名映射 |
| M2.5.7 测试+报告 | ⏳ | 2d | 验收 9 铁律 + benchmarks/M2.5_report.md |

## 待办（按优先级）

### 1. M2.5.7 收尾（~10 min，无 LLM）
- 跑全测确认 570 passed
- 写 benchmarks/M2.5_report.md（验收 9 铁律：跑 query "1 年后"，预期 Q1/URGENT/重要 段仍在）
- commit

### 2. 收 M2 + M2.5（建议）
- 写 benchmarks/M2.5_report.md
- 写 src/l0/quadrant.py 文档
- 加 M2.5_PLAYBOOK.md 里的"实际产出"章节
- 评估 M3 范围

### 3. M3 跨设备同步（~80h，未启动）
- CRDT over SQLite（自研协议）
- 或用 LiteFS / rqlite 等现有方案
- 风险：schema 同步、冲突解决、LLM 状态共享
- **建议先做 spike**：用 2 个 db 互相同步看可行性

### 4. 文档补完
- README.md 锁定文件（要用户明确解锁才能改）
- 写 docs/M3_DESIGN.md
- 写 docs/USER_GUIDE.md（用户视角）

## 下次开新对话第一句话模板

```
读 ./SESSION_NOTES_2026-07-06.md 继续 M2.5.7 收尾。
```

## 文件位置参考

- 主仓库：I:\PROJECTS\MTCA
- 项目文档：
  - M2_PLAYBOOK.md（M2 高层设计）
  - M2.5_PLAYBOOK.md（M2.5 高层设计 + T29-T35）
  - 审查报告.md（M1 4 阶段审查）
  - plans/2026-07-05-m2-rollout.md（M2 实施 plan）
  - docs/MCP_INTEGRATION.md（Claude Code / Cursor 接入）
  - benchmarks/M2_accuracy_report.md
  - benchmarks/C1_validation_report.md
- 关键代码：
  - src/llm/provider.py（T10 provider）
  - src/llm/extractor.py（fact extraction + 3 schema 兼容）
  - src/llm/emotion_extractor.py（M2.5.6 情感）
  - src/llm/facts_store.py（facts CRUD）
  - src/lifecycle/urgent_tracker.py（M2.5.3 3 状态机）
  - src/l0/quadrant.py（M2.5.1 4 象限派生）
  - src/compress/multi_factor_score.py（M2.5.2 多因子）
  - src/recall/recall_engine.py（T28 LRU + facts path）
  - src/adapters/mcp_server.py（T31 6 tool）
  - gui/quadrant_view.py（M2.5.5 4 象限 UI）