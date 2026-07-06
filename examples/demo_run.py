"""examples/demo_run.py — MTCA 一键 5 分钟 体验脚本（M2.5.8 A-2）。

CLI-only、纯 Python（不调 shell mtca 命令）、跨平台（Windows / macOS / Linux）。

调用方式：
    python examples/demo_run.py
    python examples/demo_run.py --keep-db
    python examples/demo_run.py --db /path/to/isolated.db

8 步骤（每步独立函数 + 中文输出 + 第 9 铁律保证）：
  Step 0 环境检查：Python ≥ 3.10 + src/ 可 import + examples/ 可写
  Step 1 隔离 DB 初始化：跑前自动备份（.bak-<ts>），默认 demo DB 在 __demo_db/demo.db
  Step 2 演示：创建 session + 12 消息 → save_segments 造 3 段
  Step 3 4 象限分布
  Step 4 /紧急 标记段 2（明早演示）→ urgent_state='tracking'
  Step 5 /重要 标记段 3（重构）→ current_score=10000
  Step 6 召回 'MTCA 项目'
  Step 7 第 9 铁律验证：URGENT 段 100 tick 后 score ≥ 初始值 95%
  Step 8 收尾：默认清理；--keep-db 保留

约束：
- 不引入新依赖（标准库 + 已装）
- 中文输出 + 中文 docstring
- 不污染 ~/.mtca/mtca.db（隔离 examples/__demo_db/）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional, Union

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.store.sqlite import init_db, query  # noqa: E402
from src.l0.session_writer import create_session, write_message  # noqa: E402
from src.l0.skeleton import build_skeleton, save_skeleton  # noqa: E402
from src.l0.time_segmenter import save_segments  # noqa: E402
from src.l0.quadrant import get_quadrant  # noqa: E402
from src.compress.scoring import mark_important  # noqa: E402
from src.compress.multi_factor_score import calculate_score  # noqa: E402
from src.cli.user_controls import cmd_urgent  # noqa: E402
from src.recall.recall_engine import recall  # noqa: E402


_DEMO_DIR_NAME = "__demo_db"
_DEMO_DB_NAME = "demo.db"
_DAY_MS = 86_400_000
_BACKUP_KEEP_N = 3
_PYTHON_MIN = (3, 10)


_DEMO_TOPICS: list[dict[str, Any]] = [
    {
        "topic": "MTCA 项目立项讨论",
        "segment_hint": "Q4",
        "messages": [
            ("user",      "讨论 MTCA 项目 立项：整体方向 + 定位 + 目标 + 收益。"),
            ("assistant", "讨论 MTCA 项目 核心：跨会话 + 记忆管理 + 定位 + 目标。"),
            ("user",      "讨论 MTCA 项目 差异化：本地 SQLite + 跨会话 + 定位 + 目标。"),
            ("assistant", "讨论 MTCA 项目 里程碑：Demo 阶段 + M2 完整阶段 + 定位目标。"),
        ],
    },
    {
        "topic": "明早客户演示准备",
        "segment_hint": "Q1",
        "messages": [
            ("user",      "讨论 演示 准备：明天客户 + 时间紧 + 录屏 30 秒。"),
            ("assistant", "讨论 演示 命令：CLI 命令 演示 + 30 秒 录屏 + 时间紧。"),
            ("user",      "讨论 客户 关注：数据 隐私 本地化 + 第 9 铁律 永不遗忘。"),
            ("assistant", "讨论 演示 收尾：客户 关注点 + 隐私 本地化 + 第 9 铁律。"),
        ],
    },
    {
        "topic": "重构 USER_GUIDE 设计文档",
        "segment_hint": "Q2",
        "messages": [
            ("user",      "讨论 USER_GUIDE 章节：5 分钟 体验 + 补充 demo_run.py。"),
            ("assistant", "讨论 USER_GUIDE 录屏：30 秒 中英 两版 + demo_run.py。"),
            ("user",      "讨论 USER_GUIDE 测试：现有 576 测试 + 测试 不动。"),
            ("assistant", "讨论 USER_GUIDE 收尾：5 分钟 + 录屏 + demo_run.py + 测试。"),
        ],
    },
]


class DemoError(Exception):
    """demo 流程中可恢复的错误（中文友好提示）。"""


def main(
    db: Optional[Union[Path, str]] = None,
    keep_db: bool = False,
) -> None:
    """demo 主入口。

    参数：
        db: 自定义 DB 路径。None 时使用 examples/__demo_db/demo.db。
        keep_db: True 保留 demo DB 不删；False 跑完清理。

    行为：
        - 顺序执行 8 个 step（每步中文 print）。
        - 顶层 try/except 捕获异常，中文友好提示，不堆 traceback。
    """
    # 清模块级 recall LRU cache（防止同一 DB path 跨次 demo 跑残留空结果）
    try:
        from src.recall import recall_engine as _re
        _re._RECALL_CACHE.clear()
    except Exception:
        pass

    db_path = Path(db) if db else _ROOT / "examples" / _DEMO_DIR_NAME / _DEMO_DB_NAME
    try:
        run_all_steps(db_path=db_path, keep_db=keep_db)
    except DemoError as e:
        print(f"\n[ERR] Demo 失败：{e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[WARN] Demo 被用户中断。", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        print(f"\n[ERR] Demo 意外失败：{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"\n[INFO] demo DB 位置：{db_path}")


def run_all_steps(db_path: Path, keep_db: bool) -> None:
    """8 个 step 顺序执行。"""
    print("MTCA 5 分钟 体验 demo (M2.5.8 A-2)")
    print("=" * 56)

    print("\n[Step 0] 环境检查")
    _step0_env_check()

    print("\n[Step 1] 隔离 DB 初始化")
    _step1_init_db(db_path)

    print("\n[Step 2] 创建 session + 写 3 段演示对话")
    seg_ids = _step2_create_demo_session(db_path)

    print("\n[Step 3] 4 象限分布")
    seg_ids = _step3_quadrant_distribution(db_path, seg_ids)

    print("\n[Step 4] /紧急 命令")
    _step4_urgent(db_path, seg_ids)

    print("\n[Step 5] /重要 命令")
    _step5_important(db_path, seg_ids)

    print("\n[Step 6] 召回 'MTCA 项目'")
    _step6_recall(db_path)

    print("\n[Step 7] 第 9 铁律验证")
    _step7_iron_rule_9(db_path)

    print("\n[Step 8] 收尾")
    _step8_finish(db_path, keep_db=keep_db)

    print("\n[OK] Demo 完成!MTCA 已就绪。")
    print("[DOC] 下一步看 docs/USER_GUIDE.md (中) / docs/USER_GUIDE_EN.md (英)")
    if keep_db:
        print(f"[INFO] --keep-db 已保留 demo DB:{db_path}")
    else:
        print("[INFO] 默认清理:已删除 demo DB(重跑 demo 重新创建)")


def _step0_env_check() -> None:
    """Step 0: Python ≥ 3.10 + 项目根可 import + examples/ 可写。"""
    if sys.version_info < _PYTHON_MIN:
        cur = f"{sys.version_info.major}.{sys.version_info.minor}"
        need = ".".join(str(x) for x in _PYTHON_MIN)
        raise DemoError(f"Python 版本 {cur} 低于最低要求 {need};请安装 3.10+。")

    examples_dir = _ROOT / "examples"
    if not examples_dir.is_dir():
        raise DemoError(f"examples/ 目录不存在:{examples_dir}")
    if not os.access(examples_dir, os.W_OK):
        raise DemoError(f"examples/ 目录不可写:{examples_dir}")

    print(f"  [OK] Python {sys.version.split()[0]} >= {_PYTHON_MIN[0]}.{_PYTHON_MIN[1]}")
    print(f"  [OK] 项目根:{_ROOT}")
    print(f"  [OK] examples/ 可写")


def _step1_init_db(db_path: Path) -> None:
    """Step 1: 跑前自动备份(.bak-<ts>),schema 初始化。"""
    demo_dir = db_path.parent
    demo_dir.mkdir(parents=True, exist_ok=True)

    if db_path.exists():
        backup = demo_dir / f"{db_path.name}.bak-{int(time.time())}"
        shutil.copy2(db_path, backup)
        print(f"  [OK] 旧 DB 已备份:{backup.name}")
        _cleanup_old_backups(demo_dir, db_path.name)
    else:
        print("  [OK] 首次运行(无备份)")

    init_db(db_path).close()
    print(f"  [OK] schema 已初始化:{db_path}")


def _cleanup_old_backups(demo_dir: Path, db_name: str) -> None:
    """保留最近 N 个 .bak 文件,删其余。"""
    pattern = f"{db_name}.bak-*"
    backups = sorted(demo_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[_BACKUP_KEEP_N:]:
        try:
            old.unlink()
            print(f"  [OK] 清理老备份:{old.name}")
        except OSError:
            pass


def _step2_create_demo_session(db_path: Path) -> list[str]:
    """Step 2: 1 session + 12 消息 + 手工 save_segments 造 3 段。

    用 save_segments() 直接构造 3 段(L0-B),绕开 segment_session 的
    jieba-Jaccard 启发式不确定性。仍通过 save_skeleton 完成 L0-骨架。
    返回:非骨架段落 IDs(按写入顺序)。
    """
    topic_label = "MTCA Demo: 立项 / 演示 / 重构"
    session_id = create_session(
        agent_source="demo", topic_label=topic_label, path=db_path
    )
    print(f"  [OK] 已创建 session:{session_id[:8]}...")
    print(f"  [OK] topic:{topic_label}")

    msgs_written = 0
    for topic in _DEMO_TOPICS:
        for role, content in topic["messages"]:
            write_message(session_id=session_id, role=role, content=content, path=db_path)
            msgs_written += 1
    print(f"  [OK] 写入 {msgs_written} 条消息(3 段话题各 4 条)")

    # 1) 落骨架(L0-不可变,幂等)
    skeleton = build_skeleton(session_id, path=db_path)
    save_skeleton(session_id, skeleton, path=db_path)

    # 2) 手工造 3 个段落(L0-B),明确 start/end seq + 对齐 messages 时间
    rows = query(
        "SELECT seq, created_at FROM messages WHERE session_id = ? ORDER BY seq ASC",
        (session_id,), path=db_path,
    )
    seg_dicts: list[dict[str, Any]] = []
    for i, topic in enumerate(_DEMO_TOPICS):
        n_msgs = len(topic["messages"])
        start_seq = i * n_msgs + 1
        end_seq = (i + 1) * n_msgs
        start_at = int(rows[start_seq - 1]["created_at"])
        end_at = int(rows[end_seq - 1]["created_at"])
        seg_dicts.append({
            "session_id": session_id,
            "start_msg_seq": start_seq,
            "end_msg_seq": end_seq,
            "start_at": start_at,
            "end_at": end_at,
            "message_count": n_msgs,
            "weak_merged": False,
            "gap_to_next": 60,  # 演示用:每段间 60 分钟
        })
    para_ids = save_segments(session_id, seg_dicts, path=db_path)

    print(f"  [OK] 已创建 session,含 {len(para_ids)} 段演示对话(+ 1 骨架)")
    if len(para_ids) < 3:
        print(f"  [WARN] 段数 {len(para_ids)} < 3")
    return para_ids


def _step3_quadrant_distribution(db_path: Path, seg_ids: list[str]) -> list[str]:
    """Step 3: 统计 4 象限分布;返回非骨架段落 IDs。"""
    counters = {"Q1": 0, "Q2": 0, "Q3": 0, "Q4": 0}
    real_seg_ids: list[str] = []
    for sid in seg_ids:
        rows = query(
            "SELECT urgency_level, importance_level FROM segments WHERE segment_id = ?",
            (sid,), path=db_path,
        )
        if not rows:
            continue
        urg = float(rows[0].get("urgency_level") or 0.5)
        imp = float(rows[0].get("importance_level") or 0.5)
        qx = get_quadrant(urgency=urg, importance=imp)
        counters[qx] += 1
        real_seg_ids.append(sid)

    print(f"  [QUAD] 4 象限分布: "
          f"Q1={counters['Q1']}, Q2={counters['Q2']}, "
          f"Q3={counters['Q3']}, Q4={counters['Q4']}")
    return real_seg_ids


def _step4_urgent(db_path: Path, seg_ids: list[str]) -> None:
    """Step 4: 对段 2(明早演示,第 2 段)调 /紧急,明天到期。"""
    if len(seg_ids) < 2:
        raise DemoError(f"段数 {len(seg_ids)} 不足 2,无法演示 /紧急")

    target_seg_id = seg_ids[1]
    result = cmd_urgent(
        segment_id=target_seg_id, when="明天", path=db_path,
    )
    state = result.get("urgent_state", "?")
    expires = result.get("expires_at_ms", 0)
    expires_str = _format_ms(expires)
    print(f"  [URG] 已设段 2 为紧急:urgent_state={state}, expires_at={expires_str}")


def _format_ms(ms: int) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD HH:MM' 本地时区字符串。"""
    if not ms:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))
    except (OSError, ValueError, OverflowError):
        return str(ms)


