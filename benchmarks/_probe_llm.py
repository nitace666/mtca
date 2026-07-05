import sys, time
sys.path.insert(0, ".")
from src.llm.provider import LMStudioProvider
from src.llm.extractor import build_extraction_prompt, _parse_facts_from_text
from src.l0.segment_writer import get_segment
from src.l0.session_writer import get_session_messages
provider = LMStudioProvider(base_url="http://localhost:8083", model="qwen36-heretic-q4km")
seg = get_segment("e29aa506-eae0-4875-b067-3d8d6ed64972", path="tests/_tmp_ground_truth/ground_truth.db")
print("session:", seg["session_id"])
msgs = get_session_messages(seg["session_id"], path="tests/_tmp_ground_truth/ground_truth.db")
text_msgs = [{"role": m["role"], "content": m["content"]} for m in msgs if m.get("content") is not None]
print("text msgs:", len(text_msgs))
parts = []
for m in text_msgs:
    parts.append("[" + m["role"] + "] " + m["content"])
text = "\n".join(parts)
prompt = build_extraction_prompt(text)
t0 = time.time()
raw = provider.generate(prompt["user"], max_tokens=512)
dt = time.time() - t0
print("--- RAW LLM OUTPUT (", len(raw), "chars,", round(dt,1), "s) ---")
print(raw[:2500])
print("--- END ---")
facts = _parse_facts_from_text(raw)
print("parsed facts:", len(facts))
for f in facts[:5]:
    print("  -", f)
