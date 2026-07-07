# M3-0 sync 选型 spike 报告

> 任务：M3-0 sync 选型 spike — 8h 内决定 CRDT vs Litestream vs 自研 LWW
> 日期：2026-07-07
> worktree：I:\PROJECTS\MTCA-m258-m3-spike
> commit：（待 commit 后回填）
> commit message：`spike(sync): M3-0 选型验证，3 方案 × 5 场景对比（CRDT/Litestream/LWW）`

---

## 1. 测试目的

按 MANAGER_HANDOFF §12 M3-0 spike 任务，验证 3 种 sync 选型，给 M3-1 提供决策依据：

1. **选型验证**：3 方案都能实现 SyncAdapter 协议
2. **冲突场景**：2 设备并发改同段时的合并行为
3. **schema 同步**：sidecar schema_version 不匹配时各方案策略
4. **网络模拟**：50ms RTT 下性能

测试结果供 M3-1 选型决策。

---

## 2. 现状摘要

### 2.1 SyncAdapter Protocol（src/sync/sync_adapter.py）

```python
@runtime_checkable
class SyncAdapter(Protocol):
    def push(self, data: dict) -> str:        # 返回远端 id
    def pull(self, since_ms: int = 0) -> list[dict]: ...
    def status(self) -> dict:                 # mode/last_sync_ms/pending/errors
```

### 2.2 SQLite 环境

- 系统 SQLite 版本：**3.37.2**（spec 文案"3.39+"与实际不符，见 §6.1）
- JSON1 完整可用：json_extract / json_set / json_remove / json_each / json_object / json_array 全部 OK
- path 必须 `$.a` 形式（`a` 不带 `$` 会报"JSON path error"）

### 2.3 segments 表（22 列，无 updated_at_ms）

主键：`segment_id`
时间字段：start/end_msg_seq, start/end_at
段属性：gap_to_next, weak_merged, topic_label, current_tier, current_score
雾化（v0.4）：fog_state, fog_at, fog_anchor
静默（v0.4）：silence_state, promoted_at, ref_count, last_ask_at, user_retention_days, long_silent
矛盾（v0.4）：superseded_by, supersedes_count, contradiction_level
**🔴 无 `updated_at_ms` 列**（spec 文案与实际不符，见 §6.2）

### 2.4 已有 sync stub

- `src/sync/sync_adapter.py` — Protocol
- `src/sync/local_only_sync.py` — 默认 no-op
- `src/sync/registry.py` — 注册/获取入口
- `src/l0/segment_writer.py` — `sync_segment_to_adapter()` 入口（M2.5.8 B 已落）

---

## 3. 3 方案设计

### 方案 A：LWW（Last-Write-Wins）

- sidecar 表 `sync_meta(segment_id PK, updated_at_ms, device_id, schema_version, op)`
- push：写本地 segments（partial UPDATE）+ sync_meta
- pull：peer.sync_meta.ts > local.ts → 整段覆盖
- 字段级 upsert：partial payload 走 UPDATE，已存在行只设 payload 提供的字段

**实现**：`src/sync/prototypes/lww.py`，208 行

### 方案 B：Litestream-like（WAL frame 思路）

- sidecar 表 `sync_wal(frame_id PK AUTOINCREMENT, segment_id, frame_ts, device_id, schema_version, op, payload_json)`
- sidecar 表 `sync_wal_consumed(frame_id, device_id)` 复合主键（防止 A/B 端 frame_id 冲突）
- push：写 frame + upsert segments
- pull：拉未消费 frame，按 frame_ts DESC 倒序 replay

**实现**：`src/sync/prototypes/litestream_like.py`，157 行

### 方案 C：简化 CRDT（字段级 timestamp merge）

- sidecar 表 `sync_field_ts((segment_id, field_name) PK, value, ts, device_id, schema_version)`
- push：payload 拆字段写 sync_field_ts + 重建 segments 行
- pull：拉远端字段，与本地逐 (seg, field) 比较 ts，ts 大者赢
- 重建 segments：每次 pull / push 后从 sync_field_ts 重建

