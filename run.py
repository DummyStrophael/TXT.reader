
==============================================================================
  文件：run.py
==============================================================================
#!/usr/bin/env python
"""文本阅读器启动器：在任意目录下都能正确运行（解决 "No module named pyreader"）。

用法：
    python run.py                      # 启动阅读器
    python run.py sample/晨间随笔.txt   # 启动并导入指定文件
    python run.py --selftest           # 自检（会短暂弹出窗口）
    python run.py --selftest --no-ui-selftest   # 只跑核心用例
    python run.py --data-dir ./data    # 指定数据目录

Windows 用户可直接双击同目录的「启动阅读器.bat」，无需命令行。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))          # 关键：把自己所在目录放进模块搜索路径

LOG = HERE / "pyreader-error.log"


def _report(title: str, detail: str) -> int:
    """把失败原因同时写到日志、日志窗口和弹出的错误框里，避免「一闪而过」。"""
    try:
        LOG.write_text(detail, encoding="utf-8")
    except OSError:
        pass
    sys.stderr.write(detail.rstrip() + "\n")
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, detail.strip()[:1200] + f"\n\n详细日志：\n{LOG}")
        root.destroy()
    except Exception:
        pass
    try:
        if os.name == "nt" and LOG.exists():
            os.startfile(LOG)  # type: ignore[attr-defined]
    except Exception:
        pass
    return 1


def main() -> int:
    try:
        import tkinter  # noqa: F401  仅用于提前判断 GUI 是否可用
    except Exception as exc:  # pragma: no cover
        return _report(
            "缺少 tkinter",
            f"当前 Python 没有可用的 tkinter，无法显示窗口。\n\n{type(exc).__name__}: {exc}\n\n"
            "Windows 解决办法：重新运行 python.org 的安装包 → Modify → 勾选 "
            "「tcl/tk and IDLE」→ 安装完成后重试。",
        )
    # 关键：在创建任何窗口之前声明 DPI 感知，否则 150% 缩放屏上字体是被系统
    # 拉伸的位图，看起来发糊。本文件与 pyreader 包放同一目录即可运行，路径无关。
    if not any(a == "--dpi" or a.startswith("--dpi=") for a in sys.argv[1:]):
        try:
            from pyreader import dpi as dpi_mod

            dpi_mod.enable()
        except Exception:
            pass
    try:
        from pyreader.__main__ import main as app_main

        return app_main(sys.argv[1:])
    except SystemExit as exc:  # argparse 的正常退出
        return int(exc.code or 0)
    except Exception:
        return _report("文本阅读器启动失败", "".join(traceback.format_exception(*sys.exc_info())))


if __name__ == "__main__":
    sys.exit(main())

