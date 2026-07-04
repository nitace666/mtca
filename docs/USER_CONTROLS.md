# MTCA 用户控制接口（v0.4）

> 老板的 8 条铁律之一：**用户控制接口直达 L0**，跳过任何压缩。
> v0.4 新增 `/雾化` 命令 + GUI 操作 + 静默态 AI 询问 + 矛盾检测覆盖。

---

## 1. 命令规范

| 命令 | 行为 | 物理含义 | 可逆？ |
|------|------|---------|--------|
| `/重要` | 当前段落 `score ← 10000` | 永远 L1，永远优先召回 | ✅ 可降级 |
| `/循环 X` | 当前段落打 `cycle_tag=X` | 跳过时间衰减 | ✅ 可取消 |
| `/归档` | 当前段落 `current_tier ← 'L3_hidden'` | L0 全文保留，不主动召回 | ✅ 可恢复 |
| `/雾化` | 物理擦除 L0-细节，保留 L0-骨架 | AI 召回时返回「已删除 + 关键词」| ❌ **不可逆** |

每个命令在对话中打到任意位置即可识别（自然语言前缀）。

---

## 2. 实现要点

### 2.1 /重要

```python
def mark_important(segment_id):
    db.execute(
        "UPDATE segments SET current_score=10000, current_tier='L1' WHERE segment_id=?",
        (segment_id,)
    )
    log_event(segment_id, 'user_important', 0)
```

`score=10000` 的物理意义：**任何衰减都减不到 0**，所以段落在 L1 永远保留。

### 2.2 /循环

```python
def mark_cycle(segment_id, cycle_tag):
    """cycle_tag ∈ {周一 / 周二 / ... / 月初 / 月末 / 每天}"""
    db.execute(
        "UPDATE segments SET cycle_tag=? WHERE segment_id=?",
        (cycle_tag, segment_id)
    )
    log_event(segment_id, 'user_cycle', 0)
```

**`/循环` 不阻止引用衰减**，只阻止时间衰减 —— 周期性话题不随时间衰减，但引用爆增仍能 +10。

### 2.3 /归档

```python
def archive(segment_id):
    """L0 全文保留，只是不主动召回"""
    db.execute(
        "UPDATE segments SET current_tier='L3_hidden', current_score=0 WHERE segment_id=?",
        (segment_id,)
    )
    log_event(segment_id, 'user_archive', 0)
```

L0 全文仍在 `messages` 表，要找回直接 SQL 查：
```sql
SELECT * FROM messages WHERE session_id = ? ORDER BY seq;
```

### 2.4 /雾化（v0.4 新增，**不可逆**）

```python
def fog_segment(segment_id, anchor: str):
    """物理擦除 L0-细节，保留 L0-骨架 + 锚点句。不可逆。"""
    # 1. 开启 fog 通道
    db.execute(
        "INSERT INTO fog_session VALUES (?, ?, 'user')",
        (session_id, now_ms, 'user')
    )

    # 2. 物理擦除 L0-细节
    db.execute(
        "UPDATE messages SET content=NULL, token_count=0 "
        "WHERE session_id=? AND seq BETWEEN ? AND ?",
        (session_id, start_seq, end_seq)
    )

    # 3. 标记状态
    db.execute(
        "UPDATE segments SET fog_state='fogged_once', fog_at=?, fog_anchor=? "
        "WHERE segment_id=?",
        (now_ms, anchor[:20], segment_id)
    )

    # 4. 触发 L1/L2/L3 重写（异步）
    schedule_regen(segment_id, reason='fog')

    # 5. 审计
    log_event(segment_id, 'user_fog', 0)

    # 6. 关闭 fog 通道
    db.execute("DELETE FROM fog_session WHERE session_id=?", (session_id,))
```

**AI 召回行为**（fogged_once 状态）：

```
返回：「片段 [topic_label] 已删除（[fog_anchor]）。如果你想找回，请手动查 mtca.db」
```

第二次召回 → 自动转 `archived` → 不再主动返回。

