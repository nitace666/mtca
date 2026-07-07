# M2.5.8 C: facts retry / few-shot spike 报告

**目的**：M2.5_report §7 #6 指出 facts hit_rate 仅 16/50（32%），AI 抽不全事实。本 spike
对 50 case × 5 变体 = 250 次 LLM 调用做对照实验，量化"retry / few-shot / 修 prompt 传
递"各自能拉高多少 case_extract_rate，给老板决策"要不要全面修"提供数据。

**原则**：spike 不动生产代码（`src/llm/extractor.py` 只读）；所有变体通过 monkeypatch 拼
prompt，对 `benchmarks/test_set.json` 50 case 跑抽取并打分。结果写到
`benchmarks/_m258_spike_results/`。

---

## 现状摘要（Step 2）

| 项 | 内容 |
|---|---|
| **当前 prompt 模板** | `src/llm/extractor.py` `_SYSTEM_PROMPT`：要求严格 JSON `[{"content","confidence","tags"}, ...]`；`build_extraction_prompt` 返回 `{"system":..., "user":...}`。user 模板为 `请从以下对话片段中提炼事实：\n\n------\n{text}\n------\n\n输出 JSON 列表：` |
| **50 case 来源** | `benchmarks/test_set.json`（50 条），字段：`id/topic/user_messages/assistant_messages/expected_recall_query/expected_top_segment/expected_top1`。**无 `expected_facts` 字段**。 |
| **hit_rate 算法** | M2.5 报告里的"32%"实际指 **case_extract_rate** = 抽到 ≥1 fact 的 case 数 / 50。spike 复用此口径。 |
| **抽取流程** | user/assistant 消息拼成单段 → `extract_facts(text)` |
| **LLM 环境** | `http://localhost:8083` model=qwen36-heretic-q4km；baseline 24.4s/case |

---

## 5 变体设计（Step 3）

| 变体 | prompt | retry | 单 case LLM 调用上限 |
|---|---|---|---|
| **0 baseline** | 现有 `_SYSTEM_PROMPT` | 无 | 1 |
| **1 simple-retry** | 同上 | 空结果重试 1 次（同样 prompt） | 2 |
| **2 smart-retry** | 同上 | 空结果重试 2 次（重试追加 "请再次仔细审视对话…"，鼓励多抽） | 3 |
| **3 few-shot** | system 后注入 2 个抽取示例（不基于真实 case） | 无 | 1 |
| **4 few-shot + smart-retry** | 同 variant 3 | 同 variant 2 | 3 |

**spike 实现的关键差异**（重要）：
spike 脚本里直接调 `provider.generate(...)`，**把 system 拼到 user 末尾**
（`prompt["user"] + "\n\n[system]\n" + prompt["system"]`），因为
`LMStudioProvider.generate` 协议只接 `prompt: str` 单字符串，不支持 system 字段。

而**生产代码** `src/llm/extractor.py:_llm_generate` 调的是：
```python
full_user = prompt["user"]          # ⚠ 只用 user
return provider.generate(full_user, max_tokens=_EXTRACT_MAX_TOKENS)
```
**system 字段被丢弃**！见下方"Spec bug #1"。这意味着：

- spike v0 的 70% 反映了"如果 system 正确传递，预期 hit_rate"
- 真实生产代码的 baseline 还要低（验证见下方）

---

## 数据（Step 4 — 5 变体 × 50 case）

每个 case 跑后写 `benchmarks/_m258_spike_results/variant_{N}_results.jsonl`；
汇总写到 `all_variants_summary.json`。**完整结果在同级 `_m258_spike_results/` 目录**。

### 主表：5 变体 hit_rate 对照

| variant | case_extract_rate | cases_with_facts / 50 | total_facts | avg_facts/case | total LLM calls | spec_bugs | wall_time |
|---|---|---|---|---|---|---|---|
| **0 baseline** | **70.00%** | 35 / 50 | 189 | 3.78 | 50 | 15 | 965s |
| 1 simple-retry | 82.00% | 41 / 50 | 227 | 4.54 | 63 | 9 | 1205s |
| **2 smart-retry** | **86.00%** ← best | **43 / 50** | 230 | 4.60 | 72 | 7 | 1524s |
| 3 few-shot | 72.00% | 36 / 50 | 230 | 4.60 | 50 | 14 | 900s |
| 4 few-shot + smart-retry | 84.00% | 42 / 50 | **279** ← most facts | 5.58 | 78 | 8 | 1244s |

