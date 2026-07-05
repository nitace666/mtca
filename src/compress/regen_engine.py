"""MTCA 摘要重写引擎（src/compress/regen_engine.py - T24）

L1/L2/L3 视图应随 L0 变化自动重写。V0.4_PIVOT.md §3.9 +
DATA_MODEL.md §7：
- 异步队列（threading.Queue），不阻塞主交互
- L0-骨架 + 当前视图 + reason → LLM 重新生成
- 失败保留旧视图，最多重试 3 次
- GUI 手动刷新；LLM 不可用时降级（标 stale 不重写）

触发场景：fog / supersede / promote / manual。

公共 API：
- ``schedule_regen(segment_id, reason='manual', path=None) -> int``
- ``regen_view(view_id, reason='manual', provider=None, path=None,
  max_retries=DEFAULT_MAX_RETRIES) -> dict``
- ``process_queue(max_items=100, provider=None, path=None) -> int``
- ``manual_refresh(segment_id, provider=None, path=None) -> int``
- ``start_background_worker(provider=None, path=None) -> Thread | None``
- ``stop_background_worker(timeout=2.0) -> bool``
- ``get_queue_size() -> int``
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Union

from src.l0.segment_writer import get_segment
from src.store.sqlite import execute, query


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VALID_REASONS: tuple[str, ...] = (
    "fog", "supersede", "promote", "manual",
)
DEFAULT_MAX_RETRIES: int = 3
_WORKER_POLL_INTERVAL: float = 0.2
_RETRY_BACKOFF_MAX: float = 1.0
_RETRY_BACKOFF_BASE: float = 0.05

_PROMPT_TEMPLATE: str = (
    "你是「压缩记忆」专家。基于 L0-骨架 + 旧摘要 + 重写原因，重新"
    "生成 L{tiertag} 摘要。\n"
    "\n"
    "[重写原因] {reason}\n"
    "[原因说明] {reason_hint}\n"
    "\n"
    "[话题标题] {topic}\n"
    "[锚点句] {anchor}\n"
    "\n"
    "[旧摘要]\n{old_content}\n"
    "\n"
    "输出新的 L{tiertag} 摘要（中文，简洁，≤200 字；保留关键事实；"
    "不要编造雾化后已不存在的内容）。"
)

_REASON_HINTS: dict[str, str] = {
    "fog": "L0-细节已物理擦除，只剩 L0-骨架；旧摘要里提到的事实不应再展开。",
    "supersede": "此方案已被新方案取代；新摘要要点出被取代状态并指向替代者。",
    "promote": "段落从沉默/半沉睡升级到活跃；请把摘要从简版恢复为详细版。",
    "manual": "用户手动触发强制刷新；正常压缩即可。",
}

_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
_worker_state: dict[str, Any] = {
    "thread": None, "stop_event": None,
    "provider": None, "path": None,
}
_worker_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """返回当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _coerce_int(value: Any, default: int = 0) -> int:
    """规整字段为 int；非法值回落。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_reason(reason: Any) -> str:
    """校验 reason；非法抛 ValueError。"""
    if not isinstance(reason, str):
        raise ValueError("reason 必须是字符串")
    norm = reason.strip().lower()
    if norm not in VALID_REASONS:
        raise ValueError(
            f"reason 必须是 {VALID_REASONS} 之一，收到：{reason!r}"
        )
    return norm


def _validate_segment_id(segment_id: Any) -> str:
    """校验 segment_id 合法。"""
    if not segment_id or not isinstance(segment_id, str):
        raise ValueError("segment_id 必须是非空字符串")
    return str(segment_id)


def _validate_view_id(view_id: Any) -> str:
    """校验 view_id 合法。"""
    if not view_id or not isinstance(view_id, str):
        raise ValueError("view_id 必须是非空字符串")
    return str(view_id)


def _tier_number(tier: Any) -> str:
    """从 tier='L1' / 'L2' / 'L3' / 'L3_hidden' 抽数字部分。"""
    if not tier:
        return "1"
    s = str(tier).strip()
    if len(s) >= 2 and s[0].upper() == "L" and s[1].isdigit():
        return s[1]
    return "1"


def _build_prompt(seg: dict, view: dict, reason: str) -> str:
    """构造重写 prompt（含 reason 和 reason_hint）。"""
    tier_num = _tier_number(view.get("tier"))
    topic = (seg.get("topic_label") or "").strip()
    anchor = (seg.get("fog_anchor") or "").strip()
    old = (view.get("content") or "").strip()
    hint = _REASON_HINTS.get(reason, "正常压缩。")
    return _PROMPT_TEMPLATE.format(
        tiertag=tier_num,
        reason=reason,
        reason_hint=hint,
        topic=topic or "（未命名话题）",
        anchor=anchor or "（无锚点）",
        old_content=old or "（旧摘要为空）",
    )


def _call_provider(provider: Any, prompt: str) -> str:
    """调用 provider.generate；返回字符串。任何异常向上抛。"""
    if provider is None:
        raise RuntimeError("LLM provider 未配置")
    if not hasattr(provider, "generate"):
        raise RuntimeError("LLM provider 必须实现 generate() 方法")
    return str(provider.generate(prompt))


def _get_view(view_id: str, path) -> Optional[dict]:
    rows = query(
        "SELECT * FROM views WHERE view_id = ?",
        (view_id,),
        path=path,
    )
    return rows[0] if rows else None


def _write_audit(
    segment_id: str,
    reason_text: str,
    path,
    now_ms: int,
) -> None:
    """写 regen 审计；失败吞掉。"""
    try:
        execute(
            "INSERT INTO score_events "
            "(segment_id, event_type, delta, old_score, "
            "new_score, reason, created_at) "
            "VALUES (?, 'regen_done', NULL, NULL, NULL, ?, ?)",
            (segment_id, reason_text, now_ms),
            path=path,
        )
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# schedule_regen：标 stale + 入队
# ---------------------------------------------------------------------------


def schedule_regen(
    segment_id: str,
    reason: str = "manual",
    path: Optional[Union[Path, str]] = None,
) -> int:
    """把段落下未过期视图标 stale + 入异步队列。返回入队数量。

    异常：segment_id/reason 不合法 → ValueError；段落不存在 → ValueError。
    """
    sid = _validate_segment_id(segment_id)
    norm_reason = _validate_reason(reason)
    seg = get_segment(sid, path=path)
    if not seg:
        raise ValueError(f"段落不存在：segment_id={sid}")

    now = _now_ms()
    # 仅标未过期视图为 stale；过期视图不再复活
    execute(
        "UPDATE views "
        "SET is_stale = 1, stale_reason = ? "
        "WHERE segment_id = ? "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (norm_reason, sid, now),
        path=path,
    )
    stale_views = query(
        "SELECT view_id FROM views "
        "WHERE segment_id = ? AND is_stale = 1 "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (sid, now),
        path=path,
    )
    n = 0
    for v in stale_views:
        vid = v.get("view_id")
        if not vid:
            continue
        _queue.put({
            "view_id": str(vid),
            "segment_id": sid,
            "reason": norm_reason,
            "scheduled_at": now,
        })
        n += 1
    return int(n)


# ---------------------------------------------------------------------------
# regen_view：单视图重写（带重试）
# ---------------------------------------------------------------------------


def regen_view(
    view_id: str,
    reason: str = "manual",
    provider: Any = None,
    path: Optional[Union[Path, str]] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """重写单个 view：调 LLM 生新 view，旧 view expires_at=now。

    流程：加载 view+段 → 拼 prompt → 调 provider（重试 max_retries 次）
    → INSERT 新 view + UPDATE 旧 expires_at + 写审计；失败抛
    RuntimeError，旧 view 保持原状。

    返回：``{view_id, new_view_id, tier, regen_count, reason, status}``。
    """
    vid = _validate_view_id(view_id)
    norm_reason = _validate_reason(reason)
    safe_retries = max(0, _coerce_int(max_retries, DEFAULT_MAX_RETRIES))

    view = _get_view(vid, path=path)
    if not view:
        raise ValueError(f"视图不存在：view_id={vid}")

    segment_id = str(view.get("segment_id") or "")
    if not segment_id:
        raise ValueError(f"视图缺少 segment_id：view_id={vid}")
    seg = get_segment(segment_id, path=path)
    if not seg:
        raise ValueError(f"视图关联段落不存在：segment_id={segment_id}")

    prompt = _build_prompt(seg, view, norm_reason)

    # 重试 LLM（指数退避）
    last_exc: Optional[BaseException] = None
    new_content: Optional[str] = None
    for attempt in range(safe_retries + 1):
        try:
            new_content = _call_provider(provider, prompt)
            break
        except Exception as exc:
            last_exc = exc
            if attempt < safe_retries:
                time.sleep(min(
                    _RETRY_BACKOFF_BASE * (2 ** attempt),
                    _RETRY_BACKOFF_MAX,
                ))
                continue
            break

    if new_content is None:
        raise RuntimeError(
            f"重写视图失败：view_id={vid}, reason={norm_reason}, "
            f"retries={safe_retries}, last_err={last_exc!r}"
        )

    now = _now_ms()
    new_view_id = str(uuid.uuid4())
    old_regen_count = _coerce_int(view.get("regen_count"), 0)
    new_regen_count = old_regen_count + 1
    tier = str(view.get("tier") or "L1")

    # 提交：INSERT 新 view + UPDATE 旧 view expires_at
    execute(
        "INSERT INTO views "
        "(view_id, segment_id, tier, content, "
        "created_at, expires_at, is_stale, stale_reason, regen_count) "
        "VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, ?)",
        (new_view_id, segment_id, tier, new_content,
         now, new_regen_count),
        path=path,
    )
    execute(
        "UPDATE views SET expires_at = ? WHERE view_id = ?",
        (now, vid),
        path=path,
    )
    _write_audit(
        segment_id,
        f"regen view={vid[:8]}->{new_view_id[:8]} "
        f"reason={norm_reason} count={new_regen_count}",
        path=path,
        now_ms=now,
    )

    return {
        "view_id": vid,
        "new_view_id": new_view_id,
        "tier": tier,
        "regen_count": int(new_regen_count),
        "reason": norm_reason,
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# process_queue：消费队列（同步入口）
# ---------------------------------------------------------------------------


def _drain_queue(
    max_items: int,
    provider: Any,
    path: Optional[Union[Path, str]],
) -> int:
    """从模块队列拉任务并 regen；最多 max_items 个。返回成功数。"""
    safe_max = max(1, _coerce_int(max_items, 100))
    n_ok = 0
    for _ in range(safe_max):
        try:
            task = _queue.get_nowait()
        except queue.Empty:
            break
        try:
            regen_view(
                task.get("view_id", ""),
                reason=task.get("reason", "manual"),
                provider=provider,
                path=path,
            )
            n_ok += 1
        except (RuntimeError, ValueError, TypeError):
            # 单项失败不阻塞队列
            pass
        finally:
            _queue.task_done()
    return int(n_ok)


# ---------------------------------------------------------------------------
# process_queue：消费队列（同步入口）
# ---------------------------------------------------------------------------


def process_queue(
    max_items: int = 100,
    provider: Any = None,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """拉最多 ``max_items`` 个任务并跑 ``regen_view``；返回成功数。"""
    return _drain_queue(max_items, provider, path)


# ---------------------------------------------------------------------------
# manual_refresh：GUI 同步入口
# ---------------------------------------------------------------------------


def manual_refresh(
    segment_id: str,
    provider: Any = None,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """GUI「刷新摘要」按钮：标 stale + 同步重写全部视图，返回成功数。"""
    n_scheduled = schedule_regen(segment_id, reason="manual", path=path)
    return _drain_queue(n_scheduled, provider, path)


# ---------------------------------------------------------------------------
# 后台 worker
# ---------------------------------------------------------------------------


def _worker_loop(
    provider: Any,
    path: Optional[Union[Path, str]],
    stop_event: threading.Event,
) -> None:
    """后台 worker 主循环：阻塞拉队列，直到 stop_event 置位。"""
    while not stop_event.is_set():
        try:
            task = _queue.get(timeout=_WORKER_POLL_INTERVAL)
        except queue.Empty:
            continue
        try:
            regen_view(
                task.get("view_id", ""),
                reason=task.get("reason", "manual"),
                provider=provider,
                path=path,
            )
        except (RuntimeError, ValueError, TypeError):
            pass
        finally:
            _queue.task_done()


def start_background_worker(
    provider: Any = None,
    path: Optional[Union[Path, str]] = None,
) -> Optional[threading.Thread]:
    """启动后台 daemon 线程持续消费队列；已运行则直接返回句柄。"""
    with _worker_lock:
        existing = _worker_state.get("thread")
        if existing is not None and existing.is_alive():
            return existing
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_worker_loop,
            args=(provider, path, stop_event),
            name="regen-engine-worker",
            daemon=True,
        )
        _worker_state["thread"] = thread
        _worker_state["stop_event"] = stop_event
        _worker_state["provider"] = provider
        _worker_state["path"] = path
        thread.start()
        return thread


def stop_background_worker(timeout: float = 2.0) -> bool:
    """优雅停止后台 worker；返回是否成功 join。"""
    safe_timeout = max(
        0.1, float(timeout) if isinstance(timeout, (int, float)) else 2.0,
    )
    with _worker_lock:
        thread = _worker_state.get("thread")
        stop_event = _worker_state.get("stop_event")
        if thread is None:
            return True
        if stop_event is not None:
            stop_event.set()
        thread.join(timeout=safe_timeout)
        alive = thread.is_alive()
        if not alive:
            _worker_state["thread"] = None
            _worker_state["stop_event"] = None
        return not alive


def get_queue_size() -> int:
    """返回当前队列深度。"""
    return int(_queue.qsize())


def reset_for_tests() -> None:
    """清空队列 + 停止 worker。仅供测试。"""
    stop_background_worker()
    while not _queue.empty():
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            break


__all__ = [
    "VALID_REASONS", "DEFAULT_MAX_RETRIES",
    "schedule_regen", "regen_view", "process_queue", "manual_refresh",
    "start_background_worker", "stop_background_worker", "get_queue_size",
    "reset_for_tests",
]
