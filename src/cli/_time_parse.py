"""src/cli/_time_parse.py — M2.5.4 CLI 时间解析

支持：
  - 相对时间：明天 / 后天 / 下周 / 下月 / 尽快 / 马上 / 3d / 7d
  - ISO 日期：2026-07-10 / 2026-07-10T15:30
  - 毫秒戳：1710000000000
  - 空字符串 → 抛 ValueError
"""
import re
from datetime import datetime

# 相对时间别名 → 天数
_RELATIVE_DAYS = {
    "明天": 1, "后天": 2, "下周": 7, "下月": 30,
    "尽快": 0, "马上": 0, "立刻": 0,
    "1d": 1, "3d": 3, "7d": 7, "30d": 30,
}

# 数字 + d 形式正则（如 "5d"）
_D_RE = re.compile(r"^(\d+)d$", re.IGNORECASE)

_MILLIS_PER_DAY = 86400 * 1000


def parse_expires(text, now_ms):
    """文本 → 毫秒时间戳。raise ValueError 当无法解析。"""
    if not text or not text.strip():
        raise ValueError("时间字符串为空")
    s = text.strip().lower()
    if s.isdigit() and len(s) >= 10:
        return int(s)
    if s in _RELATIVE_DAYS:
        days = _RELATIVE_DAYS[s]
        return now_ms + days * _MILLIS_PER_DAY
    m = _D_RE.match(s)
    if m:
        days = int(m.group(1))
        return now_ms + days * _MILLIS_PER_DAY
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(
        "无法解析时间：%r（支持：明天/后天/3d/7d/ISO 日期/毫秒戳）" % text
    )


__all__ = ["parse_expires"]