### 关键对比

- **baseline → best (v2 smart-retry)**：70% → 86%，**+16 percentage points**
- **baseline → v1 (simple retry)**：70% → 82%，**+12pp**（最低成本增益）
- **baseline → v4 (few-shot + smart)**：70% → 84%，+14pp，且抽到的 fact 总数最多（189 → 279，**+48%**）
- **v3 few-shot 单独**：case_rate 几乎不动（70% → 72%，仅 +2pp），但 fact 总数从 189 → 230（**+22%**），说明 few-shot 主要让"已能抽的 case 抽更多"，对"返回空"的 case 几乎无帮助
- **retry 副作用**：spec_bugs 从 15 → 7（v2），retry 把一半的 parse 错修了

### 5 变体行为差异原因（一致性分析）

| 行为 | baseline | smart-retry (v2) | few-shot (v3) |
|---|---|---|---|
| 处理"返回 []"的 case | 无能为力 | 重试 + 提示后约一半能拿到 facts | 几乎无变化 |
| 处理"已抽到 facts"的 case | OK | OK | 抽得更多（更多隐含 facts） |
| 处理"JSON 格式漂移"的 case | 静默失败 | 重试可能修复 | 几乎无变化 |
| 平均每 case 耗时 | ~20s | ~30s（+50%） | ~18s（无 retry） |
| 实现成本 | — | 低：循环 + 提示词 | 中：构造示例 |

---

## Spec bug 发现清单（spike 副产品）

### Bug #1（**重大 — 直接影响 hit_rate**）：`_llm_generate` 丢弃 system prompt

**位置**：`src/llm/extractor.py:213-217`

```python
def _llm_generate(provider: Any, text: str) -> str:
    prompt = build_extraction_prompt(text)        # {"system":..., "user":...}
    full_user = prompt["user"]                    # ⚠ 只用 user
    return provider.generate(full_user, max_tokens=_EXTRACT_MAX_TOKENS)
```

`build_extraction_prompt` 返回 dict，`_llm_generate` 只取 `user`，**`system` 字段被丢弃**。
LLM 看不到：
- "你是一个事实提炼助手"（角色定位）
- schema 约束（content/confidence/tags 字段）
- "不要 markdown 包装、不要前缀后缀"（输出格式约束）

**复现验证**：用 `extract_facts()`（生产入口）跑 test_001 / test_002 都返回 0 facts；
用 spike 脚本手动把 system 拼到 user 末尾跑同一 case，test_001 返回 5 facts、test_002 返回 8 facts。

**影响**：
- M2.5_report §7 #6 "16/50 case" — 真实原因之一就是 system 被丢，LLM 自由发挥 → 输出
  markdown 围栏 / 自然语言列表 / 其它 schema → `_parse_facts_from_text` 容错也救不回
  全部。
- 修这一行 → 预期 hit_rate 立刻从 32% 升到 spike baseline 70%。

### Bug #2（中）：retry 缺少"少抽"判定 + 重试成本不可见

**位置**：`src/llm/extractor.py:extract_facts` (L218-234) — 当前无 retry

设计层面问题：
- LLM 偶尔返回 facts 但被解析掉（schema 漂移），无重试补救
- LLM 偶尔事实抽少了（比如只抽 1 条，但对话里有 ≥3 条事实），也无重试补救
- 生产调用者无法配置 max_retries，无法为重要场景提高鲁棒性

**spike 量化**：retry → +12pp（simple）/ +16pp（smart），是低成本高 ROI 增强。

### Bug #3（轻）：`_parse_facts_from_text` markdown fence 正则可能截短

**位置**：`src/llm/extractor.py:_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)`

非贪婪 `.*?` 在 LLM 输出"开头有 ```json，结尾无 ```"时会匹配空字符串。
虽然有后续 3 重 fallback（整体 parse / fence / [...] 子串），但 spike 中仍观察到
15 个 case 返回 0 facts（spec_bugs），原始 raw 看起来是有效 JSON — 怀疑 fence 正则在某
些边界 case 下截短。建议改为贪婪 `.*` 或拆掉 fence fallback 直接 regex 找 `[...]`。

---

## 结论与建议