def _step5_important(db_path: Path, seg_ids: list[str]) -> None:
    """Step 5: 对段 3(重构,第 3 段)调 /重要 → score=10000。"""
    if len(seg_ids) < 3:
        raise DemoError(f"段数 {len(seg_ids)} 不足 3,无法演示 /重要")

    target_seg_id = seg_ids[2]
    rows = mark_important(segment_id=target_seg_id, path=db_path)

    after = query(
        "SELECT current_score, current_tier FROM segments WHERE segment_id = ?",
        (target_seg_id,), path=db_path,
    )
    if not after:
        raise DemoError("/重要 写完后查不到段(异常)")
    score = float(after[0].get("current_score") or 0.0)
    tier = after[0].get("current_tier", "?")
    print(f"  [IMP] 段 3 已标 /重要:rows={rows}, current_score={score:.0f}, current_tier={tier}")


def _step6_recall(db_path: Path) -> None:
    """Step 6: recall('MTCA 项目') 返表。"""
    results = recall(query="MTCA 项目", path=db_path)
    n = len(results)
    print(f"  [REC] 召回 'MTCA 项目' 返回 {n} 段:")
    if not results:
        return
    print("    " + "-" * 64)
    print(f"    {'segment_id':<10} {'tier':<9} {'score':>8}  emotion")
    print("    " + "-" * 64)
    for r in results[:5]:
        sid = str(r.get("segment_id", ""))[:8]
        score = float(r.get("score") or 0.0)
        tier = str(r.get("tier", "L?"))
        msgs = r.get("messages") or []
        emo = "-"
        for m in msgs:
            em = m.get("emotion_tag")
            if em:
                emo = str(em)
                break
        print(f"    {sid:<10} {tier:<9} {score:>8.2f}  {emo}")
    print("    " + "-" * 64)


