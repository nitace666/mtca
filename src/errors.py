"""MTCA 用户可见错误（src/errors.py — M2.5.8 A-3）。

定义 :class:`UserFacingError` + 退出码常量 + 异常分类器，
供 CLI / GUI 入口边界把内部异常翻译成"中文 + 可操作建议"。

设计原则：
- 内部模块（src/store/ / src/lifecycle/ / src/llm/ 等）继续抛原始异常
  （RuntimeError / sqlite3.Error / 自定义异常），**不做改造**。
- 仅 CLI/GUI 入口（``main()`` / ``_click_*`` / QMessageBox 调用点）使用本模块。
- 单文件 ≤ 1000 行（实际约 140 行）；不引入新依赖。

公共 API：
- :class:`UserFacingError`
- :data:`EXIT_OK` / :data:`EXIT_USER_ERROR` / :data:`EXIT_DB_ERROR` /
  :data:`EXIT_LLM_ERROR` / :data:`EXIT_INTERNAL`
- :func:`classify_exception` —— 把任意异常翻译为 UserFacingError
- :func:`print_user_error` —— 统一格式打印到 stderr
"""
from __future__ import annotations

import sqlite3
import sys

# click 是 lazy import：errors.py 不强制依赖 click 项目，
# 但要识别 click.BadParameter / MissingParameter 等 UsageError 子类。
try:
    import click as _click  # type: ignore
    _CLICK_USAGE_ERROR = _click.UsageError
    _CLICK_BAD_PARAMETER = _click.BadParameter
    _CLICK_MISSING_PARAMETER = _click.MissingParameter
except ImportError:  # pragma: no cover
    _CLICK_USAGE_ERROR = None  # type: ignore
    _CLICK_BAD_PARAMETER = None  # type: ignore
    _CLICK_MISSING_PARAMETER = None  # type: ignore
from typing import Optional


# ---------------------------------------------------------------------------
# 退出码常量
# ---------------------------------------------------------------------------

EXIT_OK: int = 0
"""成功。"""

EXIT_USER_ERROR: int = 1
"""用户操作错误：参数非法、文件不存在、权限不足等。"""

EXIT_DB_ERROR: int = 2
"""数据库错误：连接失败、完整性约束、OperationalError 等。"""

EXIT_LLM_ERROR: int = 3
"""LLM 调用错误：provider 不可用、网络超时、配额耗尽等。"""

EXIT_INTERNAL: int = 99
"""内部错误：理论上不应该被用户看到，仅作兜底。"""


# ---------------------------------------------------------------------------
# UserFacingError
# ---------------------------------------------------------------------------