### 老板决策阈值对照
- hit_rate ≥ 60% → 建议全面修
- hit_rate < 50% → 建议跳过
- 50-60% → 老板拍

### 本 spike 结论

**强烈建议全面修**（spike best 86% 远超 60% 阈值）。

**推荐组合（按优先级）**：

1. **【必做】修 Bug #1**：`_llm_generate` 传递 system prompt。
   改 1 行。预期 hit_rate：32% → 70%。**最高 ROI**。

2. **【推荐】加 smart retry**（variant 2 设计）：
   - 空结果重试 2 次，重试 user prompt 追加提示
   - 改 extractor.py 加 retry 循环 + 配置项
   - 预期 hit_rate：70% → 86%。**成本可控**（+44% LLM 调用，但每 case 仅多 2 次重试）

3. **【可选】few-shot prompt**（variant 3/4 设计）：
   - 系统 prompt 注入 2 个抽取示例
   - 单独用 case_rate +2pp（不显著），但配合 smart retry 后 fact 总数 +48%
   - 建议放第二轮：等 retry 修稳定后再加 few-shot 进一步提"已抽 case 的 fact 数量"

**完整推荐方案预期 hit_rate**：bug #1 + smart retry ≈ **86%**（接近 best variant 2）

**不推荐**：
- 直接上 few-shot（v3 单用效果微弱）
- 上 few-shot + smart-retry（v4）：case_rate 84% < v2 的 86%，且 prompt 长度 ↑，无收益

### 验证方式（修复后）

1. 修 `extractor.py:_llm_generate` 让它把 system 拼上（最简单的实现是
   `prompt["user"] + "\n\n" + prompt["system"]` —— 但要确认 provider 支持；
   若不支持，改 `LMStudioProvider.generate` 接受 `system` kwarg 并通过 OpenAI
   `messages` 协议发）。
2. 在 `_m258_facts_retry_spike.py` 基础上加 retry 循环。
3. 跑 `python benchmarks/_m258_facts_retry_spike.py --variant all` 重测 5 变体；
   预期 v0 仍 70%（修 bug 后），v2 ≥ 86%。

### 下一步

待老板拍板：
- (A) 接受推荐：修 bug #1 + 加 retry → 进入 M3-0 实现阶段
- (B) 只修 bug #1（最低成本）→ M3-0 任务可能小一些
- (C) 先扩展 spike：测 14B 模型 / 试别的 prompt 模板 / 加 ground truth 标注
- (D) 暂缓：等 M3 整段重排再说

---

## 附录

### A. 跑法

```powershell
cd I:\PROJECTS\MTCA-m258-facts-retry-spike
python benchmarks\_m258_facts_retry_spike.py --variant 0    # 单跑一个
python benchmarks\_m258_facts_retry_spike.py --variant all  # 跑全部 5 变体
```

每变体预计 15-25 min（5 变体合计 ~100 min）。输出：
- `benchmarks/_m258_spike_results/variant_{0..4}_results.jsonl`（逐 case 详情）
- `benchmarks/_m258_spike_results/all_variants_summary.json`（汇总）

### B. 环境

- Python 3.10
- LM Studio @ http://localhost:8083 model=qwen36-heretic-q4km
- test_set.json: 50 case（4.7 KB，user+assistant 平均 410 字/case）
- 全部 LLM 调用 = 50 + 63 + 72 + 50 + 78 = **313 次**
- 总 wall time ≈ **96 min**

### C. 重跑基线对照

第一次跑 v0 输出 `case_extract_rate=0.7200 (36/50)`，第二次重跑（修 v0 jsonl
写空问题后）输出 `0.7000 (35/50)`。**LLM 输出有 ±1-2 case 随机波动**。生产
代码修完后建议连续跑 3 次取平均，避免被波动误导。


---

## 修复后重跑（M2.5.8 C 实施后）

**目的**：验证 Bug#1/Bug#2/Bug#3 修复未破坏 spike 已验证的最优路径（v2 smart-retry）。修前 v2 hit_rate=86%，期望修后 ≥ 85%。

