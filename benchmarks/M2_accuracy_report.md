# MTCA 召回准确率报告（M2 / T30 baseline）

**生成时间**：2026-07-05 19:36 UTC
**数据库**：`tests\_tmp_ground_truth\ground_truth.db`
**测试集**：`benchmarks/test_set.json`（50 条）
**有效 n**：50（筛掉缺 expected_top1 或 query 的）

| 指标 | 值 | 说明 |
|---|---|---|
| hit_rate@1 | 0.160 | top-1 命中 ground truth 的比例 |
| hit_rate@3 | 0.160 | top-3 命中 ground truth 的比例 |
| MRR | 0.160 | Mean Reciprocal Rank（排名倒数平均，0=全没命中，1=全 top-1 命中） |

## 召回通道分布

- 召回返回 ≥1 条 segment 的 case：8/50
- 召回返回 0 条（empty）的 case：**42/50**
- 实际 top-k 命中 ground truth 的：8/50

## 逐 case 命中表

| i | case_id | query | expected_top1 | n_results | rank | 命中 |
|---|---|---|---|---|---|---|
| 0 | test_001 | Python 装饰器 wraps | `810f4d1a` | 2 | 1 | ✓ |
| 1 | test_002 | Rust 生命周期 注解 | `3b1f85be` | 0 | — | — |
| 2 | test_003 | TypeScript 类型守卫 | `e5da2a7c` | 2 | 1 | ✓ |
| 3 | test_004 | Go context 取消 | `fdad134a` | 0 | — | — |
| 4 | test_005 | MySQL 覆盖索引 | `e8673542` | 0 | — | — |
| 5 | test_006 | Redis 缓存穿透 | `aea80640` | 2 | 1 | ✓ |
| 6 | test_007 | Kafka 消息积压 | `fa9d47d4` | 0 | — | — |
| 7 | test_008 | Linux 僵尸进程 | `67f3fc53` | 0 | — | — |
| 8 | test_009 | Docker bridge 网络 | `187f1663` | 0 | — | — |
| 9 | test_010 | Q3 周报 复盘 | `4dbc2b1d` | 0 | — | — |
| 10 | test_011 | 产品 PRD 评审 | `89a81c6a` | 0 | — | — |
| 11 | test_012 | 合同 违约金 | `d401fcd4` | 0 | — | — |
| 12 | test_013 | 晋升 答辩 准备 | `da5f0835` | 0 | — | — |
| 13 | test_014 | 架构师 招聘 JD | `e51498cc` | 0 | — | — |
| 14 | test_015 | 业务 立项 评审 | `66442b57` | 0 | — | — |
| 15 | test_016 | 跨部门 协作 | `abcc08cf` | 0 | — | — |
| 16 | test_017 | 血脂 体检 偏高 | `43fc43ea` | 0 | — | — |
| 17 | test_018 | 宝宝 辅食 添加 | `6cfd5374` | 0 | — | — |
| 18 | test_019 | 水电 改造 预算 | `0d7b7f82` | 0 | — | — |
| 19 | test_020 | 西湖 一日游 | `fad35d80` | 0 | — | — |
| 20 | test_021 | 红烧肉 做法 | `b614d30c` | 0 | — | — |
| 21 | test_022 | 猫咪 绝育 | `ce123225` | 0 | — | — |
| 22 | test_023 | 健身 减脂 计划 | `bf0c7e62` | 0 | — | — |
| 23 | test_024 | 英语单词 记忆 | `fefb8cb1` | 0 | — | — |
| 24 | test_025 | PMP 备考 | `212d3281` | 2 | 1 | ✓ |
| 25 | test_026 | 深度学习 课程 笔记 | `0c7baa0a` | 0 | — | — |
| 26 | test_027 | 资治通鉴 阅读 笔记 | `27ea67f2` | 0 | — | — |
| 27 | test_028 | 高考 数学 复习 | `f0c5be81` | 0 | — | — |
| 28 | test_029 | 钢琴 练习 方法 | `500d323c` | 0 | — | — |
| 29 | test_030 | 雅思 口语 备考 | `fc0953c7` | 0 | — | — |
| 30 | test_031 | 问候 | `9128f848` | 0 | — | — |
| 31 | test_032 | Docker compose | `e0cd02be` | 3 | 1 | ✓ |
| 32 | test_033 | PYTHONPATH | `bf797ff8` | 2 | 1 | ✓ |
| 33 | test_034 | Visa 卡 | `48504bf0` | 2 | 1 | ✓ |
| 34 | test_035 | list tuple 区别 | `903f26d6` | 2 | 1 | ✓ |
| 35 | test_036 | 注意力机制 大模型 | `945b93a3` | 0 | — | — |
| 36 | test_037 | 装修 预算 细节 | `394811a3` | 0 | — | — |
| 37 | test_038 | 家庭 理财 配置 | `057b3c70` | 0 | — | — |
| 38 | test_039 | 小说 构思 科幻 记忆 | `1fa3f630` | 0 | — | — |
| 39 | test_040 | 英语 学习 规划 雅思 | `32e80a98` | 0 | — | — |
| 40 | test_041 | 篮球 足球 网球 比赛 | `1a8c8370` | 0 | — | — |
| 41 | test_042 | 苹果 华为 小米 手机 | `5fea511e` | 0 | — | — |
| 42 | test_043 | 美式 拿铁 卡布 咖啡 | `56624756` | 0 | — | — |
| 43 | test_044 | 火锅 烧烤 日料 | `dda563af` | 0 | — | — |
| 44 | test_045 | 摇滚 民谣 爵士 音乐 | `71ccdf1e` | 0 | — | — |
| 45 | test_046 | 北京 天气 | `1e703bda` | 0 | — | — |
| 46 | test_047 | 订单 统计 数据库 | `ff2073f9` | 0 | — | — |
| 47 | test_048 | 翻译 中英 | `ac720403` | 0 | — | — |
| 48 | test_049 | README 项目名 | `faea5b4a` | 0 | — | — |
| 49 | test_050 | 计时 提醒 | `c0fecaa0` | 0 | — | — |