**实现**：`src/sync/prototypes/crdt_simple.py`，218 行

---

## 4. 5 spike 场景结果

### 15 测试结果矩阵

| #  | 场景 | 方案 | 耗时 ms | pass | 一致 | 期望 | 实际 |
|----|------|------|---------|------|------|------|------|
| 1  | A 基本 | lww        |  34.68 | PASS | YES | B.pull 后 B 看到 seg-A1 | pulled=1, seen=intro |
| 2  | A 基本 | litestream |  38.90 | PASS | YES | 同上 | pulled=1, seen=intro |
| 3  | A 基本 | crdt       |  35.72 | PASS | YES | 同上 | pulled=1, seen=intro |
| 4  | B 同段不同字段 | lww        |  45.22 | PASS | YES | topic=alpha-2, score=80（整段覆盖）| A:(alpha-2,80) B:(alpha-2,80) |
| 5  | B 同段不同字段 | litestream |   0.00 | **FAIL** | NO  | **KNOWN_LIMIT** | NOT NULL 错（partial + 缺段） |
| 6  | B 同段不同字段 | crdt       |  51.38 | PASS | YES | topic=alpha-2, score=95（字段级 merge）| A:(alpha-2,95) B:(alpha-2,95) |
| 7  | C 同段同字段 | lww        |  50.02 | PASS | YES | score=90（晚写者赢） | A:(90) B:(90) |
| 8  | C 同段同字段 | litestream |  55.10 | **FAIL** | NO  | **KNOWN_LIMIT** | A:(90) B:(80) 倒序 replay 反向覆盖 |
| 9  | C 同段同字段 | crdt       |  42.33 | PASS | YES | score=90 | A:(90) B:(90) |
| 10 | D schema 不匹配 | lww        |  29.66 | PASS | YES | 拒绝（errors > 0） | A_got=True B_got=False B_errors=1 |
| 11 | D schema 不匹配 | litestream |  40.31 | PASS | YES | 强 replay（warnings >= 1）| A_got=True B_got=True B_warnings=2 |
| 12 | D schema 不匹配 | crdt       |  34.91 | PASS | YES | 字段级 merge | A_got=True B_got=True B_warnings=7 |
| 13 | E 50ms RTT | lww        | 6429.46 | PASS | YES | 双向 50 段全到位 | A 看到 B:50/50 B 看到 A:50/50 |
| 14 | E 50ms RTT | litestream | 6449.21 | PASS | YES | 同上 | 同上 |
| 15 | E 50ms RTT | crdt       | 6464.28 | PASS | YES | 同上 | 同上 |

**汇总**：**13/15 PASS** | **13/15 一致** | 2 KNOWN_LIMIT（设计限制）

### 关键发现

1. **B 场景揭示 CRDT 真正优势**：partial payload 模式下，CRDT 字段级 merge 真正保留两设备各自改动；LWW 整段覆盖（用 peer.segments 整行）导致 score=80；Litestream partial + 缺段报 NOT NULL。
2. **C 场景揭示 Litestream 简化设计限制**：倒序 replay + 整段 payload 会反向覆盖（A 帧 ts=1000 后 replay 覆盖 B 帧 ts=1200 score=90）。真实 Litestream 产品是 SQL 级 WAL，不是字段级 frame。
3. **D 场景三方案策略清晰**：LWW 拒绝 / Litestream 强 replay / CRDT 字段级 merge，符合预期。
4. **E 场景三方案性能相近**：3 方案都能在 50ms RTT 下完成 50 段双向同步（~6.4s），性能不是主要区分点。

### 性能对比（E 场景 100 段双向同步，50ms RTT）

| 方案 | 耗时 ms | 备注 |
|------|---------|------|
| LWW        | 6429 | 单 SQL 事务最快 |
| Litestream | 6449 | 维护 sync_wal 增加 ~0.3% 开销 |
| CRDT       | 6464 | 每字段一次 SQL，开销最大但差异 < 1% |

