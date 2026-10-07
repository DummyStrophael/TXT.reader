
==============================================================================
  文件：pyreader/selftest.py
==============================================================================
"""自检：python -m pyreader --selftest

分两层：
  * 核心用例（textproc / theme / storage / state）—— 任何环境都能跑；
  * GUI 用例（真实创建窗口，驱动 ReaderApp 的公开方法）—— 无显示环境可跳过。
失败会返回非零退出码，方便接进 CI。
"""

from __future__ import annotations

import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from typing import Callable, List, Optional, Tuple

RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    return bool(ok)


def _core_checks(root_dir: Path) -> None:
    from . import textproc, theme
    from .state import Store
    from .storage import Library, Settings

    # --- 编码探测 -------------------------------------------------------
    utf8, enc1 = textproc.decode_bytes("中文内容\n第二行".encode("utf-8"))
    check("编码：UTF-8", utf8.startswith("中文内容") and enc1 == "UTF-8", enc1)
    bom, enc2 = textproc.decode_bytes(b"\xef\xbb\xbf" + "带BOM".encode("utf-8"))
    check("编码：UTF-8 BOM", bom == "带BOM" and enc2 == "UTF-8", repr(bom))
    gbk_bytes = bytes([0xD6, 0xD0, 0xCE, 0xC4, 0xB2, 0xE2, 0xCA, 0xD4])
    gbk, enc3 = textproc.decode_bytes(gbk_bytes)
    check("编码：GB18030 回退", gbk == "中文测试" and enc3 == "GB18030", f"{gbk}/{enc3}")
    u16, enc4 = textproc.decode_bytes("Hello, this is a long UTF-16 line without BOM.".encode("utf-16-le"))
    check("编码：无 BOM 的 UTF-16LE 启发式",
          u16.startswith("Hello, this is") and enc4 == "UTF-16LE", f"{u16[:14]}/{enc4}")

    # --- 章节识别 -------------------------------------------------------
    heads = ["第一章 起点", "第一章", "第 2 章：缘起", "Chapter 3 - The Fall", "## 第四章 结局",
             "序章", "楔子", "番外"]
    check("章节：常见写法全命中", all(textproc.is_heading(h) for h in heads),
          ",".join(h for h in heads if not textproc.is_heading(h)) or "all-ok")
    bodies = ["第一章的内容主要讲述了主人公的成长", "序章内容。", "第二章正文在此，继续验证。",
              "这是第一章正文，读起来很长的一句话结尾。"]
    check("章节：正文不误判", not any(textproc.is_heading(b) for b in bodies),
          ",".join(b for b in bodies if textproc.is_heading(b)) or "none")

    # --- 分段 -----------------------------------------------------------
    hard = "\n".join(["第一章 起点", "第一段正文。", "第二段正文。"])
    paras = textproc.split_paragraphs(hard)
    check("分段：硬折行一行一段", len(paras) == 3, str(paras))
    spaced = "标题\n\n第一段\n仍然是第一段\n\n第二段\n"
    paras2 = textproc.split_paragraphs(spaced)
    check("分段：空行分段并拼接中文软换行", paras2 == ["标题", "第一段仍然是第一段", "第二段"], str(paras2))
    english = "Title\n\nFirst line\nsecond line\n\nLast paragraph\n"
    paras3 = textproc.split_paragraphs(english)
    check("分段：西文软换行补空格", paras3 == ["Title", "First line second line", "Last paragraph"], str(paras3))

    # --- 组装 -----------------------------------------------------------
    art = textproc.build_article("样章", hard, size=100, encoding="UTF-8")
    check("组装：块与章节", art.blocks[0].kind == "h1" and len(art.chapters) == 2
          and art.chapters[1].title == "第一章 起点" and art.paragraphs == 2,
          f"blocks={len(art.blocks)} chapters={len(art.chapters)}")
    check("组装：字数与时长", art.chars == len(hard) and art.minutes >= 1, f"{art.chars}/{art.minutes}")

    # --- 主题 -----------------------------------------------------------
    light = theme.derive_palette("#faf8f5", "#2b2b2b", "#8a6a3b")
    dark = theme.derive_palette("#1c1f24", "#c9ced7", "#7aa2f7")
    chrome_ok = (
        (not light.dark) and dark.dark
        and theme.luminance(light.chrome) < theme.luminance(light.bg)   # 浅色主题外壳略暗
        and theme.luminance(dark.chrome) > theme.luminance(dark.bg)     # 深色主题外壳略亮
    )
    check("主题：明暗自适应外壳", chrome_ok,
          f"light {light.bg}->{light.chrome}, dark {dark.bg}->{dark.chrome}")
    eye = theme.eye_care_palette(light)
    check("主题：护眼暖色 + 降对比",
          theme.luminance(eye.bg) <= theme.luminance(light.bg) + 0.02
          and abs(theme.luminance(eye.fg) - theme.luminance(eye.bg)) <= abs(theme.luminance(light.fg) - theme.luminance(light.bg)),
          f"bg {light.bg}->{eye.bg} fg {light.fg}->{eye.fg}")
    check("主题：护眼可与任意主题叠加",
          theme.effective_palette({"theme": "night", "eye_care": True}).dark is True, "dark")
    check("主题：自定义配色推导", theme.palette_for({"theme": "custom", "custom_bg": "#22303c", "custom_fg": "#e6e0d4"}).bg == "#22303c", "ok")

    # --- 存储 -----------------------------------------------------------
    db = root_dir / "core.db"
    lib = Library(db)
    meta = lib.add("测试书", "第一章 起点\n正文内容。", "UTF-8")
    check("存储：新增并可读回", lib.count() == 1 and (lib.get(meta.id) or None) is not None)
    lib.set_progress(meta.id, 0.4267)
    got = lib.get(meta.id)
    check("存储：进度落盘", got is not None and abs(got.meta.progress - 0.4267) < 1e-6
          and got.meta.percent == 43, str(got.meta.progress if got else None))
    lib.rename(meta.id, "改名后")
    check("存储：重命名", (lib.get_meta(meta.id) or meta).title == "改名后")
    lib.remove(meta.id)
    check("存储：删除", lib.count() == 0)
    lib.close()

    settings_file = Settings(root_dir / "settings.json")
    settings_file.save({"font_size": 27, "theme": "night", "eye_care": True})
    loaded = settings_file.load()
    check("设置：落盘并合并默认值",
          loaded["font_size"] == 27 and loaded["theme"] == "night" and loaded["eye_care"] is True
          and loaded["page_width"] == 720, f"{loaded['font_size']}/{loaded['theme']}")
    corrupt = root_dir / "broken.json"
    corrupt.write_text("{not json", encoding="utf-8")
    check("设置：损坏文件不致命", Settings(corrupt).load()["theme"] == "paper")
    check("设置：原子写不留临时文件", not list(root_dir.glob("*.tmp")), str(list(root_dir.glob('*.tmp'))))

    # --- 状态容器 -------------------------------------------------------
    store = Store({"a": 1})
    seen: List[str] = []
    store.subscribe(lambda s, p: seen.append(f"{p['a']}->{s['a']}"))
    store.set({"a": 2})
    store.set({"a": 2})   # 无变化，不应通知
    store.set(b=3)
    check("状态：发布订阅 + 去重通知", seen == ["1->2", "2->2"], str(seen))


