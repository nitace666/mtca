# MTCA 向量召回层 V0

## 概述

M3-5 P0 阶段交付：加一层向量化召回（语义检索），跟 FTS5 字面 + facts 精确召回并存。
技术栈：Ollama nomic-embed-text + ChromaDB 持久化 + RRF 融合。**默认关闭，需手动 enable。**

## 架构

```
                       query
                         |
        +----------------+----------------+
        |                |                |
        v                v                v
   FTS5 字面召回    facts 精确召回    vector 语义召回
        |                |                |
        +----------------+----------------+
                         |
                         v
               RRF 融合 (K=60)
                         |
                         v
                    最终 hits
                  （向量层默认 disable）

  向量失败 → fallback FTS5+facts（主流程不断）
```

## 设计偏离与决策

### 偏离 1：单例 helper 而非 `__new__` 单例（Step 3）

- 原因：Step 2 测试每次 `_setup()` 期望新实例，`__new__` 会跨测试共享 conn + Ollama call_count，导致大量测试 fail
- 影响：helper（模块级 `_default_index + get_default_index()`）提供全局单例 + 懒加载语义等价
- 后续维护：用 helper 时不要 `set_mtca_conn()`（会污染全局）

### 偏离 2：recall_engine.py 整合用临时构造而非 helper（Step 4）

- 原因：recall() 短生命周期 conn，helper 是模块级单例；`get_default_index().search()` 共享 conn 会跨调用污染
- 影响：recall() 末尾 try/finally 临时构造 `VectorIndex(mtca_conn=conn)`，单例干净 + conn 隔离
- 后续维护：helper 不带 conn 路径仍可用于 benchmark

### 偏离 3：test 业务测试自建 DDL 而非依赖 v3 migration（Step 2 测试设计）

- 原因：13 测试全 mock 跑得快；Step 3 单独验证 schema migration
- 影响：测试 fixture 用 `CREATE TABLE IF NOT EXISTS vector_cache` 自建；v3 migration 上线后该 DDL 是 no-op
- 后续维护：不要在测试里写业务级 migration 验证，那是 Step 3 的活

## 已修问题

| Bug | 触发时机 | 修法 |
|---|---|---|
| Step 3 漏 import chromadb | Step 5 真连 ChromaDB 触发 `NameError: name chromadb is not defined` | +1 行 `import chromadb`（`src/recall/vector_index.py:27` 后）|

## 待修 spec bug（独立任务排期，不在本 commit 修）

| Bug | 严重性 | 修复方向 |
|---|---|---|
| FTS5 unicode61 中文 token 化失效 | **HIGH**（MTCA 全局）| 换 `tokenize=trigram`（FTS5 内置）或加 jieba 分词插件 |
| `recall_engine.py` 向量块位置 bug | MEDIUM（M3-5 P0 完整性）| 向量块（L1365-1402）前移到 `if not merged: return []` 之前；或 `if not merged` 时先尝试向量召回再决定 return |
| `_recall_cache_key` 不含 `vector_recall_enabled` | LOW | cache key 加 vector 状态读 |

## 已知局限

- L0 合成数据：本次实测基于合成 5 段 ground truth（topic_label + fog_anchor），无真实老板历史对话。5 场景 query 召回集合 = 0（已知 / 数据局限）
- FTS5 baseline recall = 0（unicode61 对中文 token 化失效；见上 Bug）
- p95 真连测验证：embedding + ChromaDB cosine + RRF merge = **4.2ms**（远小于 100ms 目标）
- ChromaDB 首次冷启：~240ms（首次 query 慢；后续 < 5ms）

## 配置

### ChromaDB 路径

`Path.home() / ".mtca" / "vector_db"`
（Windows: `C:\Users\<user>\.mtca\vector_db\`，git 仓库外，天然不入 commit）

### Embedding

- 模型：Ollama `nomic-embed-text`（ID 0a109f422b47，274MB，768 维，2048 context）
- 端点：`POST http://localhost:11434/api/embeddings`
- 长段切块：max_chars=1500 中文字（按段落切）

### 开关：VECTOR_RECALL_ENABLED

存 `settings` 表（M2.5 既有 KV 表，`src/store/sqlite.py:213`）key=`vector_recall_enabled`，值 `0`（默认，关）或 `1`（开）。

**启用**（手动）：
```bash
python -c "import sqlite3; c=sqlite3.connect(r'~/.mtca/mtca.db'); c.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', ('vector_recall_enabled','1',0)); c.commit(); print('enabled')"
```

**关闭**：
```bash
python -c "import sqlite3; c=sqlite3.connect(r'~/.mtca/mtca.db'); c.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', ('vector_recall_enabled','0',0)); c.commit(); print('disabled')"
```

