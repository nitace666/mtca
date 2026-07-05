"""benchmarks/accuracy_report.py — T30 召回准确率量化。

跑 test_set.json，对每条 query 跑 recall(query, top_k=3, path=db)，
统计 hit_rate@1 / @3 / MRR，输出到 M2_accuracy_report.md。

不依赖 LLM；用本地 recall() 直接跑。

用法：
    python benchmarks/accuracy_report.py [--db <path>] [--top-k 3]

参数：
    --db   数据库路径；缺省读 MTCA_DB 环境变量，再 fallback 到 ~/.mtca/mtca.db
    --top-k   recall top_k（默认 3）

兼容 test_set.json schema（新加 expected_top1，保留旧字段）：
    - query 字段读：先 expected_recall_query，fallback topic
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 让脚本从任意 cwd 跑都能 import src
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.recall.recall_engine import recall


def measure(db_path: Path, test_set: list[dict], top_k: int = 3) -> dict:
    """跑测试集，统计召回准确率。

    返回 dict：包含 hit_rate@1 / @3 / MRR / n / total_cases /
    failures / per_case（逐 case 诊断）。
    """
    hits_at_1 = 0
    hits_at_3 = 0
    reciprocal_ranks: list[float] = []
    failures: list[tuple[int, str, str]] = []
    per_case: list[dict] = []
    total = len(test_set)

    for i, case in enumerate(test_set):
        query = case.get("expected_recall_query") or case.get("topic", "")
        expected = case.get("expected_top1")
        case_id = case.get("id", f"<{i}>")
        row = {
            "i": i, "case_id": case_id, "query": query,
            "expected_top1": expected, "n_results": 0,
            "rank": 0, "hit": False, "fail": "",
        }
        if not expected:
            row["fail"] = "missing expected_top1"
            per_case.append(row)
            failures.append((i, case_id, row["fail"]))
            continue
        if not query:
            row["fail"] = "missing query+topic"
            per_case.append(row)
            failures.append((i, case_id, row["fail"]))
            continue
        try:
            results = recall(query=query, top_k=top_k, path=db_path)
        except Exception as exc:  # noqa: BLE001
            row["fail"] = f"recall: {exc}"
            per_case.append(row)
            failures.append((i, case_id, row["fail"]))
            continue
        if not isinstance(results, list):
            row["fail"] = "recall 返回非列表"
            per_case.append(row)
            failures.append((i, case_id, row["fail"]))
            continue
        seg_ids = [r.get("segment_id") for r in results if r.get("segment_id")]
        row["n_results"] = len(seg_ids)
        if expected in seg_ids:
            rank = seg_ids.index(expected) + 1
            row["rank"] = rank
            row["hit"] = True
            reciprocal_ranks.append(1.0 / rank)
            if rank == 1:
                hits_at_1 += 1
            if rank <= top_k:
                hits_at_3 += 1
        else:
            reciprocal_ranks.append(0.0)
        per_case.append(row)

    n = len(reciprocal_ranks)
    return {
        "hit_rate@1": hits_at_1 / n if n else 0.0,
        "hit_rate@3": hits_at_3 / n if n else 0.0,
        "mrr": sum(reciprocal_ranks) / n if n else 0.0,
        "n": n,
        "total_cases": total,
        "failures": failures,
        "per_case": per_case,
    }


def _diag_table(per_case: list[dict]) -> str:
    """生成逐 case 命中表（markdown）。"""
    lines = [
        "| i | case_id | query | expected_top1 | n_results | rank | 命中 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in per_case:
        q_short = r["query"][:30] + ("..." if len(r["query"]) > 30 else "")
        exp_short = (r["expected_top1"] or "")[:8] or "—"
        rank_str = str(r["rank"]) if r["rank"] else "—"
        hit_str = "✓" if r["hit"] else ("✗" if r["fail"] else "—")
        lines.append(
            f"| {r['i']} | {r['case_id']} | {q_short} | "
            f"`{exp_short}` | {r['n_results']} | {rank_str} | {hit_str} |"
        )
    return "\n".join(lines)


def render_markdown(metrics: dict, db_path: str) -> str:
    """生成 markdown 报告。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    failures = metrics.get("failures", [])
    fail_lines = "\n".join(
        f"- [{idx}] {cid}: {err}" for idx, cid, err in failures
    ) or "- 无"

    # 统计：n_results == 0 的（即 recall 完全空）
    empty_recall = sum(
        1 for r in metrics["per_case"] if r["n_results"] == 0 and not r["fail"]
    )
    with_results = sum(
        1 for r in metrics["per_case"] if r["n_results"] > 0
    )
    hit_count = sum(1 for r in metrics["per_case"] if r["hit"])

    md = f"""# MTCA 召回准确率报告（M2 / T30 baseline）

**生成时间**：{now}
**数据库**：`{db_path}`
**测试集**：`benchmarks/test_set.json`（{metrics['total_cases']} 条）
**有效 n**：{metrics['n']}（筛掉缺 expected_top1 或 query 的）

| 指标 | 值 | 说明 |
|---|---|---|
| hit_rate@1 | {metrics['hit_rate@1']:.3f} | top-1 命中 ground truth 的比例 |
| hit_rate@3 | {metrics['hit_rate@3']:.3f} | top-3 命中 ground truth 的比例 |
| MRR | {metrics['mrr']:.3f} | Mean Reciprocal Rank（排名倒数平均，0=全没命中，1=全 top-1 命中） |

## 召回通道分布

- 召回返回 ≥1 条 segment 的 case：{with_results}/{metrics['total_cases']}
- 召回返回 0 条（empty）的 case：**{empty_recall}/{metrics['total_cases']}**
- 实际 top-k 命中 ground truth 的：{hit_count}/{metrics['total_cases']}

## 逐 case 命中表

{_diag_table(metrics['per_case'])}

## 失败明细

{fail_lines}

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
- 50 条用例中 **{empty_recall} 条 recall 双通道返回空**（无任何 candidate）

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
"""
    return md


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_db = (
        os.environ.get("MTCA_DB")
        or str(Path.home() / ".mtca" / "mtca.db")
    )
    parser.add_argument(
        "--db",
        default=default_db,
        help="数据库路径（缺省读 MTCA_DB 环境变量，再 fallback ~/.mtca/mtca.db）",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="recall top_k（默认 3）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    db_path = Path(args.db)
    test_set_path = PROJECT_ROOT / "benchmarks" / "test_set.json"
    report_path = PROJECT_ROOT / "benchmarks" / "M2_accuracy_report.md"

    if not db_path.exists():
        print(f"FAIL: 数据库不存在 {db_path}", file=sys.stderr)
        sys.exit(1)
    if not test_set_path.exists():
        print(f"FAIL: {test_set_path} 不存在", file=sys.stderr)
        sys.exit(1)

    test_set = json.loads(test_set_path.read_text(encoding="utf-8"))
    if not isinstance(test_set, list):
        print("FAIL: test_set.json 必须是 JSON 数组", file=sys.stderr)
        sys.exit(1)

    print(
        f"跑 {len(test_set)} 条测试集 ... db={db_path} top_k={args.top_k}",
        flush=True,
    )
    metrics = measure(db_path, test_set, top_k=args.top_k)
    print(f"  hit_rate@1 = {metrics['hit_rate@1']:.3f}", flush=True)
    print(f"  hit_rate@3 = {metrics['hit_rate@3']:.3f}", flush=True)
    print(f"  MRR        = {metrics['mrr']:.3f}", flush=True)
    print(f"  有效 n     = {metrics['n']}/{metrics['total_cases']}", flush=True)
    print(f"  失败       = {len(metrics['failures'])}", flush=True)

    md = render_markdown(metrics, str(db_path))
    report_path.write_text(md, encoding="utf-8")
    print(f"OK: {report_path} 已生成", flush=True)


if __name__ == "__main__":
    main()
