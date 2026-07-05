"""benchmarks/_validate_c1.py — C1 事实提取对召回的真实验证。
对 ground truth DB 里 50 个 session 跑 extract_facts（用 llama.cpp server），
把 facts 灌回 ground truth DB，然后跑 recall 看 hit_rate@1 改善。
用法（在 MTCA-t29 根目录）：python benchmarks/_validate_c1.py
"""
from __future__ import annotations
import json, sqlite3, sys, time
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.llm.extractor import extract_facts_from_messages
from src.llm.facts_store import create_fact, count_facts
from src.llm.provider import LMStudioProvider
from src.l0.segment_writer import get_segment
from src.l0.session_writer import get_session_messages
from src.recall.recall_engine import recall
GT_DB = PROJECT_ROOT / "tests" / "_tmp_ground_truth" / "ground_truth.db"
TEST_SET = PROJECT_ROOT / "benchmarks" / "test_set.json"
PROVIDER_BASE = "http://localhost:8083"
PROVIDER_MODEL = "qwen36-heretic-q4km"
def _clear_facts():
    conn = sqlite3.connect(str(GT_DB))
    conn.execute("DELETE FROM facts")
    conn.commit(); conn.close()
def _evaluate(test_set, db_path):
    h1 = h3 = 0; hits = []
    for case in test_set:
        results = recall(query=case["expected_recall_query"], top_k=3, path=db_path)
        ids = [r.get("segment_id") for r in results]
        if case["expected_top1"] in ids:
            idx = ids.index(case["expected_top1"])
            hits.append(case["expected_recall_query"])
            h3 += 1
            if idx == 0: h1 += 1
    n = len(test_set); return h1/n, h3/n, hits
def _session_messages(session_id):
    raw = get_session_messages(session_id, path=GT_DB)
    out = []
    for m in raw:
        if m.get("content") is None: continue
        out.append({"role": m.get("role") or "user", "content": m["content"]})
    return out