## 接入 API（for 后续开发者）

### 单例 helper（不带 conn，benchmark / 简单调用）

```python
from src.recall.vector_index import get_default_index
idx = get_default_index()
hits = idx.search(query, k=10, path=None)  # 自动读 settings 决定开/关
```

### 临时构造（conn 隔离，recall_engine 整合用）

```python
from src.recall.vector_index import VectorIndex
idx = VectorIndex(mtca_conn=conn)  # conn 在 try/finally 里短开短关
hits = idx.search(query, k=10, path=None)
```

## 测试

- **619 passed in 96.18s**（606 baseline + 13 新增）
- **0 回归**
- 13 测试全 MagicMock `_client` + 假 `ollama_fn`（核心设计：业务测试不依赖真 Ollama/ChromaDB）

## 下一步（独立任务排期）

1. 修 FTS5 中文 token 化（独立任务，影响 MTCA 全部中文检索）
2. 修 `recall_engine.py` 向量块位置（保证向量层"救命"功能）
3. 等老板有真 L0 历史对话数据 → 重跑 "real benchmark"
4. 真 benchmark 验证实战 p95（本次是冷启前 + 4.2ms 链路）

## 依赖

- `chromadb>=0.5`（`requirements.txt` 已加）
- Ollama 服务 + `nomic-embed-text` 模型（老板本地已部署）

## 文件清单（本 commit）

| 文件 | 类型 | 行数 |
|---|---|---|
| `src/recall/vector_index.py` | 新建 | 300（<= 800）|
| `src/store/sqlite.py` | 修改 | +31 行（v3 migration + init_db 调用）|
| `src/recall/recall_engine.py` | 修改 | +80 行（末尾 try/except + `_fuse_3way_rrf` + `import sqlite3`）|
| `tests/test_vector_recall.py` | 新建 | 13 测试 |
| `benchmarks/_m35_vector_benchmark.py` | 新建 | 284 行 |
| `docs/VECTOR_RECALL.md` | 新建 | 本文档 |
| `requirements.txt` | 修改 | +1 行（chromadb>=0.5）|

## Commit

```
feat(recall): M3-5 加向量召回层（Ollama nomic-embed + ChromaDB），3 路并存，Ollama 不可达 fallback FTS5，默认关闭
```

## 现状（M3.5 整合后，2026-07-08）

本节为 M3.5 整体整合后的最新状态（相对初版有 3 个 bug 修复 + openclaw HTTP transport 集成）：

### 已修问题

| Bug | 触发时机 | 修法 | commit |
|---|---|---|---|
| Step 3 漏 import chromadb | Step 5 真连 ChromaDB 触发 NameError | +1 行 import chromadb（src/recall/vector_index.py:27 后）| adf0967 |
| FTS5 unicode61 中文 token 化失效（Bug 2 / B2）| 老板 L0 probe：unicode61 整段当 1 token 命中 0；M3-5 benchmark 4/5 场景空 | 换 tokenize=trigram（v4 migration，DROP+CREATE+rebuild）| df0ecad |
| recall_engine.py 向量块位置 bug（Bug 3 / B3）| baseline 空场景走不到向量层 | 删 if not merged 的 return []，让流程继续到向量块 | 77f5a06 |
| _recall_cache_key 不含 vector 状态（Bug 4 / B4）| 同一 query 在 vector 开/关切换后 cache 命中错位 | cache key 增 vector_enabled 参数，recall 入口读 settings 一次传入 | 77f5a06 |

### 仍待修 spec bug（独立任务排期，不在本 commit 修）

| Bug | 严重性 | 修复方向 |
|---|---|---|
| FTS5 trigram 对 2 字符 query 不命中 | LOW（实战 query 多 >= 3 字符）| 可能要 jieba 分词 + custom tokenize，或 jieba-ikt 配合 unicode61 |
| vector_cache schema 验证缺失 | LOW（schema 简单，Step 3 已手测）| 加 tests/test_vector_cache_schema.py（PRAGMA table_info 断言）|

### openclaw HTTP transport 集成（Step 10）

MTCA 现已通过 src/adapters/openclaw_http_adapter.py 接 openclaw HTTP transport（MCP 2025-03-26 spec）。
openclaw 实战配 mcp.servers.trae.url = http://127.0.0.1:8765/mcp，adapter 绑 127.0.0.1:8765/mcp 端点，6 tool 路由到既有 src.adapters.mcp_server.TOOLS。

详细使用见 src/adapters/openclaw_http_adapter.py docstring。

### 完整交付报告

见 docs/M3.5_REPORT.md（M3.5 整体交付总结：6 commit / 9 文件 / 2154 行 / 633 测试 / openclaw HTTP 实通）。
