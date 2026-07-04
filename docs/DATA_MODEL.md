# MTCA 数据模型详解（v0.4）

> 详细 schema 见 `DEVELOPER_PLAN.md` 第 2.2 节，这里补充设计动机、迁移与一致性。
> v0.4 新增：L0 双层（骨架/细节）+ 静默态字段 + 矛盾检测字段 + 关系图谱表 + 摘要重写字段。

---

## 1. 6 层结构与数据库表对应

| 层 | 物理位置 | 是否可变 | 数据库表 / 字段 |
|----|---------|---------|------------------|
| **L0-骨架** | 磁盘 | ❌ 不可变（用户/AI 都无权删）| `segments` 字段：topic_label / start_at / fog_anchor / 关键词 |
| **L0-细节** | 磁盘 | AI 不可变 / 用户可雾化 | `messages.content`（物理擦除）|
| **L0-B** 段落 | 磁盘 | AI 不可变 / 用户可雾化 | `segments` 段级元数据 |
| **L1** 轻压 | 磁盘 + 视图 | ✅ 可重写 | `views(tier='L1')` |
| **L2** 重压 | 磁盘 + 视图 | ✅ 可重写 | `views(tier='L2')` |
| **L3** 抽象 | 磁盘 + 视图 | ✅ 可重写 | `views(tier='L3')` |
| **关系图** | 磁盘 | ✅ 可增删 | `segment_relations` |
| **审计** | 磁盘 | ✅ 仅追加 | `score_events` |

**关键原则**：
- L0-骨架 / L0-细节 / L0-B 不允许 AI 修改/删除
- 雾化操作是**用户授权的特殊通道**，物理擦除 L0-细节
- `views` 允许 rewrite（插入新行 + 旧行 expires_at）
- `segment_relations` 允许增删

---

## 2. L0 不可变约束的实现

### 2.1 L0-细节 不可变（AI 不可写）

```sql
-- 禁止 AI 通道修改
CREATE TRIGGER no_update_messages_ai
BEFORE UPDATE ON messages
WHEN NOT EXISTS (SELECT 1 FROM fog_session WHERE session_id = messages.session_id)
BEGIN
  SELECT RAISE(ABORT, 'L0 不可变，禁止 UPDATE messages（fog 通道除外）');
END;

CREATE TRIGGER no_delete_messages
BEFORE DELETE ON messages
BEGIN
  SELECT RAISE(ABORT, 'L0 不可变，禁止 DELETE messages');
END;
```

### 2.2 L0-骨架 永远不删

```sql
-- segments 表的骨架字段禁止 UPDATE
CREATE TRIGGER no_update_skeleton
BEFORE UPDATE OF topic_label, start_at, fog_anchor ON segments
BEGIN
  SELECT RAISE(ABORT, 'L0-骨架 不可变');
END;
```

`fog_state` / `fog_at` / `silence_state` / `superseded_by` 字段允许更新（系统行为）。

### 2.3 雾化通道

```sql
-- 临时开启 fog 通道（一次雾化操作的事务内）
CREATE TABLE fog_session (
  session_id  TEXT,
  enabled_at  INTEGER,
  enabled_by  TEXT  -- 'user' 严格校验
);
```

应用层：
1. 校验 `enabled_by == 'user'`（AI 调用此 API 直接拒绝）
2. 事务内：UPDATE messages SET content=NULL + INSERT score_events(user_fog) + UPDATE segments.fog_state
3. 事务结束清空 fog_session

---

## 3. 静默态状态机

```
        ┌────────┐
promote │ silent │  demote
◄───────┤        ├──────────►
        └────┬───┘
             │ promote
             ▼
        ┌────────┐
promote │dormant │  demote
◄───────┤        ├──────────►
        └────┬───┘
             │ promote
             ▼
        ┌────────┐
        │ active │
        └────────┘
   （任何升级到此 → promoted_at = now，重新计时）
```

**5 条铁律**（静默态专属）：

1. **升级 = 重新计时**：`promoted_at = now`
2. **降级 = 时间衰减**：`promoted_at + retention_days < now AND ref_count == 0` → 降级
3. **双向都允许**：用户心血来潮 → 一阵子不聊 → 自动回 silent
4. **用户可锁定**：GUI 标"永久活跃" → 永不下沉
5. **用户可强制下沉**：GUI 手动"提前静默" → 跳 dormant 直接 silent

**AI 询问机制**：

- 仅 silent 段被引用时触发
- 7 天内同一段只问 1 次
- ref_count ≥ 阈值（默认 3）自动升 active

---

## 4. 雾化三态

```
   /雾化              召回 1 次              永久
clear ─────► fogged_once ─────────► archived
                │
                │ 用户 LLM 不可
                │ 调用 /雾化
                ▼
            （AI 无 fog 权限）
```

**物理操作**：