## 失败明细

- 无

## 说明

- hit_rate@1 是 M2 关键指标：MCP Server（Stage T31）给 Claude Code / Cursor 用的就是 top-1
- hit_rate@3 = 召回深度，留 3 个让 LLM 自己挑
- MRR = 综合排名质量

## Baseline 局限性

本次 baseline 有**两层独立性局限**，互相叠加：

### 1. Ground truth 是 synthetic 标注

生成方式（`benchmarks/_seed_test_set.py`，不入 commit）：

1. 在 `tests/_tmp_ground_truth/ground_truth.db` 建临时 db（不影响 `~/.mtca/mtca.db`）
2. 每条 case 创建 1 session，写入 user+assistant 对话，跑 `write_segments`
3. `expected_top1 = seg_ids[0]`（L0 骨架 ID）

recall 跑在与 ground truth 完全一致的 db 上，**没有**真实噪声段落。

### 2. FTS5 unicode61 tokenizer 不切中文（**核心瓶颈**）

实测发现：`messages_fts` 用 `tokenize='unicode61'`（见 store sqlite 初始化），
unicode61 不会按字符切中文，中文连续段被当成**单个 token**。导致：

- 中文 query（如 `"Rust 生命周期 注解"`）需要整串作为 token 命中消息
- 实际只有 query 含独立 ASCII token 的 case 能召回（例：`"Python 装饰器 wraps"` 因含 `Python/wraps`）
- 50 条用例中 **42 条 recall 双通道返回空**（无任何 candidate）

这就是 hit_rate@1 偏低的**真正原因**：
- 如果 FTS5 能切中文，理论上 synthetic ground truth 上 hit_rate@1 应当 ≈1.0（recall 找的是自己写入的段）
- 实际 0.160，是因为底层 FTS5 token 化能力限制，不是 recall 排序或 expected_top1 写入错误

### 改进路径

- **Stage T29 加向量召回**（bge-small-zh + sqlite-vss）：向量化不依赖 tokenizer，
  中文按语义召回；预期 hit_rate@1 提升 +30-50%
- **Stage T31 加 MCP 后**：top_k 可放宽到 5，缓解精度压力
- **更长期**：换 FTS5 tokenizer 为 `trigram` 或接 jieba 中文分词插件

## 输出文件

- `benchmarks/M2_accuracy_report.md`（本文件）
- `benchmarks/accuracy_report.py`（脚本）
- `benchmarks/test_set.json`（input，标注好 ground truth + expected_top1）
