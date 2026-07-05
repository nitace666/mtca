"""retention 引擎 (src/lifecycle/retention_engine.py - T20)

实现 MTCA 长期记忆架构中的「时间触发的自动归档」(silence 状态机):

设计要点：
- ``DEFAULT_RETENTION_POLICY`` 4 类默认 retention 天数；健康/财务为永久。
- ``tick()`` 每轮检查 active/dormant 段落的时间，到期降级；/循环 段落跳过。
- ``demote`` 单步下沉；``promote`` 升级且重置 ``promoted_at``；
  ``lock_permanent`` 永不下沉；``force_silent`` 跳 dormant 直 silent。
- ``get_retention_for(category)`` 纯函数策略查询。
- 后台守护：``start_background`` + ``stop_background`` 周期跑 tick。

5 条铁律对齐 V0.4_PIVOT.md §3.6：
1. 升级 = 重新计时 (promoted_at = now)
2. 降级 = 时间衰减 (active → dormant → silent)
3. 双向都允许 (promote/demote 双向)
4. 用户可锁定 (lock_permanent / user_retention_days = PERMANENT)
5. 用户可强制下沉 (force_silent)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

from src.l0.segment_writer import get_segment, update_segment
from src.store.sqlite import query

# ---------------------------------------------------------------------------
# 常量（V0.4_PIVOT.md §3.6 默认策略表）
# ---------------------------------------------------------------------------

# 永久 sentinel：user_retention_days == PERMANENT 时永不衰减
PERMANENT: int = -1

# 默认 retention 策略：键 = 话题分类，值 = 天数 (PERMANENT 表示不衰减)
DEFAULT_RETENTION_POLICY: dict[str, int] = {
    "编程/技术": 365,
    "工作项目": 180,
    "生活/娱乐": 30,
    "健康/财务": PERMANENT,
}

# 兜底 retention 天数（topic_label 无法分类时使用）
FALLBACK_RETENTION_DAYS: int = 90

# dormant 期倍数：dormant → silent 触发 = retention * DORMANT_MULTIPLIER
DORMANT_MULTIPLIER: int = 2

# 后台 tick 默认周期（秒）
DEFAULT_BACKGROUND_INTERVAL: int = 3600

# 合法 silence_state 取值
_VALID_STATES: tuple[str, ...] = ("active", "dormant", "silent")

# 话题分类启发式关键词（按 segment.topic_label 做轻量匹配）
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "编程/技术": (
        "代码", "编程", "开发", "python", "java", "js", "javascript",
        "技术", "bug", "api", "函数", "重构", "部署", "测试", "docker",
        "git", "MTCA", "code", "develop", "tech", "program",
    ),
    "工作项目": (
        "项目", "工作", "任务", "需求", "会议", "客户", "boss",
        "进度", "deadline", "需求评审", "需求变更", "周报",
        "project", "task", "work", "client",
    ),
    "生活/娱乐": (
        "生活", "娱乐", "游戏", "电影", "音乐", "吃", "玩", "旅游",
        "猫", "狗", "周末", "电视剧", "小说", "动漫", "live",
        "life", "movie", "game", "music", "travel",
    ),
    "健康/财务": (
        "健康", "财务", "钱", "医疗", "运动", "饮食", "投资", "理财",
        "保险", "体检", "心理", "睡眠",
        "health", "money", "fitness", "invest", "insurance", "sleep",
    ),
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """返回当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _days_to_ms(days: int) -> int:
    """天 → 毫秒（天 * 24 * 3600 * 1000）。"""
    return int(days) * 24 * 60 * 60 * 1000