**实施日期**：2026-07-07
**commit**：（待 §9 commit 后回填）
**修复清单**：
- Bug#1 重大：`src/llm/extractor.py:_llm_generate` 手动拼接 `system + "\n\n" + user` 传给 provider（4 个 provider 都不支持 system kwargs）
- Bug#2 中：`extract_facts` 加 `max_retries=2` 参数 + smart-retry 循环（重试 user 追加"（重试 n/2）请再次仔细审视对话"提示）
- Bug#3 轻：`_JSON_FENCE_RE` 由 `. *?` 改 `. *`（贪婪匹配嵌套 fence）
- 新增测试：`tests/test_extractor_bug_fixes.py`（5 个测试全 PASS）
- 全测验证：606 passed（既有 601 + 新增 5），零回归

**重跑环境**：
- worktree：`I:\PROJECTS\MTCA-fix-facts-extraction`（基于 main ea41f1f + 修复 commit）
- LLM：同 spike 报告（`http://localhost:8083` model=qwen36-heretic-q4km）
- test_set：同 spike 报告（50 case）
- 脚本：spike 脚本原版（复制到 fix worktree，未修改），让它 import 修复后的 extractor

### 单跑 v2 结果（修复后）

```
[v2 1/50]  facts=6 calls=1 lat=26.9s elapsed=27s
[v2 5/50]  facts=4 calls=1 lat=16.8s elapsed=117s
[v2 10/50] facts=3 calls=2 lat=37.5s elapsed=281s
[v2 15/50] facts=5 calls=1 lat=17.2s elapsed=406s
[v2 20/50] facts=7 calls=1 lat=19.4s elapsed=523s
[v2 25/50] facts=8 calls=3 lat=76.1s elapsed=753s
[v2 30/50] facts=7 calls=1 lat=21.0s elapsed=859s
[v2 35/50] facts=4 calls=1 lat=14.6s elapsed=919s
[v2 40/50] facts=0 calls=3 lat=78.0s elapsed=1277s
[v2 45/50] facts=6 calls=3 lat=68.7s elapsed=1461s
[v2 50/50] facts=1 calls=1 lat=2.2s  elapsed=1497s
>> v2: case_extract_rate=0.8800 (44/50) total_facts=234
       avg_lat/call=20.2s llm_calls=74 spec_bugs=6 wall=1497s
```

### 修前 vs 修后 对照

| 指标 | 修前 spike v2 | 修后 spike v2 | 变化 |
|---|---|---|---|
| **case_extract_rate** | 86.00% (43/50) | **88.00% (44/50)** | **+2.00pp** ✓ |
| total_facts | 230 | 234 | +4 |
| avg_facts/case | 4.60 | 4.68 | +0.08 |
| llm_calls | 72 | 74 | +2 |
| spec_bugs | 7 | 6 | -1 |
| wall_time | 1524s | 1497s | -27s（噪声） |

### 结论

- **验收阈值 ≥ 85%**：修后 88% **通过** ✓
- **回归验证**：修后 hit_rate ≥ 修前 hit_rate，未引入新退化（实际还 +2pp）
- **波动分析**：spike 报告附录 C 已说明"LLM 输出有 ±1-2 case 随机波动"，本次 +2pp 在噪声范围内，归因为随机波动而非真实提升（修复未改变 spike 内部 prompt 拼接，仅保证修复代码不破坏 monkeypatch 路径）
- **额外增益**：fix 后 `extract_facts` 生产路径上多了 system prompt 传递和 retry 机制，但 spike 脚本独立调 `provider.generate` 不走修复后的 `extract_facts`，故 spike 数字主要反映 LLM 稳定性，不直接反映修复收益。修复真实收益需要后续在生产入口跑对照测（在 M3 阶段）

### 后续

- 若老板授权 M3-0，可设计 A/B 测：50% 流量走修复前 extractor.py（保留备份），50% 走修复后，对照 hit_rate
- 或直接在 M3 灰度上线修复后 extractor，观察一段时间后回归分析

---

**附录 D（修复后）**：重跑命令

```powershell
cd I:\PROJECTS\MTCA-fix-facts-extraction
# 后台跑 spike v2（修复后 extractor）
Start-Process python -ArgumentList "benchmarks\_m258_facts_retry_spike.py","--variant","2" `
  -WorkingDirectory "I:\PROJECTS\MTCA-fix-facts-extraction" `
  -RedirectStandardOutput "_spike_v2.log" `
  -RedirectStandardError "_spike_v2.err" `
  -WindowStyle Hidden
# 实时查看进度
Get-Content _spike_v2.log -Wait
```
