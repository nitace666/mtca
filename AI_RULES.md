# MTCA AI 协作规则（v0.4 Vibe Coding 版）

> **所有 AI 开发者必读本文件，遵守后才能开始写代码。**
> 本文件由 `I:\PROJECTS\MTCA\.trae\rules\mtca.md` 项目规则引用。
> 最后更新：2026-07-04

---

## 1. 项目背景

| 项 | 值 |
|---|---|
| 项目名 | MTCA (Multi-Tier Context Architecture) |
| 类型 | AI 长期记忆中间件 |
| 核心理念 | 用户主权 + L0 不可变 + 雾化不可逆 + 本地优先 |
| 技术栈 | Python 3.10+ / SQLite (FTS5) / PySide6 GUI / pytest |
| 设计文档 | DEVELOPER_PLAN.md + docs/*.md |
| 任务清单 | PLAYBOOK.md（28 个 T 任务）|
| 协议 | MIT（M5 改 AGPL-3.0）|
| GitHub | https://github.com/nitace666/mtca |

---

## 2. 角色定义

### 用户（PM / 任务派发员）
- **不写代码**
- 只做 3 件事：复制 prompt → 看 AI 输出 → 验收
- 用 PLAYBOOK.md 的 T 任务清单派发工作
- 用验收清单逐项打勾

### AI（开发者）
- 串行执行 T0 → T28 任务
- 每个 T 一次 commit
- 写完必跑 pytest
- 失败 → 报告 + 等用户指令（不要自动改）

---

## 3. 工作模式（Vibe Coding）

### 3.1 单 T 任务流程
```
1. 用户粘贴 prompt（含必读文档 + 具体要求）
2. AI 用 Read 工具读必读文档（不靠记忆）
3. AI 用 Write 工具写代码到指定路径
4. AI 用 Bash 跑 pytest
5. AI 输出：文件清单 + 行数 + pytest 结果
6. 用户验收（用 PLAYBOOK 验收清单）
7. 通过 → git commit → 下一个 T
8. 失败 → 复制错误给 AI → 重做
```

### 3.2 关键约束
- **每 T 一个新对话**（不要长对话连做多个 T）
- **每 T 一个 commit**（message 格式 `T{id}: 简述`）
- **每 T 重读必读文档**（不靠记忆）

---

## 4. 必读文档清单

按任务类型：

| 任务 | 必读 |
|---|---|
| T1 (SQLite) | DEVELOPER_PLAN.md §2.2 + DATA_MODEL.md §2 |
| T2-T5 (L0 层) | DEVELOPER_PLAN.md §4.1 + 已写完的 src/ 模块 |
| T6 (雾化) | DATA_MODEL.md §4 + USER_CONTROLS.md §2.4 |
| T7 (打分) | DEVELOPER_PLAN.md §4.2 + DATA_MODEL.md §3 |
| T8 (召回) | DEVELOPER_PLAN.md §4.4 + ARCHITECTURE.md §2 |
| T10 (LLM) | DEVELOPER_PLAN.md §7 |
| T11/T12 (CLI) | USER_CONTROLS.md §2 + §6 |
| T13 (GUI) | USER_CONTROLS.md §3.1-3.3 |
| T20-T21 (lifecycle) | V0.4_PIVOT.md §3.6 |
| T22 (矛盾) | V0.4_PIVOT.md §3.7 + DATA_MODEL.md §5 |
| T23 (关系图) | V0.4_PIVOT.md §3.8 |
| T24 (重写) | V0.4_PIVOT.md §3.9 + DATA_MODEL.md §7 |
| T25 (GUI 知识图谱) | USER_CONTROLS.md §3.4 |

---

## 5. ✅ 必须做

1. **写代码前重读必读文档**（每次新对话都读，不靠记忆）
2. **函数 docstring 用中文**（简短，1-2 行）
3. **错误消息用中文**（raise RuntimeError("...") 之类）
4. **用 sqlite3 标准库**（不用 SQLAlchemy / Peewee）
5. **所有 SQL 用 ? 占位符**（防注入）
6. **单文件 ≤ 任务规定的行数**：
   - T1: 700 / T2: 400 / T3: 300 / T4: 500 / T5: 350
   - T6: 450 / T7: 750 / T8: 750 / T9: 250 / T10: 700
   - T11: 400 / T12: 500 / T13: 1500
   - T20: 600 / T21: 400 / T22: 550 / T23: 700 / T24: 500 / T25: 1000
7. **改完文件必跑 pytest**（不能口头说"应该过了"）
8. **输出格式**：文件清单 + 行数 + pytest 结果 + commit hash
9. **commit message 中文**（`T1: SQLite 存储层 + 不可变触发器`）
10. **完成任务后跑**：
    ```bash
    git add . && git commit -m "T{id}: 简述"
    git push origin main
    ```

---

## 6. ❌ 绝对禁止

### 6.1 文件操作禁止
- ❌ 改用户没让你改的目录
- ❌ 改 `README.md` / `LICENSE` / `.gitignore` / `AI_RULES.md`（4 个锁定文件）
- ❌ 改 `PLAYBOOK.md` / `DEVELOPER_PLAN.md` / `docs/*.md` 设计文档
- ❌ 写超 1000 行的单文件（必须拆）
- ❌ 创建空文件以外的杂物（.DS_Store, Thumbs.db, .vscode/, .idea/）

### 6.2 行为禁止
- ❌ 跳任务（必须 T1 完才能 T2）
- ❌ 跳过测试
- ❌ 添加"改进" / "顺便优化" / "我建议..."（超出 prompt 范围）
- ❌ 假设用户已经做了某个步骤（每次都先确认）
- ❌ 输出完整代码（用户要"贴"才贴）
- ❌ 用 emoji 装饰输出
- ❌ 改用户反馈过的代码风格（已经定的就保持）
- ❌ 在 commit 里塞入无关文件

### 6.3 技术禁止
- ❌ 用 SQLAlchemy / Peewee（用 sqlite3）
- ❌ 用 f-string 拼 SQL（用 ? 占位符）
- ❌ 引入新依赖（任务明确说 `pip install` 才装）
- ❌ 用 emoji 在代码注释里
- ❌ 用全局变量
- ❌ 写死路径（用 `Path.home() / ".mtca"`）

### 6.4 Git 禁止
- ❌ `git push --force`（除非用户明确说）
- ❌ 直接 push 到 main 当作"试试"（先 commit，确认后再 push）
- ❌ 改 git config global
- ❌ 删 .git 目录

---

## 7. 技术栈约束

### 7.1 强制使用
- **Python 3.10+**（`match` 语句、`|` 类型联合）
- **sqlite3**（标准库）
- **pytest**（测试，不用 unittest）
- **click**（CLI 入口）
- **rich**（CLI 输出美化）
- **jieba**（中文分词，T3 引入）
- **PySide6**（GUI，T13 引入）
- **httpx**（HTTP，T10 引入）

### 7.2 禁止使用
- ❌ SQLAlchemy / Peewee / Django ORM
- ❌ pylint / flake8（用 pytest）
- ❌ unittest（用 pytest）
- ❌ FastAPI / Flask / Django（M2 才考虑）
- ❌ Qdrant / Pinecone / 向量 DB（M3 之前不接）
- ❌ LangChain / LlamaIndex（不依赖框架）
- ❌ OpenAI SDK 之前不要直接 import（用我们的 LLMProvider）

---

## 8. 输出格式模板

每 T 完成后输出：

```
## T{id} 完成
- 修改文件:
  - src/store/sqlite.py (612 行, 22 函数)
  - tests/conftest.py (45 行, 2 fixture)
  - tests/test_store_sqlite.py (185 行, 8 测试)
- pytest 结果: 8 passed in 0.42s
- 覆盖: 92%
- commit: abc1234 "T1: SQLite 存储层 + 不可变触发器"
- 推送: ✅ origin/main
- 验收清单: 8/8 通过
- 备注: （如有降级或异常）
```

---

## 9. Git 工作流

### 9.1 单 T 流程
```bash
# 1. 写代码
# 2. 跑测试
pytest tests/test_xxx.py -v
# 3. 全过才能 commit
git add .
git commit -m "T{id}: 简述"
git push origin main
```

### 9.2 commit message 规范
```
T{id}: 简述（中文，≤ 30 字）

详细说明（可选，2-3 行）
- 关键点 1
- 关键点 2
- 测试结果

Refs: PLAYBOOK T{id}
```

### 9.3 失败回退
```bash
# 撤回未 commit 的修改
git checkout -- path/to/file

# 撤回最近 1 个 commit（保留修改）
git reset --soft HEAD~1

# 撤回最近 1 个 commit（丢弃修改）⚠️ 慎用
git reset --hard HEAD~1
```

---

## 10. 测试要求

### 10.1 必须
- 每个 T 任务写对应测试文件
- pytest 全过才能 commit
- 关键边界 case 覆盖（empty / null / max / race）

### 10.2 跑测试命令
```bash
# 跑全部
pytest -v

# 跑单个文件
pytest tests/test_xxx.py -v

# 跑单个测试
pytest tests/test_xxx.py::test_yyy -v

# 覆盖率
pytest --cov=src --cov-report=html
```

### 10.3 失败时
- AI 不要自动改代码
- 复制 pytest 完整输出给用户
- 等用户指令再改

---

## 11. 错误处理

### 11.1 AI 自己写错
- pytest 失败 → 报告错误，不自动重试
- import 错误 → 检查 src/ 依赖，可能漏写文件
- 文件路径错 → 报告路径，重新创建

### 11.2 用户输入错误
- 用户给的 prompt 有歧义 → 先问清楚再写
- 用户要求超范围 → 拒绝 + 解释为什么
- 用户要求改锁定文件 → 拒绝 + 列出锁定文件清单

---

## 12. M1 范围（不做）

**M1 阶段绝对不要做**：

- ❌ MCP Server（M2 才做）
- ❌ REST API（M2 才做）
- ❌ 向量数据库 / Qdrant / Pinecone
- ❌ 真实 Agent 平台适配（OpenClaw/Claude Code/Cursor）
- ❌ 云 LLM API 真实调用（T10 留接口，不实调）
- ❌ 移动端 / Web 端
- ❌ 用户系统 / 多用户隔离
- ❌ 性能优化（M1 先跑通）
- ❌ 单元测试覆盖率 ≥ 90%（M1 目标 ≥ 60%）

---

## 13. 性能 / 风格

### 13.1 代码风格
- 命名：`snake_case`（函数/变量） / `PascalCase`（类）/ `UPPER_CASE`（常量）
- 缩进：4 空格
- 引号：双引号优先
- 行长度：≤ 100 字符
- import 顺序：标准库 → 第三方 → 本地

### 13.2 函数规范
- 单个函数 ≤ 50 行（多就拆）
- 嵌套 ≤ 3 层
- 不要 magic number（提取为常量）

### 13.3 性能基线（M1 验收）
| 指标 | 目标 |
|---|---|
| pytest 全跑 | < 30 秒 |
| 主召回延迟 (P95) | < 50 ms |
| GUI 启动 | < 5 秒 |
| 安装启动 | < 30 秒 |

---

## 14. 沟通约定

### 14.1 AI 反馈
- 写完代码 → 跑测试 → 输出结果 → 等用户验收
- 不要问"是否继续"（已确定就继续）
- 不要列 N 个选项让用户选（按 prompt 要求做）

### 14.2 用户提问
- "为什么" → 解释原理 + 引用文档
- "怎么改" → 给出 1 个推荐方案
- "哪里有问题" → 检查后给具体位置

### 14.3 中文
- 文档、commit、注释、错误信息全用中文
- 代码标识符用英文（避免编码问题）
- 提示词用中文

---

## 15. 锁定文件清单（任何 AI 不得改）

```
README.md
LICENSE
.gitignore
AI_RULES.md
PLAYBOOK.md
DEVELOPER_PLAN.md
docs/*.md
```

改这 8 类文件需要用户在 prompt 明确说"改 XX 文件"，否则拒绝。

---

## 16. VIBE CODING 特别规则（M1 阶段）

1. **每 T 一个 commit**：message 格式 `T{id}: 简述`
2. **失败回退**：pytest 失败 → 复制错误给用户，AI 不要自动改
3. **不引依赖**：除非任务明确说 `pip install` 才装包
4. **不写大文件**：单文件 > 1000 行要拆
5. **输出格式**：文件清单 + 行数 + pytest 结果 + commit hash
6. **M1 范围外不做**：MCP、Vector DB、云 LLM、Agent 平台
7. **不抢跑下一个 T**：T1 没验收完，不动 T2
8. **新对话重读规则**：每 T 开新对话，先说"已读 AI_RULES.md"

---

## 附录 A：T 任务清单（快速参考）

详见 `PLAYBOOK.md` §5，这里只列编号：

```
T0  仓库初始化 ✅ 已完成（项目同步到 GitHub）
T1  SQLite 存储层 ← 你现在做这个
T2  L0-细节 写入
T3  L0-骨架 生成
T4  时间分段器
T5  段落写入器
T6  雾化引擎
T7  动态打分
T8  召回引擎
T9  雾化召回协议
T10 LLM 抽象层
T11 CLI 用户控制
T12 CLI 时间线
T13 GUI MVP
T14 召回基础测试
T15 雾化测试
T16 测试数据集
T17 QUICKSTART
T18 LLM 接入指南
T19 M1 验收报告
T20 retention 引擎
T21 AI 询问升级
T22 矛盾检测
T23 关系图谱
T24 摘要重写
T25 GUI 知识图谱
T26 关系图测试
T27 重写测试
T28 竞品分析 ✅ 已完成
```

---

## 附录 B：紧急联系 / 反馈

发现规则不合理 → 改 `AI_RULES.md` → commit → 通知所有 AI 开发者

发现 AI 飘了 → 复制 `已读 AI_RULES.md，重做 T{id}：不要做 XX` → 重新派发

---

**规则版本**：v0.4
**生效日期**：2026-07-04
**下次复审**：T10 完成时