def _step7_iron_rule_9(db_path: Path) -> None:
    """Step 7: URGENT 段 + /重要 段都不衰减。

    URGENT:f_time=1.0(base 也不衰减),100 tick 后 score ≥ 初始 95%。
    /重要:current_score ≥ 10000 → 锁定 L1,任何 tick 后不变。
    """
    # URGENT 段验证
    urgent_rows = query(
        "SELECT * FROM segments WHERE urgent_state = 'tracking' LIMIT 1",
        path=db_path,
    )
    if not urgent_rows:
        raise DemoError("没找到 URGENT 段(Step 4 应已标记)")
    urgent_seg = dict(urgent_rows[0])
    promoted_at = int(urgent_seg.get("promoted_at") or 0)
    initial_score, _ = calculate_score(urgent_seg, promoted_at)
    future_score, _ = calculate_score(urgent_seg, promoted_at + 100 * _DAY_MS)
    decay_ok = future_score >= initial_score * 0.95
    status_u = "OK" if decay_ok else "FAIL"
    print(f"  [IRON9] URGENT 段 100 tick 后 score={future_score:.2f} "
          f"(初始 {initial_score:.2f}, 不衰减 {status_u})")
    if not decay_ok:
        raise DemoError(
            f"第 9 铁律失守:URGENT 段衰减 initial={initial_score:.2f} → after={future_score:.2f}"
        )

    # /重要 段验证
    important_rows = query(
        "SELECT * FROM segments WHERE current_score >= 10000.0 LIMIT 1",
        path=db_path,
    )
    if not important_rows:
        raise DemoError("没找到 /重要 段(Step 5 应已标记)")
    imp_seg = dict(important_rows[0])
    imp_initial = float(imp_seg.get("current_score") or 0.0)
    imp_future, tier = calculate_score(imp_seg, promoted_at + 100 * _DAY_MS)
    imp_locked = abs(imp_future - imp_initial) < 0.01 and tier == "L1"
    status_i = "OK" if imp_locked else "FAIL"
    print(f"  [IRON9] /重要 段 100 tick 后 score={imp_future:.0f} "
          f"(初始 {imp_initial:.0f}, tier={tier}, 锁分 {status_i})")
    if not imp_locked:
        raise DemoError(
            f"第 9 铁律失守:/重要 段分数变化 initial={imp_initial:.0f} → after={imp_future:.0f}"
        )


