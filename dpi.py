
==============================================================================
  文件：pyreader/dpi.py
==============================================================================
"""高 DPI（清晰度）支持。

问题：Windows 在 150% 缩放时，如果进程没有声明 DPI 感知，系统会把窗口先按
96 DPI 渲染成位图、再整体放大 1.5 倍 —— 于是字全是糊的。

修法：在**创建任何窗口之前**声明 DPI 感知（PerMonitorV2 → PerMonitor → System 逐级降级），
此后 Tk 直接按真实 DPI（如 144）渲染，字体原生清晰；界面里所有像素常量再用
ReaderApp.S() 乘上 DPI 比例，保证窗口与控件的物理尺寸不变。

命令行自检：
    python -m pyreader.dpi
"""

from __future__ import annotations

import sys
from typing import Optional

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

_enabled: Optional[str] = None


def enable() -> str:
    """声明 DPI 感知，返回使用的方式（重复调用返回首次结果）。

    必须在 tkinter.Tk() 之前调用；一旦进程里已经创建过窗口，Windows 会拒绝修改。
    """
    global _enabled
    if _enabled is not None:
        return _enabled
    if sys.platform != "win32":
        _enabled = "non-win32（由系统/桌面环境处理）"
        return _enabled

    import ctypes

    user32 = ctypes.windll.user32
    try:  # Windows 10 1703+：每显示器感知 v2
        if user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            _enabled = "PerMonitorV2"
            return _enabled
    except Exception:
        pass
    try:  # Windows 8.1+：每显示器感知
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            _enabled = "PerMonitor"
            return _enabled
    except Exception:
        pass
    try:  # Vista+：系统级感知
        if user32.SetProcessDPIAware():
            _enabled = "System"
            return _enabled
    except Exception:
        pass
    _enabled = "未启用（系统将缩放位图，界面可能偏糊）"
    return _enabled


def main() -> int:
    """打印当前显示环境，便于确认清晰度是否生效。"""
    import tkinter as tk
    import tkinter.font as tkfont

    how = enable()
    root = tk.Tk()
    root.withdraw()
    dpi = root.winfo_fpixels("1i")
    scaling = float(root.tk.call("tk", "scaling"))
    font = tkfont.Font(root=root, family="Microsoft YaHei UI", size=19)
    print(f"DPI 感知方式 : {how}")
    print(f"系统 DPI     : {dpi:.0f}（96 = 100%，144 = 150%）")
    print(f"Tk 缩放      : {scaling:.3f}")
    print(f"19pt 行高    : {font.metrics('linespace')} px（原生像素，无系统拉伸）")
    print(f"屏幕逻辑尺寸 : {root.winfo_screenwidth()}x{root.winfo_screenheight()}")
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