class UserFacingError(Exception):
    """用户可见错误，含可操作建议。

    用于 CLI / GUI 入口边界，把内部异常包装成用户友好提示。
    用户不会看到 Python traceback，只会看到 ``message`` + ``suggestion``。

    字段：
        message: 简短中文错误说明（一行）
        suggestion: 可操作建议（一行，可选）
        code: 退出码（默认 :data:`EXIT_USER_ERROR` = 1）
    """

    def __init__(
        self,
        message: str,
        suggestion: Optional[str] = None,
        code: int = EXIT_USER_ERROR,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.suggestion: Optional[str] = suggestion
        self.code: int = code


# ---------------------------------------------------------------------------
# 异常分类器：把任意异常翻译成 UserFacingError
# ---------------------------------------------------------------------------


def classify_exception(exc: BaseException) -> UserFacingError:
    """把任意异常翻译成 :class:`UserFacingError`。

    分类规则（按优先级）：

    1. :class:`UserFacingError` —— 原样返回，保留 message / suggestion / code
    2. :class:`click.exceptions.BadParameter` —— 参数值非法，退出码 1（M2.5.8 A-3.5）
    3. :class:`click.exceptions.MissingParameter` —— 缺少参数，退出码 1
    4. :class:`click.exceptions.UsageError`（其他） —— 参数错误，退出码 1
    5. :class:`FileNotFoundError` —— 文件不存在，退出码 :data:`EXIT_USER_ERROR` (1)
    6. 裸 :class:`sqlite3.DatabaseError` —— 数据库错误，退出码 :data:`EXIT_DB_ERROR` (2)
    7. :class:`ValueError` —— 历史约定退出码 2（向后兼容旧测试）
    8. :class:`PermissionError` —— 历史约定退出码 2（向后兼容 fog 旧测试）
    9. :class:`RuntimeError` 含"数据库 / 完整性 / 操作失败"关键词 —— 数据库错误 (2)
    10. 其他 :class:`RuntimeError` —— 历史约定退出码 2
    11. 其他一切异常 —— 内部错误兜底 (:data:`EXIT_INTERNAL` = 99)

    退出码 2 会被 DB 错误 / ValueError / PermissionError / RuntimeError 共享，
    这是为了向后兼容既有 581 测试（spec 第 8 条"不改既有测试"）。
    用户从 stderr 看到的 message 文本不同，足够区分。
    """
    # 1. UserFacingError 原样返回
    if isinstance(exc, UserFacingError):
        return exc

    # 2/3/4. click.UsageError 子类 —— 参数错误（M2.5.8 A-3.5）
    if _CLICK_USAGE_ERROR is not None and isinstance(exc, _CLICK_USAGE_ERROR):
        param_hint = getattr(exc, "param_hint", None)
        if param_hint is None:
            # click Choice 等场景只传 param 不传 param_hint；自动从 opts/name 推导
            param = getattr(exc, "param", None)
            if param is not None:
                opts = getattr(param, "opts", None) or []
                if opts:
                    param_hint = opts[0]  # 例如 "--period"
                else:
                    param_hint = getattr(param, "name", None) or "未知参数"
            else:
                param_hint = "未知参数"
        raw_msg = getattr(exc, "message", None) or str(exc)
        if _CLICK_BAD_PARAMETER is not None and isinstance(exc, _CLICK_BAD_PARAMETER):
            return UserFacingError(
                message=f"参数 {param_hint} 非法",
                suggestion=f"检查参数值是否合法。{raw_msg}",
                code=EXIT_USER_ERROR,
            )
        if _CLICK_MISSING_PARAMETER is not None and isinstance(exc, _CLICK_MISSING_PARAMETER):
            return UserFacingError(
                message=f"缺少参数 {param_hint}",
                suggestion=f"请提供 {param_hint}。{raw_msg}",
                code=EXIT_USER_ERROR,
            )
        # 其他 UsageError（如 NoSuchOption 等）
        return UserFacingError(
            message=f"参数错误：{param_hint}",
            suggestion=f"检查命令行参数。{raw_msg}",
            code=EXIT_USER_ERROR,
        )

    # 5. FileNotFoundError → EXIT_USER_ERROR (1)
    if isinstance(exc, FileNotFoundError):
        target = getattr(exc, "filename", None) or str(exc) or "未知路径"
        return UserFacingError(
            message=f"文件不存在：{target}",
            suggestion="检查路径是否正确，或文件是否已被删除",
            code=EXIT_USER_ERROR,
        )

    # 3. 裸 sqlite3.DatabaseError（未走 sqlite.py 包装） → EXIT_DB_ERROR (2)
    if isinstance(exc, sqlite3.DatabaseError):
        return UserFacingError(
            message="数据库错误",
            suggestion=(
                "检查 ~/.mtca/mtca.db 是否存在且可写。"
                f"原始错误：{exc}"
            ),
            code=EXIT_DB_ERROR,
        )

    # 4. ValueError → 退出码 2（向后兼容：旧 _click_* 的 catch-all 行为）
    if isinstance(exc, ValueError):
        return UserFacingError(
            message=str(exc) or "参数错误",
            suggestion="检查输入参数是否合法",
            code=2,
        )

    # 5. PermissionError → 退出码 2（向后兼容：fog 旧 _click_* 多包了 PermissionError）
    if isinstance(exc, PermissionError):
        target = getattr(exc, "filename", None) or str(exc) or "未知目标"
        return UserFacingError(
            message=f"权限不足：{target}",
            suggestion="检查文件权限，或用管理员/属主身份运行",
            code=2,
        )

    # 6/7. RuntimeError —— 含 DB 关键词走 DB 错误，否则走业务退出码 2
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        if any(kw in msg for kw in ("数据库", "完整性", "操作失败")):
            return UserFacingError(
                message="数据库错误",
                suggestion=(
                    "检查 ~/.mtca/mtca.db 是否存在且可写。"
                    f"原始错误：{msg}"
                ),
                code=EXIT_DB_ERROR,
            )
        # 业务层 RuntimeError（参数错 / 段不存在等）：退出码 2 向后兼容
        return UserFacingError(
            message=msg or "运行错误",
            suggestion="请检查输入参数或重试；如反复出现请报告此错误",
            code=2,
        )

    # 8. 兜底：一切未预期的异常
    return UserFacingError(
        message=f"内部错误：{type(exc).__name__}",
        suggestion=f"请报告此错误。详细信息：{exc}",
        code=EXIT_INTERNAL,
    )


# ---------------------------------------------------------------------------
# 统一输出
# ---------------------------------------------------------------------------


def print_user_error(err: UserFacingError) -> None:
    """把 :class:`UserFacingError` 按统一格式写到 stderr。

    格式::

        ❌ {message}
        💡 建议：{suggestion}
        （退出码 {code}）

    三行顺序固定，便于脚本解析（grep "❌" / grep "💡" / grep "退出码"）。
    """
    print(f"❌ {err.message}", file=sys.stderr)
    if err.suggestion:
        print(f"💡 建议：{err.suggestion}", file=sys.stderr)
    print(f"（退出码 {err.code}）", file=sys.stderr)


__all__ = [
    "UserFacingError",
    "EXIT_OK",
    "EXIT_USER_ERROR",
    "EXIT_DB_ERROR",
    "EXIT_LLM_ERROR",
    "EXIT_INTERNAL",
    "classify_exception",
    "print_user_error",
]