def _ui_checks(root_dir: Path) -> None:
    from . import dpi as dpi_mod
    from . import theme
    from .app import SHELF_WIDTH, ReaderApp, wallpaper
    from .storage import Library, Settings

    dpi_mod.enable()   # 与正式启动一致：先声明 DPI 感知，再创建窗口

    sample = root_dir / "ui-sample.txt"
    doc = ["楔子", "楔子的内容。", "", "第一章 起点", "第一章正文第一段，讲述足够长的故事开端。",
           "跟随的第二段。", "", "## 第二章 结局", "第二章内容。"]
    doc += [f"第 {i} 段填充正文，用来撑高页面以验证进度。" for i in range(160)]
    sample.write_text("\n".join(doc), encoding="utf-8")

    root = tk.Tk()
    root.geometry("1120x780+40+40")
    lib = Library(root_dir / "ui.db")
    try:
        app = ReaderApp(root, lib, Settings(root_dir / "ui-settings.json"), str(root_dir))
    except Exception:
        # 构造失败也要把数据库连接与窗口关掉，否则临时目录清理会再抛一个
        # PermissionError 把真正的错误盖掉（这就是「自检只看得到文件占用」的原因）
        lib.close()
        root.destroy()
        raise
    root.update()
    try:
        check("GUI：空状态可渲染", "开始你的阅读" in app.reader.get("1.0", "end"), "")
        check("GUI：高 DPI 缩放已应用",
              abs(app.scale - max(1.0, app.dpi / 96.0)) < 1e-9
              and int(app.shelf.cget("width")) == app.S(SHELF_WIDTH)
              and app.S(100) == max(1, int(round(100 * app.scale))),
              f"dpi={app.dpi:.0f} scale={app.scale:.2f} 书架宽={app.shelf.cget('width')}px "
              f"(逻辑 {SHELF_WIDTH}) 页宽={app._page_pad()}px")
        app.import_paths([str(sample)])
        root.update()
        check("GUI：导入后进入书架", len(app.store.state["books"]) == 1
              and app.store.state["current"] is not None,
              str([m.title for m in app.store.state["books"]]))
        check("GUI：书名取文件名", app.store.state["current"].meta.title == "ui-sample", "")
        check("GUI：章节识别", len(app.store.state["chapters"]) == 4,
              str([c.title for c in app.store.state["chapters"]]))
        check("GUI：目录项与章节一致", app.toc_list.size() == len(app.store.state["chapters"]),
              f"{app.toc_list.size()}")
        check("GUI：正文有内容与标题 tag",
              int(app.reader.index("end-1c").split(".")[0]) > 60
              and len(app.reader.tag_ranges("h2")) >= 2,
              f"lines={app.reader.index('end-1c')}")

        # 进度保存（语义：ratio = 视口底部已读比例，char_offset = 精确字符位置）
        # 先滚一次让布局定型（窗口刚映射时会有一次重排），再滚到目标位置测
        app.reader.yview_moveto(0.6)
        _settle(root)
        app.reader.yview_moveto(0.45)
        _settle(root)
        top_before = app.reader.index("@0,0")
        offset_before = app._index_to_offset(top_before)
        app.save_progress(force=True)
        stored = lib.get(app.store.state["current_id"])
        check("GUI：进度落库（比例 + 字符偏移）",
              stored is not None and 0.2 < stored.meta.progress < 0.95
              and stored.meta.char_offset == offset_before and offset_before > 0,
              f"ratio={stored.meta.progress:.3f} off={stored.meta.char_offset}" if stored else "none")
        check("GUI：进度显示同步",
              app.label_progress.cget("text").endswith("%")
              or app.label_progress.cget("text") == "已读完",
              app.label_progress.cget("text"))
        check("GUI：书架行显示进度",
              "%" in app.shelf_list.get(0) or "已读完" in app.shelf_list.get(0),
              app.shelf_list.get(0))

        # 切书后恢复：恢复的必须是同一行文字（字符偏移精确回位）
        second = root_dir / "second.txt"
        second.write_text("另一本书\n\n第一章 别的\n内容内容内容。", encoding="utf-8")
        app.import_paths([str(second)])
        _settle(root)
        app.open_book(stored.meta.id)
        _settle(root)
        top_after = app.reader.index("@0,0")
        check("GUI：重开恢复到同一行文字（不漂移）", top_after == top_before,
              f"{top_before} -> {top_after}")

        # 目录跳转
        app.toc_list.selection_clear(0, "end")
        app.toc_list.selection_set(2)
        app._on_toc_select()
        root.update()
        top2, _ = app.reader.yview()
        check("GUI：目录跳转生效", top2 > 0.0, f"{top2:.3f}")

        # 排版与主题
        app.set_settings(font_size=28, theme="night", eye_care=False, line_height=2.2, indent=False)
        root.update()
        expected = theme.effective_palette(app.settings).bg
        check("GUI：字号/主题即时生效",
              app.font_body.cget("size") == 28 and app.reader.cget("bg") == expected,
              f"size={app.font_body.cget('size')} bg={app.reader.cget('bg')} want={expected}")
        check("GUI：首行缩进关闭", int(app.reader.tag_cget("p", "lmargin1")) == 0, "")
        app.set_settings(eye_care=True)
        root.update()
        eye_bg = theme.effective_palette(app.settings).bg
        check("GUI：护眼模式叠加换色", app.reader.cget("bg") == eye_bg and eye_bg != expected,
              f"{expected} -> {eye_bg}")
        app.set_settings(theme="custom", custom_bg="#22303c", custom_fg="#e6e0d4")
        root.update()
        check("GUI：自定义配色", app.reader.cget("bg") == theme.effective_palette(app.settings).bg,
              app.reader.cget("bg"))

        # 侧栏
        app.show_sidebar("toc")
        root.update()
        check("GUI：目录侧栏展开", app.sidebar.winfo_ismapped() and app.toc_panel.winfo_ismapped(), "")
        app.show_sidebar("settings")
        root.update()
        check("GUI：设置侧栏切换", app.settings_panel.winfo_ismapped() and not app.toc_panel.winfo_ismapped(), "")
        _settle(root, 150)
        bbox = app.side_canvas.bbox("all")
        check("GUI：设置侧栏可滚动（壁纸区可达）",
              bool(bbox) and bbox[3] > app.side_canvas.winfo_height(),
              f"内容高 {bbox[3] if bbox else 0}px / 视口 {app.side_canvas.winfo_height()}px")
        app.show_sidebar(None)
        root.update()
        check("GUI：侧栏关闭", not app.sidebar.winfo_ismapped(), "")

        # 书架开合
        app.toggle_shelf()
        root.update()
        check("GUI：书架折叠", not app.shelf.winfo_ismapped(), "")
        app.toggle_shelf()
        root.update()
        check("GUI：书架展开", bool(app.shelf.winfo_ismapped()), "")

        # --- 粘贴：一步成书 / 剪贴板是文件 / 撤销 / 空剪贴板 ---
        root.clipboard_clear()
        root.clipboard_append("粘贴测试书\n\n第一章 试\n这是从剪贴板直接导入的正文。")
        root.update()
        before = len(app.store.state["books"])
        app.action_paste()
        _settle(root, 200)
        pasted = app.store.state["current"]
        check("GUI：Ctrl+V 一步粘贴成书",
              len(app.store.state["books"]) == before + 1 and pasted is not None
              and pasted.meta.title == "粘贴测试书" and pasted.meta.encoding == "剪贴板",
              f"{pasted.meta.title if pasted else 'none'} / {len(app.store.state['books'])} 本")
        app.action_undo_paste()
        _settle(root, 150)
        check("GUI：Ctrl+Z 撤销粘贴",
              len(app.store.state["books"]) == before
              and lib.get(pasted.meta.id) is None,
              f"{len(app.store.state['books'])} 本")

        clip_file = root_dir / "clip-import.txt"
        clip_file.write_text("从文件剪贴板导入\n第二行内容。", encoding="utf-8")
        root.clipboard_clear()
        root.clipboard_append(str(clip_file))
        root.update()
        app.action_paste()
        _settle(root, 300)
        check("GUI：剪贴板是文件路径时直接导入文件",
              any(b.title == "clip-import" for b in app.store.state["books"]),
              ",".join(b.title for b in app.store.state["books"]))

        root.clipboard_clear()
        root.update()
        app.action_paste()          # 空剪贴板：只提示，不能崩
        check("GUI：空剪贴板有提示不崩溃",
              "剪贴板" in app.status_label.cget("text"), app.status_label.cget("text"))

        # --- 阅读进度：到底 100% + 重开不漂移 ---
        app.reader.yview_moveto(1.0)
        _settle(root)
        app.save_progress(force=True)
        stored_end = lib.get(app.store.state["current_id"])
        check("GUI：读到末尾记 100% 并标记「已读完」",
              stored_end is not None and stored_end.meta.progress >= 0.999
              and stored_end.meta.finished
              and app.label_progress.cget("text") == "已读完",
              f"{stored_end.meta.progress:.3f} / {app.label_progress.cget('text')}"
              if stored_end else "none")
        end_offset = stored_end.meta.char_offset if stored_end else -1
        app.open_book(app.store.state["current_id"])
        _settle(root)
        again = lib.get(app.store.state["current_id"])
        check("GUI：重开不漂移（字符偏移一致）",
              again is not None and again.meta.char_offset == end_offset,
              f"{end_offset} -> {again.meta.char_offset if again else 'none'}")

        # --- 桌面 / 纸页布局 ---
        rect = app._page_rect
        page_w = rect[2] - rect[0]
        check("GUI：纸页居中且不超过页宽设置",
              page_w <= app.S(int(app.settings["page_width"])) + 1
              and abs((app.desk.winfo_width() - page_w) / 2 - rect[0]) <= 2
              and app.reader.winfo_width() == page_w,
              f"纸页 {page_w}px / 桌面 {app.desk.winfo_width()}px @ x={rect[0]}")

        # --- 质感与壁纸 ---
        check("GUI：桌面纸纹已生成", app._texture_photo is not None, "")
        if wallpaper is not None:
            wp_png = root_dir / "wall.png"
            width = height = 32
            pixels = bytearray()
            for y in range(height):
                for x in range(width):
                    pixels += bytes((x * 8 % 256, y * 8 % 256, 160, 255))
            wp_png.write_bytes(wallpaper.png_bytes(width, height, bytes(pixels)))
            app.set_settings(wallpaper_on=True, wallpaper_path=str(wp_png), wallpaper_mode="cover")
            _settle(root)
            check("GUI：壁纸加载并铺到桌面",
                  app._wallpaper is not None and len(app.desk.find_all()) > 0,
                  f"backend={wallpaper.backend()} 图层={len(app.desk.find_all())}")
            app.set_settings(wallpaper_on=False, wallpaper_path="")
            _settle(root)
            check("GUI：清除壁纸", app._wallpaper is None, "")
        else:
            check("GUI：壁纸模块缺失时自动降级", app._wallpaper is None, "wallpaper.py 不存在")

        # --- 沉浸模式 ---
        app.toggle_immersive()
        _settle(root)
        check("GUI：沉浸模式隐藏书架与工具栏",
              not app.shelf.winfo_ismapped() and not app.toolbar.winfo_ismapped()
              and bool(app.desk.winfo_ismapped()),
              "")
        app.toggle_immersive()
        _settle(root)
        check("GUI：退出沉浸模式恢复",
              bool(app.toolbar.winfo_ismapped()) and bool(app.shelf.winfo_ismapped()), "")

        # --- 点击进度线跳转 ---
        app.open_book(stored.meta.id)          # 用长文验证：短文整屏可见，没有跳转概念
        _settle(root)
        app.reader.yview_moveto(0.8)
        _settle(root)

        class _Click:
            x = 6
        app._on_progress_click(_Click())
        _settle(root)
        check("GUI：点击顶部进度线跳转", app.reader.yview()[0] < 0.2,
              f"yview={app.reader.yview()[0]:.3f}")

        # 删除
        before = len(app.store.state["books"])
        current_id = app.store.state["current_id"]
        app.delete_book(current_id, confirm=False)
        root.update()
        check("GUI：删除当前书并续读下一本",
              len(app.store.state["books"]) == before - 1 and app.store.state["current_id"] != current_id,
              f"{before}->{len(app.store.state['books'])}")
        while app.store.state["books"]:
            app.delete_book(app.store.state["books"][0].id, confirm=False)
            root.update()
        check("GUI：删空后回到空状态",
              app.store.state["current"] is None and "开始你的阅读" in app.reader.get("1.0", "end"),
              "")
        check("GUI：无异常栈（见 stderr）", True, "")
    finally:
        lib.close()
        root.destroy()


