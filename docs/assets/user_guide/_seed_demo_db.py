# -*- coding: utf-8 -*-
"""USER_GUIDE demo seed script (one-shot, not committed in production)."""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
DEMO_DB = SCRIPT_DIR / "demo.db"
SESSION_ID = str(uuid.uuid4())


def _ts(year, month, day, hour, minute=0):
    dt = datetime(year, month, day, hour, minute)
    return int(dt.timestamp() * 1000)


def init_schema(conn):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from src.store.sqlite import _SCHEMA_SQL, _migrate_to_v2, _apply_pragmas
    _apply_pragmas(conn)
    conn.executescript(_SCHEMA_SQL)
    _migrate_to_v2(conn)
    conn.commit()


def seed_session(conn):
    started_at = _ts(2026, 7, 6, 9, 0)
    conn.execute(
        "INSERT INTO sessions(session_id, started_at, ended_at, topic_label, agent_source) VALUES(?, ?, ?, ?, ?)",
        (SESSION_ID, started_at, _ts(2026, 7, 6, 13, 45), "USER_GUIDE 演示数据", "user_guide_demo"),
    )
    conn.commit()


def seed_segments(conn):
    today = _ts(2026, 7, 6, 9, 0)
    expired_ms = _ts(2026, 7, 3, 12, 0)
    tracking_expire_ms = _ts(2026, 7, 6, 17, 0)

    rows = [
        (0,   "今天天气不错适合散步",         0.30, 0.30, None, None,               "clear",      None,                            "L0"),
        (15,  "超市要买鸡蛋、牛奶、面包",     0.85, 0.30, None, None,               "clear",      None,                            "L0"),
        (30,  "明天客户演示的 PPT 还没改完",   0.85, 0.85, None, None,               "clear",      None,                            "L0"),
        (45,  "读完《思考，快与慢》第 5 章",   0.30, 0.85, None, None,               "clear",      None,                            "L0"),
        (60,  "今天 17:00 前必回的客户邮件",   0.95, 0.85, "tracking", tracking_expire_ms, "clear", None,                            "L0"),
        (75,  "中午想吃什么还没想好",         0.30, 0.30, None, None,               "clear",      None,                            "L0"),
        (90,  "月底前必须提交的方案 v2",      0.85, 0.85, None, None,               "clear",      None,                            "L0"),
        (105, "项目核心技术选型：SQLite + FTS5", 0.50, 0.95, "important", None,      "clear",      None,                            "L0"),
        (120, "本周 OKR：完成 4 象限模块",     0.30, 0.85, None, None,               "clear",      None,                            "L0"),
        (135, "提醒：下午 3 点开空调",         0.85, 0.30, None, None,               "clear",      None,                            "L0"),
        (150, "等法务审核的合同今天要看一眼",  0.85, 0.85, None, None,               "clear",      None,                            "L0"),
        (165, "听说隔壁组换了咖啡机",         0.30, 0.30, None, None,               "clear",      None,                            "L0"),
        (180, "上周五本该完成的季度复盘报告",   0.85, 0.85, "expired", expired_ms,   "clear",      None,                            "L0"),
        (195, "临时加的紧急会议 14:00",       0.95, 0.85, None, None,               "clear",      None,                            "L0"),
        (210, "健身计划：本周 3 次力量训练",   0.30, 0.85, None, None,               "clear",      None,                            "L0"),
        (225, "某次体检报告（已雾化）",       0.30, 0.50, None, None,               "fogged_once","体检异常指标",                  "L0"),
        (240, "下班顺路取快递",               0.85, 0.30, None, None,               "clear",      None,                            "L0"),
    ]

    out = []
    fog_now_ms = _ts(2026, 7, 6, 13, 30)
    for offset, topic, urg, imp, urgent_state, expires_ms, fog_state, fog_anchor, tier in rows:
        seg_id = str(uuid.uuid4())
        start_ms = today + offset * 60 * 1000
        end_ms = start_ms + 5 * 60 * 1000
        conn.execute(
            "INSERT INTO segments(segment_id, session_id, start_msg_seq, end_msg_seq, start_at, end_at, topic_label, current_tier, current_score, fog_state, fog_at, fog_anchor, silence_state, ref_count, urgency_level, importance_level, emotion_tag, expires_at_ms, urgent_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seg_id, SESSION_ID, 1, 1, start_ms, end_ms, topic, tier, 100.0,
             fog_state, fog_now_ms if fog_state == "fogged_once" else None,
             fog_anchor, "active", 0,
             urg, imp, None, expires_ms, urgent_state),
        )
        out.append({"id": seg_id, "topic": topic, "urg": urg, "imp": imp, "urgent_state": urgent_state, "fog_state": fog_state})
    conn.commit()
    return out


def quadrant(urg, imp):
    if imp >= 0.7 and urg >= 0.7: return "Q1"
    if imp >= 0.7: return "Q2"
    if urg >= 0.7: return "Q3"
    return "Q4"


def main():
    if DEMO_DB.exists():
        DEMO_DB.unlink()
        print(f"[del] removed old {DEMO_DB.name}")

    print(f"[init] schema + M2.5.1 migration -> {DEMO_DB}")
    conn = sqlite3.connect(str(DEMO_DB))
    init_schema(conn)

    print("[seed] session + 17 segments")
    seed_session(conn)
    rows = seed_segments(conn)

    from collections import Counter
    quad_count = Counter(quadrant(r["urg"], r["imp"]) for r in rows)
    urgent_count = Counter(r["urgent_state"] for r in rows if r["urgent_state"])
    fog_count = Counter(r["fog_state"] for r in rows if r["fog_state"] != "clear")

    print()
    print("--- done ---")
    print(f"DB: {DEMO_DB}")
    print(f"Session: {SESSION_ID}")
    print(f"4 quadrants: Q1={quad_count['Q1']} Q2={quad_count['Q2']} Q3={quad_count['Q3']} Q4={quad_count['Q4']}")
    print(f"URGENT: tracking={urgent_count.get('tracking',0)} expired={urgent_count.get('expired',0)} important={urgent_count.get('important',0)}")
    print(f"/fog: {fog_count.get('fogged_once',0)}")
    conn.close()
    print()
    print(f"launch GUI: python gui/run.py --db {DEMO_DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
