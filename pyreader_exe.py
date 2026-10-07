
==============================================================================
  文件：pyreader_exe.py
==============================================================================
"""PyInstaller 打包入口（模块相对导入在脚本模式下不可用，所以用这个薄壳）。

用法：
    TextReader.exe                # 启动阅读器
    TextReader.exe --selftest     # 自检（GUI 版无控制台，看不到输出，请用 SelfTest.exe）

打包好的 GUI 版没有控制台：一旦启动过程抛异常，用户看到的就是「双击没反应」。
所以这里统一兜住异常 —— 写日志 + 弹错误框，绝不让失败静默。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _base_dir() -> Path:
    """exe 所在目录（打包后）；源码运行时是脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _report_crash(detail: str) -> int:
    log = _base_dir() / "启动失败-日志.txt"
    try:
        log.write_text(detail, encoding="utf-8")
    except OSError:
        log = None  # type: ignore[assignment]
    sys.stderr.write(detail)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "文本阅读器启动失败",
            detail.strip()[-1200:] + (f"\n\n完整日志：\n{log}" if log else ""),
        )
        root.destroy()
    except Exception:
        pass
    try:
        if log is not None and sys.platform == "win32":
            import os

            os.startfile(log)                 # type: ignore[attr-defined]
    except Exception:
        pass
    return 1


if __name__ == "__main__":
    here = _base_dir()
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    try:
        from pyreader import dpi as dpi_mod

        dpi_mod.enable()          # 必须在创建任何窗口之前
        from pyreader.__main__ import main

        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:         # noqa: BLE001 - 兜住一切，转成可见的错误
        sys.exit(_report_crash("".join(traceback.format_exception(*sys.exc_info()))))

