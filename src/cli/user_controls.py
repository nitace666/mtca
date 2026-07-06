"""CLI 用户控制（src/cli/user_controls.py — T11 / v0.4）

USER_CONTROLS.md §2 的 4 条命令直达 L0 层：
- /重要：段落 score ← 10000，永远 L1
- /循环 X：打 cycle_tag 跳过时间衰减
- /归档：tier ← L3_hidden，不删 L0 全文
- /雾化：物理擦除 L0-细节，保留骨架 + 锚点句（不可逆）

M2.5.4 新增 3 条紧急追踪命令：
- /紧急 <seg> <时间>：标记为紧急追踪，到期后强制询问
- /完成 <seg>：用户对 expired/tracking 段标记 completed
- /延期 <seg> <时间>：重置过期时间（默认 +7d）

公共 API：
- ``cmd_important(segment_id, path=None) -> int``
- ``cmd_cycle(segment_id, cycle_tag, path=None) -> int``
- ``cmd_archive(segment_id, path=None) -> int``
- ``cmd_fog(segment_id, anchor, path=None) -> bool``
- ``cmd_urgent(segment_id, when, anchor=None, path=None) -> dict``
- ``cmd_done(segment_id, path=None) -> dict``
- ``cmd_postpone(segment_id, when, path=None) -> dict``
- ``parse_command(text) -> (cmd, args) | None``
- ``parse_natural(text) -> (cmd, args) | None``
- ``main()`` click 组装的 CLI 入口 + argparse 风格 fallback
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional, Union

import click

try:
    from src.compress.scoring import archive as _archive
except ImportError:
    from compress.scoring import archive as _archive
try:
    from src.compress.scoring import mark_cycle as _mark_cycle
except ImportError:
    from compress.scoring import mark_cycle as _mark_cycle
try:
    from src.compress.scoring import mark_important as _mark_important
except ImportError:
    from compress.scoring import mark_important as _mark_important
try:
    from src.fog.fog_engine import fog_segment as _fog_segment
except ImportError:
    from fog.fog_engine import fog_segment as _fog_segment
try:
    from src.errors import classify_exception, print_user_error
except ImportError:
    from errors import classify_exception, print_user_error


# ---------------------------------------------------------------------------
# 常量（USER_CONTROLS.md §2 + fog_engine.ANCHOR_MAX_LEN）
# ---------------------------------------------------------------------------

# /雾化 锚点句最大长度（与 fog_engine.ANCHOR_MAX_LEN 对齐）
ANCHOR_MAX_LEN: int = 20

# 命令白名单
_VALID_COMMANDS: frozenset[str] = frozenset({
    "important", "cycle", "archive", "fog",
    "urgent", "done", "postpone",
})

# 前缀命令（斜杠 + 中文）→ 命令名
_PREFIX_TO_CMD: dict[str, str] = {
    "/重要": "important",
    "/循环": "cycle",
    "/归档": "archive",
    "/雾化": "fog",
    "/紧急": "urgent",
    "/完成": "done",
    "/延期": "postpone",
}

# 自然语言中文关键词 → 命令名（用于 parse_natural）
_CN_KEYWORD_TO_CMD: dict[str, str] = {
    "重要": "important",
    "循环": "cycle",
    "归档": "archive",
    "雾化": "fog",
    "紧急": "urgent",
    "完成": "done",
    "延期": "postpone",
}

# 自然语言正则：提取 UUID 风格 segment_id
_SEGMENT_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# 自然语言正则：循环 + tag（如"循环 每周一" / "循环 周一"）
_NATURAL_CYCLE_RE = re.compile(
    r"循环\s*[\u4e00-\u9fffA-Za-z0-9]*?\s*"
    r"(?P<sid>[0-9a-fA-F-]{8,})\s*"
    r"(?P<tag>[\u4e00-\u9fffA-Za-z0-9]+)"
)

# 自然语言正则：雾化 + 锚点（贪婪匹配到行尾 / 标点）
_NATURAL_FOG_RE = re.compile(
    r"雾化\s*(?P<sid>[0-9a-fA-F-]{8,})"
    r"(?:\s*锚点?\s*[::]?\s*(?P<anchor>[^\r\n,，。；;]+))?"
)


# ---------------------------------------------------------------------------
# 4 个命令函数（直接调 src 模块）
# ---------------------------------------------------------------------------


def cmd_important(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """/重要：段落 score=10000，tier=L1。"""
    return _mark_important(segment_id, path=path)


def cmd_cycle(
    segment_id: str,
    cycle_tag: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """/循环 <tag>：段落打 cycle_tag，跳过时间衰减。"""
    return _mark_cycle(segment_id, cycle_tag, path=path)


def cmd_archive(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> int:
    """/归档：段落 tier=L3_hidden，不删 L0 全文。"""
    return _archive(segment_id, path=path)


def cmd_fog(
    segment_id: str,
    anchor: str,
    path: Optional[Union[Path, str]] = None,
) -> bool:
    """/雾化：物理擦除 L0-细节，保留骨架 + 锚点句（不可逆）。"""
    return _fog_segment(segment_id, anchor, called_by="user", path=path)


def cmd_urgent(
    segment_id: str,
    when: str,
    anchor: Optional[str] = None,
    path: Optional[Union[Path, str]] = None,
) -> dict:
    """/紧急 <seg> <时间>：标记段为 urgent_state='tracking'。

    ``when`` 支持：明天/后天/3d/7d/ISO 日期/毫秒戳（见 _time_parse.parse_expires）。
    返回 track_urgent 的结果 dict。
    """
    # 本地相对导入，避免顶层导入链 + 循环
    from src.cli._time_parse import parse_expires
    from src.lifecycle.urgent_tracker import track_urgent
    now_ms = int(time.time() * 1000)
    expires = parse_expires(when, now_ms)
    return track_urgent(segment_id, expires, anchor=anchor, path=path)


def cmd_done(
    segment_id: str,
    path: Optional[Union[Path, str]] = None,
) -> dict:
    """/完成 <seg>：对 expired/tracking 段标 completed（降 L2 + 重写摘要）。"""
    from src.lifecycle.urgent_tracker import handle_urgent_response
    return handle_urgent_response(segment_id, "completed", path=path)


def cmd_postpone(
    segment_id: str,
    when: Optional[str] = None,
    path: Optional[Union[Path, str]] = None,
) -> dict:
    """/延期 <seg> [时间]：重置 expires_at_ms（默认 +7d，由 urgent_tracker.POSTPONE_DAYS）。

    ``when`` 为可选扩展；当前实现走默认 7d 延期（与 M2.5.3 urgent_tracker 兼容）。
    """
    from src.lifecycle.urgent_tracker import handle_urgent_response
    return handle_urgent_response(segment_id, "postponed", path=path)


# ---------------------------------------------------------------------------
# 命令解析
# ---------------------------------------------------------------------------


def _extract_segment_id(text: str) -> Optional[str]:
    """从文本中提取首个 UUID 风格 segment_id，找不到返回 None。"""
    m = _SEGMENT_ID_RE.search(text)
    return m.group(0) if m else None


def parse_command(text: str) -> Optional[tuple[str, list[str]]]:
    """识别 /重要 /循环 /归档 /雾化 前缀命令。

    支持行首 / 行中 / 行尾位置（USER_CONTROLS.md §1："打到任意位置即可识别"）。

    示例：
        "/重要 abc-123"            -> ("important", ["abc-123"])
        "/循环 abc-123 周一"       -> ("cycle", ["abc-123", "周一"])
        "/归档 abc-123"            -> ("archive", ["abc-123"])
        "/雾化 abc-123 --anchor X" -> ("fog", ["abc-123", "--anchor", "X"])
        "请帮我 /重要 abc-123"     -> ("important", ["abc-123"])
        "abc-123 /重要"            -> ("important", ["abc-123"])  # 无 args 也 OK

    返回：
        (command, [args]) —— command ∈ _VALID_COMMANDS
        或 None（无法识别）。
    """
    if not isinstance(text, str) or not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None

    # 用 token 序列扫描，识别任意位置的前缀命令
    tokens = stripped.split()
    for idx, tok in enumerate(tokens):
        cmd = _PREFIX_TO_CMD.get(tok)
        if cmd is not None:
            rest = tokens[idx + 1:]
            return (cmd, rest)

    # 没有显式 /前缀 时，尝试从段中提取 UUID + 关键词（兜底）
    return None


def parse_natural(text: str) -> Optional[tuple[str, list[str]]]:
    """自然语言识别（USER_CONTROLS.md §1："打到任意位置即可识别"）。

    示例：
        "把 abc-123 标记为重要"        -> ("important", ["abc-123"])
        "循环 abc-123 周一"            -> ("cycle", ["abc-123", "周一"])
        "把 abc-123 归档"              -> ("archive", ["abc-123"])
        "雾化 abc-123 关键词 AI 记忆"  -> ("fog", ["abc-123", "关键词AI记忆"])

    返回：
        (command, [args]) 或 None。
    """
    if not isinstance(text, str) or not text:
        return None
    s = text.strip()
    if not s:
        return None

    # 1. /循环 X tag — 优先匹配（结构最特别）
    m = _NATURAL_CYCLE_RE.search(s)
    if m:
        return ("cycle", [m.group("sid"), m.group("tag")])

    # 2. /雾化 id [anchor]
    m = _NATURAL_FOG_RE.search(s)
    if m:
        anchor = m.group("anchor")
        if not anchor:
            anchor = s.replace("雾化", "").strip()[:ANCHOR_MAX_LEN]
        # 锚点归一化：去空白、截断到 ANCHOR_MAX_LEN
        anchor = "".join(anchor.split())[:ANCHOR_MAX_LEN] or "雾化"
        return ("fog", [m.group("sid"), anchor])

    # 3. 关键词优先顺序：重要 > 归档 > 循环 > 雾化
    for kw in ("重要", "归档", "循环", "雾化"):
        if kw in s:
            sid = _extract_segment_id(s)
            if sid:
                cmd = _CN_KEYWORD_TO_CMD[kw]
                return (cmd, [sid])

    return None


# ---------------------------------------------------------------------------
# click 组装的 CLI 入口
# ---------------------------------------------------------------------------


def _echo_ok(msg: str) -> None:
    """统一 OK 输出。"""
    click.echo(f"[OK] {msg}")


def _echo_err(msg: str) -> None:
    """统一错误输出。"""
    click.echo(f"[ERR] {msg}", err=True)


class _UserControlsGroup(click.Group):
    """MTCA CLI 组：把 click.UsageError（含 BadParameter / MissingParameter）
    翻译为中文友好提示（M2.5.8 A-3.5）。
    """

    def invoke(self, ctx):
        """M2.5.8 A-3.5：在子命令 invoke 阶段 catch click.UsageError。
        
        为什么不在 parse_args：Group 的 parse_args 只解析组本身的参数；
        子命令的 parse_args（在子命令的 make_context 内）才是 raise 点。
        所以 catch 必须放在 invoke 里，包裹 super().invoke(ctx) 调用。
        """
        try:
            return super().invoke(ctx)
        except click.UsageError as e:
            wrapped = classify_exception(e)
            print_user_error(wrapped)
            ctx.exit(wrapped.code)



cli = _UserControlsGroup(help="MTCA 用户控制 CLI（T11 + M2.5.4）")


@cli.command("important")
@click.argument("segment_id")
@click.option(
    "--db", "db_path",
    type=click.Path(dir_okay=False), default=None,
    help="数据库路径（默认 ~/.mtca/mtca.db）",
)
def _click_important(segment_id: str, db_path: Optional[str]) -> None:
    """/重要 <segment_id>：score → 10000，tier → L1。"""
    try:
        rows = cmd_important(segment_id, path=db_path)
    except Exception as e:
        wrapped = classify_exception(e)
        print_user_error(wrapped)
        sys.exit(wrapped.code)
    _echo_ok(f"已 /重要 {segment_id}（rows={rows}）")


@cli.command("cycle")
@click.argument("segment_id")
@click.argument("tag")
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None)
def _click_cycle(segment_id: str, tag: str, db_path: Optional[str]) -> None:
    """/循环 <segment_id> <tag>：打 cycle_tag，跳过时间衰减。"""
    try:
        rows = cmd_cycle(segment_id, tag, path=db_path)
    except Exception as e:
        wrapped = classify_exception(e)
        print_user_error(wrapped)
        sys.exit(wrapped.code)
    _echo_ok(f"已 /循环 {tag} {segment_id}（rows={rows}）")


@cli.command("archive")
@click.argument("segment_id")
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None)
def _click_archive(segment_id: str, db_path: Optional[str]) -> None:
    """/归档 <segment_id>：tier → L3_hidden。"""
    try:
        rows = cmd_archive(segment_id, path=db_path)
    except Exception as e:
        wrapped = classify_exception(e)
        print_user_error(wrapped)
        sys.exit(wrapped.code)
    _echo_ok(f"已 /归档 {segment_id}（rows={rows}）")


@cli.command("fog")
@click.argument("segment_id")
@click.option(
    "--anchor", required=True,
    help=f"锚点句（≤{ANCHOR_MAX_LEN} 字）",
)
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None)
def _click_fog(segment_id: str, anchor: str, db_path: Optional[str]) -> None:
    """/雾化 <segment_id> --anchor "..."：物理擦除 L0-细节（不可逆）。"""
    try:
        ok = cmd_fog(segment_id, anchor, path=db_path)
    except Exception as e:
        wrapped = classify_exception(e)
        print_user_error(wrapped)
        sys.exit(wrapped.code)
    _echo_ok(f"已 /雾化 {segment_id} -> {ok}")


@cli.command("urgent")
@click.argument("segment_id")
@click.argument("when")
@click.option("--anchor", default=None, help="紧急事件的简短说明")
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None)
def _click_urgent(
    segment_id: str,
    when: str,
    anchor: Optional[str],
    db_path: Optional[str],
) -> None:
    """/紧急 <segment_id> <when>：标记段为紧急追踪。

    WHEN 支持：明天/后天/3d/7d/ISO 日期/毫秒戳。
    """
    try:
        result = cmd_urgent(segment_id, when, anchor=anchor, path=db_path)
    except Exception as e:
        wrapped = classify_exception(e)
        print_user_error(wrapped)
        sys.exit(wrapped.code)
    _echo_ok(
        f"已 /紧急 {segment_id} → expires={result['expires_at_ms']} (state={result['urgent_state']})"
    )


@cli.command("done")
@click.argument("segment_id")
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None)
def _click_done(segment_id: str, db_path: Optional[str]) -> None:
    """/完成 <segment_id>：用户对 expired/tracking 段标记 completed。"""
    try:
        result = cmd_done(segment_id, path=db_path)
    except Exception as e:
        wrapped = classify_exception(e)
        print_user_error(wrapped)
        sys.exit(wrapped.code)
    _echo_ok(
        f"已 /完成 {segment_id} → state={result['new_state']} tier={result.get('current_tier', '-')}"
    )


@cli.command("postpone")
@click.argument("segment_id")
@click.argument("when", required=False, default=None)
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None)
def _click_postpone(
    segment_id: str,
    when: Optional[str],
    db_path: Optional[str],
) -> None:
    """/延期 <segment_id> [when]：重置过期时间（默认 +7d）。"""
    try:
        result = cmd_postpone(segment_id, when, path=db_path)
    except Exception as e:
        wrapped = classify_exception(e)
        print_user_error(wrapped)
        sys.exit(wrapped.code)
    _echo_ok(
        f"已 /延期 {segment_id} → expires={result.get('expires_at_ms', '-')} (state={result['new_state']})"
    )


# ---------------------------------------------------------------------------
# argparse fallback（click 不可用时）
# ---------------------------------------------------------------------------


def _argparse_main(argv: Optional[list[str]] = None) -> int:
    """argparse 风格 fallback，逻辑与 click 等价。"""
    parser = argparse.ArgumentParser(
        prog="mtca-user",
        description="MTCA 用户控制 CLI（T11，argparse fallback）",
    )
    parser.add_argument(
        "--db", dest="db_path", default=None,
        help="数据库路径（默认 ~/.mtca/mtca.db）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("important", help="/重要")
    p_imp.add_argument("segment_id")

    p_cyc = sub.add_parser("cycle", help="/循环")
    p_cyc.add_argument("segment_id")
    p_cyc.add_argument("tag")

    p_arc = sub.add_parser("archive", help="/归档")
    p_arc.add_argument("segment_id")

    p_fog = sub.add_parser("fog", help="/雾化")
    p_fog.add_argument("segment_id")
    p_fog.add_argument("--anchor", required=True)

    p_urg = sub.add_parser("urgent", help="/紧急")
    p_urg.add_argument("segment_id")
    p_urg.add_argument("when")
    p_urg.add_argument("--anchor", default=None)

    p_done = sub.add_parser("done", help="/完成")
    p_done.add_argument("segment_id")

    p_post = sub.add_parser("postpone", help="/延期")
    p_post.add_argument("segment_id")
    p_post.add_argument("when", nargs="?", default=None)

    args = parser.parse_args(argv)
    db_path = args.db_path
    try:
        if args.cmd == "important":
            rows = cmd_important(args.segment_id, path=db_path)
            print(f"[OK] 已 /重要 {args.segment_id}（rows={rows}）")
        elif args.cmd == "cycle":
            rows = cmd_cycle(args.segment_id, args.tag, path=db_path)
            print(f"[OK] 已 /循环 {args.tag} {args.segment_id}（rows={rows}）")
        elif args.cmd == "archive":
            rows = cmd_archive(args.segment_id, path=db_path)
            print(f"[OK] 已 /归档 {args.segment_id}（rows={rows}）")
        elif args.cmd == "fog":
            ok = cmd_fog(args.segment_id, args.anchor, path=db_path)
            print(f"[OK] 已 /雾化 {args.segment_id} -> {ok}")
        elif args.cmd == "urgent":
            result = cmd_urgent(args.segment_id, args.when, anchor=args.anchor, path=db_path)
            print(f"[OK] 已 /紧急 {args.segment_id} → expires={result['expires_at_ms']} (state={result['urgent_state']})")
        elif args.cmd == "done":
            result = cmd_done(args.segment_id, path=db_path)
            print(f"[OK] 已 /完成 {args.segment_id} → state={result['new_state']} tier={result.get('current_tier', '-')}")
        elif args.cmd == "postpone":
            result = cmd_postpone(args.segment_id, args.when, path=db_path)
            print(f"[OK] 已 /延期 {args.segment_id} → expires={result.get('expires_at_ms', '-')} (state={result['new_state']})")
    except Exception as e:
        wrapped = classify_exception(e)
        print_user_error(wrapped)
        return wrapped.code
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI 入口：click 优先；click 调用失败时回落 argparse。"""
    if len(sys.argv) > 1 and sys.argv[1] == "--argparse":
        # 显式触发 argparse fallback（调试用）
        rc = _argparse_main(sys.argv[2:])
        sys.exit(rc)
    try:
        cli()
    except SystemExit:
        raise
    except Exception:
        # click 自身抛错时回落 argparse（防御性，正常不应触发）
        rc = _argparse_main(sys.argv[1:])
        sys.exit(rc)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "ANCHOR_MAX_LEN",
    "cmd_important", "cmd_cycle", "cmd_archive", "cmd_fog",
    "cmd_urgent", "cmd_done", "cmd_postpone",
    "parse_command", "parse_natural",
    "main", "cli",
]