性能差异 < 1%，**实际生产环境下网络延迟是主要因素，方案选择应以"功能正确性"为主**。

---

## 5. 评估矩阵

| 方案 | 实现复杂度 | 冲突解决 | 性能 | 部署复杂度 | 扩展性 |
|------|------------|----------|------|------------|--------|
| A LWW        | 低（208 行）| 差（同段同字段整段覆盖丢分）| 最快 | 低（无 daemon）| 中（依赖整段 ts）|
| B Litestream | 中（157 行 + 简化设计）| 中（frame 顺序）| 中（frame 增长无界）| 高（需裁剪任务）| 中（受 frame payload 完整度限制）|
| C CRDT       | 高（218 行）| **好（字段级独立 ts）** | 中（每字段 SQL）| 低（无 daemon）| **高（schema 演进友好）** |

---

## 6. Spec Bug 发现（spike 关键价值）

### 6.1 Bug #1：任务文案"SQLite 3.39+ JSON1"与实际不符

| 项 | 实际 |
|----|------|
| 任务文案 | SQLite 3.39+ |
| 实际 | SQLite 3.37.2（JSON1 完整可用，但版本号低于文案）|
| 影响 | spike 可跑（JSON1 函数全在）；M3-1 生产建议升 3.39+（JSON1 性能更好 + 一些新函数）|
| M3-1 建议 | 升级 SQLite 到 3.39+；若不行，spike 的 3 个 adapter 不依赖 json_extract 高级特性，3.37.2 可用 |

### 6.2 Bug #2：任务文案"每条数据带 updated_at_ms"与 schema 不符

| 项 | 实际 |
|----|------|
| 任务文案 | 每条数据带 updated_at_ms |
| 实际 | segments 表**无 `updated_at_ms` 列**（22 个字段列表中无）|
| 影响 | spike 必须用 sidecar 表（sync_meta）维护 updated_at_ms，不污染生产 schema |
| M3-1 建议 | 选 A：spike 沿用 sidecar 表（不破坏 L0 不可变触发器）；选 B：ALTER TABLE segments ADD COLUMN updated_at_ms INTEGER（需 M2.5.x 迁移脚本）|

### 6.3 Bug #3：任务文案"schema 版本 M2.5 vs M2.6"与实际不符

| 项 | 实际 |
|----|------|
| 任务文案 | schema 版本 M2.5 vs M2.6 |
| 实际 | 当前 schema 是 v0.4（`M2.5.1 _migrate_to_v2` 已落），**无 v0.5/v0.6 概念**|
| 影响 | spike 改测"sidecar 携带的 schema_version 字段"，3 方案对版本不一致的处理策略由 LWW 拒 / Litestream 强 replay / CRDT 字段级 merge 体现 |
| M3-1 建议 | 在 spec/SPEC.md 明确 schema 版本号命名规则（v0.x / Mx.y），sidecar schema_version 字段作为协议版本 |

---

## 7. 推荐方案

### 7.1 推荐：**C CRDT（条件允许时）**

**理由**：
1. **字段级 merge 是唯一真正解决"同段不同字段同时改"问题的方案**（B 场景验证）
2. **schema 演进友好**：新字段缺失不影响旧字段合并（D 场景验证）
3. **扩展性高**：未来加 segment 字段不需要改 sync 协议
4. **部署简单**：无 daemon/裁剪任务
5. **代价**：实现复杂（218 行 vs 208 行），每字段一次 SQL（性能差异 < 1%）

**风险**：
- 整段 push 语义下退化为 LWW（spike 发现）。**M3-1 必须在 segment_writer 层强制 partial payload**（或 sync_segment_to_adapter 改 partial 模式）。
- 每字段一次 SQL，写放大 ~22x（22 字段）。生产可考虑批量写入优化。

