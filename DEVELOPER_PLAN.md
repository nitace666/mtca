# MTCA 完整开发文档 v0.3

> **创建时间**：2026-07-04 11:01 GMT+8
> **作者**：老板（产品 + 架构决策） + 波波（PM 流程 + 文档落地）
> **状态**：v0.3 终版（设计哲学锁定，可执行）
> **位置**：`I:\PROJECTS\MTCA\`

---

## 0. 这是什么（精简版，不重复 README）

MTCA = **多档上下文架构（Multi-Tier Context Architecture）**——AI 的外挂长期记忆中间件。

**与现有 Agent 平台的关系**：
- 不替代 Agent
- 不侵入 Agent 内部
- 通过 **MCP Server** 或 **REST API** 在外部提供服务
- Agent 跑它的 → MTCA 在外存全文 → Agent 需要历史时调 MTCA → 取回的就是 L0 原文

---

## 1. 设计哲学（8 条铁律）

完整版见 `README.md` 设计哲学一节。这里给每条配一句因果：

| # | 铁律 | 因果 |
|---|------|------|
| 1 | 数据主权在用户 | 本地 SQLite + 用户可维护 + 系统自带加密 |
| 2 | L0 双层 + AI 不可变 | 骨架（话题/时间/关键词/锚点）+ 细节（完整对话）；AI 无权改/删任何层；用户可雾化细节 |
| 3 | L1–L3 是视图 | 视图是给 AI 看的"快速回忆胶片"，可损失 |
| 4 | 雾化 = 用户主动丢弃细节 | 物理擦除 L0-细节，保留骨架；AI 召回时第一次返回「已删除」+ 关键词，之后不主动提起 |
| 5 | 召回错误自动回 L0-骨架 | 摘要丢失的，骨架 + 关键词兜底 |
| 6 | 用户控制接口直达 L0 | `/重要 /循环 /归档 /雾化` = 跳过任何压缩、不可逆地改 L0 状态 |
| 7 | GUI 优先 | 时间线 / 树状 / 雾化 / 话题编辑全在 GUI；CLI 是开发期辅助 |
| 8 | 存储成本由用户承担 | 纯文本 1–2 GB/年，可忽略 |

---

## 2. 数据模型

### 2.1 五层结构（明确边界）

```
┌─────────────────────────────────────────────────────────────┐
│ L0-骨架  话题级    SQLite   用户/AI 都不可变                  │  ← 话题标题/时间/关键词/锚点句
├─────────────────────────────────────────────────────────────┤
│ L0-细节  会话级    SQLite   AI 不可变 / 用户可雾化(物理擦除)  │  ← 完整对话 + 工具调用 + 时间戳
├─────────────────────────────────────────────────────────────┤
│ L0-B    段落级    SQLite   AI 不可变 / 用户可雾化             │  ← 30–60 分钟 gap 切分的子段（细节）
├─────────────────────────────────────────────────────────────┤
│ L1     轻压视图  SQLite   视图可变                           │  ← 段落 + 关联摘要（去掉工具调用）
├─────────────────────────────────────────────────────────────┤
│ L2     重压视图  SQLite   视图可变                           │  ← 三元组（问题/方法/结论）
├─────────────────────────────────────────────────────────────┤
│ L3     抽象视图  SQLite   视图可变                           │  ← (结论 + 情绪 + 重要性) 三元组
└─────────────────────────────────────────────────────────────┘
                  │
                  ▼ 召回双通道
        L0-骨架/L0-细节/L0-B (主 10ms) + L1–L3 (辅 100ms)
                  │
                  ▼ 雾化过滤 + 静默过滤
        fogged_once: 1 次返回「已删除 + 关键词」 → archived
        archived:    不主动召回（除非用户直接搜关键词）
        silent:      不主动召回（仅用户显式搜关键词才出）
        dormant:     召回分数 ×0.5（按时间衰减）
        升级 = 重新计时；降级 = 时间触发（双向流动）