**AI 无 fog 权限**：

```python
def fog_segment(segment_id, anchor, called_by='user'):
    if called_by != 'user':
        raise PermissionError("AI 无 /雾化 权限")
    # ... 实际执行
```

API 端点（`/api/fog`）也校验 token 权限范围，AI token 没有 fog 权限。

---

## 3. GUI 操作（v0.4 优先）

M1 直接出 GUI MVP，操作比 CLI 更直观。

### 3.1 主界面：时间线 + 话题树

```
┌─ MTCA ──────────────────────────────────────────────┐
│ 📅 2026-07-04                                        │
│ ├─ 10:00-11:00 ⭐MTCA开发                            │
│ │   └─ 哲学宪法 ⭐/重要 ⭐/循环 月初                  │
│ │       [雾化] [归档] [重要] [循环]                    │
│ ├─ 09:00-10:00 工作                                  │
│ │   └─ 项目会议                                      │
│ │       [雾化] [归档] [重要] [循环]                    │
│ └─ 08:00-09:00 例行                                  │
│                                                     │
│ [切换视图: 时间线 | 树状 | 知识图谱]                  │
│ [搜索] [新建话题] [刷新] [设置]                       │
└─────────────────────────────────────────────────────┘
```

### 3.2 段落详情（点击进入）

```
┌─ 哲学宪法（2026-07-04 10:00-11:00）─────────────────┐
│ ⭐/重要  ⭐/循环 月初                                 │
│                                                     │
│ 状态: active (已激活 23 天)                          │
│ 重要性: 10000 (锁)                                   │
│ 引用次数: 12                                         │
│ 关联段: 5 个 related_to / 2 个 references            │
│                                                     │
│ L0-骨架: 8 条铁律 / L0 不可变 / 数据主权             │
│ L1 摘要: MTCA v0.4 设计哲学的核心 8 条铁律...        │
│ L2 三元组: (问题)AI 长期记忆如何不丢? (方法)...       │
│ L3 标签: 重要性=高 / 情绪=正面                       │
│                                                     │
│ [展开 L0 全文] [刷新摘要] [雾化] [归档] [分享]        │
└─────────────────────────────────────────────────────┘
```

### 3.3 雾化确认对话框（**强警告**）

```
┌─ ⚠️ 雾化操作不可逆 ─────────────────────────┐
│                                             │
│ 你将永久擦除段落的详细对话内容。             │
│                                             │
│ 保留: 话题标题 / 时间 / 关键词 / 锚点句     │
│ 擦除: 完整对话 / 工具调用 / 长文本           │
│                                             │
│ 后果:                                       │
│ • AI 召回时只能看到「已删除 + 关键词」      │
│ • 之后不再主动提起                          │
│ • 不可恢复                                  │
│                                             │
│ 锚点句（≤20 字）: ________________________  │
│                                             │
│        [取消]  [我确定要雾化]                │
└─────────────────────────────────────────────┘
```

### 3.4 知识图谱视图（T25）

```
┌─ 知识图谱 ──────────────────────────────────────┐
│                                                 │
│         [漫剧A] ──supersedes──► [漫剧B]          │
│            │                       │             │
│         related_to              references       │
│            ▼                       ▼             │
│         [脚本]                 [角色]            │
│                                                 │
│ 时间滑块: [2025-07 ═══●═══════ 2026-07]          │
│ 话题过滤: [全部▼]  静默态: [active+dormant▼]    │
│                                                 │
│ [节点颜色] = 静默态  [节点大小] = 重要性         │
│ [边颜色] = 关系类型                              │
└─────────────────────────────────────────────────┘
```

### 3.5 静默态 GUI 控制

每个段落有 4 个按钮：

| 按钮 | 行为 |
|---|---|
| **标记为活跃** | `silence_state='active'`, `promoted_at=now`（重新计时）|
| **永久活跃** | `silence_state='active'`, 永不下沉（特殊标记）|
| **提前静默** | `silence_state='silent'`, 跳过 dormant |
| **调整保留期** | 弹窗输入天数，`user_retention_days=输入值` |

