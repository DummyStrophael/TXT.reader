
==============================================================================
  文件：pyreader/__main__.py
==============================================================================
"""命令行入口：python -m pyreader"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from typing import List, Optional

from . import dpi as dpi_mod
from . import selftest as selftest_mod
from .app import ReaderApp
from .storage import Library, Settings, default_data_dir, migrate_legacy_data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pyreader", description="文本阅读器（Python + tkinter）")
    parser.add_argument("files", nargs="*", help="启动后直接导入这些文本文件")
    parser.add_argument("--data-dir", help="数据目录（默认：程序目录\\data，便携模式）")
    parser.add_argument("--selftest", action="store_true", help="运行自检后退出（不进入主循环）")
    parser.add_argument("--no-ui-selftest", action="store_true", help="自检时跳过 GUI 用例")
    parser.add_argument("--dpi", choices=("auto", "off"), default="auto",
                        help="是否声明 DPI 感知以获得清晰字体（off 可强制关闭，默认 auto）")
    args = parser.parse_args(argv)

    # 必须在创建任何窗口之前声明 DPI 感知，否则 150% 缩放下字会被系统拉伸变糊
    if args.dpi == "auto":
        dpi_mod.enable()

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()

    if args.selftest:
        # 自检会在数据目录里建测试书籍：没有显式指定 --data-dir 时一律用临时目录，
        # 绝不污染用户真实书架。
        test_dir = Path(args.data_dir).expanduser() if args.data_dir else None
        return selftest_mod.run(data_dir=test_dir, ui=not args.no_ui_selftest)

    # 老版本数据在 %APPDATA%：第一次以便携模式运行时复制过来，老目录保留
    migrated = migrate_legacy_data(data_dir)

    root = tk.Tk()
    library = Library(data_dir / "library.db")
    settings = Settings(data_dir / "settings.json")
    app = ReaderApp(root, library, settings, str(data_dir))
    if migrated:
        app.status(f"已把原书架迁移到：{data_dir}")
    if args.files:
        root.after(120, lambda: app.import_paths(list(args.files)))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

