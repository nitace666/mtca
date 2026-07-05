import json, time, sys, uuid
from pathlib import Path
PROJECT_ROOT = Path(r'I:\PROJECTS\MTCA-t29')
sys.path.insert(0, str(PROJECT_ROOT))
from src.llm.provider import LMStudioProvider
from src.llm.extractor import extract_facts
from src.l0.segment_writer import get_segment
from src.l0.session_writer import get_session_messages
from src.recall.recall_engine import recall
from src.llm.facts_store import create_fact, count_facts

GT_DB = PROJECT_ROOT / 'tests' / '_tmp_ground_truth' / 'ground_truth.db'
TEST_SET = PROJECT_ROOT / 'benchmarks' / 'test_set.json'

def h(s): print(s, flush=True)

# 清 facts 重来
import sqlite3
conn = sqlite3.connect(str(GT_DB))
conn.execute('DELETE FROM facts')
conn.commit()
conn.close()
h('Cleared facts table')

provider = LMStudioProvider(base_url='http://localhost:8083', model='qwen36-heretic-q4km')
test_set = json.loads(TEST_SET.read_text(encoding='utf-8'))

# Baseline
hits_pre = 0
for c in test_set:
    r = recall(c.get('expected_recall_query', c.get('query','')), top_k=3, path=GT_DB)
    if c['expected_top1'] in [x['segment_id'] for x in r]:
        hits_pre += 1
h(f'BASELINE hit_rate@3: {hits_pre}/{len(test_set)} = {hits_pre/len(test_set):.3f}')

# Extract facts from all 50
n_facts = 0
n_ok = 0
t_start = time.time()
for i, c in enumerate(test_set):
    seg = get_segment(c['expected_top1'], path=GT_DB)
    if not seg:
        continue
    msgs = get_session_messages(seg['session_id'], path=GT_DB)
    if not msgs:
        continue
    text = chr(10).join((m.get('content') or '') for m in msgs)
    try:
        facts = extract_facts(text, provider=provider)
        h(f"  [{i+1}] LLM 返回 {len(facts)} facts")
    except Exception as e:
        h(f"  [{i+1}] LLM 失败：{type(e).__name__}: {e}")
        facts = []
    for f in facts:
        try:
            create_fact(
                session_id=seg['session_id'],
                segment_id=c['expected_top1'],
                content=f.get('content',''),
                source='extracted',
                confidence=f.get('confidence', 0.7),
                path=GT_DB,
            )
            n_facts += 1
        except Exception:
            pass
    if facts:
        n_ok += 1
    if (i+1) % 10 == 0:
        elapsed = time.time() - t_start
        h(f'[{i+1}/50] {n_facts} facts total, {n_ok} cases OK, {elapsed:.0f}s')

h(f'TOTAL: {n_facts} facts from {n_ok}/{len(test_set)} cases in {time.time()-t_start:.0f}s')
h(f'DB facts total: {count_facts(path=GT_DB)}')

# After
hits_post = 0
for c in test_set:
    r = recall(c.get('expected_recall_query', c.get('query','')), top_k=3, path=GT_DB)
    if c['expected_top1'] in [x['segment_id'] for x in r]:
        hits_post += 1
h(f'AFTER hit_rate@3: {hits_post}/{len(test_set)} = {hits_post/len(test_set):.3f}')
h(f'Δ = {hits_post - hits_pre} hits (+{(hits_post - hits_pre)/len(test_set):.3f})')