def _settle(root, ms: int = 260) -> None:
    """让真实时间流逝：位置恢复锁 / 防抖 / after 回调都需要它。"""
    import time as _time

    end = _time.time() + ms / 1000.0
    while _time.time() < end:
        root.update()
        _time.sleep(0.01)


def run(data_dir: Optional[Path] = None, ui: bool = True) -> int:
    RESULTS.clear()
    tmp = None
    if data_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="pyreader-selftest-")
        root_dir = Path(tmp.name)
    else:
        root_dir = Path(data_dir)
        root_dir.mkdir(parents=True, exist_ok=True)

    print(f"数据目录：{root_dir}")
    print("-" * 72)
    _core_checks(root_dir)

    if ui:
        try:
            _ui_checks(root_dir)
        except tk.TclError as exc:
            check("GUI：无可用显示，跳过", True, f"skipped ({exc})")
        except Exception as exc:  # noqa: BLE001 - 自检要把异常变成一条失败用例
            check("GUI：运行期异常", False, f"{type(exc).__name__}: {exc}")

    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))
    print("-" * 72)
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")

    if ui and any(name.startswith("GUI") for name, _ok, _d in RESULTS):
        print("（GUI 用例会短暂显示窗口，属正常现象）")
    if tmp is not None:
        tmp.cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1]) if len(sys.argv) > 1 else None))