```sql
-- 1. 开启 fog 通道
INSERT INTO fog_session VALUES (?, ?, 'user');

-- 2. 物理擦除
UPDATE messages SET content = NULL, token_count = 0
WHERE session_id = ? AND seq BETWEEN ? AND ?;

-- 3. 标记状态
UPDATE segments
SET fog_state = 'fogged_once', fog_at = ?, fog_anchor = ?
WHERE segment_id = ?;

-- 4. 记录审计
INSERT INTO score_events (segment_id, event_type, reason, ...)
VALUES (?, 'user_fog', '不可逆', ...);

-- 5. 关闭 fog 通道
DELETE FROM fog_session WHERE session_id = ?;
```

**不可逆保证**：
- 无 `/取消雾化` / `/恢复` 命令
- L1/L2/L3 摘要可重生，但基于 L0-骨架
- L0-细节 物理擦除后无法恢复（除非用户自己备份）

---

## 5. 矛盾检测与 supersede

**触发**：新段落创建后，异步检查

```sql
-- 找候选：同 topic_label + 最近 90 天 + active
SELECT segment_id, topic_label
FROM segments
WHERE topic_label = ?
  AND silence_state = 'active'
  AND superseded_by IS NULL
  AND start_at > ?  -- 90 天内
LIMIT 10;
```

**LLM 判断**（对每对候选）：

```
Prompt: 段落 A 说 X，段落 B 说 Y。是否矛盾？级别？
Output: { contradicts: bool, level: 'minor'/'major'/'full', reason: str }
```

**写入关系**：

```sql
INSERT INTO segment_relations (relation_id, seg_a_id, seg_b_id, relation_type, auto_created, created_at)
VALUES (?, ?, ?, 'supersedes', 1, ?);  -- B 取代 A，B 是新段

UPDATE segments SET superseded_by = ?, supersedes_count = supersedes_count + 1
WHERE segment_id = ?;  -- 标记 A
```

**AI 召回行为**：

| 状态 | 召回反馈 |
|---|---|
| A（被 supersede）| "1 年前的 A 方案已被 B 取代（2026-07-04）" + B 骨架 |
| B（取代者）| 正常全文 + "本方案取代了 A" 标注 |

**用户可覆盖**：GUI 右键 A → "A 不是 supersede，是分支" → 删关系，标 `related_to`

---

## 6. 关系图谱（4 种关系）

| 关系类型 | 含义 | 触发 |
|---|---|---|
| `references` | A 显式提到 B | LLM 检测对话内容 |
| `supersedes` | B 取代 A | 矛盾检测自动生成 |
| `related_to` | 同 topic_label | 分类后自动生成 |
| `derived_from` | B 是 A 的后续 | 时间相邻 + 关键词重叠 |

**Schema**：`segment_relations` 表（见 `DEVELOPER_PLAN.md` 2.2.3.5）

**核心查询**：

```sql
-- 话题簇：找所有 related_to 当前段
SELECT b.* FROM segments b
JOIN segment_relations r ON r.seg_b_id = b.segment_id
WHERE r.seg_a_id = ? AND r.relation_type = 'related_to'
  AND b.silence_state != 'silent';

-- 话题演化：跟随 supersedes 链
WITH RECURSIVE evolution AS (
  SELECT segment_id, superseded_by FROM segments WHERE segment_id = ?
  UNION ALL
  SELECT s.segment_id, s.superseded_by
  FROM segments s JOIN evolution e ON s.segment_id = e.superseded_by
)
SELECT * FROM evolution;
```

---

## 7. 摘要自动重写

**触发场景**：

| 触发 | 重写范围 | stale_reason |
|---|---|---|
| 段落 `/雾化` | L1/L2/L3 全部基于 L0-骨架 重生 | `fog` |
| `supersede` 关系生成 | 被取代段 L1/L2 标记 `stale` | `supersede` |
| 段落 promote 到 active | dormant 期的 L1 升级为详细版 | `promote` |
| GUI 手动"刷新摘要" | 全部视图重生 | `manual` |

**Schema 扩展**（`views` 表）：

```sql
ALTER TABLE views ADD COLUMN is_stale     INTEGER DEFAULT 0;
ALTER TABLE views ADD COLUMN stale_reason TEXT;
ALTER TABLE views ADD COLUMN regen_count  INTEGER DEFAULT 0;
```

**重写流程**（异步队列）：

```
1. 检测 is_stale=1 的 views
2. 加载 L0-骨架 + 旧 view content
3. 调 LLM 重新生成（用 stale_reason 调整 prompt）
4. 写新 view（regen_count++），旧 view expires_at = now
5. 失败时保留旧 view，重试 3 次后放弃
```

**降级**：本地 LLM 不可用时，标 `stale` 但不重写，下次启动时重试。

---

## 8. 数据迁移

- `migrate_v1_to_v2.py` 之类，**仅在 schema 变更时**用
- 用 SQLite `VACUUM INTO` 备份后执行
- MTCA 不携带远端迁移工具，全部本地
- M1 → M2 之间的 schema 变更：使用 `PRAGMA user_version` 跟踪