def _coerce_int(value: Any, default: int = 0) -> int:
    """规整为 int；None / 非法值回落到 default。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_segment_id(segment_id: Any) -> None:
    """校验 segment_id 合法（用于 raise）。"""
    if not segment_id or not isinstance(segment_id, str):
        raise ValueError("segment_id 必须是非空字符串")


def _get_segment_or_raise(segment_id: str, path: Optional[Union[Path, str]]) -> dict:
    """查询段落，不存在抛 ValueError。"""
    _validate_segment_id(segment_id)
    seg = get_segment(segment_id, path=path)
    if not seg:
        raise ValueError(f"段落不存在：{segment_id}")
    return seg


# ---------------------------------------------------------------------------
# 策略查询
# ---------------------------------------------------------------------------


def get_retention_for(topic_category: str) -> int:
    """查询话题分类对应的 retention 天数。

    参数：
        topic_category: 分类名（如 '编程/技术' / '工作项目' 等）；
            非法或空回退到 ``FALLBACK_RETENTION_DAYS``。

    返回：
        retention 天数（PERMANENT=-1 表示不衰减）。
    """
    if not topic_category or not isinstance(topic_category, str):
        return FALLBACK_RETENTION_DAYS
    return DEFAULT_RETENTION_POLICY.get(
        topic_category.strip(), FALLBACK_RETENTION_DAYS
    )


def _detect_category(topic_label: Optional[str]) -> str:
    """从 ``topic_label`` 启发式识别分类；未命中返回空串。"""
    if not topic_label:
        return ""
    text = str(topic_label).lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw and kw.lower() in text:
                return category
    return ""


def _resolve_retention_days(segment: dict) -> int:
    """解析段落的 retention 天数。

    优先级：
        1. ``user_retention_days`` 用户覆盖 (None/0 = 不覆盖)
        2. ``topic_label`` 启发式分类 → 策略表
        3. ``FALLBACK_RETENTION_DAYS``
    """
    if not segment:
        return FALLBACK_RETENTION_DAYS

    # 1. 用户覆盖
    user_days = segment.get("user_retention_days")
    if user_days is not None:
        coerced = _coerce_int(user_days, default=0)
        if coerced != 0:
            return coerced  # 包含 PERMANENT (-1) 和正整数值

    # 2. 分类启发式
    category = _detect_category(segment.get("topic_label"))
    if category:
        return get_retention_for(category)

    # 3. 兜底
    return FALLBACK_RETENTION_DAYS


# ---------------------------------------------------------------------------
# session 复用：cycle_tag 查询
# ---------------------------------------------------------------------------


def _session_cycle_tag(
    session_id: Optional[str],
    path: Optional[Union[Path, str]] = None,
) -> Optional[str]:
    """读取 sessions.cycle_tag；空时返回 None。"""
    if not session_id:
        return None
    rows = query(
        "SELECT cycle_tag FROM sessions WHERE session_id = ?",
        (session_id,),
        path=path,
    )
    if not rows:
        return None
    tag = rows[0].get("cycle_tag")
    return str(tag).strip() if tag else None


# ---------------------------------------------------------------------------
# 审计（复用 score_events 表，event_type 前缀 retention_*）
# ---------------------------------------------------------------------------


def _log_event(
    segment_id: str,
    event_type: str,
    reason: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """写一条 retention 审计到 ``score_events`` 表。

    event_type 取值：
        - ``retention_demote``    自动 / 手动下沉
        - ``retention_promote``   升级
        - ``retention_force``     强制静默 / 锁定 / 强制下沉
    """
    from src.store.sqlite import execute  # 局部 import 避循环
    return execute(
        "INSERT INTO score_events "
        "(segment_id, event_type, delta, old_score, new_score, reason, created_at) "
        "VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
        (segment_id, event_type, reason, _now_ms()),
        path=path,
    )


# ---------------------------------------------------------------------------
# 用户控制接口
# ---------------------------------------------------------------------------


def demote(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """将段落下沉一档：active → dormant → silent。

    参数：
        segment_id: 目标段落 UUID。
        path: 数据库路径。

    返回：
        更新行数（0=已在 silent，1=已下沉）。

    异常：
        ValueError: segment_id 非法或段落不存在。
    """
    seg = _get_segment_or_raise(segment_id, path=path)
    current = seg.get("silence_state") or "active"
    if current == "silent":
        return 0

    new_state = "dormant" if current == "active" else "silent"
    rows = update_segment(segment_id, path=path, silence_state=new_state)
    _log_event(
        segment_id, "retention_demote",
        f"manual {current}->{new_state}",
        path=path,
    )
    return rows


def promote(
    segment_id: str,
    target: str = "dormant",
    path: Optional[Union[Path, str]] = None,
) -> int:
    """将段落升级并重置 ``promoted_at = now``。

    参数：
        segment_id: 目标段落 UUID。
        target: 目标状态，仅接受 'dormant' 或 'active'。
        path: 数据库路径。

    返回：
        更新行数（0/1）。

    异常：
        ValueError: target 非法 / segment_id 非法 / 段落不存在。
    """
    if target not in ("dormant", "active"):
        raise ValueError(
            f"target 必须是 'dormant' 或 'active'，收到：{target!r}"
        )
    seg = _get_segment_or_raise(segment_id, path=path)
    old_state = seg.get("silence_state") or "active"

    rows = update_segment(
        segment_id, path=path,
        silence_state=target, promoted_at=_now_ms(),
    )
    _log_event(
        segment_id, "retention_promote",
        f"manual {old_state}->{target} (timer_reset)",
        path=path,
    )
    return rows


def lock_permanent(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """锁定段落为「永久活跃」：``user_retention_days = PERMANENT``。

    ``tick()`` 检测到 ``user_retention_days == PERMANENT`` 直接跳过该段。

    异常：
        ValueError: segment_id 非法或段落不存在。
    """
    seg = _get_segment_or_raise(segment_id, path=path)
    rows = update_segment(
        segment_id, path=path,
        user_retention_days=PERMANENT,
    )
    _log_event(
        segment_id, "retention_force",
        "lock_permanent (user_retention_days=-1)",
        path=path,
    )
    return rows


def force_silent(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """将段落强制置为 silent，跳过 dormant。

    异常：
        ValueError: segment_id 非法或段落不存在。
    """
    seg = _get_segment_or_raise(segment_id, path=path)
    old_state = seg.get("silence_state") or "active"
    rows = update_segment(
        segment_id, path=path,
        silence_state="silent",
    )
    _log_event(
        segment_id, "retention_force",
        f"force_silent (skip dormant) old={old_state}",
        path=path,
    )
    return rows


# ---------------------------------------------------------------------------
# tick() 主入口
# ---------------------------------------------------------------------------


def _list_decay_candidates(
    path: Optional[Union[Path, str]] = None,
) -> list[dict]:
    """列出所有可能受 tick 影响的段（active / dormant）。"""
    return query(
        "SELECT segment_id, session_id, silence_state, promoted_at, "
        "user_retention_days, topic_label "
        "FROM segments "
        "WHERE silence_state IN ('active', 'dormant') "
        "AND promoted_at IS NOT NULL",
        path=path,
    )


def tick(
    path: Optional[Union[Path, str]] = None,
    now_ms: Optional[int] = None,
) -> int:
    """每轮检查：active 段超期降 dormant，dormant 段超 2 倍期降 silent。

    跳过规则：
        - ``lock_permanent`` (``user_retention_days == PERMANENT``) → 跳过
        - 分类为永久（健康/财务）的段 → 跳过
        - session 有 ``cycle_tag`` (``/循环``) → 跳过

    参数：
        path: 数据库路径。
        now_ms: 测试用「当前毫秒时间戳」；None 时取真实时间。

    返回：
        实际下沉段数（不含跳过的）。
    """
    now = int(now_ms) if now_ms is not None else _now_ms()
    candidates = _list_decay_candidates(path=path)
    if not candidates:
        return 0

    n_demoted = 0
    for seg in candidates:
        seg_id = seg["segment_id"]
        state = seg.get("silence_state") or "active"
        session_id = seg.get("session_id")

        # /循环 跳过
        if _session_cycle_tag(session_id, path=path):
            continue

        # 永久段（用户锁或分类永久）跳过
        user_days = seg.get("user_retention_days")
        if user_days is not None and _coerce_int(user_days, 0) == PERMANENT:
            continue
        retention = _resolve_retention_days(seg)
        if retention == PERMANENT or retention <= 0:
            continue

        promoted_at = _coerce_int(seg.get("promoted_at"), 0)
        if promoted_at <= 0:
            continue

        age_ms = now - promoted_at

        if state == "active":
            threshold_ms = _days_to_ms(retention)
            if age_ms >= threshold_ms:
                update_segment(seg_id, path=path, silence_state="dormant")
                _log_event(
                    seg_id, "retention_demote",
                    f"tick active->dormant (age={age_ms}ms "
                    f"threshold={threshold_ms}ms retention={retention}d)",
                    path=path,
                )
                n_demoted += 1
        elif state == "dormant":
            threshold_ms = _days_to_ms(retention * DORMANT_MULTIPLIER)
            if age_ms >= threshold_ms:
                update_segment(seg_id, path=path, silence_state="silent")
                _log_event(
                    seg_id, "retention_demote",
                    f"tick dormant->silent (age={age_ms}ms "
                    f"threshold={threshold_ms}ms retention={retention}d*{DORMANT_MULTIPLIER})",
                    path=path,
                )
                n_demoted += 1

    return n_demoted


# ---------------------------------------------------------------------------
# 后台守护（threading.Timer）
# ---------------------------------------------------------------------------


def _background_loop(
    interval_seconds: float,
    path: Optional[Union[Path, str]],
    state: dict,
) -> None:
    """后台循环体：跑一次 tick，再调度下一次。"""
    if state.get("stopped"):
        return
    try:
        tick(path=path)
    except Exception:
        # 静默吞掉异常，避免 daemon 线程崩溃；生产可接入日志
        pass
    if state.get("stopped"):
        return
    timer = threading.Timer(
        float(interval_seconds), _background_loop,
        args=(float(interval_seconds), path, state),
    )
    timer.daemon = True
    state["timer"] = timer
    timer.start()


def start_background(
    interval_seconds: float = DEFAULT_BACKGROUND_INTERVAL,
    path: Optional[Union[Path, str]] = None,
) -> dict:
    """启动后台 tick 守护线程（daemon=True，不阻塞进程退出）。

    参数：
        interval_seconds: 两次 tick 的间隔秒数；默认 3600。
        path: 数据库路径。

    返回：
        一个 dict 句柄，包含 ``stop()`` 方法和 ``timer`` 字段；
        调用 ``stop_background(handle)`` 或 ``handle['stop']()`` 停止。

    说明：
        第一次 tick 在 ``min(0.1, interval_seconds)`` 后执行；
        后续按 ``interval_seconds`` 周期跑。
        线程为 daemon，主进程退出时自动终止。
    """
    state: dict = {"stopped": False, "timer": None}

    def _stop() -> bool:
        state["stopped"] = True
        t = state.get("timer")
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
        return True

    state["stop"] = _stop

    # 第一次立即跑一次（间隔不超过 0.1 秒）
    first_delay = min(0.1, float(interval_seconds))
    timer = threading.Timer(
        first_delay, _background_loop,
        args=(float(interval_seconds), path, state),
    )
    timer.daemon = True
    state["timer"] = timer
    timer.start()
    return state


def stop_background(state: Optional[dict]) -> bool:
    """停止后台 tick 守护。

    参数：
        state: ``start_background`` 返回的句柄；None/空则返回 False。

    返回：
        True=已停止，False=句柄非法。
    """
    if not isinstance(state, dict):
        return False
    stop_fn = state.get("stop")
    if not callable(stop_fn):
        return False
    return bool(stop_fn())


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


__all__ = [
    "PERMANENT", "DEFAULT_RETENTION_POLICY", "FALLBACK_RETENTION_DAYS",
    "DORMANT_MULTIPLIER", "DEFAULT_BACKGROUND_INTERVAL",
    "get_retention_for", "demote", "promote",
    "lock_permanent", "force_silent", "tick",
    "start_background", "stop_background",
]
