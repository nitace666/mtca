# -*- coding: utf-8 -*-
"""USER_GUIDE GUI 截图 wrapper（一次性，不入生产）。

启动 GUI + 切到各 Tab + Qt.grab() 截图 + 退出。
用法：python _capture_gui.py <demo.db> <out_dir> [cn|en]
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 把 worktree 根加进 sys.path
WORKTREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKTREE))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from gui.main_window import MainWindow


def main():
    if len(sys.argv) < 3:
        print("usage: _capture_gui.py <demo.db> <out_dir>")
        return 1
    db_path = sys.argv[1]
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("MTCA")
    app.setOrganizationName("MTCA")

    win = MainWindow()
    win.resize(1280, 800)
    win._db_path = db_path
    win._timeline.set_db_path(db_path)
    win._detail.set_db_path(db_path)
    win._quadrant.set_db_path(db_path)
    win._graph.set_db_path(db_path)
    win._db_label.setText(f"DB：{db_path}")
    win._timeline.refresh()
    win._quadrant.refresh()
    win.show()

    # 截图顺序：tab 索引 → 文件名
    steps = [
        (0, "tab0_timeline.png"),    # 时间线
        (1, "tab1_detail.png"),      # 详情
        (2, "tab2_graph.png"),       # 知识图谱
        (3, "tab3_actions.png"),     # 操作
        (4, "tab4_quadrant.png"),    # 4 象限
    ]

    def grab(idx, fname):
        win._tabs.setCurrentIndex(idx)
        QApplication.processEvents()
        for _ in range(5):
            QApplication.processEvents()
            time.sleep(0.1)
        pix = win.grab()
        out = out_dir / fname
        pix.save(str(out))
        print(f"saved: {out.name}  size={out.stat().st_size}")

    def step_iter(i):
        if i >= len(steps):
            # 主窗口整图（默认 Tab 0）
            pix = win.grab()
            main_out = out_dir / "main_window.png"
            pix.save(str(main_out))
            print(f"saved: {main_out.name}  size={main_out.stat().st_size}")
            app.quit()
            return
        idx, fname = steps[i]
        grab(idx, fname)
        QTimer.singleShot(200, lambda: step_iter(i + 1))

    QTimer.singleShot(1500, lambda: step_iter(0))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
