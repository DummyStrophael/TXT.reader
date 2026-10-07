
==============================================================================
  文件：pyreader/theme.py
==============================================================================
"""配色与主题令牌。

思路与 Web 版一致：主题不是一个布尔开关，而是由「背景色 + 文字色」两个输入
推导出整套 UI 令牌（外壳、分隔线、次要文字、悬停色、强调色）。这样自定义配色
时，书架 / 工具栏 / 目录等外壳部分依然协调，不会出现「正文是深色、外壳还是浅灰」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

RGB = Tuple[int, int, int]


@dataclass(frozen=True)
class Palette:
    bg: str  # 正文背景
    fg: str  # 正文文字
    chrome: str  # 外壳（工具栏 / 书架 / 侧栏）背景
    line: str  # 分隔线
    soft: str  # 次要文字
    hover: str  # 悬停 / 激活背景
    accent: str  # 强调色（按钮、进度、选中）
    on_accent: str  # 强调色上的文字色
    dark: bool

    def as_dict(self) -> Dict[str, str]:
        return {
            "bg": self.bg, "fg": self.fg, "chrome": self.chrome, "line": self.line,
            "soft": self.soft, "hover": self.hover, "accent": self.accent,
            "on_accent": self.on_accent,
        }


# 6 套预设，和 Web 版一一对应
THEMES: Dict[str, Dict[str, str]] = {
    "paper": {"name": "纸白", "bg": "#faf8f5", "fg": "#2b2b2b", "accent": "#8a6a3b"},
    "sepia": {"name": "米黄", "bg": "#f5ecd9", "fg": "#4a3b28", "accent": "#96702f"},
    "green": {"name": "护眼绿", "bg": "#e4eee0", "fg": "#2f3a2c", "accent": "#4b7a3f"},
    "gray": {"name": "淡灰", "bg": "#ecf0f2", "fg": "#2c3238", "accent": "#3d6b8e"},
    "night": {"name": "夜间", "bg": "#1c1f24", "fg": "#c9ced7", "accent": "#7aa2f7"},
    "ink": {"name": "纯黑", "bg": "#000000", "fg": "#b9bec7", "accent": "#8ab4f8"},
}


# --------------------------------------------------------------------------
# 颜色运算（全部基于 sRGB 的简单线性混合，够用且无依赖）
# --------------------------------------------------------------------------


def hex_to_rgb(value: str) -> RGB:
    s = str(value).lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    s = (s + "000000")[:6]
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def rgb_to_hex(rgb: RGB) -> str:
    return "#" + "".join(f"{max(0, min(255, int(round(v)))):02x}" for v in rgb)


def luminance(color: str) -> float:
    r, g, b = (v / 255 for v in hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def mix(a: str, b: str, t: float) -> str:
    """把 a 向 b 混合 t（0~1）。"""
    ca, cb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(tuple(ca[i] + (cb[i] - ca[i]) * t for i in range(3)))  # type: ignore[arg-type]


def shade(color: str, amount: float) -> str:
    """amount > 0 提亮（向白），< 0 压暗（向黑）。"""
    return mix(color, "#ffffff" if amount > 0 else "#000000", abs(amount))


def contrast_text(color: str) -> str:
    """在给定底色上选择可读的文字色。"""
    return "#1a1a1a" if luminance(color) > 0.55 else "#ffffff"


def hsv_to_hex(hue: float, sat: float, val: float) -> str:
    """HSV(0~360, 0~1, 0~1) → '#rrggbb'。用于给每本书生成稳定的书脊颜色。"""
    import colorsys

    r, g, b = colorsys.hsv_to_rgb((float(hue) % 360.0) / 360.0,
                                  max(0.0, min(1.0, float(sat))),
                                  max(0.0, min(1.0, float(val))))
    return rgb_to_hex((r * 255.0, g * 255.0, b * 255.0))


def derive_palette(bg: str, fg: str, accent: str = "", dark: Optional[bool] = None) -> Palette:
    if dark is None:
        dark = luminance(bg) < 0.42
    acc = accent or mix(fg, "#7aa2f7" if dark else "#b07d3c", 0.55)
    return Palette(
        bg=bg,
        fg=fg,
        chrome=shade(bg, 0.05 if dark else -0.035),
        line=shade(bg, 0.13 if dark else -0.11),
        soft=mix(fg, bg, 0.42),
        hover=shade(bg, 0.09 if dark else -0.06),
        accent=acc,
        on_accent=contrast_text(acc),
        dark=dark,
    )


def palette_for(settings: Dict) -> Palette:
    """根据设置得到基础调色板（不含护眼叠加）。"""
    key = settings.get("theme", "paper")
    if key == "custom":
        return derive_palette(settings.get("custom_bg", "#faf8f5"), settings.get("custom_fg", "#2b2b2b"))
    t = THEMES.get(key, THEMES["paper"])
    return derive_palette(t["bg"], t["fg"], t["accent"])


def eye_care_palette(base: Palette) -> Palette:
    """护眼模式：在任意主题之上做暖色降蓝光 + 降低对比度。

    tkinter 没有全局滤镜，所以这里等价地做两件事：
      1) 背景向暖色（浅色主题偏米黄、深色主题偏暖棕）偏移；
      2) 文字向背景靠拢一点，降低反差，长时间阅读更柔和。
    因为是对调色板做变换，所以它能和 6 套预设以及自定义配色自由叠加。
    """
    if base.dark:
        bg = mix(base.bg, "#3a2c1c", 0.45)
        fg = mix(base.fg, bg, 0.12)
    else:
        bg = mix(base.bg, "#ffe6bd", 0.22)
        fg = mix(base.fg, bg, 0.14)
    acc = mix(base.accent, "#b8862f" if not base.dark else "#d9a441", 0.35)
    return Palette(
        bg=bg, fg=fg,
        chrome=mix(base.chrome, bg, 0.55),
        line=mix(base.line, bg, 0.35),
        soft=mix(base.soft, bg, 0.25),
        hover=mix(base.hover, bg, 0.35),
        accent=acc,
        on_accent=contrast_text(acc),
        dark=base.dark,
    )


def effective_palette(settings: Dict) -> Palette:
    base = palette_for(settings)
    return eye_care_palette(base) if settings.get("eye_care") else base

