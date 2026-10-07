
==============================================================================
  文件：pyreader/app.py
==============================================================================
"""阅读器主窗口：布局、渲染、交互。

分层：textproc（纯逻辑）→ storage（持久化）→ state（发布订阅）→ app（tkinter UI）。
本文件只做「把 state 画到控件上」和「把用户操作翻译成 state 变更」两件事，
所有渲染函数都用「渲染指纹」判重，避免滚动/进度落盘时重建正文控件。
"""

from __future__ import annotations

import os
import bisect
import re
import subprocess
import sys
import time
import tkinter as tk
from dataclasses import replace
from tkinter import colorchooser, filedialog, font as tkfont, messagebox
from typing import Callable, Dict, List, Optional, Tuple

from . import textproc, theme
from .state import Store
from .storage import DEFAULT_SETTINGS, Book, BookMeta, Library, Settings

try:                                   # 壁纸/纹理模块：缺失时自动降级为「无壁纸」
    from . import wallpaper
except Exception:                      # pragma: no cover
    wallpaper = None                   # type: ignore[assignment]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# 以下尺寸均以「96 DPI 的逻辑像素」书写，使用时统一乘 ReaderApp.scale
# （见 ReaderApp.S）。这样在 125% / 150% / 200% 缩放下物理尺寸一致，
# 配合 dpi.enable() 的原生渲染，文字既清晰又不会变小。
WINDOW_DEFAULT = (960, 680)
WINDOW_MIN = (720, 480)
SHELF_WIDTH = 288
SIDEBAR_WIDTH = 306
SCROLLBAR_WIDTH = 11
SHELF_SCROLLBAR_WIDTH = 10
PAGE_PAD_Y = 30
PAGE_PAD_X_MIN = 18
GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-])(\d+)([+-])(\d+))?$")

FONT_SIZE_RANGE = (12, 34)
LINE_HEIGHT_RANGE = (1.4, 2.6)
PAGE_WIDTH_RANGE = (520, 1040)

SERIF_STACK = ("Source Han Serif SC", "Noto Serif CJK SC", "Songti SC", "SimSun", "宋体",
               "Georgia", "Times New Roman")
SANS_STACK = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
              "Hiragino Sans GB", "Segoe UI", "DejaVu Sans")
UI_STACK = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
            "Hiragino Sans GB", "Segoe UI", "DejaVu Sans")

SAVE_DELAY_MS = 650      # 进度落盘防抖
SETTINGS_DELAY_MS = 300  # 设置落盘防抖

EMPTY_HINT_TITLE = "开始你的阅读"
EMPTY_HINT_BODY = (
    "导入本地 .txt / .md 文件，或直接粘贴文本。\n"
    "书架、字号、背景色与阅读进度都会保存在本机。"
)
EMPTY_HINT_KEYS = "Ctrl+O 导入 · Ctrl+B 书架 · Ctrl+L 目录 · Ctrl+, 设置 · Ctrl+E 护眼 · Ctrl+= / Ctrl+- 字号"