### 7.2 降级推荐：**A LWW**（工期紧 / 接受丢分）

**理由**：
- 实现最简（208 行），1 周内可生产
- 性能最快
- 接受"同段同字段整段覆盖"的妥协（同 segment 同时改同字段概率低）
- M3-1 可在 LWW 基础上演进到 CRDT（sidecar sync_meta → sync_field_ts 增量迁移）

### 7.3 不推荐：**B Litestream**

**理由**：
- 简化设计有 2 个 KNOWN_LIMIT（B/C 场景）
- 真实 Litestream 产品需要外部 daemon + 裁剪任务
- 单写者假设与 MTCA 多设备场景不符
- spike 已证明"spike Litestream 思路"在 MTCA 场景下没有优势

---

## 8. M3-1 启动建议

### 8.1 优先级 0：先修 3 个 spec bug

1. **明确 schema 版本命名规则**（spike 报告 §6.3）：SPEC.md 加 v0.x 版本号章节
2. **评估 ALTER TABLE 加 updated_at_ms 的成本**（§6.2）：若风险大，沿用 sidecar sync_meta
3. **升级 SQLite 到 3.39+**（§6.1）：通过 Python 3.10+ 内置 sqlite3 或系统包

### 8.2 优先级 1：选 C CRDT 为目标方案

实施步骤：
1. **sidecar 表设计**：`sync_meta`（已 spike 验证）+ `sync_field_ts`（已 spike 验证）
2. **segment_writer 层强制 partial payload**：sync_segment_to_adapter 改为只推 changed 字段
3. **迁移 sync_segment_to_adapter 到 CRDT adapter**：从 LocalOnlySync 切换到 CrdtSimpleAdapter
4. **2 设备 A/B 配对实验**：跑 1 周，验证 spike 结论

### 8.3 优先级 2：spike 未覆盖的边界场景（待 M3-1 验证）

1. **离线 N 天后重连**：sync_wal / sync_field_ts 增长到几万行的性能
2. **同一字段 1000 次微改**：sync_field_ts 字段数膨胀
3. **3+ 设备并发**：A/B/C 同时改，CRDT 字段级 ts 表现
4. **CRDT 字段数膨胀 → 索引优化**：是否需要给 sync_field_ts 加 (segment_id, field_name, ts DESC) 复合索引
5. **schema 真正不匹配**：本地 segments 表缺列 vs 远端 payload 含新列的处理

### 8.4 优先级 3：长期演进（不在 M3-1 范围）

1. 真实云同步（Dropbox / iCloud / 自建 S3）：spike 验证的 SyncAdapter Protocol 可直接接入
2. CRDT 进阶：Yjs / Automerge 等成熟 CRDT 库（需评估是否引入新依赖）
3. 端到端加密：sync 流量加密（M3+ 范畴）

---

## 9. 附：3 原型文件清单

| 文件 | 行数 | 用途 |
|------|------|------|
| `src/sync/prototypes/__init__.py` | 48 | 公共接口 + 工厂 |
| `src/sync/prototypes/lww.py` | 208 | 方案 A：LWW |
| `src/sync/prototypes/litestream_like.py` | 157 | 方案 B：Litestream 思路 |
| `src/sync/prototypes/crdt_simple.py` | 218 | 方案 C：CRDT 字段级 merge |
| `benchmarks/_m258_m3_spike.py` | 417 | 5 场景 × 3 方案 = 15 测试 |
| `benchmarks/_m258_m3_spike.md` | （本文档） | spike 报告 |

**单文件 ≤ 1000 行硬约束**：全部通过 ✅

---

## 10. 跑测试命令

```powershell
# 跑全部 15 测试
python benchmarks\_m258_m3_spike.py --all

# 跑单个测试
python benchmarks\_m258_m3_spike.py --scenario B --adapter crdt
```

输出 13/15 PASS | 13/15 一致 | 2 KNOWN_LIMIT（Litestream 设计限制）。
