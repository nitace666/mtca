# 小八启动指令（M1 长期记忆 MVP v0.4）

> 给小八看的执行单。详细设计在 `DEVELOPER_PLAN.md` + `docs/V0.4_PIVOT.md`。本份是执行清单 + 验收标准。

---

## 启动前必读（按顺序）

1. `I:\PROJECTS\MTCA\README.md` —— 项目导览 + 8 条铁律
2. `I:\PROJECTS\MTCA\DEVELOPER_PLAN.md` —— 完整开发文档
3. `I:\PROJECTS\MTCA\docs\DATA_MODEL.md` —— schema 详细说明
4. `I:\PROJECTS\MTCA\docs\ARCHITECTURE.md` —— 架构图与设计动机
5. `I:\PROJECTS\MTCA\docs\V0.4_PIVOT.md` —— v0.4 方向调整（必读）
6. `I:\PROJECTS\MTCA\docs\COMPETITOR_ANALYSIS.md` —— 6 家竞品横向对比

---

## 启动确认

小八读完后**必须**先回一句 **"已加载 v0.4"**，然后开始下面任务。

不回 = 裸跑 = 立即 kill 重派。

---

## M1 任务清单（T0–T28，按顺序，~110h）

```
[基础设施层] T0–T2  6h
T0  git init 仓库 + 写 .gitignore + 项目骨架              0.5h
T1  src/store/sqlite.py（schema + WAL + FTS5 + 不可变触发器）  3h
T2  src/l0/session_writer.py（L0-细节 消息入库）         3h

[数据层] T3–T6  12h
T3  src/l0/skeleton.py（L0-骨架锚点 + 关键词生成）       2h
T4  src/l0/time_segmenter.py（3 规则 + 弱合并）         4h
T5  src/l0/segment_writer.py（段落 + L0-B 写入）         3h
T6  src/fog/fog_engine.py（雾化：物理擦除 L0-细节）     3h

[智能层] T7–T10  18h
T7  src/compress/scoring.py（动态打分）                  6h
T8  src/recall/recall_engine.py（双通道召回）            6h
T9  src/recall/fog_protocol.py（雾化 3 态召回行为）     2h
T10 src/llm/provider.py（Ollama / LM Studio / llama.cpp / 云 API 抽象）  4h

[接口层] T11–T13  19h
T11 src/cli/user_controls.py（/重要 /循环 /归档 /雾化）  3h
T12 src/cli/timeline.py（CLI 时间线，GUI 出后降级）      4h
T13 GUI MVP（时间线 + 话题树 + 雾化按钮，技术栈待定）  12h

[生命管理层] T20–T22  11h
T20 src/lifecycle/retention_engine.py（retention 策略 + 静默态转移）  4h
T21 src/lifecycle/ask_restore.py（AI 询问 + ref_count 升级）         3h
T22 src/lifecycle/contradiction_detector.py（矛盾 → supersede）      4h

[关系图谱] T23  5h
T23 src/relations/graph.py（4 种关系生成 + 图遍历）      5h

[摘要重写] T24  4h
T24 src/compress/regen_engine.py（fog/supersede 触发重写）  4h

[GUI 知识图谱] T25  8h
T25 GUI 知识图谱视图（节点 + 边 + 时间倒带滑块）         8h

[测试] T14–T15, T26–T27  10h
T14 tests/test_recall_basic.py（50 段用例）              5h
T15 tests/test_fog_protocol.py（雾化 3 态测试）          3h
T26 tests/test_relations_graph.py（图谱 + supersede 流转）  4h
T27 tests/test_regen_engine.py（重写触发 + 失败回退）  3h

[文档] T17–T19, T28  5h
T16 benchmarks/test_set.json（30 老板真历史 + 20 编）    4h
T17 docs/QUICKSTART.md（GUI 启动 + 雾化教程）          2h
T18 docs/LLM_PROVIDERS.md（4 种后端接入指南）          2h
T19 benchmarks/M1_report.md（验收报告）                 3h
T28 docs/COMPETITOR_ANALYSIS.md（6 家横向对比）         3h

                          M1 总: ~110h （实际 2.5 周，1 人全时）
```

---

## 验收标准（M1 升级版）

| 项 | 指标 |
|----|------|
| L0-细节 完整入库 | 100% 消息不丢（pytest 验）|
| L0-骨架 生成 | 每个段落都有锚点句 + 关键词（pytest 验）|
| 时间分段准确率 | 边界合理（人工抽检 50 段 ≥ 85%）|
| 动态打分 | 24 小时模拟后重要话题保持 L1 ≥ 95% |
| 静默态转移 | 升级/降级触发条件正确（pytest 验）|
| 矛盾检测 | supersede 关系生成正确（pytest 验）|
| 摘要重写 | fog 后 L1 自动重生（pytest 验）|
| 关系图谱 | 4 种关系类型正确生成 + 遍历（pytest 验）|
| 召回准确率 | 老板真历史 30 段测试 ≥ 90% |
| L0 兜底 | 摘要失败时回 L0-骨架（pytest 验）|
| 用户控制接口 | /重要 /循环 /归档 /雾化 行为正确 |
| 主召回延迟 | < 50ms（P95，10k 段压测）|
| GUI 启动 | < 5 秒 |
| 安装门槛 | `pip install -e .` 后 `mtca` 单文件启动 |

---

## 不需要做的（明确划界）

- ❌ 不接入 Qdrant / 向量数据库（M3 之前不接）
- ❌ 不做 MCP Server（M2 才做）
- ❌ 不接真实 Agent 平台（M4 才做）
- ❌ 不接云 LLM API（T10 留接口，M1 不实际启用云）

M1 一切只是 **架构骨架 + GUI MVP**：
把 SQLite + L0 双层 + 静默/雾化/矛盾检测 + 关系图谱 + GUI 全跑通，**足够验证 PMF**。

---

## 失败处理

- 任一任务失败 ≤ 2 次 → 重试
- ≥ 2 次仍失败 → **降级 1 次**（砍非核心功能保核心跑通）
- 降级后仍失败 → **暂停 + 汇报老板**

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

---

## 完工回报格式

```
## 任务完成 v0.4
T0–T28 完成情况 + 总工时（实际 vs 预计）
产物：
- 文件路径列表
- M1 报告路径：benchmarks/M1_report.md
- 截图：GUI 关键页面
等待老板验收
```

或者：

```
## 任务失败
T{id} 挂掉
- 原因
- 已经试过的降级
- 需要老板决策的点
```

---

## 联系

- 项目路径：`I:\PROJECTS\MTCA\`
- 启动口令：老板 / 波波发布
- v0.4 必读：`docs/V0.4_PIVOT.md`