def elide(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ReaderApp:
    """整个应用。对外暴露 action_* / set_settings / open_book 等方法便于自动化测试。"""

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    def __init__(self, root: tk.Tk, library: Library, settings_file: Settings, data_dir: str):
        self.root = root
        self.lib = library
        self.settings_file = settings_file
        self.data_dir = data_dir

        # 高 DPI：先声明 DPI 感知（由入口在创建窗口前调用 dpi.enable()），
        # 此时 winfo_fpixels('1i') 会返回真实 DPI（150% → 144；未声明感知则恒为 96）。
        self.dpi = float(root.winfo_fpixels("1i"))
        self.scale = max(1.0, self.dpi / 96.0)

        initial = settings_file.load()
        self.store = Store({
            "books": [],
            "books_rev": 0,
            "current_id": None,
            "current": None,        # Book
            "article": None,        # textproc.Article
            "chapters": [],
            "settings": initial,
            "sidebar": initial.get("sidebar"),
            "progress": 0.0,
        })

        self._fp = {"reader": object(), "shelf": object(), "sidebar": object(), "toc": object()}
        self._sig_theme = None
        self._sig_type = None
        self._sig_wall = None
        self._styleable: List[Tuple[tk.Widget, str]] = []
        self._swatch_borders: Dict[str, tk.Frame] = {}
        self._swatch_buttons: Dict[str, tk.Frame] = {}

        self._ratio = 0.0            # 已读比例（视口底部字符 / 总字符，到底=1.0）
        self._char_offset = 0        # 视口首行首字符在全文中的下标（排版无关的精确位置）
        self._line_offsets: List[int] = []      # 每个逻辑行的首字符下标
        self._chapter_offsets: List[Tuple[int, int]] = []   # [(章节下标, 字符下标)]
        self._total_chars = 1
        self._restoring = False      # 恢复位置期间不回写，避免「越开越偏」
        self._chapter_lines: List[Tuple[int, int]] = []   # 兼容字段（旧引用）
        self._save_job: Optional[str] = None
        self._settings_job: Optional[str] = None
        self._status_job: Optional[str] = None
        self._pad_job: Optional[str] = None
        self._desk_job: Optional[str] = None
        self._wallpaper_job: Optional[str] = None
        self._desk_last_size: Tuple[int, int] = (0, 0)
        self._toc_sync = False
        self._paste_undo_id: Optional[str] = None      # 上一次粘贴导入的书（Ctrl+Z 撤销）

        self.pal = theme.effective_palette(initial)
        self.surface = theme.mix(self.pal.chrome, self.pal.bg, 0.6)
        self.chrome = self.pal.chrome          # 外壳色（有壁纸时会融入壁纸）
        self.texture: Optional[tk.PhotoImage] = None
        self.error_color = "#ff8679" if self.pal.dark else "#b3402f"

        self._init_fonts()
        self._build_ui()
        self._apply_theme(force=True)
        self._bind_keys()
        self.store.subscribe(self._on_state)
        self._load_library()
        # 窗口映射后再做视觉初始化（壁纸/纸纹需要真实尺寸）
        self.root.after(60, self._init_visuals)
        # 窗口映射之后再落一次设置：既让 settings.json 从首次启动就存在，
        # 也避免在窗口尚未映射时把 "1x1+0+0" 这种占位几何写进配置。
        self.root.after(SETTINGS_DELAY_MS + 200, self._flush_settings)

    # ------------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------------

    def S(self, px: float) -> int:
        """96 DPI 逻辑像素 → 本机物理像素。"""
        return max(1, int(round(px * self.scale)))

    @property
    def settings(self) -> Dict:
        """始终返回当前设置（state 是唯一数据源）。"""
        return self.store.state["settings"]

    @property
    def current(self) -> Optional[Book]:
        return self.store.state["current"]

    # ------------------------------------------------------------------
    # 字体
    # ------------------------------------------------------------------

    def _init_fonts(self) -> None:
        families = set(tkfont.families(self.root))

        def pick(stack: Tuple[str, ...], fallback: str) -> str:
            for name in stack:
                if name in families:
                    return name
            return fallback

        self.ui_family = pick(UI_STACK, "TkDefaultFont")
        self.read_family = {
            "serif": pick(SERIF_STACK, self.ui_family),
            "sans": pick(SANS_STACK, self.ui_family),
        }
        size = int(self.settings["font_size"])
        self.font_body = tkfont.Font(root=self.root, family=self.read_family["serif"], size=size)
        self.font_h1 = tkfont.Font(root=self.root, family=self.read_family["serif"], size=size + 8, weight="bold")
        self.font_h2 = tkfont.Font(root=self.root, family=self.read_family["serif"], size=size + 3, weight="bold")
        self.font_meta = tkfont.Font(root=self.root, family=self.ui_family, size=max(9, size - 5))
        self.font_empty_h = tkfont.Font(root=self.root, family=self.ui_family, size=size + 2, weight="bold")
        self.font_empty_p = tkfont.Font(root=self.root, family=self.ui_family, size=max(10, size - 3))
        self.font_ui = tkfont.Font(root=self.root, family=self.ui_family, size=10)
        self.font_ui_bold = tkfont.Font(root=self.root, family=self.ui_family, size=10, weight="bold")
        self.font_ui_small = tkfont.Font(root=self.root, family=self.ui_family, size=9)
        self.font_title = tkfont.Font(root=self.root, family=self.ui_family, size=11, weight="bold")

    def _sync_fonts(self) -> None:
        s = self.settings
        size = int(s["font_size"])
        family = self.read_family.get(s["font_family"], self.read_family["serif"])
        self.font_body.configure(family=family, size=size)
        self.font_h1.configure(family=family, size=size + 8)
        self.font_h2.configure(family=family, size=size + 3)
        self.font_meta.configure(size=max(9, size - 5))
        self.font_empty_h.configure(size=size + 2)
        self.font_empty_p.configure(size=max(10, size - 3))

    # ------------------------------------------------------------------
    # 控件工厂（带主题注册，改配色时统一重刷）
    # ------------------------------------------------------------------

    def _reg(self, widget: tk.Widget, role: str) -> tk.Widget:
        self._styleable.append((widget, role))
        return widget

    def _frame(self, parent: tk.Widget, role: str = "chrome.bg", **kw) -> tk.Frame:
        return self._reg(tk.Frame(parent, bd=0, highlightthickness=0, **kw), role)  # type: ignore[return-value]

    def _label(self, parent: tk.Widget, text: str = "", role: str = "chrome.fg", **kw) -> tk.Label:
        return self._reg(tk.Label(parent, text=text, bd=0, highlightthickness=0, **kw), role)  # type: ignore[return-value]

    def _button(self, parent: tk.Widget, text: str, command: Callable[[], None],
                role: str = "chrome.btn", **kw) -> tk.Button:
        kw.setdefault("padx", self.S(10))
        kw.setdefault("pady", self.S(4))
        return self._reg(tk.Button(parent, text=text, command=command, bd=0, highlightthickness=0,
                                   cursor="hand2", **kw), role)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.title("文本阅读器")
        root.geometry(self._restore_geometry())
        root.minsize(self.S(WINDOW_MIN[0]), self.S(WINDOW_MIN[1]))
        root.grid_rowconfigure(2, weight=1)
        root.grid_columnconfigure(1, weight=1)

        self._build_menu()

        # --- 工具栏 ------------------------------------------------------
        bar = self.toolbar = self._frame(root, "chrome.bg")
        bar.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.btn_shelf = self._button(bar, "☰ 书架", self.toggle_shelf, "chrome.icon")
        self.btn_shelf.pack(side="left", padx=(self.S(8), self.S(4)), pady=self.S(8))
        self.label_title = self._label(bar, "文本阅读器", "chrome.title")
        self.label_title.configure(font=self.font_title, anchor="w")
        self.label_title.pack(side="left", padx=self.S(6))
        self.label_progress = self._label(bar, "0%", "chrome.soft")
        self.label_progress.configure(font=self.font_ui)
        self.label_progress.pack(side="right", padx=(self.S(4), self.S(10)))
        self.btn_next = self._button(bar, "▶", lambda: self.jump_chapter(1), "chrome.icon")
        self.btn_next.pack(side="right", padx=self.S(2), pady=self.S(8))
        self.btn_prev = self._button(bar, "◀", lambda: self.jump_chapter(-1), "chrome.icon")
        self.btn_prev.pack(side="right", padx=self.S(2), pady=self.S(8))
        self.btn_settings = self._button(bar, "Aa 设置", lambda: self.show_sidebar("settings"), "chrome.icon")
        self.btn_settings.pack(side="right", padx=self.S(4), pady=self.S(8))
        self.btn_toc = self._button(bar, "目录", lambda: self.show_sidebar("toc"), "chrome.icon")
        self.btn_toc.pack(side="right", padx=self.S(4), pady=self.S(8))
        self.btn_eye = self._button(bar, "护眼", self.toggle_eye_care, "chrome.icon")
        self.btn_eye.pack(side="right", padx=self.S(4), pady=self.S(8))
        self.btn_paste = self._button(bar, "粘贴", self.action_paste, "chrome.icon")
        self.btn_paste.pack(side="right", padx=self.S(4), pady=self.S(8))
        self.btn_import = self._button(bar, "导入文本", self.action_import, "accent.btn")
        self.btn_import.pack(side="right", padx=self.S(4), pady=self.S(8))

        # --- 顶部进度线（2 逻辑像素） ------------------------------------
        line_h = self.S(2)
        self.progress_track = self._reg(tk.Frame(root, height=line_h, bd=0, highlightthickness=0), "line")
        self.progress_track.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.progress_fill = tk.Frame(self.progress_track, height=line_h, bd=0, highlightthickness=0,
                                      bg=self.pal.accent)
        self.progress_fill.place(x=0, y=0, relwidth=0.0, relheight=1.0)

        # --- 书架列 ------------------------------------------------------
        self.shelf = self._frame(root, "chrome.bg", width=self.S(SHELF_WIDTH))
        self.shelf.grid(row=2, column=0, sticky="nsw")
        self.shelf.grid_propagate(False)
        head = self._frame(self.shelf, "chrome.bg")
        head.pack(fill="x", padx=self.S(14), pady=(self.S(14), self.S(6)))
        self._label(head, "书架", "chrome.soft").pack(side="left")
        self.label_shelf_count = self._label(head, "0 本", "chrome.soft")
        self.label_shelf_count.configure(font=self.font_ui_small)
        self.label_shelf_count.pack(side="right")

        listwrap = self._frame(self.shelf, "chrome.bg")
        listwrap.pack(fill="both", expand=True, padx=self.S(10))
        self.shelf_list = tk.Listbox(listwrap, activestyle="none", exportselection=False,
                                     bd=0, highlightthickness=0, selectmode="browse",
                                     font=self.font_ui)
        self.shelf_scroll = tk.Scrollbar(listwrap, orient="vertical", width=self.S(SHELF_SCROLLBAR_WIDTH),
                                         bd=0, highlightthickness=0, command=self.shelf_list.yview)
        self.shelf_list.configure(yscrollcommand=self.shelf_scroll.set)
        self._reg(self.shelf_list, "list")
        self._reg(self.shelf_scroll, "scrollbar")
        self.shelf_scroll.pack(side="right", fill="y")
        self.shelf_list.pack(side="left", fill="both", expand=True)
        self.shelf_list.bind("<<ListboxSelect>>", self._on_shelf_select)
        self.shelf_list.bind("<Double-Button-1>", lambda e: self.action_rename())

        self.label_shelf_info = self._label(self.shelf, "", "chrome.soft", justify="left", anchor="w")
        self.label_shelf_info.configure(font=self.font_ui_small)
        self.label_shelf_info.pack(fill="x", padx=self.S(16), pady=(self.S(8), self.S(2)))

        tools = self._frame(self.shelf, "chrome.bg")
        tools.pack(fill="x", padx=self.S(10), pady=(self.S(4), self.S(12)))
        self._button(tools, "删除", self.action_delete, "chrome.icon").pack(
            side="left", expand=True, fill="x", padx=self.S(2))
        self._button(tools, "重命名", self.action_rename, "chrome.icon").pack(
            side="left", expand=True, fill="x", padx=self.S(2))

        # --- 阅读区：桌面 Canvas（壁纸 + 纸纹）+ 居中「纸页」Text ---------
        self.reader_wrap = self._frame(root, "page.bg")
        self.reader_wrap.grid(row=2, column=1, sticky="nsew")
        self.desk = tk.Canvas(self.reader_wrap, bd=0, highlightthickness=0, takefocus=0)
        self._reg(self.desk, "page.bg")          # 桌面底色跟随主题（无壁纸时）
        self.desk.pack(side="left", fill="both", expand=True)
        self.reader = tk.Text(self.reader_wrap, wrap="word", bd=0, highlightthickness=0,
                              cursor="arrow", padx=self.S(34), pady=self.S(PAGE_PAD_Y),
                              spacing1=0, spacing3=self.S(10))
        self.reader_scroll = tk.Scrollbar(self.reader_wrap, orient="vertical", width=self.S(SCROLLBAR_WIDTH),
                                          bd=0, highlightthickness=0, command=self._on_scroll)
        self.reader.configure(yscrollcommand=self._on_yscroll)
        self._reg(self.reader, "page")
        self._reg(self.reader_scroll, "scrollbar")
        self.reader_scroll.pack(side="right", fill="y")
        self.reader.place(x=0, y=0, width=1, height=1)   # 位置由 _apply_page_layout 计算
        self.desk.bind("<Configure>", self._on_desk_configure)
        # 桌面上的图层 id：壁纸 / 纸纹 / 纸页阴影 / 纸页边框
        self._desk_items: List[int] = []
        self._texture_photo = None      # 必须保存引用，否则会被 GC
        self._dim_photo = None          # 放大后的压暗覆盖层
        self._dim_key = None
        self._wallpaper = None          # wallpaper.Wallpaper 实例
        self._wallpaper_tints: Dict[str, str] = {}
        self._page_rect: Tuple[int, int, int, int] = (0, 0, 0, 0)

        # --- 右侧栏（目录 / 设置，只显示一个） ---------------------------
        self.sidebar = self._frame(root, "chrome.bg", width=self.S(SIDEBAR_WIDTH))
        self.sidebar.grid(row=2, column=2, sticky="nse")
        self.sidebar.grid_propagate(False)
        side_head = self._frame(self.sidebar, "chrome.bg")
        side_head.pack(fill="x", padx=self.S(12), pady=(self.S(14), self.S(8)))
        self.tab_toc = self._button(side_head, "目录", lambda: self.show_sidebar("toc"), "chrome.icon")
        self.tab_toc.pack(side="left", padx=(0, self.S(6)))
        self.tab_settings = self._button(side_head, "阅读设置", lambda: self.show_sidebar("settings"), "chrome.icon")
        self.tab_settings.pack(side="left")
        self._button(side_head, "✕", lambda: self.show_sidebar(None), "chrome.icon").pack(side="right")

        self.side_body = self._frame(self.sidebar, "chrome.bg")
        self.side_body.pack(fill="both", expand=True)

        # 侧栏内容可滚动：设置项变多后，小窗口里放不下（壁纸区在下面）
        self.side_canvas = tk.Canvas(self.side_body, bd=0, highlightthickness=0)
        self._reg(self.side_canvas, "chrome.bg")
        self.side_scroll = tk.Scrollbar(self.side_body, orient="vertical", width=self.S(SHELF_SCROLLBAR_WIDTH),
                                        bd=0, highlightthickness=0, command=self.side_canvas.yview)
        self._reg(self.side_scroll, "scrollbar")
        self.side_canvas.configure(yscrollcommand=self.side_scroll.set)
        self.side_scroll.pack(side="right", fill="y")
        self.side_canvas.pack(side="left", fill="both", expand=True)
        self._side_window: Optional[int] = None
        self.side_canvas.bind("<Configure>", self._on_side_canvas_configure)
        for widget in (self.sidebar, self.side_canvas):
            widget.bind("<Enter>", self._bind_side_wheel)
            widget.bind("<Leave>", self._unbind_side_wheel)

        self.toc_panel = self._frame(self.side_canvas, "chrome.bg")
        self.toc_list = tk.Listbox(self.toc_panel, activestyle="none", exportselection=False,
                                   bd=0, highlightthickness=0, selectmode="browse", font=self.font_ui)
        self._reg(self.toc_list, "list")
        self.toc_list.pack(fill="both", expand=True, padx=self.S(12), pady=(0, self.S(12)))
        self.toc_list.bind("<<ListboxSelect>>", self._on_toc_select)

        self.settings_panel = self._build_settings_panel(self.side_canvas)

        # --- 状态栏 ------------------------------------------------------
        status = self.statusbar = self._frame(root, "chrome.bg")
        status.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.status_label = self._label(status, "就绪", "chrome.soft", anchor="w")
        self.status_label.configure(font=self.font_ui_small)
        self.status_label.pack(side="left", padx=self.S(12), pady=self.S(5))
        self.chapter_label = self._label(status, "", "chrome.soft", anchor="w")
        self.chapter_label.configure(font=self.font_ui_small)
        self.chapter_label.pack(side="left", padx=self.S(10), pady=self.S(5))
        self.info_label = self._label(status, "", "chrome.soft", anchor="e")
        self.info_label.configure(font=self.font_ui_small)
        self.info_label.pack(side="right", padx=self.S(12), pady=self.S(5))

        self._configure_reader_tags()
        self._render_empty()

    # ------------------------------------------------------------------
    # 窗口几何：以「逻辑像素」持久化，换 DPI 后窗口物理尺寸不变
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> str:
        raw = str(self.settings.get("window") or "")
        m = GEOMETRY_RE.match(raw)
        if not m:
            return f"{self.S(WINDOW_DEFAULT[0])}x{self.S(WINDOW_DEFAULT[1])}"
        width, height = self.S(int(m.group(1))), self.S(int(m.group(2)))
        if m.group(3):
            return (f"{width}x{height}{m.group(3)}{self.S(int(m.group(4)))}"
                    f"{m.group(5)}{self.S(int(m.group(6)))}")
        return f"{width}x{height}"

    def _logical_geometry(self) -> str:
        """把当前物理几何换算回逻辑像素，写进设置。"""
        m = GEOMETRY_RE.match(self.root.geometry())
        if not m:
            return ""
        width = int(round(int(m.group(1)) / self.scale))
        height = int(round(int(m.group(2)) / self.scale))
        if m.group(3):
            return (f"{width}x{height}{m.group(3)}{int(round(int(m.group(4)) / self.scale))}"
                    f"{m.group(5)}{int(round(int(m.group(6)) / self.scale))}")
        return f"{width}x{height}"

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="导入文件…", accelerator="Ctrl+O", command=self.action_import)
        m_file.add_command(label="粘贴剪贴板内容", accelerator="Ctrl+V", command=self.action_paste)
        m_file.add_command(label="粘贴并预览…", accelerator="Ctrl+Shift+V", command=self.action_paste_preview)
        m_file.add_command(label="撤销粘贴", accelerator="Ctrl+Z", command=self.action_undo_paste)
        m_file.add_separator()
        m_file.add_command(label="删除当前文本", command=self.action_delete)
        m_file.add_command(label="重命名当前文本…", command=self.action_rename)
        m_file.add_separator()
        m_file.add_command(label="打开数据文件夹", command=self.action_open_data_dir)
        m_file.add_separator()
        m_file.add_command(label="退出", accelerator="Ctrl+Q", command=self.on_close)
        menubar.add_cascade(label="文件", menu=m_file)

        m_view = tk.Menu(menubar, tearoff=0)
        m_view.add_command(label="书架", accelerator="Ctrl+B", command=self.toggle_shelf)
        m_view.add_command(label="目录", accelerator="Ctrl+L", command=lambda: self.show_sidebar("toc"))
        m_view.add_command(label="阅读设置", accelerator="Ctrl+,", command=lambda: self.show_sidebar("settings"))
        m_view.add_separator()
        m_view.add_command(label="护眼模式", accelerator="Ctrl+E", command=self.toggle_eye_care)
        m_view.add_separator()
        m_view.add_command(label="放大字号", accelerator="Ctrl+=", command=lambda: self.nudge_font(1))
        m_view.add_command(label="缩小字号", accelerator="Ctrl+-", command=lambda: self.nudge_font(-1))
        m_view.add_separator()
        m_view.add_command(label="更换壁纸…", command=self.action_pick_wallpaper)
        m_view.add_command(label="清除壁纸", command=self.action_clear_wallpaper)
        m_view.add_command(label="沉浸模式", accelerator="F11", command=self.toggle_immersive)
        menubar.add_cascade(label="视图", menu=m_view)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="快捷键", command=self.action_shortcuts)
        m_help.add_command(label="关于", command=self.action_about)
        menubar.add_cascade(label="帮助", menu=m_help)
        self.root.configure(menu=menubar)
        self.menubar = menubar

    def _build_settings_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = self._frame(parent, "chrome.bg")
        s = self.settings

        # 字号
        row, self.scale_font, self.value_font = self._slider_row(
            panel, "字号", *FONT_SIZE_RANGE, 1, s["font_size"],
            lambda v: self.set_settings(font_size=int(round(v))),
            lambda v: f"{int(round(v))} pt")
        # 行距
        row, self.scale_line, self.value_line = self._slider_row(
            panel, "行距", *LINE_HEIGHT_RANGE, 0.05, s["line_height"],
            lambda v: self.set_settings(line_height=round(float(v), 2)),
            lambda v: f"{float(v):.2f}")
        # 页宽
        row, self.scale_width, self.value_width = self._slider_row(
            panel, "页宽", *PAGE_WIDTH_RANGE, 20, s["page_width"],
            lambda v: self.set_settings(page_width=int(round(v))),
            lambda v: f"{int(round(v))} px")

        # 字体
        wrap = self._frame(panel, "chrome.bg")
        wrap.pack(fill="x", padx=self.S(14), pady=(0, self.S(12)))
        self._label(wrap, "字体", "chrome.fg").pack(anchor="w", pady=(0, self.S(4)))
        seg = self._frame(wrap, "chrome.bg")
        seg.pack(fill="x")
        self.btn_serif = self._button(seg, "衬线（宋体）", lambda: self.set_settings(font_family="serif"), "seg.btn")
        self.btn_serif.pack(side="left", expand=True, fill="x", padx=(0, self.S(3)))
        self.btn_sans = self._button(seg, "无衬线（黑体）", lambda: self.set_settings(font_family="sans"), "seg.btn")
        self.btn_sans.pack(side="left", expand=True, fill="x", padx=(self.S(3), 0))

        # 首行缩进
        self.var_indent = tk.BooleanVar(value=bool(s["indent"]))
        self.chk_indent = self._check(panel, "首行缩进（中文排版习惯）", self.var_indent,
                                      lambda: self.set_settings(indent=self.var_indent.get()))

        # 主题
        wrap = self._frame(panel, "chrome.bg")
        wrap.pack(fill="x", padx=self.S(14), pady=(0, self.S(6)))
        self._label(wrap, "背景色", "chrome.fg").pack(anchor="w", pady=(0, self.S(6)))
        grid = self._frame(wrap, "chrome.bg")
        grid.pack(fill="x")
        for i, (key, spec) in enumerate(theme.THEMES.items()):
            border = self._frame(grid, "line")
            border.grid(row=i // 3, column=i % 3, sticky="nsew", padx=self.S(3), pady=self.S(3))
            btn = tk.Frame(border, bd=0, highlightthickness=0, cursor="hand2")
            btn.pack(fill="both", expand=True, padx=self.S(2), pady=self.S(2))
            inner = tk.Label(btn, text=f"Aa\n{spec['name']}", bd=0, highlightthickness=0,
                             bg=spec["bg"], fg=spec["fg"], font=self.font_ui_small,
                             justify="center", cursor="hand2")
            inner.pack(fill="both", expand=True, ipady=self.S(6))
            inner.bind("<Button-1>", lambda e, k=key: self.set_settings(theme=k))
            btn.bind("<Button-1>", lambda e, k=key: self.set_settings(theme=k))
            self._swatch_borders[key] = border
            self._swatch_buttons[key] = btn
        for c in range(3):
            grid.grid_columnconfigure(c, weight=1)

        custom = self._frame(panel, "chrome.bg")
        custom.pack(fill="x", padx=self.S(14), pady=(self.S(8), self.S(12)))
        self.btn_bg_color = self._button(custom, "自定义背景…", lambda: self._pick_custom("bg"), "chrome.icon")
        self.btn_bg_color.pack(side="left", expand=True, fill="x", padx=(0, self.S(3)))
        self.btn_fg_color = self._button(custom, "自定义文字…", lambda: self._pick_custom("fg"), "chrome.icon")
        self.btn_fg_color.pack(side="left", expand=True, fill="x", padx=(self.S(3), 0))

        # 护眼
        self.var_eye = tk.BooleanVar(value=bool(s["eye_care"]))
        self.chk_eye = self._check(panel, "护眼模式（暖色降蓝光 · 降低对比度）", self.var_eye,
                                   lambda: self.set_settings(eye_care=self.var_eye.get()), hint=True)

        # ---- 全局壁纸 ----
        wp = self._frame(panel, "chrome.bg")
        wp.pack(fill="x", padx=self.S(14), pady=(0, self.S(6)))
        self._label(wp, "全局壁纸", "chrome.fg").pack(anchor="w", pady=(0, self.S(6)))
        row = self._frame(wp, "chrome.bg")
        row.pack(fill="x")
        self.btn_wallpaper = self._button(row, "选择图片…", self.action_pick_wallpaper, "chrome.icon")
        self.btn_wallpaper.pack(side="left", expand=True, fill="x", padx=(0, self.S(3)))
        self._button(row, "清除", self.action_clear_wallpaper, "chrome.icon").pack(
            side="left", expand=True, fill="x", padx=(self.S(3), 0))

        self.label_wallpaper = self._label(wp, "未设置", "chrome.soft", anchor="w",
                                           justify="left", wraplength=self.S(260))
        self.label_wallpaper.configure(font=self.font_ui_small)
        self.label_wallpaper.pack(fill="x", pady=(self.S(6), 0))

        seg = self._frame(wp, "chrome.bg")
        seg.pack(fill="x", pady=(self.S(6), 0))
        self.wallpaper_mode_buttons: Dict[str, tk.Button] = {}
        for key, name in (("cover", "充满"), ("contain", "适应"), ("tile", "平铺"), ("stretch", "拉伸")):
            btn = self._button(seg, name, lambda k=key: self.set_settings(wallpaper_mode=k), "seg.btn")
            btn.pack(side="left", expand=True, fill="x", padx=self.S(1))
            self.wallpaper_mode_buttons[key] = btn

        _, self.scale_dim, self.value_dim = self._slider_row(
            wp, "壁纸融合", 0.0, 0.7, 0.05, s["wallpaper_dim"],
            lambda v: self.set_settings(wallpaper_dim=round(float(v), 2)),
            lambda v: f"{int(round(float(v) * 100))}%", pad=0)
        _, self.scale_panel, self.value_panel = self._slider_row(
            wp, "面板融入", 0.0, 0.6, 0.05, s["wallpaper_panel"],
            lambda v: self.set_settings(wallpaper_panel=round(float(v), 2)),
            lambda v: f"{int(round(float(v) * 100))}%", pad=0)

        # ---- 质感 ----
        self.var_texture = tk.BooleanVar(value=bool(s["texture_on"]))
        self.chk_texture = self._check(panel, "桌面纸纹（程序化生成，更有质感）", self.var_texture,
                                       lambda: self.set_settings(texture_on=self.var_texture.get()))

        # 恢复默认
        self._button(panel, "恢复默认设置", self.action_reset_settings, "chrome.icon").pack(
            fill="x", padx=self.S(14), pady=self.S(6))

        self.label_storage = self._label(panel, "", "chrome.soft", justify="left", anchor="w",
                                        wraplength=self.S(266))
        self.label_storage.configure(font=self.font_ui_small)
        self.label_storage.pack(fill="x", padx=self.S(14), pady=(self.S(4), self.S(14)))
        return panel

    def _slider_row(self, parent: tk.Widget, label: str, lo: float, hi: float, res: float,
                    value: float, on_change: Callable[[float], None],
                    fmt: Callable[[float], str], pad: int = 14) -> Tuple[tk.Frame, tk.Scale, tk.Label]:
        row = self._frame(parent, "chrome.bg")
        row.pack(fill="x", padx=self.S(pad), pady=(0, self.S(10)))
        head = self._frame(row, "chrome.bg")
        head.pack(fill="x")
        self._label(head, label, "chrome.fg").pack(side="left")
        val = self._label(head, fmt(float(value)), "chrome.soft")
        val.configure(font=self.font_ui_small)
        val.pack(side="right")
        scale = tk.Scale(row, from_=lo, to=hi, resolution=res, orient="horizontal", showvalue=0,
                         bd=0, highlightthickness=0, sliderlength=self.S(22),
                         width=self.S(13), length=self.S(200),
                         command=lambda v: on_change(float(v)))
        scale.set(value)
        scale.pack(fill="x")
        self._reg(scale, "scale")
        # 值文案随拖动实时更新
        scale.configure(command=lambda v, f=fmt, lb=val, cb=on_change: (lb.configure(text=f(float(v))), cb(float(v))))
        return row, scale, val

    def _check(self, parent: tk.Widget, text: str, var: tk.BooleanVar,
               command: Callable[[], None], hint: bool = False) -> tk.Checkbutton:
        chk = tk.Checkbutton(parent, text=text, variable=var, command=command, anchor="w",
                             bd=0, highlightthickness=0, cursor="hand2",
                             font=self.font_ui)
        chk.pack(fill="x", padx=self.S(14), pady=(0, self.S(12) if not hint else self.S(16)))
        return self._reg(chk, "check")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------

    def _apply_role(self, widget: tk.Widget, role: str) -> None:
        p = self.pal
        c = self.chrome        # 外壳色：无壁纸时 == p.chrome，有壁纸时向壁纸靠拢
        if role == "chrome.bg":
            widget.configure(bg=c)
        elif role == "chrome.fg":
            widget.configure(bg=c, fg=p.fg)
        elif role == "chrome.soft":
            widget.configure(bg=c, fg=p.soft)
        elif role == "chrome.title":
            widget.configure(bg=c, fg=p.fg)
        elif role == "chrome.btn":
            widget.configure(bg=c, fg=p.fg, activebackground=p.hover, activeforeground=p.fg,
                             selectbackground=p.hover)
        elif role == "chrome.icon":
            widget.configure(bg=c, fg=p.fg, activebackground=p.hover, activeforeground=p.accent,
                             selectbackground=p.hover, padx=self.S(9), pady=self.S(4))
        elif role == "accent.btn":
            widget.configure(bg=p.accent, fg=p.on_accent, activebackground=theme.shade(p.accent, -0.12),
                             activeforeground=p.on_accent)
        elif role == "seg.btn":
            widget.configure(bg=self.surface, fg=p.fg, activebackground=p.hover, activeforeground=p.fg)
        elif role == "line":
            widget.configure(bg=theme.mix(p.line, c, 0.35))
        elif role == "page.bg":
            widget.configure(bg=p.bg)
        elif role == "page":
            widget.configure(bg=p.bg, fg=p.fg, insertbackground=p.fg,
                             selectbackground=p.accent, selectforeground=p.on_accent)
        elif role == "list":
            widget.configure(bg=self.surface, fg=p.fg, selectbackground=p.accent,
                             selectforeground=p.on_accent, highlightbackground=p.line)
        elif role == "scrollbar":
            # Scrollbar: bg 是滑块色，troughcolor 是滑槽色，必须拉开对比才看得见
            widget.configure(bg=theme.mix(c, p.fg, 0.30), activebackground=p.accent,
                             troughcolor=p.bg, highlightbackground=c,
                             bd=0, relief="flat", elementborderwidth=0, activerelief="flat")
        elif role == "scale":
            # Scale: bg 同时是控件底色与滑块色，滑槽用 troughcolor，两者对比要够
            widget.configure(bg=self.surface, fg=p.fg,
                             troughcolor=theme.mix(c, p.fg, 0.22),
                             activebackground=p.accent, highlightthickness=0, bd=0,
                             relief="flat", sliderrelief="raised", elementborderwidth=0)
        elif role == "check":
            widget.configure(bg=c, fg=p.fg, activebackground=c, activeforeground=p.fg,
                             selectcolor=self.surface if p.dark else p.bg)

    def _apply_theme(self, force: bool = False) -> None:
        s = self.settings
        sig_theme = (s["theme"], s["custom_bg"], s["custom_fg"], s["eye_care"])
        sig_type = (s["font_size"], s["line_height"], s["page_width"], s["font_family"], s["indent"])
        sig_wall = (s["wallpaper_on"], s["wallpaper_path"], s["wallpaper_mode"],
                    s["wallpaper_dim"], s["wallpaper_panel"],
                    s["texture_on"], s["texture_strength"])
        theme_changed = force or sig_theme != self._sig_theme
        type_changed = force or sig_type != self._sig_type
        wall_changed = force or sig_wall != self._sig_wall
        if not (theme_changed or type_changed or wall_changed):
            return
        self._sig_theme, self._sig_type, self._sig_wall = sig_theme, sig_type, sig_wall

        self.pal = theme.effective_palette(s)
        self.surface = theme.mix(self.pal.chrome, self.pal.bg, 0.6)
        self.error_color = "#ff8679" if self.pal.dark else "#b3402f"
        if wall_changed and hasattr(self, "desk"):
            self._refresh_texture()
            self._set_wallpaper_from_settings()
        self._refresh_tints()          # 外壳色（有壁纸时向壁纸靠拢）必须在刷控件之前算好
        if hasattr(self, "progress_fill"):
            self.progress_fill.configure(bg=self.pal.accent)

        if theme_changed or force:
            self.root.configure(bg=self.chrome)
            for widget, role in self._styleable:
                try:
                    self._apply_role(widget, role)
                except tk.TclError:
                    pass
            self._paint_swatch_selection()
            if hasattr(self, "desk"):
                self._redraw_desk()
            self._sync_sidebar_buttons()

        if type_changed:
            self._sync_fonts()
            # 字号/行距/页宽/缩进变化会重新换行：按字符下标把阅读位置锚回去
            if self.store.state["current"] and hasattr(self, "desk"):
                self._apply_page_layout()
                self._reflow()

        # 段落/标题 tag、内边距、按钮激活态都需要跟着走
        self._configure_reader_tags()
        self._sync_segment_buttons()
        self.label_title.configure(font=self.font_title)
        self._update_progress_ui(self._ratio)

    def _paint_swatch_selection(self) -> None:
        current = self.settings["theme"]
        for key, border in self._swatch_borders.items():
            border.configure(bg=self.pal.accent if key == current else self.pal.line)

    def _sync_segment_buttons(self) -> None:
        active = self.settings["font_family"]
        for btn, key in ((self.btn_serif, "serif"), (self.btn_sans, "sans")):
            btn.configure(bg=self.pal.accent if active == key else self.surface,
                          fg=self.pal.on_accent if active == key else self.pal.fg,
                          activebackground=self.pal.accent if active == key else self.pal.hover)

    # ------------------------------------------------------------------
    # 正文渲染
    # ------------------------------------------------------------------

    def _line_gap(self) -> int:
        lh = float(self.settings["line_height"])
        base = self.font_body.metrics("linespace")
        return max(0, int(round(base * (lh - 1.0))))

    def _init_visuals(self) -> None:
        """首次映射窗口后：纸纹 + 壁纸 + 纸页布局。"""
        self._apply_page_layout()
        self._refresh_texture()
        self._set_wallpaper_from_settings()
        self._refresh_tints()
        self._apply_theme(force=True)
        self._apply_immersive()

    def _refresh_texture(self) -> None:
        """程序化纸纹（噪声）：给桌面一点质感，纯 Python 生成，无需图片文件。"""
        on = bool(self.settings.get("texture_on", True)) and wallpaper is not None
        if not on:
            self.texture = None
            self._texture_photo = None
            if hasattr(self, "desk"):
                self._redraw_desk()
            return
        if self.texture is None:
            try:
                # 注意是 alpha=strength：tile 尺寸保持 64（太小会产生上万个画布图元）
                self.texture = wallpaper.noise_tile(
                    size=64, alpha=max(0, min(40, int(self.settings.get("texture_strength", 10)))))
            except Exception:
                self.texture = None
        self._texture_photo = self.texture
        if hasattr(self, "desk"):
            self._redraw_desk()

    def _refresh_tints(self) -> None:
        """有壁纸时让外壳颜色向壁纸靠拢，视觉上更统一。"""
        self.chrome = self.pal.chrome
        if self._wallpaper is None:
            return
        strength = float(self.settings.get("wallpaper_panel", 0.18))
        if strength <= 0:
            return
        try:
            self.chrome = theme.mix(self.pal.chrome, self._wallpaper.average_color(),
                                    min(0.6, strength))
        except Exception:
            self.chrome = self.pal.chrome

    def _page_pad(self) -> int:
        """正文在纸页内部左右各留多少（纸页本身的宽度由 _apply_page_layout 决定）。"""
        return self.S(34)

    # ------------------------------------------------------------------
    # 桌面：壁纸 / 纸纹 / 纸页阴影
    # ------------------------------------------------------------------

    DESK_MARGIN_X = 22      # 纸页与桌面左右的最小留白（逻辑像素）
    DESK_MARGIN_Y = 18

    def _apply_page_layout(self) -> None:
        """计算纸页位置与宽度，并把 Text 放到 Canvas 上（壁纸因此在四周露出）。"""
        if not hasattr(self, "desk"):
            return
        self.reader_wrap.update_idletasks()
        avail_w = max(1, self.desk.winfo_width())
        avail_h = max(1, self.desk.winfo_height())
        margin_x = self.S(self.DESK_MARGIN_X)
        margin_y = self.S(self.DESK_MARGIN_Y)
        want = self.S(int(self.settings["page_width"]))
        page_w = max(self.S(320), min(want, avail_w - 2 * margin_x))
        page_h = max(self.S(240), avail_h - 2 * margin_y)
        page_x = max(margin_x, (avail_w - page_w) // 2)
        page_y = max(margin_y, (avail_h - page_h) // 2)
        self._page_rect = (page_x, page_y, page_x + page_w, page_y + page_h)
        if self._page_rect == getattr(self, "_page_rect_applied", None):
            return                       # 尺寸没变就别重画（否则会与 <Configure> 互相触发）
        self._page_rect_applied = self._page_rect
        self.reader.place_configure(x=page_x, y=page_y, width=page_w, height=page_h)
        self._redraw_desk()

    def _desk_size(self) -> Tuple[int, int]:
        return max(1, self.desk.winfo_width()), max(1, self.desk.winfo_height())

    def _redraw_desk(self) -> None:
        """重画桌面：壁纸 → 纸纹 → 纸页阴影/边框。"""
        if not hasattr(self, "desk"):
            return
        self.desk.delete("all")
        self._desk_items = []
        width, height = self._desk_size()
        p = self.pal

        # 1) 壁纸（Canvas 会自动裁剪超出部分）
        if self._wallpaper is not None:
            try:
                anchor, wx, wy = self._wallpaper.place()
                self._desk_items.append(self.desk.create_image(wx, wy, anchor=anchor,
                                                               image=self._wallpaper.photo))
                overlay = self._dim_overlay_for(width, height)
                if overlay is not None:
                    self._desk_items.append(self.desk.create_image(0, 0, anchor="nw", image=overlay))
            except tk.TclError:
                pass
        else:
            self.desk.configure(bg=p.bg)

        # 2) 纸纹（程序化噪声，平铺；给桌面一点质感）
        if self._texture_photo is not None:
            tile_w = self._texture_photo.width()
            tile_h = self._texture_photo.height()
            for ty in range(0, height + tile_h, tile_h):
                for tx in range(0, width + tile_w, tile_w):
                    self._desk_items.append(self.desk.create_image(tx, ty, anchor="nw",
                                                                   image=self._texture_photo))

        # 3) 纸页：投影 + 边框（Text 本身不透明，只露出这一圈）
        x0, y0, x1, y1 = self._page_rect
        if x1 - x0 > 8 and y1 - y0 > 8:
            shadow = theme.mix(p.bg, "#000000", 0.35 if not p.dark else 0.6)
            for i, offset in enumerate((self.S(4), self.S(2))):
                self.desk.create_rectangle(x0 + offset, y0 + offset, x1 + offset, y1 + offset,
                                           fill=shadow, outline="", tags="shadow")
            self.desk.create_rectangle(x0 - 1, y0 - 1, x1 + 1, y1 + 1,
                                       outline=theme.mix(p.bg, p.fg, 0.18), width=1)

    def _dim_overlay_for(self, width: int, height: int) -> Optional[tk.PhotoImage]:
        """把 4x4 的半透明压暗层放大到覆盖整个桌面。

        注意：zoom() 会生成**新**的 PhotoImage，必须自己保存引用，
        否则被 GC 回收后画布上会变成空白（这是 tkinter 的经典坑）。
        """
        if self._wallpaper is None:
            return None
        small = self._wallpaper.dim_overlay()
        if small is None:
            self._dim_photo = None
            return None
        tile = max(1, small.width())
        factor = max(1, -(-max(width, height) // tile))     # 向上取整
        key = (str(small), factor)
        if getattr(self, "_dim_key", None) != key:
            try:
                self._dim_photo = small.zoom(factor)
            except tk.TclError:
                self._dim_photo = small
            self._dim_key = key
        return self._dim_photo

    def _set_wallpaper_from_settings(self) -> None:
        """按设置加载/清除壁纸。加载失败只提示，不影响阅读。"""
        path = str(self.settings.get("wallpaper_path") or "")
        if not self.settings.get("wallpaper_on") or not path:
            self._wallpaper = None
            self._wallpaper_tints = {}
            return
        if wallpaper is None:
            self._wallpaper = None
            return
        width, height = self._desk_size()
        if width < 8 or height < 8:
            return                     # 窗口还没映射，等 _init_visuals 再来
        try:
            self._wallpaper = wallpaper.Wallpaper(
                path, (width, height),
                mode=str(self.settings.get("wallpaper_mode", "cover")),
                dim=float(self.settings.get("wallpaper_dim", 0.28)),
                tint=self.pal.bg)
            self._wallpaper_tints = self._panel_tints()
        except Exception as exc:  # noqa: BLE001 - 壁纸坏了不能影响阅读
            self._wallpaper = None
            self._wallpaper_tints = {}
            self.status(f"壁纸加载失败：{exc}", error=True)

    def _panel_tints(self) -> Dict[str, str]:
        """从壁纸上取各面板区域的平均色，让外壳「融入」壁纸。"""
        if self._wallpaper is None:
            return {}
        strength = float(self.settings.get("wallpaper_panel", 0.18))
        if strength <= 0:
            return {}
        width, height = self._desk_size()
        regions = {
            "toolbar": (0, 0, width, self.S(56)),
            "shelf": (0, 0, self.S(SHELF_WIDTH), height),
            "sidebar": (width - self.S(SIDEBAR_WIDTH), 0, width, height),
            "status": (0, height - self.S(30), width, height),
        }
        out: Dict[str, str] = {}
        for key, box in regions.items():
            try:
                colour = self._wallpaper.region_color(*box)
            except Exception:
                continue
            out[key] = theme.mix(self.pal.chrome, colour, min(0.6, strength))
        return out

    def _configure_reader_tags(self) -> None:
        if not hasattr(self, "reader"):
            return
        p = self.pal
        s = self.settings
        gap = self._line_gap()
        indent = self.font_body.measure("\u3000\u3000") if s["indent"] else 0
        self.reader.configure(bg=p.bg, fg=p.fg, spacing1=0, spacing2=gap, spacing3=gap,
                              padx=self._page_pad(), pady=self.S(PAGE_PAD_Y), insertwidth=0)
        self.reader.tag_configure("p", font=self.font_body, foreground=p.fg,
                                  spacing2=gap, spacing3=gap, lmargin1=indent, lmargin2=0)
        self.reader.tag_configure("h1", font=self.font_h1, foreground=p.fg,
                                  spacing1=self.S(4), spacing3=self.S(8))
        self.reader.tag_configure("meta", font=self.font_meta, foreground=p.soft,
                                  spacing3=self.S(26))
        self.reader.tag_configure("h2", font=self.font_h2, foreground=p.fg,
                                  spacing1=self.S(26), spacing3=self.S(16))
        self.reader.tag_configure("empty_h", font=self.font_empty_h, foreground=p.fg,
                                  justify="center", spacing1=self.S(110), spacing3=self.S(12))
        self.reader.tag_configure("empty_p", font=self.font_empty_p, foreground=p.soft,
                                  justify="center", spacing2=self.S(6), spacing3=self.S(10))

    def _on_state(self, state: Dict, prev: Dict) -> None:
        if state["settings"] is not prev["settings"]:
            self._apply_theme()
            self._sync_settings_controls()
        self._render_chrome(state)
        self._render_shelf(state)
        self._render_reader(state, prev)
        self._render_sidebar(state)

    def _render_chrome(self, state: Dict) -> None:
        book: Optional[Book] = state["current"]
        art: Optional[textproc.Article] = state["article"]
        if book and art:
            self.label_title.configure(
                text=f"{elide(book.meta.title, 42)}    {art.chars:,} 字 · "
                     f"{len(state['chapters']) - 1} 章" if len(state["chapters"]) > 1
                     else f"{elide(book.meta.title, 42)}    {art.chars:,} 字 · 无章节")
            self.info_label.configure(
                text=f"{textproc.human_size(book.meta.size)} · {book.meta.encoding} · "
                     f"{self._storage_label()}")
        else:
            self.label_title.configure(text="文本阅读器    未选择文本")
            self.info_label.configure(text=self._storage_label())
        self.btn_eye.configure(bg=self.pal.accent if self.settings["eye_care"] else self.pal.chrome,
                               fg=self.pal.on_accent if self.settings["eye_care"] else self.pal.fg)
        self.btn_toc.configure(state="normal" if book else "disabled")
        self._sync_sidebar_buttons()

    def _storage_label(self) -> str:
        return f"书库 {self.lib.count()} 本 · SQLite · {os.path.basename(self.data_dir)}"

    def action_open_data_dir(self) -> None:
        """便携模式下数据就在程序目录里，一键打开方便备份。"""
        path = str(self.data_dir)
        try:
            if sys.platform == "win32":
                os.startfile(path)                     # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            self.status(f"数据目录：{path}")
        except Exception as exc:                       # noqa: BLE001
            messagebox.showinfo("数据目录", f"{path}\n\n（自动打开失败：{exc}）", parent=self.root)

    def _render_reader(self, state: Dict, prev: Dict) -> None:
        current_id = state["current_id"]
        if current_id == self._fp["reader"] and prev.get("current_id") == current_id:
            return
        self._fp["reader"] = current_id
        book: Optional[Book] = state["current"]
        art: Optional[textproc.Article] = state["article"]
        if not book or not art:
            self._render_empty()
            self._reset_position()
            self._update_progress_ui(0.0)
            return

        text = self.reader
        text.configure(state="normal")
        text.delete("1.0", "end")
        # 一边插入一边记录「每个逻辑行首字符在全文中的下标」。
        # 后面用字符下标定位/还原：改字号、改页宽、改窗口大小都不会漂。
        offsets: List[int] = []
        cursor = 0
        for block in art.blocks:
            offsets.append(cursor)
            text.insert("end", block.text + "\n", block.kind)
            cursor += len(block.text) + 1
        text.configure(state="disabled")
        self._line_offsets = offsets
        self._total_chars = max(1, cursor)
        self._chapter_offsets = [
            (idx, offsets[ch.block_index] if ch.block_index < len(offsets) else 0)
            for idx, ch in enumerate(art.chapters)
        ]
        self._chapter_lines = [(idx, 1) for idx, _ in self._chapter_offsets]   # 兼容旧引用
        self._render_toc_list(state)
        self._restore_position(book.meta)

    # ---------------- 位置换算：字符下标 ⇄ 控件索引 ----------------

    def _index_to_offset(self, index: str) -> int:
        """把 Tk 的 '行.列' 索引换算成全文字符下标。"""
        try:
            line_s, col_s = str(index).split(".")
            line, col = int(line_s), int(col_s)
        except ValueError:
            return 0
        offsets = self._line_offsets
        if not offsets:
            return 0
        line = max(1, min(line, len(offsets)))
        return max(0, offsets[line - 1] + col)

    def _offset_to_index(self, offset: int) -> str:
        """字符下标 → Tk 索引（二分反查行号）。"""
        offsets = self._line_offsets
        if not offsets:
            return "1.0"
        offset = max(0, min(int(offset), self._total_chars))
        line = bisect.bisect_right(offsets, offset)
        line = max(1, min(line, len(offsets)))
        return f"{line}.{max(0, offset - offsets[line - 1])}"

    def _current_ratio(self) -> float:
        """已读比例：视口底部字符 / 总字符；末尾已进入视口就算 100%（已读完）。"""
        _first, last = self.reader.yview()
        if float(last) >= 0.9995:
            return 1.0
        height = max(2, self.reader.winfo_height())
        bottom = self.reader.index(f"@0,{height - 4}")
        if self.reader.compare(bottom, ">=", "end-2c"):
            return 1.0
        return max(0.0, min(1.0, self._index_to_offset(bottom) / self._total_chars))

    def _reset_position(self) -> None:
        self._ratio = 0.0
        self._char_offset = 0
        self._line_offsets = []
        self._chapter_offsets = []
        self._chapter_lines = []
        self._total_chars = 1
        self._restoring = False

    def _restore_position(self, meta: BookMeta) -> None:
        """按字符下标精确恢复；老数据没有 char_offset 时退回比例。"""
        self._restoring = True
        try:
            self.reader.update_idletasks()
            if meta.char_offset > 0:
                self.reader.yview(self._offset_to_index(meta.char_offset))
            elif meta.progress > 0:
                self.reader.yview_moveto(float(meta.progress))
            else:
                self.reader.yview_moveto(0.0)
            self._sync_from_view(initial=True)
        finally:
            # 等布局定型再解除静默；期间不接受滚动事件，避免把中间态写回库
            self.root.after(140, lambda bid=meta.id: self._end_restore(bid))

    def _end_restore(self, book_id: Optional[str] = None) -> None:
        # 期间用户可能已经翻到别的书了，那就别再拿旧书的控件状态去刷 UI
        if book_id is not None and self.store.state["current_id"] != book_id:
            self._restoring = False
            return
        self._restoring = False
        self._sync_from_view()
        self._sync_toc_selection()

    def _update_chapter_label(self) -> None:
        """状态栏显示「第 n/N 节 · 标题」，只在变化时刷新。"""
        if not hasattr(self, "chapter_label"):
            return
        chapters = self.store.state["chapters"]
        if not self.store.state["current"] or not self._chapter_offsets:
            text = ""
        else:
            index = self.current_chapter_index()
            title = chapters[index].title if index < len(chapters) else ""
            text = f"第 {index + 1}/{len(self._chapter_offsets)} 节 · {elide(title, 18)}"
        if text != getattr(self, "_chapter_label_text", None):
            self._chapter_label_text = text
            self.chapter_label.configure(text=text)

    def _sync_from_view(self, initial: bool = False) -> None:
        """从控件真实位置推出 (字符下标, 已读比例)，变化时刷新 UI 并安排落盘。"""
        if not self.store.state["current"]:
            return
        offset = self._index_to_offset(self.reader.index("@0,0"))
        ratio = self._current_ratio()
        changed = initial or offset != self._char_offset or abs(ratio - self._ratio) > 1e-4
        self._char_offset, self._ratio = offset, ratio
        if not changed:
            return
        self._update_progress_ui(ratio)
        self._update_chapter_label()
        if not initial:
            self._sync_toc_selection()
            self._schedule_save()

    def _render_empty(self) -> None:
        text = self.reader
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("end", EMPTY_HINT_TITLE + "\n", "empty_h")
        text.insert("end", EMPTY_HINT_BODY + "\n", "empty_p")
        text.insert("end", "\n", "empty_p")
        text.insert("end", EMPTY_HINT_KEYS + "\n", "empty_p")
        text.configure(state="disabled")

    # ------------------------------------------------------------------
    # 书架
    # ------------------------------------------------------------------

    def _render_shelf(self, state: Dict) -> None:
        key = (state["books_rev"], state["current_id"])
        if key == self._fp["shelf"]:
            return
        self._fp["shelf"] = key
        books: List[BookMeta] = state["books"]
        self.label_shelf_count.configure(text=f"{len(books)} 本")
        self.shelf_list.delete(0, "end")
        for i, meta in enumerate(books):
            self.shelf_list.insert("end", self._shelf_row(meta))
            self._paint_shelf_item(i, meta)
        if not books:
            self.label_shelf_info.configure(text="书架还是空的：\n点「导入文本」或拖入文件。")
        else:
            index = next((i for i, m in enumerate(books) if m.id == state["current_id"]), -1)
            if index >= 0:
                self.shelf_list.selection_clear(0, "end")
                self.shelf_list.selection_set(index)
                self.shelf_list.see(index)
                self.shelf_list.activate(index)
            self._refresh_shelf_info()

    def _shelf_row(self, meta: BookMeta) -> str:
        return f"▍{elide(meta.title, 20)}   ·  {meta.percent_text}"

    def _refresh_shelf_info(self, meta: Optional[BookMeta] = None) -> None:
        if meta is None:
            book: Optional[Book] = self.store.state["current"]
            meta = book.meta if book else None
        if meta is None:
            self.label_shelf_info.configure(text="")
            return
        when = time.strftime("%m-%d %H:%M", time.localtime(meta.last_read_at)) if meta.last_read_at else "—"
        self.label_shelf_info.configure(
            text=f"{textproc.human_size(meta.size)} · {meta.encoding}\n"
                 f"进度 {meta.percent_text} · 最近阅读 {when}")

    def _paint_shelf_progress(self, meta: BookMeta) -> None:
        """只改一行，不重建整个列表（每次进度落盘都会调用）。"""
        books: List[BookMeta] = self.store.state["books"]
        for i, item in enumerate(books):
            if item.id == meta.id:
                books[i] = meta
                self.shelf_list.delete(i)
                self.shelf_list.insert(i, self._shelf_row(meta))
                self.shelf_list.selection_clear(0, "end")
                self.shelf_list.selection_set(i)
                self._paint_shelf_item(i, meta)
                self._refresh_shelf_info(meta)
                return

    def _paint_shelf_item(self, index: int, meta: BookMeta) -> None:
        """给书架行上「书脊色 + 进度底纹」：当前这本用强调色，其余用书封面色的淡色。"""
        current = meta.id == self.store.state["current_id"]
        spine = self._spine_color(meta)
        if current:
            bg = self.pal.hover
        else:
            bg = theme.mix(self.surface, spine, 0.10)
        try:
            self.shelf_list.itemconfig(index, background=bg,
                                       foreground=self.pal.fg,
                                       selectbackground=self.pal.accent,
                                       selectforeground=self.pal.on_accent)
        except tk.TclError:
            pass

    def _spine_color(self, meta: BookMeta) -> str:
        """书名 → 稳定色相：同一本书永远同一个书脊颜色。"""
        base = sum((i + 1) * ord(c) for i, c in enumerate(meta.title)) % 360
        return theme.hsv_to_hex(base, 0.34 if not self.pal.dark else 0.42,
                                0.72 if not self.pal.dark else 0.85)

    # ------------------------------------------------------------------
    # 目录与侧栏
    # ------------------------------------------------------------------

    def _render_toc_list(self, state: Dict) -> None:
        chapters: List[textproc.Chapter] = state["chapters"]
        if not state["current"]:
            self.toc_list.delete(0, "end")
            return
        self.toc_list.delete(0, "end")
        if len(chapters) > 1:
            for ch in chapters:
                self.toc_list.insert("end", elide(ch.title, 26))
        else:
            self.toc_list.insert("end", "未识别到章节")
            self.toc_list.insert("end", "支持：第一章 / Chapter 1")
            self.toc_list.insert("end", "序章 / # 标题")

    def _render_sidebar(self, state: Dict) -> None:
        key = (state["sidebar"], state["current_id"] if state["sidebar"] == "toc" else None)
        if key == self._fp["sidebar"]:
            return
        self._fp["sidebar"] = key
        sidebar = state["sidebar"]
        if sidebar is None:
            self.sidebar.grid_remove()
        else:
            self.sidebar.grid()
        self.toc_panel.pack_forget()
        self.settings_panel.pack_forget()
        if self._side_window is not None:
            self.side_canvas.delete(self._side_window)
            self._side_window = None
        if sidebar == "toc":
            self._side_window = self.side_canvas.create_window(
                0, 0, window=self.toc_panel, anchor="nw")
            self._render_toc_list(state)
            self._sync_toc_selection()
        elif sidebar == "settings":
            self._side_window = self.side_canvas.create_window(
                0, 0, window=self.settings_panel, anchor="nw")
            self._sync_settings_controls()
        self._on_side_canvas_configure()
        self.root.after(30, self.refresh_side_scroll)
        self._sync_sidebar_buttons()

    # ------------------------------------------------------------------
    # 可滚动侧栏
    # ------------------------------------------------------------------

    def _on_side_canvas_configure(self, _event=None) -> None:
        if self._side_window is not None:
            self.side_canvas.itemconfigure(self._side_window, width=self.side_canvas.winfo_width())
        self.side_canvas.configure(scrollregion=self.side_canvas.bbox("all"))

    def refresh_side_scroll(self) -> None:
        self.side_canvas.update_idletasks()
        self.side_canvas.configure(scrollregion=self.side_canvas.bbox("all"))
        bbox = self.side_canvas.bbox("all")
        need = bool(bbox) and bbox[3] > self.side_canvas.winfo_height() + 2
        if need:
            self.side_scroll.pack(side="right", fill="y")
        else:
            self.side_scroll.pack_forget()
            self.side_canvas.yview_moveto(0.0)

    def _bind_side_wheel(self, _event=None) -> None:
        self.root.bind_all("<MouseWheel>", self._on_side_wheel, add="+")

    def _unbind_side_wheel(self, _event=None) -> None:
        self.root.unbind_all("<MouseWheel>")

    def _on_side_wheel(self, event) -> str:
        bbox = self.side_canvas.bbox("all")
        if not bbox or bbox[3] <= self.side_canvas.winfo_height() + 2:
            return ""                       # 内容放得下就不用滚
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:
            delta = 120 if int(getattr(event, "num", 0) or 0) == 4 else -120
        self.side_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _sync_sidebar_buttons(self) -> None:
        sidebar = self.store.state["sidebar"]
        for btn, key in ((self.btn_toc, "toc"), (self.btn_settings, "settings"),
                         (self.tab_toc, "toc"), (self.tab_settings, "settings")):
            on = sidebar == key
            btn.configure(bg=self.pal.accent if on else self.pal.chrome,
                          fg=self.pal.on_accent if on else self.pal.fg)

    def _sync_settings_controls(self) -> None:
        s = self.settings
        for scale, value in ((self.scale_font, s["font_size"]),
                             (self.scale_line, s["line_height"]),
                             (self.scale_width, s["page_width"])):
            if abs(float(scale.get()) - float(value)) > 1e-9:
                scale.set(value)
        self.value_font.configure(text=f"{int(s['font_size'])} pt")
        self.value_line.configure(text=f"{float(s['line_height']):.2f}")
        self.value_width.configure(text=f"{int(s['page_width'])} px")
        self.var_indent.set(bool(s["indent"]))
        self.var_eye.set(bool(s["eye_care"]))
        self.var_texture.set(bool(s["texture_on"]))
        for scale, value in ((self.scale_dim, s["wallpaper_dim"]),
                             (self.scale_panel, s["wallpaper_panel"])):
            if abs(float(scale.get()) - float(value)) > 1e-9:
                scale.set(value)
        self.value_dim.configure(text=f"{int(round(float(s['wallpaper_dim']) * 100))}%")
        self.value_panel.configure(text=f"{int(round(float(s['wallpaper_panel']) * 100))}%")
        active = str(s.get("wallpaper_mode", "cover"))
        for key, btn in self.wallpaper_mode_buttons.items():
            btn.configure(bg=self.pal.accent if key == active else self.surface,
                          fg=self.pal.on_accent if key == active else self.pal.fg)
        path = str(s.get("wallpaper_path") or "")
        if not path:
            self.label_wallpaper.configure(text="未设置（点「选择图片…」）")
        elif not s.get("wallpaper_on"):
            self.label_wallpaper.configure(text=f"已选择：{os.path.basename(path)}\n当前为关闭状态")
        else:
            backend_name = getattr(wallpaper, "backend", lambda: "?")() if wallpaper else "?"
            self.label_wallpaper.configure(
                text=f"当前：{os.path.basename(path)}\n缩放后端：{backend_name}")
        self._paint_swatch_selection()
        self._sync_segment_buttons()
        self._sync_sidebar_buttons()

    def show_sidebar(self, which: Optional[str]) -> None:
        if which and self.settings.get("immersive"):
            self.set_settings(immersive=False)      # 打开面板时自动退出沉浸
        current = self.store.state["sidebar"]
        self.store.set({"sidebar": None if current == which else which})
        self._schedule_settings_flush()   # 侧栏选择也要记住

    def toggle_shelf(self) -> None:
        s = dict(self.settings)
        s["shelf_open"] = not s["shelf_open"]
        self._apply_settings(s)

    def _sync_shelf_visibility(self) -> None:
        show = bool(self.settings["shelf_open"]) and not bool(self.settings.get("immersive"))
        if show:
            self.shelf.grid()
            self.btn_shelf.configure(text="☰ 书架")
        else:
            self.shelf.grid_remove()
            self.btn_shelf.configure(text="☰")

    # ------------------------------------------------------------------
    # 进度
    # ------------------------------------------------------------------

    def _on_scroll(self, *args) -> None:
        self.reader.yview(*args)
        self._on_view_changed()

    def _on_yscroll(self, first, last) -> None:
        self.reader_scroll.set(first, last)
        if not getattr(self, "_yscroll_pending", False):
            self._yscroll_pending = True
            self.root.after_idle(self._after_yscroll)

    def _after_yscroll(self) -> None:
        self._yscroll_pending = False
        self._on_view_changed()

    def _on_view_changed(self) -> None:
        if self._restoring or not self.store.state["current"]:
            return
        self._sync_from_view()

    def _update_progress_ui(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, float(ratio)))
        self.label_progress.configure(text="已读完" if ratio >= 0.999 else f"{int(round(ratio * 100))}%")
        self.progress_fill.place_configure(relwidth=ratio)

    def _schedule_save(self) -> None:
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(SAVE_DELAY_MS, self.save_progress)

    def save_progress(self, force: bool = False) -> None:
        """把 (已读比例, 字符下标) 一起落库；字符下标是精确位置，比例用于显示。"""
        self._save_job = None
        book: Optional[Book] = self.store.state["current"]
        if not book:
            return
        # 恢复位置期间不接受「自动」保存（那时布局还没定型），但强制保存（关窗/切书）必须照做
        if self._restoring and not force:
            return
        ratio = self._current_ratio()
        offset = self._index_to_offset(self.reader.index("@0,0"))
        if not force and offset == book.meta.char_offset and abs(ratio - book.meta.progress) < 0.002:
            return
        self.lib.set_progress(book.meta.id, ratio, offset)
        meta = replace(book.meta, progress=ratio, char_offset=offset,
                       progress_updated_at=time.time())
        self._char_offset, self._ratio = offset, ratio
        self.store.set({"current": replace(book, meta=meta)})   # 指纹不变，不会重建正文
        self._paint_shelf_progress(meta)
        self.label_progress.configure(fg=self.pal.accent)
        self.root.after(900, lambda: self.label_progress.configure(fg=self.pal.soft))

    def _sync_toc_selection(self) -> None:
        if self.store.state["sidebar"] != "toc" or not self._chapter_offsets:
            return
        if self.toc_list.size() != len(self.store.state["chapters"]):
            return
        index = 0
        for idx, offset in self._chapter_offsets:
            if offset <= self._char_offset:
                index = idx
            else:
                break
        if self.toc_list.curselection() and self.toc_list.curselection()[0] == index:
            return
        self._toc_sync = True
        try:
            self.toc_list.selection_clear(0, "end")
            self.toc_list.selection_set(index)
            self.toc_list.see(index)
        finally:
            self._toc_sync = False

    # ------------------------------------------------------------------
    # 交互：导入 / 粘贴 / 删除 / 重命名
    # ------------------------------------------------------------------

    def action_import(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root, title="导入文本",
            filetypes=[("文本文件", "*.txt *.md *.markdown *.text *.log"), ("所有文件", "*.*")])
        if paths:
            self.import_paths(list(paths))

    def import_paths(self, paths: List[str]) -> int:
        """导入若干文件，返回成功数量。供菜单与自动化测试共用。"""
        added: List[BookMeta] = []
        skipped: List[str] = []
        for path in paths:
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                skipped.append(f"{os.path.basename(path)}（{exc.strerror or exc}）")
                continue
            text, encoding = textproc.decode_bytes(data)
            if not text.strip():
                skipped.append(f"{os.path.basename(path)}（内容为空）")
                continue
            title = textproc.guess_title(os.path.basename(path), text)
            added.append(self.lib.add(title, text, encoding, size=len(data)))
        if added:
            self._reload_library(open_id=added[-1].id)
            msg = f"已导入 {len(added)} 个文件"
            if skipped:
                msg += f"，{len(skipped)} 个跳过"
            self.status(msg)
        elif skipped:
            self.status("导入失败：" + "；".join(skipped[:2]), error=True)
            messagebox.showerror("导入失败", "无法导入：\n" + "\n".join(skipped), parent=self.root)
        return len(added)

    # ------------------------------------------------------------------
    # 粘贴导入：一步到位（读剪贴板 → 直接成书），不需要先弹对话框
    # ------------------------------------------------------------------

    def clipboard_text(self) -> str:
        """读系统剪贴板文本；没有文本就返回空串（不抛异常）。"""
        try:
            return self.root.clipboard_get(type="STRING")
        except tk.TclError:
            pass
        try:
            return self.root.clipboard_get()
        except tk.TclError:
            return ""

    @staticmethod
    def clipboard_file_paths(text: str) -> List[str]:
        """剪贴板里是「复制文件」时，把路径还原出来（Explorer 会用 {} 包住带空格的路径）。"""
        if not text or "\n" not in text.strip() and not os.path.exists(text.strip()):
            return []
        paths: List[str] = []
        for raw in text.replace("{", "\n{").split("\n"):
            item = raw.strip().strip("{}").strip()
            if not item:
                continue
            if os.path.isfile(item) and item.lower().endswith(
                    (".txt", ".md", ".markdown", ".text", ".log", ".csv", ".json", ".srt")):
                paths.append(item)
            else:
                return []          # 有一行不是文本文件就当普通文本处理
        return paths

    def action_paste(self, preview: bool = False) -> None:
        """粘贴按钮 / Ctrl+V：直接把剪贴板内容变成一本书（最快路径）。

        preview=True（Ctrl+Shift+V 或菜单）才打开可编辑的预览对话框。
        """
        text = self.clipboard_text()
        if not text.strip():
            self.status("剪贴板里没有文本：先复制一段文字或几个 txt 文件再按 Ctrl+V", error=True)
            return
        paths = self.clipboard_file_paths(text)
        if paths:
            count = self.import_paths(paths)
            if count:
                self.status(f"已从剪贴板导入 {count} 个文件")
            return
        if preview:
            PasteDialog(self, prefill=text)
            return
        meta = self.add_book_from_text(textproc.guess_title("", text), text, "剪贴板")
        if meta:
            self._paste_undo_id = meta.id
            self.status(f"已粘贴《{elide(meta.title, 18)}》· {len(text):,} 字 · Ctrl+Z 撤销")

    def action_paste_preview(self) -> None:
        self.action_paste(preview=True)

    def action_undo_paste(self) -> None:
        """Ctrl+Z：撤销「上一次粘贴导入」（只对粘贴生效，避免误删手动导入的书）。"""
        book_id = self._paste_undo_id
        if not book_id:
            self.status("没有可撤销的粘贴")
            return
        self._paste_undo_id = None
        self.delete_book(book_id, confirm=False)

    def add_book_from_text(self, title: str, content: str, encoding: str = "剪贴板") -> Optional[BookMeta]:
        if not content.strip():
            self.status("内容为空，未导入", error=True)
            return None
        meta = self.lib.add(title or textproc.guess_title("", content), content, encoding)
        self._reload_library(open_id=meta.id)
        return meta

    def action_delete(self) -> None:
        index = self._shelf_index()
        if index is None:
            self.status("请先在书架中选择一本")
            return
        meta = self.store.state["books"][index]
        self.delete_book(meta.id)

    def delete_book(self, book_id: str, confirm: bool = True) -> bool:
        book = self.lib.get(book_id)
        if book is None:
            return False
        if confirm and not messagebox.askyesno(
                "删除文本", f"确定删除《{book.meta.title}》吗？\n该文本及其阅读进度将从本机移除。",
                parent=self.root):
            return False
        self.lib.remove(book_id)
        was_current = self.store.state["current_id"] == book_id
        books = self.lib.all_meta()
        patch = {"books": books, "books_rev": self.store.state["books_rev"] + 1}
        if was_current:
            patch.update({"current_id": None, "current": None, "article": None, "chapters": []})
        self.store.set(patch)
        self.status(f"已删除《{book.meta.title}》")
        if was_current and books:
            self.open_book(books[0].id)   # 自动续读最近一本
        return True

    def action_rename(self) -> None:
        index = self._shelf_index()
        if index is None:
            self.status("请先在书架中选择一本")
            return
        meta = self.store.state["books"][index]
        new_title = SimplePrompt(self, "重命名", "新的书名：", meta.title).result
        if new_title and new_title.strip() and new_title.strip() != meta.title:
            self.lib.rename(meta.id, new_title.strip()[:80])
            self._reload_library(open_id=self.store.state["current_id"] or meta.id)

    def _shelf_index(self) -> Optional[int]:
        sel = self.shelf_list.curselection()
        return int(sel[0]) if sel else None

    def _on_shelf_select(self, _event=None) -> None:
        index = self._shelf_index()
        if index is None:
            return
        books: List[BookMeta] = self.store.state["books"]
        if index >= len(books):
            return
        meta = books[index]
        self.store.set({"sidebar": self.store.state["sidebar"]})  # 强制刷新侧栏指纹
        self._refresh_shelf_info(meta)
        if meta.id != self.store.state["current_id"]:
            self.open_book(meta.id)

    def _on_toc_select(self, _event=None) -> None:
        if self._toc_sync:
            return
        sel = self.toc_list.curselection()
        if not sel or not self._chapter_offsets:
            return
        index = int(sel[0])
        if index < len(self._chapter_offsets):
            self.goto_chapter(index)

    # ------------------------------------------------------------------
    # 打开 / 列表
    # ------------------------------------------------------------------

    def _load_library(self) -> None:
        books = self.lib.all_meta()
        self.store.set({"books": books, "books_rev": self.store.state["books_rev"] + 1})
        self.label_storage.configure(
            text=f"存储：SQLite（{os.path.basename(self.data_dir)}/library.db）\n"
                 f"设置：settings.json · 进度按书保存")
        if books:
            self.open_book(books[0].id)   # 自动续读最近一本
        else:
            self._render_empty()
            self._render_toc_list(self.store.state)

    def _reload_library(self, open_id: Optional[str] = None) -> None:
        books = self.lib.all_meta()
        self.store.set({"books": books, "books_rev": self.store.state["books_rev"] + 1})
        if open_id:
            self.open_book(open_id)

    def open_book(self, book_id: str) -> None:
        book = self.lib.get(book_id)
        if book is None:
            self.status("文本已不存在", error=True)
            return
        self.save_progress(force=True)          # 切书前先把上一本的进度落盘
        article = textproc.build_article(book.meta.title, book.content,
                                         book.meta.size, book.meta.encoding)
        self.lib.touch(book_id)
        self.store.set({
            "current_id": book_id,
            "current": book,
            "article": article,
            "chapters": article.chapters,
            "books_rev": self.store.state["books_rev"],   # 不触发列表重建
        })
        self._refresh_shelf_info(book.meta)
        self.status(f"《{book.meta.title}》 · {article.chars:,} 字 · "
                    f"{max(0, len(article.chapters) - 1)} 章")

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def set_settings(self, **patch) -> None:
        s = dict(self.settings)
        s.update(patch)
        self._apply_settings(s)

    def _apply_settings(self, settings: Dict) -> None:
        self.store.set({"settings": settings})
        self._sync_shelf_visibility()
        self._apply_immersive()
        self._schedule_settings_flush()

    def _schedule_settings_flush(self) -> None:
        if self._settings_job:
            self.root.after_cancel(self._settings_job)
        self._settings_job = self.root.after(SETTINGS_DELAY_MS, self._flush_settings)

    def _flush_settings(self) -> None:
        self._settings_job = None
        payload = dict(self.settings)
        payload["sidebar"] = self.store.state["sidebar"]
        try:
            payload["window"] = self._logical_geometry()   # 存逻辑尺寸，换 DPI 不跑偏
        except tk.TclError:
            pass
        self.settings_file.save(payload)

    def action_reset_settings(self) -> None:
        keep = {"shelf_open": self.settings["shelf_open"], "window": self.settings.get("window", "")}
        reset = dict(DEFAULT_SETTINGS)
        reset.update(keep)
        self._apply_settings(reset)
        self.status("已恢复默认设置")

    def _pick_custom(self, which: str) -> None:
        s = self.settings
        current = s["custom_bg"] if which == "bg" else s["custom_fg"]
        if s["theme"] != "custom":
            # 从当前预设出发，避免自定义时突然跳到无关颜色
            base = theme.palette_for(s)
            current = base.bg if which == "bg" else base.fg
        _rgb, value = colorchooser.askcolor(color=current, parent=self.root,
                                            title="选择自定义背景色" if which == "bg" else "选择自定义文字颜色")
        if not value:
            return
        patch = {"theme": "custom"}
        if s["theme"] != "custom":
            base = theme.palette_for(s)
            patch["custom_bg"], patch["custom_fg"] = base.bg, base.fg
        patch["custom_bg" if which == "bg" else "custom_fg"] = value
        self.set_settings(**patch)

    def nudge_font(self, delta: int) -> None:
        lo, hi = FONT_SIZE_RANGE
        self.set_settings(font_size=max(lo, min(hi, int(self.settings["font_size"]) + delta)))

    def toggle_eye_care(self) -> None:
        on = not self.settings["eye_care"]
        self.set_settings(eye_care=on)
        self.status("护眼模式已开启" if on else "护眼模式已关闭")

    # ------------------------------------------------------------------
    # 状态栏 / 快捷键 / 关闭
    # ------------------------------------------------------------------

    def status(self, message: str, error: bool = False) -> None:
        self.status_label.configure(text=message, fg=self.error_color if error else self.pal.soft)
        if self._status_job:
            self.root.after_cancel(self._status_job)
        if not error:
            self._status_job = self.root.after(3200, lambda: self.status_label.configure(text="就绪", fg=self.pal.soft))

    def _bind_keys(self) -> None:
        r = self.root
        r.bind("<Control-o>", lambda e: self.action_import())
        r.bind("<Control-O>", lambda e: self.action_import())
        r.bind("<Control-q>", lambda e: self.on_close())
        r.bind("<Control-b>", lambda e: self.toggle_shelf())
        r.bind("<Control-l>", lambda e: self.show_sidebar("toc"))
        r.bind("<Control-comma>", lambda e: self.show_sidebar("settings"))
        r.bind("<Control-e>", lambda e: self.toggle_eye_care())
        # 粘贴：Ctrl+V 一步到位，Ctrl+Shift+V 预览，Ctrl+Z 撤销
        r.bind("<Control-v>", lambda e: self.action_paste())
        r.bind("<Control-V>", lambda e: self.action_paste())
        r.bind("<Control-Shift-V>", lambda e: self.action_paste_preview())
        r.bind("<Control-Shift-v>", lambda e: self.action_paste_preview())
        r.bind("<Control-z>", lambda e: self.action_undo_paste())
        r.bind("<Control-Z>", lambda e: self.action_undo_paste())
        for seq in ("<Control-equal>", "<Control-plus>", "<Control-KP_Add>"):
            r.bind(seq, lambda e: self.nudge_font(1))
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            r.bind(seq, lambda e: self.nudge_font(-1))
        r.bind("<Alt-Up>", lambda e: self.jump_chapter(-1))
        r.bind("<Alt-Down>", lambda e: self.jump_chapter(1))
        r.bind("<F11>", lambda e: self.toggle_immersive())
        r.bind("<Control-i>", lambda e: self.toggle_immersive())
        r.bind("<Key>", self._on_plain_key)
        r.bind("<Escape>", lambda e: self.show_sidebar(None))
        # 阅读区键盘滚动（Text 处于 disabled 状态也要能翻页）
        self.reader.bind("<Prior>", lambda e: self._scroll_by(-1, "pages"))
        self.reader.bind("<Next>", lambda e: self._scroll_by(1, "pages"))
        self.reader.bind("<space>", lambda e: self._scroll_by(1, "pages"))
        self.reader.bind("<Up>", lambda e: self._scroll_by(-3, "units"))
        self.reader.bind("<Down>", lambda e: self._scroll_by(3, "units"))
        self.reader.bind("<Home>", lambda e: self._moveto(0.0))
        self.reader.bind("<End>", lambda e: self._moveto(1.0))
        # Ctrl+滚轮 调字号（阅读器上最符合直觉的手势）
        for seq in ("<Control-MouseWheel>", "<Control-Button-4>", "<Control-Button-5>"):
            self.reader.bind(seq, self._on_ctrl_wheel)
            self.desk.bind(seq, self._on_ctrl_wheel)
        # 双击正文 = 沉浸模式开关
        self.reader.bind("<Double-Button-1>", lambda e: self.toggle_immersive())
        # 右键菜单
        self.reader.bind("<Button-3>", self._popup_reader_menu)
        self.desk.bind("<Button-3>", self._popup_reader_menu)
        self.shelf_list.bind("<Button-3>", self._popup_shelf_menu)
        # 点击顶部进度线跳转
        for widget in (self.progress_track, self.progress_fill):
            widget.bind("<Button-1>", self._on_progress_click)
            widget.bind("<Motion>", lambda e: self.progress_track.configure(cursor="hand2"))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _scroll_by(self, amount: int, what: str) -> str:
        self.reader.yview_scroll(amount, what)
        self._on_view_changed()
        return "break"

    def _moveto(self, ratio: float) -> str:
        self.reader.yview_moveto(ratio)
        self._on_view_changed()
        return "break"

    def jump_chapter(self, delta: int) -> None:
        if not self._chapter_offsets:
            return
        current = self.current_chapter_index()
        target = max(0, min(len(self._chapter_offsets) - 1, current + delta))
        if target == current and delta:
            return
        self.goto_chapter(target)

    def current_chapter_index(self) -> int:
        """当前视口顶部落在第几个目录项上。"""
        index = 0
        for idx, offset in self._chapter_offsets:
            if offset <= self._char_offset:
                index = idx
            else:
                break
        return index

    def goto_chapter(self, index: int) -> None:
        if not self._chapter_offsets or not (0 <= index < len(self._chapter_offsets)):
            return
        _idx, offset = self._chapter_offsets[index]
        self.reader.yview(self._offset_to_index(offset))
        self._sync_from_view()
        self._sync_toc_selection()
        chapters = self.store.state["chapters"]
        if index < len(chapters):
            self.status(f"第 {index + 1} / {len(self._chapter_offsets)} 节："
                        f"{elide(chapters[index].title, 24)}")

    def _on_plain_key(self, event: tk.Event) -> Optional[str]:
        focus = self.root.focus_get()
        if isinstance(focus, (tk.Entry, tk.Text)) and focus is not self.reader:
            return None            # 正在输入框里打字，不劫持
        char = (event.char or "").lower()
        if char == "t" or event.keysym.lower() == "t":
            self.toggle_shelf()
        elif char == "s":
            self.show_sidebar("settings")
        elif char in ("c", "l"):
            self.show_sidebar("toc")
        elif char == "e":
            self.toggle_eye_care()
        elif char == "j":
            self.jump_chapter(1)
        elif char == "k":
            self.jump_chapter(-1)
        return None

    def on_close(self) -> None:
        try:
            self.save_progress(force=True)
            self._flush_settings()
        finally:
            self.lib.close()
            self.root.destroy()

    # ------------------------------------------------------------------
    # 阅读区尺寸变化：重算页宽 + 保持阅读位置
    # ------------------------------------------------------------------

    def _on_reader_configure(self, _event=None) -> None:
        if self._pad_job:
            self.root.after_cancel(self._pad_job)
        self._pad_job = self.root.after(60, self._reflow)

    def _reflow(self) -> None:
        self._pad_job = None
        self._apply_page_layout()
        if self._char_offset > 0 or self._ratio > 0:
            # 重新换行后按「字符下标」回到同一处文字（比比例更准）
            self._restoring = True
            try:
                self.reader.yview(self._offset_to_index(self._char_offset)
                                  if self._char_offset > 0 else "1.0")
            finally:
                self.root.after(120, self._end_restore)

    def _on_desk_configure(self, _event=None) -> None:
        """桌面尺寸变化（窗口缩放 / 侧栏开合）→ 防抖重排。"""
        if self._desk_job:
            self.root.after_cancel(self._desk_job)
        self._desk_job = self.root.after(70, self._on_desk_resize)

    # 兼容旧引用
    def _on_reader_configure(self, _event=None) -> None:
        self._on_desk_configure(_event)

    def _on_desk_resize(self) -> None:
        self._desk_job = None
        width, height = self._desk_size()
        changed = (width, height) != self._desk_last_size
        self._desk_last_size = (width, height)
        self._apply_page_layout()
        self._reflow()
        if changed and self._wallpaper is not None:
            # 壁纸要按新尺寸重做，但重做比较贵，再延迟一点
            if self._wallpaper_job:
                self.root.after_cancel(self._wallpaper_job)
            self._wallpaper_job = self.root.after(350, self._reload_wallpaper)

    def _reload_wallpaper(self) -> None:
        self._wallpaper_job = None
        if self._wallpaper is None:
            return
        self._set_wallpaper_from_settings()
        self._refresh_tints()
        self._apply_theme(force=True)

    # ------------------------------------------------------------------
    # 全局壁纸
    # ------------------------------------------------------------------

    def action_pick_wallpaper(self) -> None:
        if wallpaper is None:
            messagebox.showerror("全局壁纸", "壁纸模块不可用（pyreader/wallpaper.py 缺失）",
                                 parent=self.root)
            return
        exts = " ".join("*" + e for e in wallpaper.supported_extensions())
        path = filedialog.askopenfilename(
            parent=self.root, title="选择全局壁纸",
            filetypes=[("图片文件", exts), ("所有文件", "*.*")])
        if not path:
            return
        ok, why = wallpaper.can_open(path)
        if not ok:
            messagebox.showerror("无法使用这张图", why, parent=self.root)
            return
        self.set_settings(wallpaper_on=True, wallpaper_path=path)
        self.status(f"壁纸已应用：{os.path.basename(path)}")

    def action_clear_wallpaper(self) -> None:
        self.set_settings(wallpaper_on=False, wallpaper_path="")
        self.status("已清除壁纸")

    # ------------------------------------------------------------------
    # 沉浸模式
    # ------------------------------------------------------------------

    def toggle_immersive(self) -> None:
        on = not bool(self.settings.get("immersive"))
        self.set_settings(immersive=on)
        self.status("沉浸模式已开启（双击正文 / F11 退出）" if on else "已退出沉浸模式")

    def _apply_immersive(self) -> None:
        on = bool(self.settings.get("immersive"))
        if on == getattr(self, "_immersive_state", None):
            return
        self._immersive_state = on
        if on:
            self.shelf.grid_remove()
            self.sidebar.grid_remove()
            self.toolbar.grid_remove()
        else:
            self.toolbar.grid()
            if self.settings.get("shelf_open"):
                self.shelf.grid()
            if self.store.state["sidebar"]:
                self.sidebar.grid()
        self.root.after(60, self._apply_page_layout)

    # ------------------------------------------------------------------
    # 鼠标交互
    # ------------------------------------------------------------------

    def _on_ctrl_wheel(self, event) -> str:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:                      # X11 用 Button-4/5
            delta = 120 if int(getattr(event, "num", 0) or 0) == 4 else -120
        self.nudge_font(1 if delta > 0 else -1)
        return "break"

    def _on_progress_click(self, event) -> None:
        width = max(1, self.progress_track.winfo_width())
        ratio = max(0.0, min(1.0, event.x / width))
        self.reader.yview_moveto(ratio)
        self._sync_from_view()
        self._sync_toc_selection()
        self.status(f"跳转到约 {int(ratio * 100)}%")

    def _popup_reader_menu(self, event) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="上一节", command=lambda: self.jump_chapter(-1))
        menu.add_command(label="下一节", command=lambda: self.jump_chapter(1))
        menu.add_separator()
        menu.add_command(label="回到开头", command=lambda: self._moveto(0.0))
        menu.add_command(label="跳到结尾", command=lambda: self._moveto(1.0))
        menu.add_separator()
        menu.add_command(label="放大字号", command=lambda: self.nudge_font(1))
        menu.add_command(label="缩小字号", command=lambda: self.nudge_font(-1))
        menu.add_separator()
        menu.add_command(label="护眼模式", command=self.toggle_eye_care)
        menu.add_command(label="沉浸模式", command=self.toggle_immersive)
        menu.add_command(label="更换壁纸…", command=self.action_pick_wallpaper)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _popup_shelf_menu(self, event) -> None:
        index = self.shelf_list.nearest(event.y)
        if 0 <= index < self.shelf_list.size():
            self.shelf_list.selection_clear(0, "end")
            self.shelf_list.selection_set(index)
            self._on_shelf_select()
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="打开", command=lambda: self._on_shelf_select())
        menu.add_command(label="重命名…", command=self.action_rename)
        menu.add_separator()
        menu.add_command(label="删除", command=self.action_delete)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ------------------------------------------------------------------
    # 帮助
    # ------------------------------------------------------------------

    def action_shortcuts(self) -> None:
        messagebox.showinfo(
            "快捷键",
            "Ctrl+O        导入文件\n"
            "Ctrl+V        粘贴剪贴板内容（一步成书；剪贴板里是文件则直接导入文件）\n"
            "Ctrl+Shift+V  粘贴并预览（可改书名、可重新读取剪贴板）\n"
            "Ctrl+Z        撤销上一次粘贴\n"
            "Ctrl+B        书架\n"
            "Ctrl+L        目录\n"
            "Ctrl+,        阅读设置\n"
            "Ctrl+E        护眼模式\n"
            "Ctrl+= / -    放大 / 缩小字号\n"
            "Alt+↑ / ↓     上一节 / 下一节\n"
            "PgUp / PgDn   翻页（空格下翻一页）\n"
            "Home / End    跳到开头 / 结尾\n"
            "T / S / C / E 书架 / 设置 / 目录 / 护眼（焦点不在输入框时）\n"
            "Esc           关闭侧栏",
            parent=self.root)

    def action_about(self) -> None:
        messagebox.showinfo(
            "关于",
            "文本阅读器 · Python 版\n\n"
            "技术栈：Python 3 + tkinter/ttk（标准库）\n"
            "存储：SQLite（书库） + JSON（设置），零第三方依赖\n"
            f"数据目录：{self.data_dir}",
            parent=self.root)


# ==========================================================================
# 对话框
# ==========================================================================


class SimplePrompt:
    """单行输入对话框（重命名用）。"""

    def __init__(self, app: "ReaderApp", title: str, label: str, initial: str):
        S = app.S
        pal = app.pal
        parent = app.root
        self.result: Optional[str] = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.configure(bg=pal.chrome)
        self.top.transient(parent)
        self.top.resizable(False, False)
        frame = tk.Frame(self.top, bg=pal.chrome)
        frame.pack(fill="both", expand=True, padx=S(16), pady=S(14))
        tk.Label(frame, text=label, bg=pal.chrome, fg=pal.fg, font=app.font_ui).pack(anchor="w")
        self.var = tk.StringVar(value=initial)
        entry = tk.Entry(frame, textvariable=self.var, width=34, bd=1, relief="solid",
                         bg=pal.bg, fg=pal.fg, insertbackground=pal.fg, highlightthickness=0,
                         font=app.font_ui)
        entry.pack(fill="x", pady=(S(6), S(12)))
        entry.focus_set()
        entry.select_range(0, "end")
        row = tk.Frame(frame, bg=pal.chrome)
        row.pack(fill="x")
        tk.Button(row, text="取消", command=self._cancel, bd=0, padx=S(12), pady=S(4), cursor="hand2",
                  font=app.font_ui, bg=pal.chrome, fg=pal.fg,
                  activebackground=pal.hover).pack(side="right")
        tk.Button(row, text="确定", command=self._ok, bd=0, padx=S(12), pady=S(4), cursor="hand2",
                  font=app.font_ui, bg=pal.accent, fg=pal.on_accent, activebackground=pal.accent,
                  activeforeground=pal.on_accent).pack(side="right", padx=(0, S(8)))
        entry.bind("<Return>", lambda e: self._ok())
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.grab_set()
        parent.wait_window(self.top)

    def _ok(self) -> None:
        self.result = self.var.get()
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()


class PasteDialog:
    """粘贴预览：打开时已经填好剪贴板内容，改完点「导入」即可。

    日常用法其实不需要它（工具栏「粘贴」/ Ctrl+V 是一步到位），
    这里给的是「想先看看、改改书名」的路径。
    """

    def __init__(self, app: ReaderApp, prefill: str = ""):
        self.app = app
        S = app.S
        pal = app.pal
        self.top = tk.Toplevel(app.root)
        self.top.title("粘贴预览")
        self.top.configure(bg=pal.chrome)
        self.top.transient(app.root)
        self.top.geometry(f"{S(660)}x{S(560)}")
        self.top.minsize(S(460), S(360))
        frame = tk.Frame(self.top, bg=pal.chrome)
        frame.pack(fill="both", expand=True, padx=S(16), pady=S(14))

        head = tk.Frame(frame, bg=pal.chrome)
        head.pack(fill="x")
        tk.Label(head, text="书名", bg=pal.chrome, fg=pal.soft, font=app.font_ui).pack(side="left")
        self.label_count = tk.Label(head, text="", bg=pal.chrome, fg=pal.soft, font=app.font_ui_small)
        self.label_count.pack(side="right")
        self.var_title = tk.StringVar()
        entry = tk.Entry(frame, textvariable=self.var_title, bd=1, relief="solid", bg=pal.bg, fg=pal.fg,
                         insertbackground=pal.fg, highlightthickness=0, font=app.font_ui)
        entry.pack(fill="x", pady=(S(4), S(2)))
        self.label_hint = tk.Label(frame, text="", bg=pal.chrome, fg=pal.soft, font=app.font_ui_small)
        self.label_hint.pack(anchor="w", pady=(0, S(8)))

        wrap = tk.Frame(frame, bg=pal.chrome)
        wrap.pack(fill="both", expand=True, pady=(0, S(10)))
        self.text = tk.Text(wrap, wrap="word", bd=1, relief="solid", bg=pal.bg, fg=pal.fg,
                            insertbackground=pal.fg, highlightthickness=0, undo=True,
                            font=app.font_body)
        scroll = tk.Scrollbar(wrap, command=self.text.yview, width=S(SCROLLBAR_WIDTH), bd=0,
                              highlightthickness=0, bg=pal.chrome, troughcolor=pal.chrome,
                              activebackground=pal.accent)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        row = tk.Frame(frame, bg=pal.chrome)
        row.pack(fill="x")
        tk.Button(row, text="重新读取剪贴板", command=self._reload, bd=0, padx=S(10), pady=S(4),
                  cursor="hand2", font=app.font_ui, bg=pal.chrome, fg=pal.fg,
                  activebackground=pal.hover).pack(side="left")
        tk.Button(row, text="取消", command=self.top.destroy, bd=0, padx=S(14), pady=S(4), cursor="hand2",
                  font=app.font_ui, bg=pal.chrome, fg=pal.fg,
                  activebackground=pal.hover).pack(side="right")
        tk.Button(row, text="导入（Ctrl+Enter）", command=self._submit, bd=0, padx=S(14), pady=S(4),
                  cursor="hand2", font=app.font_ui, bg=pal.accent, fg=pal.on_accent,
                  activebackground=pal.accent,
                  activeforeground=pal.on_accent).pack(side="right", padx=(0, S(8)))

        self.text.bind("<<Modified>>", self._on_modified)
        self.var_title.trace_add("write", lambda *_: self._refresh())
        self.top.bind("<Escape>", lambda e: self.top.destroy())
        self.top.bind("<Control-Return>", lambda e: self._submit())
        self._reload(prefill)
        self.text.focus_set()
        self.top.grab_set()

    # -- 内部 ---------------------------------------------------------------

    def _reload(self, prefill: str = "") -> None:
        content = prefill or self.app.clipboard_text()
        self.text.delete("1.0", "end")
        if content:
            self.text.insert("1.0", content)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self._refresh()

    def _on_modified(self, _event=None) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._refresh()

    def _suggest_title(self) -> str:
        return textproc.guess_title("", self.text.get("1.0", "end-1c"))

    def _refresh(self) -> None:
        content = self.text.get("1.0", "end-1c")
        chars = len(content)
        lines = content.count("\n") + (1 if content.strip() else 0)
        self.label_count.configure(text=f"{chars:,} 字 · {lines} 行")
        typed = self.var_title.get().strip()
        if typed:
            self.label_hint.configure(text="书名已手动指定")
        else:
            self.label_hint.configure(text=f"留空则自动取首行：{elide(self._suggest_title(), 22)}")

    def _submit(self) -> None:
        content = self.text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showwarning("粘贴预览", "内容为空：先复制一段文字，或点「重新读取剪贴板」",
                                   parent=self.top)
            return
        title = self.var_title.get().strip() or self._suggest_title()
        self.top.destroy()
        meta = self.app.add_book_from_text(title, content)
        if meta:
            self.app._paste_undo_id = meta.id
            self.app.status(f"已粘贴《{elide(meta.title, 18)}》· {len(content):,} 字 · Ctrl+Z 撤销")