```

### 2.2 数据库 Schema（SQLite）

#### 表 1: `sessions` 会话级（L0-A 不可变）

```sql
CREATE TABLE sessions (
  session_id     TEXT PRIMARY KEY,           -- UUID
  started_at     INTEGER NOT NULL,           -- 时间戳（ms）
  ended_at       INTEGER,
  topic_label    TEXT,                        -- 由本地 5B 异步生成 / 用户手动打
  message_count  INTEGER DEFAULT 0,
  token_estimate INTEGER DEFAULT 0,
  agent_source   TEXT,                       -- 'openclaw' | 'hermes' | 'coze' | ...
  is_archived    INTEGER DEFAULT 0,           -- /归档
  is_important   INTEGER DEFAULT 0,           -- /重要
  cycle_tag      TEXT,                        -- /循环 周一 / /循环 月初
  current_score  REAL DEFAULT 100,           -- 动态打分（每轮实时更新）
  created_at     INTEGER DEFAULT (strftime('%s','now') * 1000)
);

CREATE INDEX idx_sessions_started ON sessions(started_at);
CREATE INDEX idx_sessions_score   ON sessions(current_score);
```

#### 表 2: `messages` 消息（L0-A 第二张表）

```sql
CREATE TABLE messages (
  message_id     TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL,
  seq            INTEGER NOT NULL,           -- 会话内序号
  role           TEXT NOT NULL,              -- 'user' | 'assistant' | 'tool' | 'system'
  content        TEXT NOT NULL,              -- 完整原文
  tool_calls     TEXT,                       -- JSON（assistant 调的工具）
  tool_results   TEXT,                       -- JSON（工具返回值）
  token_count    INTEGER,
  created_at     INTEGER NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX idx_messages_session ON messages(session_id, seq);

-- FTS5 全文索引
CREATE VIRTUAL TABLE messages_fts USING fts5(
  content,
  content_rowid=ROWID,
  tokenize='unicode61'
);
```

#### 表 3: `segments` 段落级（L0-B 时间聚类产物）

```sql
CREATE TABLE segments (
  segment_id     TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL,
  start_msg_seq  INTEGER NOT NULL,
  end_msg_seq    INTEGER NOT NULL,
  start_at       INTEGER NOT NULL,
  end_at         INTEGER NOT NULL,
  gap_to_next    INTEGER,                    -- 距下一段的分钟数
  weak_merged    INTEGER DEFAULT 0,          -- 是否被"弱合并"规则合并跨 gap
  topic_label    TEXT,                       -- 段落级话题（由 LLM 异步提取 / 用户手动打）
  current_tier   TEXT DEFAULT 'L0',          -- L0/L1/L2/L3 当前视图层
  current_score  REAL DEFAULT 100,
  -- 雾化字段（v0.4 新增）
  fog_state      TEXT DEFAULT 'clear',       -- 'clear' | 'fogged_once' | 'archived'
  fog_at         INTEGER,                    -- 雾化时间戳
  fog_anchor     TEXT,                       -- 雾化时保留的锚点句（≤20 字）
  -- 静默态字段（v0.4 新增）
  silence_state  TEXT DEFAULT 'active',      -- 'active' | 'dormant' | 'silent'
  promoted_at    INTEGER,                    -- 上次升级时间（重新计时锚点）
  ref_count      INTEGER DEFAULT 0,          -- 用户"是"次数
  last_ask_at    INTEGER,                    -- 防骚扰（7 天只问 1 次）
  user_retention_days INTEGER,                -- 用户覆盖（NULL=用策略默认）
  -- 矛盾检测字段（v0.4 新增）
  superseded_by        TEXT,                 -- 取代本段的新段 segment_id
  supersedes_count     INTEGER DEFAULT 0,    -- 本段取代了几条旧段
  contradiction_level  TEXT,                 -- 'minor' | 'major' | 'full'
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX idx_segments_session  ON segments(session_id);
CREATE INDEX idx_segments_time     ON segments(start_at);
CREATE INDEX idx_segments_fog      ON segments(fog_state);
CREATE INDEX idx_segments_silence  ON segments(silence_state);
CREATE INDEX idx_segments_super    ON segments(superseded_by);
```

#### 表 3.5: `segment_relations` 多向引用图谱（v0.4 新增）

```sql
CREATE TABLE segment_relations (
  relation_id    TEXT PRIMARY KEY,
  seg_a_id       TEXT NOT NULL,
  seg_b_id       TEXT NOT NULL,
  relation_type  TEXT NOT NULL,             -- 'references' | 'supersedes' | 'related_to' | 'derived_from'
  weight         REAL DEFAULT 1.0,          -- 用于排序
  auto_created   INTEGER DEFAULT 0,         -- 0=用户标 / 1=LLM 生成
  created_at     INTEGER NOT NULL,
  expires_at     INTEGER,                   -- 临时关系（如 supersedes 被撤销）
  FOREIGN KEY (seg_a_id) REFERENCES segments(segment_id),
  FOREIGN KEY (seg_b_id) REFERENCES segments(segment_id)
);

CREATE INDEX idx_relations_a    ON segment_relations(seg_a_id);
CREATE INDEX idx_relations_b    ON segment_relations(seg_b_id);
CREATE INDEX idx_relations_type ON segment_relations(relation_type);
```

#### 表 4: `views` 多档视图（L1–L3 同一张表，靠 tier 区分）

```sql
CREATE TABLE views (
  view_id        TEXT PRIMARY KEY,
  segment_id     TEXT NOT NULL,
  tier           TEXT NOT NULL,              -- 'L1' | 'L2' | 'L3'
  content        TEXT NOT NULL,              -- 视图内容
  emotion_tag    TEXT,                       -- L3 才有
  embedding      BLOB,                       -- bge-small-zh 嵌入（可选，用于 LLM 概览）
  created_at     INTEGER NOT NULL,
  expires_at     INTEGER,                    -- 视图生命期（被新视图替换则无效）
  -- 自动重写字段（v0.4 新增）
  is_stale       INTEGER DEFAULT 0,          -- 是否需重写
  stale_reason   TEXT,                       -- 'fog' | 'supersede' | 'manual' | NULL
  regen_count    INTEGER DEFAULT 0,          -- 重写次数（审计）
  FOREIGN KEY (segment_id) REFERENCES segments(segment_id)
);

CREATE INDEX idx_views_segment ON views(segment_id);
CREATE INDEX idx_views_tier    ON views(tier);
CREATE INDEX idx_views_stale   ON views(is_stale);
```

#### 表 5: `score_events` 打分历史（审计 / 调试用）

```sql
CREATE TABLE score_events (
  event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id     TEXT NOT NULL,
  event_type     TEXT NOT NULL,              -- 'decay' | 'boost' | 'threshold' | 'user_important' | 'user_cycle' | 'user_archive'
  delta          REAL,
  old_score      REAL,
  new_score      REAL,
  reason         TEXT,
  created_at     INTEGER NOT NULL
);

CREATE INDEX idx_score_events_segment ON score_events(segment_id, created_at);
```

---

## 3. 核心模块（按 M1–M5 划分）

### 3.1 M1 v0.3 长期记忆 MVP（50h，1 周多一点）

| 模块 | 任务 | 工时 | 产出路径 |
|------|------|------|---------|
| **M1.1 存储基础** | SQLite 连接池 + FTS5 + 表结构创建 | 6h | `src/store/sqlite.py` |
| **M1.2 会话入库** | 适配器从 Agent 接收消息 + 写入 L0-A | 6h | `src/l0/session_writer.py` |
| **M1.3 时间分段** | 3 规则 + 弱合并 SQL | 4h | `src/l0/time_segmenter.py` |
| **M1.4 段落生成** | 段落切分 + 写入 L0-B | 4h | `src/l0/segment_writer.py` |
| **M1.5 动态打分** | 每轮实时打分（衰减 + boost + 阈值）| 8h | `src/compress/scoring.py` |
| **M1.6 用户控制接口** | `/重要 /循环 /归档` 命令解析 | 3h | `src/cli/user_controls.py` |
| **M1.7 召回链路** | 双通道召回（SQLite 主 + 向量辅）+ 段落展开 | 6h | `src/recall/recall_engine.py` |
| **M1.8 CLI + 时间线** | 命令行时间线可视化 | 4h | `src/cli/timeline.py` |
| **M1.9 测试** | 50 段老板真实历史 + 20 编造 | 5h | `tests/` |
| **M1.10 文档** | API 文档 + 接入指南 | 4h | `docs/` |
| **M1 总工时** | | **50h** | |

### 3.2 M2 v0.4 MCP Server + Web UI（70h）

| 模块 | 任务 | 工时 |
|------|------|------|
| MCP Server | 协议适配 + 资源定义 | 20h |
| 召回分段接口 | recall(query, time_window, topics) | 12h |
| REST API | 跨语言支持 | 15h |
| Web 时间线 + 树状 | 前端实现 | 20h |
| M2 文档更新 | `docs/ADAPTERS.md` | 3h |

### 3.3 M3 v0.5 L1–L3 压缩视图 + 本地 5B（80h）

| 模块 | 任务 | 工时 |
|------|------|------|
| L1 轻压 | 段落 + 关联摘要 | 15h |
| L2 重压 | 三元组压缩 | 12h |
| L3 抽象 | 结论 + 情绪标签 | 15h |
| 本地 5B 接入 | Qwen / Ollama adapter | 12h |
| 异步压缩调度 | 会话结束 / 阈值触发 | 18h |
| 多档自适应 | 显存检测 + 自动降级 | 8h |

> M3 模型选型：**默认 Qwen3.6 35B A3B**（老板选）/ 显存不够降级到 Qwen2.5-7B-Instruct
> 注：以上是默认推荐型号，模型需根据老板实际硬件确定。

### 3.4 M4 v0.6 主流 Agent 适配器（60h）

| 平台 | 适配方式 | 工时 |
|------|---------|------|
| OpenClaw | 内部事件回调订阅 | 15h |
| Hermes | MCP Client 集成 | 12h |
| 扣子（Coze）| OpenAPI 直连 | 15h |
| Claude Code | MCP Server | 10h |
| Cursor / Devin / Cline 等 | 通用 REST 适配 | 8h |

### 3.5 M5 v1.0（80h）

- 100 段真实用户测试
- GitHub 开源
- 完整文档 + 教程
- 性能验收（G1–G4）

---

## 4. 5 大核心机制（不是模块，是横切关注点）

### 4.1 时间分段（3 规则）

```python
# src/l0/time_segmenter.py

MAX_GAP_MINUTES = 60      # 超 60 分钟 = 强制切
MIN_SEGMENT_MSGS = 3       # 短于 3 轮不叫段落
NIGHT_MERGE_WINDOW = (23, 7) # 23:00–07:00 跨天会话合并

def segment_session(session_id):
    msgs = get_messages(session_id)
    segments = []
    current = []
    last_ts = None
    for m in msgs:
        if last_ts is None:
            current.append(m)
        else:
            gap = (m.created_at - last_ts) / 60000
            if gap > MAX_GAP_MINUTES:
                if len(current) >= MIN_SEGMENT_MSGS:
                    segments.append(emit_segment(current))
                current = [m]
            elif _weak_merge_check(current, m, gap):
                current.append(m)   # 跨 gap 但关键词重叠 → 同一段
            else:
                if len(current) >= MIN_SEGMENT_MSGS:
                    segments.append(emit_segment(current))
                current = [m]
        last_ts = m.created_at
    if len(current) >= MIN_SEGMENT_MSGS:
        segments.append(emit_segment(current))
    return segments
```

### 4.2 动态打分（每轮实时）

```python
# src/compress/scoring.py

INITIAL_SCORE   = 100.0
PER_TICK_DECAY  = 1.0      # 每轮 -1
REFERENCE_BOOST = 10.0     # 被引用 +10
L1_THRESHOLD    = 70.0     # 降到 L2
L2_THRESHOLD    = 50.0     # 降到 L3
L3_THRESHOLD    = 30.0     # 降到隐藏
IMPORTANT_SCORE = 10000.0  # /重要 锁定

def tick(session_id):
    """每轮对话后异步调用（不阻塞主线程）"""
    segments = get_active_segments(session_id)
    for seg in segments:
        if seg.cycle_tag:    # /循环 跳过衰减
            continue
        # 1. 衰减
        new_score = max(0, seg.current_score - PER_TICK_DECAY)
        # 2. 引用检测
        if has_been_referenced(seg):
            new_score = min(IMPORTANT_SCORE, new_score + REFERENCE_BOOST)
        # 3. 阈值触发降级
        new_tier = seg.current_tier
        if new_score < L3_THRESHOLD:
            new_tier = 'L3_hidden'
        elif new_score < L2_THRESHOLD:
            new_tier = 'L3'
        elif new_score < L1_THRESHOLD:
            new_tier = 'L2'
        # 4. 落库
        update_segment_score(seg.segment_id, new_score, new_tier)
        log_score_event(seg.segment_id, 'decay', -PER_TICK_DECAY, ...)
```

### 4.3 引用检测（嵌入相似度，本地 5B）

```python
# src/recall/reference_detector.py

def has_been_referenced(seg, recent_msgs=None):
    """检测段落是否在过去 N 轮被引用"""
    if recent_msgs is None:
        recent_msgs = get_recent_messages(window=10)
    seg_embed = embed(seg.summary or seg.first_msg)
    for msg in recent_msgs:
        msg_embed = embed(msg.content)
        sim = cosine_similarity(seg_embed, msg_embed)
        if sim > 0.7:   # 阈值
            return True
    return False
```

### 4.4 双通道召回（主 SQLite / 辅视图）

```python
# src/recall/recall_engine.py

def recall(query, time_window=None, topics=None, top_k=5):
    """召回主入口：永远返回 L0 原文"""
    candidates = []

    # 主通道：SQLite L0-A + L0-B
    cands_a = sqlite_search_sessions(query, time_window, topics, top_k * 2)
    cands_b = sqlite_search_segments(query, time_window, topics, top_k * 2)
    candidates.extend(cands_a + cands_b)

    # 辅通道：L1–L3 视图（嵌入相似度，供 LLM 概览用）
    cands_view = vector_search_views(query, top_k=top_k)
    # 找到 view 对应的 segment_id
    cands_seg = [get_segment(c.segment_id) for c in cands_view]
    candidates.extend(cands_seg)

    # 去重 + 段落展开（前后各 1 段）
    expanded = expand_neighbors(set(candidates), window=1)

    # 评分合并
    ranked = rerank(query, expanded)

    # 取 top_k 段，但 返回时加载段内全部 L0-A 原文
    results = []
    for seg in ranked[:top_k]:
        msgs = get_messages_in_segment(seg)
        results.append({
            'segment_id': seg.segment_id,
            'session_id': seg.session_id,
            'tier': seg.current_tier,
            'score': seg.current_score,
            'messages': msgs,   # ← L0-A 原文，永不丢
        })
    return results
```

### 4.5 L0 兜底（关键约束）

```python
# src/recall/l0_fallback.py

def recall_with_fallback(query, **kwargs):
    """任何召回失败，自动回 L0-A 会话全文"""
    results = recall(query, **kwargs)
    if not results or len(results) == 0:
        # 退路：直接列用户最近的所有 L0-A 会话，让 Agent 选
        return get_recent_sessions(limit=10)
    return results
```

---

## 5. 用户控制接口

### 5.1 命令规范

| 命令 | 行为 | 实现 |
|------|------|------|
| `/重要` | 当前段落 `score ← 10000`, `cycle_tag ← NULL` 强制保持 | `user_controls.py::mark_important()` |
| `/循环 周一/月初/每天` | 段落打 `cycle_tag`，跳过衰减 | `user_controls.py::mark_cycle()` |
| `/归档` | 当前段落 `current_tier ← 'L3_hidden'`,不删 L0 | `user_controls.py::archive()` |
| `/雾化` | 当前段落: **物理擦除 L0-细节**，`fog_state ← 'fogged_once'`，保留 L0-骨架 + 锚点句。**不可逆** | `fog_engine.py::fog_segment()` |

### 5.2 可视化（树状 + 时间线）

```
[时间线模式]
2026-07-04 ████████████████████  7 段 / 4 个 /循环 / 1 个 /重要
├─ 10:00-11:00 ⭐MTCA开发⭐
│   └─ 哲学宪法 ⭐/重要
├─ 09:00-10:00 工作
└─ ...

[树状模式]
MTCA 开发（项目）
├─ 哲学宪法（2026-07-04）⭐/重要 ⭐/循环
├─ 8 条铁律（2026-07-04）
├─ 时间分段（2026-07-04）
└─ ...
```

---

## 6. Agent 适配策略（互不干扰原则）

### 6.1 设计哲学

> **Agent 跑它的 → MTCA 在外独立存全文 → 调用时按需召回。两者不侵入，不抢资源。**

实现：
- MTCA 独立进程（`mtca-server`）
- 通过 **MCP Server** 或 **REST API** 对外
- Agent 集成 MCP Client，启动时配置 MTCA 地址
- MTCA 后台异步跑打分 / 压缩，不阻塞 Agent 进程

### 6.2 各平台适配

| 平台 | 适配方式 | 备注 |
|------|---------|------|
| **OpenClaw** | 内部事件订阅 | 直接拿到完整对话 |
| **Hermes** | MCP Client 集成 | 标准 MCP |
| **扣子（Coze）** | 插件运行时注入 | 通过 OpenAPI |
| **Claude Code** | MCP Server | Anthropic 标准 |
| **Cursor / Devin / Cline** | REST 适配 | 通用 |
| **ChatGPT / Gemini 网页** | 不适配 | 无 API 不在 MTCA 范围 |

---

## 7. 技术栈（精简单）

| 组件 | 选型 | 备注 |
|------|------|------|
| **存储** | SQLite + FTS5 + WAL | 单文件可移植 |
| **嵌入模型** | bge-small-zh-v1.5 | 30MB，本地 |
| **本地压缩模型** | Qwen3.6 35B A3B（推荐） | 老板选 |
| **降级模型** | Qwen2.5-7B-Instruct | 显存不够时 |
| **在线兜底** | Claude / GPT-4 API（OpenAI 兼容协议） | 任选 |
| **MCP** | mcp-python-sdk | 官方 |
| **CLI** | click + rich | 树状 + 时间线 |
| **Web UI** | HTMX + Jinja | 不上 React，避免依赖 |
| **测试** | pytest | |

> 不依赖 Docker / 云 / 任何云服务；纯 Python 3.10+；单 `pip install` 可装。

---

## 8. 验收标准（M1）

| 项 | 指标 |
|----|------|
| L0-A 完整入库 | 100% 消息不丢 |
| 时间分段准确率 | 边界合理（人工抽检 50 段 ≥ 85%）|
| 动态打分 | 24 小时模拟后重要话题保持 L1 ≥ 95% |
| 召回准确率 | 老板真历史 30 段测试 ≥ 90% |
| 用户控制接口 | /重要 /循环 /归档 行为正确 |
| 召回延迟 | 主通道 < 50ms |
| 安装门槛 | `pip install mtca-memory` 后单文件启动 |

---

## 9. 风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| Qwen3.6 35B A3B 老板电脑跑不动 | 🟡 中 | 自动降级到 Qwen2.5-7B-Instruct，再降级到 3B |
| 用户标记 /重要 后 AI 还是不调用 | 🟢 低 | /重要 锁分数 10000，永远 tier L1 |
| SQLite 大文件查询变慢 | 🟡 中 | FTS5 全文索引 + 时间戳分区 |
| 多 Agent 平台同时写 L0 冲突 | 🟡 中 | SQLite WAL 模式 + 行级锁 |
| 用户电脑磁盘满 | 🟢 低 | 用户主权，文档说明 1–2G/年，可忽略 |
| AI 召回错（视图层损失）| 🟢 低 | 自动回 L0 兜底 |

---

## 10. 路线图总览

```
M0 (今天) ── 设计哲学锁定 8 条铁律
M1 (1 周)── 50h ── 长期记忆 MVP（CLI 时间线 + L0 + 打分）
M2 (2 周)── 70h ── MCP Server + 双通道召回 + Web UI
M3 (4 周)── 80h ── L1–L3 压缩视图 + 本地 5B + 多档自适应
M4 (3 周)── 60h ── OpenClaw / Hermes / 扣子 / Claude Code 适配
M5 (4 周)── 80h ── 100 用户测试 + 开源 + 文档完整
                                          ── v1.0 公测发布
```

---

## 11. 当前任务（M1 即刻动手）

### 11.1 今晚 / 明天交接给小八（推荐优先级）

```
□ T0  仓库初始化（git init + .gitignore + 项目骨架）        30 min
□ T1  src/store/sqlite.py（schema + WAL + FTS5 + L0 不可变触发器）  3h
□ T2  src/l0/session_writer.py（L0-细节 消息入库）         3h
□ T3  src/l0/skeleton.py（L0-骨架锚点生成 + 关键词提取）  2h
□ T4  src/l0/time_segmenter.py（3 规则 + 弱合并）         4h
□ T5  src/l0/segment_writer.py（段落生成 + L0-B 写入）    3h
□ T6  src/fog/fog_engine.py（雾化操作：物理擦除 L0-细节） 3h
□ T7  src/compress/scoring.py（动态打分后端）             6h
□ T8  src/recall/recall_engine.py（双通道召回）           6h
□ T9  src/recall/fog_protocol.py（雾化召回行为: 3 态）    2h
□ T10 src/llm/provider.py（LLM 抽象: Ollama/LM Studio/llama.cpp/云） 4h
□ T11 src/cli/user_controls.py（/重要 /循环 /归档 /雾化） 3h
□ T12 src/cli/timeline.py（CLI 时间线，开发期辅助）       4h
□ T13 GUI MVP（时间线 + 话题树 + 雾化按钮，技术栈待定）  12h
□ T14 tests/test_recall_basic.py（50 段用例）              5h
□ T15 tests/test_fog_protocol.py（雾化 3 态行为测试）     3h
□ T16 benchmarks/test_set.json（30 老板真历史 + 20 编）   4h
□ T17 docs/QUICKSTART.md（GUI 启动 + 雾化教程）          2h
□ T18 docs/LLM_PROVIDERS.md（4 种后端接入指南）          2h
□ T19 benchmarks/M1_report.md（验收报告）                 3h
□ T20 src/lifecycle/retention_engine.py（retention 策略 + 静默态转移）  4h
□ T21 src/lifecycle/ask_restore.py（AI 询问 + ref_count 升级）  3h
□ T22 src/lifecycle/contradiction_detector.py（矛盾检测 → supersede）   4h
□ T23 src/relations/graph.py（4 种关系生成 + 图遍历）  5h
□ T24 src/compress/regen_engine.py（雾化/supersede 触发摘要重写）  4h
□ T25 GUI 知识图谱视图（节点+边+时间倒带滑块）            8h
□ T26 tests/test_relations_graph.py（图谱 + supersede 流转）  4h
□ T27 tests/test_regen_engine.py（重写触发 + 失败回退）  3h
□ T28 docs/COMPETITOR_ANALYSIS.md（6 家横向对比）         3h
                                   M1 总: ~110h （实际 2.5 周）
```

### 11.2 交接给老板验收的产物

- M1 报告：`benchmarks/M1_report.md`
- 50 段测试集：`benchmarks/test_set.json`
- 启动指南：`docs/QUICKSTART.md`

---

## 12. 版本历史

| 版本 | 日期 | 内容 |
|------|------|------|
| v0.1 | 2026-07-04 上午 | 35 轮对话理论（v1–v10 哲学）|
| v0.2 | 2026-07-04 10:03 | 完整开发计划（10 模块、6 里程碑）|
| **v0.3** | **2026-07-04 11:01** | **本版：哲学宪法 8 条 + L0–L3 多档视图 + 时间分段 + 动态打分 + 树状可视化 + 主流 Agent 适配互不干扰** |

---

## 13. 联系 / 协议

- 仓库：`I:\PROJECTS\MTCA\`
- 协议：MIT
- 作者：老板（设计） + 波波（PM） + 小八（开发落地）
- 版本：v0.3 终版