def _step8_finish(db_path: Path, keep_db: bool) -> None:
    """Step 8: 清理(默认删 DB + .bak 留最近 3 个)。"""
    if keep_db:
        print(f"  [KEEP] --keep-db:保留 demo DB 与 .bak 备份")
        return

    demo_dir = db_path.parent
    if db_path.exists():
        try:
            db_path.unlink()
            print(f"  [OK] 已删除 demo DB:{db_path.name}")
        except OSError as e:
            print(f"  [WARN] 删 DB 失败:{e}", file=sys.stderr)
    else:
        print("  [OK] demo DB 不存在,跳过清理")

    _cleanup_old_backups(demo_dir, db_path.name)


def _build_parser() -> argparse.ArgumentParser:
    """argparse: --keep-db + --db。"""
    p = argparse.ArgumentParser(
        prog="mtca-demo",
        description="MTCA 一键 5 分钟 体验 (M2.5.8 A-2)",
    )
    p.add_argument(
        "--keep-db",
        action="store_true",
        help="跑完保留 demo DB 不删(默认清理)",
    )
    p.add_argument(
        "--db",
        dest="db_path",
        type=Path,
        default=None,
        help="自定义 DB 路径(默认 examples/__demo_db/demo.db)",
    )
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    main(db=args.db_path, keep_db=args.keep_db)


__all__ = ["main", "DemoError", "run_all_steps"]