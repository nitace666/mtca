"""MTCA GUI 启动入口（gui/run.py — T13）。

启动方式：

.. code-block:: bash

    python gui/run.py

可选参数：
    --db PATH     指定 SQLite 数据库路径（默认 ~/.mtca/mtca.db）

设计要点：
- 不依赖任何 MTCA 业务模块做参数解析（避免 import 过早失败）；
  argparse 只接收 DB 路径。
- 主窗口构造前显式 setStyle，让 Windows 上字体不至于太丑；不做
  任何额外美化（M1 不堆 UI）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow
from src.store.sqlite import MTCA_DB_PATH


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """极简参数解析：仅 DB 路径。"""
    parser = argparse.ArgumentParser(
        prog="mtca-gui",
        description="MTCA GUI MVP（T13）",
    )
    parser.add_argument(
        "--db", dest="db_path", default=None,
        help=f"数据库路径（默认 {MTCA_DB_PATH}）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """启动 Qt 应用并显示主窗口。"""
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("MTCA")
    app.setOrganizationName("MTCA")

    win = MainWindow()
    if args.db_path:
        win._db_path = args.db_path
        win._timeline.set_db_path(args.db_path)
        win._detail.set_db_path(args.db_path)
        win._db_label.setText(f"DB：{args.db_path}")
    win._timeline.refresh()
    win.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]