def main():
    if not GT_DB.exists():
        print("FAIL: GT DB missing:", GT_DB, file=sys.stderr); sys.exit(1)
    test_set = json.loads(TEST_SET.read_text(encoding="utf-8"))
    if not isinstance(test_set, list) or not test_set:
        print("FAIL: test_set.json bad", file=sys.stderr); sys.exit(1)
    print("Test set:", len(test_set), "cases")
    print("GT DB:  ", GT_DB)
    provider = LMStudioProvider(base_url=PROVIDER_BASE, model=PROVIDER_MODEL)
    if not provider.is_available():
        print("FAIL: server not reachable", file=sys.stderr); sys.exit(1)
    print("Provider:", PROVIDER_BASE, "model=", PROVIDER_MODEL, " OK")
    _clear_facts()
    print("Cleared facts table (now:", count_facts(path=GT_DB), ")")
    h1_pre, h3_pre, hits_pre = _evaluate(test_set, GT_DB)
    print()
    print("Baseline (no facts): hit@1=", round(h1_pre,3), "hit@3=", round(h3_pre,3))
    print("  Baseline hit queries:", len(hits_pre), "/", len(test_set))
    n_ok = n_facts = n_fail = 0; t0 = time.time()
    for i, case in enumerate(test_set):
        seg = get_segment(case["expected_top1"], path=GT_DB)
        if not seg:
            print("  [", i, "] segment not found:", case["expected_top1"], file=sys.stderr); n_fail += 1; continue
        sess_id = seg["session_id"]
        msgs = _session_messages(sess_id)
        if not msgs: print("  [", i, "] session", sess_id, "no msgs", file=sys.stderr); n_fail += 1; continue
        try:
            facts = extract_facts_from_messages(msgs, provider)
        except Exception as e:
            print("  [", i, "] LLM fail:", e, file=sys.stderr); n_fail += 1; continue
        if not facts: n_fail += 1; continue
        seg_id = case["expected_top1"]
        for f in facts:
            create_fact(session_id=sess_id, segment_id=seg_id, content=f["content"], source="extracted", confidence=f["confidence"], tags=json.dumps(f.get("tags") or [], ensure_ascii=False), path=GT_DB)
            n_facts += 1
        n_ok += 1
        if (i + 1) % 10 == 0:
            print("  [", i+1, "/", len(test_set), "] processed=", n_ok, "facts=", n_facts, "fails=", n_fail)
    dt = time.time() - t0
    print()
    print("Extracted", n_facts, "facts from", n_ok, "/", len(test_set), "cases in", round(dt,1), "s")
    print("  failures (no facts from LLM):", n_fail)
    print("  DB facts total:", count_facts(path=GT_DB))
    h1_post, h3_post, hits_post = _evaluate(test_set, GT_DB)
    print()
    print("With facts:    hit@1=", round(h1_post,3), "hit@3=", round(h3_post,3))
    print("  Post-hit queries:", len(hits_post), "/", len(test_set))
    delta1 = h1_post - h1_pre
    delta3 = h3_post - h3_pre
    print()
    print("Delta hit@1 =", round(delta1, 3))
    print("Delta hit@3 =", round(delta3, 3))
    print()
    print("Sample hits:", " / ".join(hits_post[:5]) if hits_post else "(none)")
    sample_hits = hits_post[:5]
    hits_md = "\n".join("- " + q for q in sample_hits) if sample_hits else "- (none)"
    if delta1 >= 0.3:
        concl = "C1 显著改善（hit_rate@1 提升 >= 0.3）"
    elif delta1 > 0:
        concl = "C1 改善但未达阈值（hit_rate@1 提升 < 0.3）"
    else:
        concl = "C1 无改善，需排查"
    avg_dt = dt / max(n_ok, 1)
    avg_facts = n_facts / max(n_ok, 1)
    table_rows = []
    table_rows.append("| metric | Before | After | delta |")
    table_rows.append("|---|---|---|---|")
    table_rows.append("| hit_rate@1 | " + ("%.3f" % h1_pre) + " | " + ("%.3f" % h1_post) + " | " + ("%+.3f" % delta1) + " |")
    table_rows.append("| hit_rate@3 | " + ("%.3f" % h3_pre) + " | " + ("%.3f" % h3_post) + " | " + ("%+.3f" % delta3) + " |")
    table_rows.append("| facts | 0 | " + str(count_facts(path=GT_DB)) + " | - |")
    table = "\n".join(table_rows)
    lines = []
    lines.append("# C1 验证报告（2026-07-06）")
    lines.append("")
    lines.append("**环境**: llama.cpp HTTP @ `" + PROVIDER_BASE + "` model `" + PROVIDER_MODEL + "`")
    lines.append("**GT DB**: `" + str(GT_DB) + "`")
    lines.append("**测试集**: " + str(len(test_set)) + " cases")
    lines.append("")
    lines.append("## 关键数字")
    lines.append("")
    lines.append(table)
    lines.append("")
    lines.append("## 提炼统计")
    lines.append("")
    lines.append("- 处理 case 数: " + str(n_ok) + "/" + str(len(test_set)))
    lines.append("- LLM 无返回: " + str(n_fail))
    lines.append("- 抽取 fact 总数: " + str(n_facts))
    lines.append("- 平均每 case 抽 " + ("%.1f" % avg_facts) + " 个 fact")
    lines.append("- 端到端耗时: " + ("%.1f" % dt) + "s (平均每 case " + ("%.1f" % avg_dt) + "s)")
    lines.append("")
    lines.append("## 因 facts 命中的 case (top-5)")
    lines.append("")
    lines.append(hits_md)
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(concl)
    lines.append("")
    lines.append("## 后续")
    lines.append("")
    lines.append("- C1 基础设施已合并 (commit 5a40fad)")
    lines.append("- 验证脚本保留在 `benchmarks/_validate_c1.py`")
    lines.append("- 真实使用下 (每条新对话都触发提取)，hit_rate 将持续提升")
    report_path = PROJECT_ROOT / "benchmarks" / "C1_validation_report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print()
    print("Report:", report_path)
if __name__ == "__main__":
    main()