---

## 4. 静默态 AI 询问

AI 召回时遇到 silent 段被新话题引用，**附带提示**（不强迫用户回答）：

```
🤖 AI 提示：
"你 1 年前（2025-07）做过 [漫剧方案 A]，已被静默。
 当前话题 [漫剧 B] 似乎相关，是否启用 A 作为参考？
 
 [启用] [不启用] [7 天内不再问]"
```

- 启用 → `ref_count++` → 升 dormant；ref_count ≥ 阈值自动升 active
- 不启用 → 保持 silent，`last_ask_at = now`（7 天防骚扰）
- 7 天内不再问 → 标记 long_silent，本话题长期不再问

---

## 5. 矛盾检测覆盖

GUI 检测到 supersede 关系后，右键被取代段：

```
┌─ 「漫剧方案 A」被 B 取代 ─────────┐
│                                   │
│ 2026-07-04 自动检测:              │
│ B 方案与 A 在目标 / 方法上有矛盾  │
│                                   │
│ 你可以:                           │
│ • 接受 supersede（A 标记为已废弃）│
│ • 改为 related_to（平行方案）      │
│ • 撤销（无关系）                  │
│                                   │
│  [接受]  [改为分支]  [撤销]        │
└───────────────────────────────────┘
```

---

## 6. 可视化（树状 + 时间线）

### 6.1 时间线视图

```
2026-07-04 ████████████████████  7 段 / 4 个 /循环 / 1 个 /重要 / 0 个 /雾化
├─ 10:00-11:00 ⭐MTCA开发⭐
│   └─ 哲学宪法 ⭐/重要
├─ 09:00-10:00 工作
└─ 08:00-09:00 例行

2026-07-03 ████████████████████
└─ 例行工作

2025-07-04 ░░░░░░░░░░░░░░░░░░░░  1 段 (silent)
└─ 漫剧方案 A [启用] [查看骨架]
```

颜色规则：
- 亮色：active
- 灰色：dormant
- 暗色 + 问号：silent（可点击启用）
- 红色边框：fogged_once
- 删除线：superseded

### 6.2 树状视图

```
MTCA 开发（项目）
├─ 哲学宪法（2026-07-04）⭐/重要 ⭐/循环
├─ 8 条铁律（2026-07-04）⭐/重要
├─ 时间分段（2026-07-04）
├─ 动态打分（2026-07-04）
└─ L1–L3 视图（2026-07-04）

漫剧（个人项目）
├─ 漫剧方案 B（2026-07-04）⭐/活跃
├─ 漫剧方案 A（2025-07-04）[superseded by B] 暗色
└─ 漫剧脚本（2025-08-12）[related to A] 灰色
```

### 6.3 实现

GUI 用 PySide6 / Tauri（待定），CLI 用 `rich` 库（开发期辅助）。

```python
# src/cli/timeline.py（开发期辅助）
def render_timeline(period='day'):
    sessions = get_recent_sessions(period=period)
    print(format_timeline(sessions))  # rich 树状

def render_tree(project=None):
    segments = get_segments_by_project(project)
    print(format_tree(segments))
```

---

## 7. 用户可维护性

老板对数据库的直接访问：

```bash
sqlite3 I:\PROJECTS\MTCA\mtca.db

sqlite> SELECT started_at, topic_label, current_tier, current_score, fog_state, silence_state
        FROM segments ORDER BY started_at DESC LIMIT 20;
```

无需 MTCA GUI/CLI 也能查任何内容。所有雾化 / 静默 / 矛盾检测信息在 SQL 视图中可见。

---

## 8. 隐私

- 默认本地 SQLite 不联网
- 系统自带磁盘加密（BitLocker / FileVault）保护磁盘文件
- 不向任何云端发送数据（除非用户主动启用云 LLM）
- 启动 / 停止 MTCA 服务老板自己说了算
- 雾化操作物理擦除后，连本地数据库都不再有原文
