
==============================================================================
  文件：pyreader/wallpaper.py
==============================================================================
"""壁纸（背景图）加载、缩放与着色。

设计要点
--------
* **双后端**：装了 Pillow 就用 Pillow（LANCZOS 缩放、getpixel 取色）；没装 Pillow
  就退回 Tk 自带的 ``tk.PhotoImage``（整数倍 zoom/subsample、get 取色）。
  对外只有 ``backend()`` 一个判断点，其余 API 的**语义**在两种后端下一致
  （取色坐标系、可见区域、四种模式的尺寸关系都相同）；只有平均色的具体数值会
  因采样方式略有差异：Pillow 用 ``resize((1,1))`` 精确求均值，Tk 用 ≤400 点网格
  采样估算（两者通常相差几个色阶，足够侧栏/工具栏融入背景）。
* **dim 统一交给覆盖层**：``photo`` 始终是**未着色**的缩放图，压暗/染色由
  ``dim_overlay()`` 返回的 4×4 半透明覆盖层负责 —— Tk 后端没有像素级混合能力，
  只有这样两种后端的最终显示才一致，``sample()``/``region_color()`` 也不会因为
  后端不同而返回「被压暗」或「未压暗」两种颜色。
  若确实想让 Pillow 把颜色直接混进图里，传 ``bake_dim=True``：此时
  ``dim_in_photo=True`` 且 ``dim_overlay()`` 返回 ``None``，避免叠加两次。
* **PhotoImage 的 GC 陷阱**：``zoom()`` / ``subsample()`` / ``PhotoImage(data=...)``
  返回的都是**新对象**，一旦 Python 侧没有引用，Tk 里的图片会被销毁，界面上
  直接变成空白（实测：``del`` 之后 ``image names`` 里就没了）。本模块把主图
  挂在 ``Wallpaper`` 实例上、覆盖层挂在实例缓存里；调用方自己创建的对象
  （``noise_tile()``、以及 ``dim_overlay().zoom(k)`` 出来的大图）也**必须自己
  保存引用**。
* **photo 的类型随后端变化**：pillow 后端是 ``PIL.ImageTk.PhotoImage``（没有
  ``zoom``/``subsample`` 方法），Tk 后端是 ``tk.PhotoImage``。两者都能直接交给
  ``Canvas.create_image``；需要 ``zoom``/``subsample`` 的只有本模块用 Tk 编码器
  生成的图（覆盖层、纸纹、暗角），它们在两种后端下都是 ``tk.PhotoImage``。
* **Tk 8.6 不能裁剪**：Python 的 ``tkinter.PhotoImage.copy()`` 只接受 ``self``
  （签名就是 ``copy(self)``，没有 ``from_coords``），所以 cover 模式不裁剪：
  缩放结果可能大于目标，由 Canvas 居中放置自然裁掉（``place()`` 返回 ``center``）。
* **Tk 的整数缩放是向上取整**：``subsample(k)`` 的尺寸是 ``ceil(n/k)``（实测
  9→5 with k=2），本模块的尺寸推算按此实现，保证 cover/stretch 结果 ≥ 目标、
  contain/tile 结果 ≤ 目标。

用法::

    wp = Wallpaper("bg.png", (w, h), mode="cover", dim=0.3, tint="#101418")
    anchor, x, y = wp.place()               # 注意顺序是 (anchor, x, y)
    canvas.create_image(x, y, anchor=anchor, image=wp.photo)     # 主图
    ov = wp.dim_overlay()                                        # 压暗层（可能为 None）
    if ov is not None:
        canvas.create_image(0, 0, anchor="nw", image=ov.zoom(k))  # 调用方放大/平铺

命令行自检::

    python -m pyreader.wallpaper          # 打印 [PASS]/[FAIL] 与后端名
    set PYREADER_WALLPAPER_BACKEND=tk     # 有 Pillow 时也可强制走 Tk 回退路径
"""

from __future__ import annotations

import base64
import math
import os
import random
import struct
import sys
import tempfile
import tkinter as tk
import zlib
from typing import Any, Optional, Sequence

from .theme import hex_to_rgb, rgb_to_hex

Mode = str  # "cover" | "contain" | "tile" | "stretch"

RGB = tuple[int, int, int]
Size = tuple[int, int]

MODES: tuple[str, ...] = ("cover", "contain", "tile", "stretch")

# 各后端能打开的扩展名（小写，带点）
PILLOW_EXTENSIONS: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff",
)
TK_EXTENSIONS: tuple[str, ...] = (".png", ".gif", ".ppm", ".pgm")

# 覆盖层默认边长：很小，交给调用方 zoom/平铺（Tk 不能裁剪，小图最省内存）
_OVERLAY_SIZE = 4


class WallpaperError(RuntimeError):
    """壁纸相关的错误，message 一定是中文说明，便于直接显示在界面上。"""


# --------------------------------------------------------------------------
# 后端探测
# --------------------------------------------------------------------------

_PIL_MODULES: Optional[tuple[Any, Any]] = None
_PIL_TRIED = False
_FORCED: Optional[str] = None
_ENV_FORCED = os.environ.get("PYREADER_WALLPAPER_BACKEND", "").strip().lower()


def _pil() -> Optional[tuple[Any, Any]]:
    """尝试导入 Pillow，返回 ``(Image, ImageOps)``；没有则返回 None（结果缓存）。"""
    global _PIL_MODULES, _PIL_TRIED
    if not _PIL_TRIED:
        _PIL_TRIED = True
        try:
            from PIL import Image, ImageOps  # type: ignore[import-not-found]
        except Exception:
            _PIL_MODULES = None
        else:
            _PIL_MODULES = (Image, ImageOps)
    return _PIL_MODULES


def force_backend(name: Optional[str]) -> None:
    """强制指定后端（``"pillow"`` / ``"tk"`` / ``None``=自动）。

    自检与排错用：在没有卸载 Pillow 的情况下也能验证 Tk 回退路径。
    环境变量 ``PYREADER_WALLPAPER_BACKEND`` 可提供同样的初值。
    """
    global _FORCED
    if name is None:
        _FORCED = None
        return
    key = str(name).strip().lower()
    if key not in ("pillow", "tk"):
        raise ValueError(f"未知的后端：{name!r}（可选 'pillow' / 'tk' / None）")
    _FORCED = key


def backend() -> str:
    """返回 "pillow" 或 "tk"，表示当前实际可用的图像后端。"""
    want = _FORCED or _ENV_FORCED
    if want == "tk":
        return "tk"
    have_pil = _pil() is not None
    if want == "pillow" and have_pil:
        return "pillow"
    return "pillow" if have_pil else "tk"


def supported_extensions() -> tuple[str, ...]:
    """当前后端能打开的图片扩展名（小写，带点）。

    pillow: .png .jpg .jpeg .bmp .gif .webp .tif .tiff
    无 pillow（Tk 自带解码器）: .png .gif .ppm .pgm
    """
    return PILLOW_EXTENSIONS if backend() == "pillow" else TK_EXTENSIONS


def _magic_ok(path: str, ext: str) -> tuple[bool, str]:
    """只读文件头 26 字节，廉价判断内容是否像对应格式（Tk 后端用，不解码像素）。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(26)
    except OSError as exc:
        return False, f"文件读不出来：{exc}"
    if not head:
        return False, "文件是空的"
    if ext == ".png":
        if not head.startswith(b"\x89PNG\r\n\x1a\n"):
            return False, "文件内容不是 PNG（可能已损坏或扩展名不对）"
        # 签名之后必须紧跟 IHDR 块：长度(4) + "IHDR"(4) + 宽(4) + 高(4)
        if len(head) < 24 or head[12:16] != b"IHDR":
            return False, "PNG 结构损坏（缺少 IHDR 块）"
        if struct.unpack(">II", head[16:24]) in ((0, 0), (0xFFFFFFFF, 0xFFFFFFFF)):
            return False, "PNG 宽高非法，文件已损坏"
    elif ext == ".gif":
        if head[:6] not in (b"GIF87a", b"GIF89a") or len(head) < 13:
            return False, "文件内容不是 GIF（可能已损坏或扩展名不对）"
    elif ext in (".ppm", ".pgm"):
        if head[:2] not in (b"P1", b"P2", b"P3", b"P4", b"P5", b"P6") or len(head) < 4:
            return False, f"文件内容不是 {ext.lstrip('.').upper()}（缺少 P1~P6 魔数）"
    return True, f"{ext.lstrip('.').upper()} 图片（Tk 后端：已校验文件头，未解码像素）"


def can_open(path: object) -> tuple[bool, str]:
    """(能否打开, 说明文字)，用于界面提示。

    * 文件不存在 → 中文原因；
    * Tk 后端：扩展名必须在 ``.png/.gif/.ppm/.pgm`` 里，且文件头要对（不做完整解码）；
    * Pillow 后端：真的尝试打开一次（Pillow 认识的格式比白名单更多），
      打不开则返回 Pillow 的报错文本。
    """
    p = str(path)
    if not os.path.isfile(p):
        return False, f"图片不存在：{p}"
    ext = os.path.splitext(p)[1].lower()
    if backend() == "pillow":
        Image = _pil()[0]  # type: ignore[index]
        try:
            with Image.open(p) as im:
                width, height = im.size
                fmt = str(im.format or ext.lstrip(".").upper())
        except Exception as exc:  # 打不开的图：给出中文说明 + 原始报错
            return False, f"无法识别的图片：{ext or '（无扩展名）'}（{exc}）"
        return True, f"{fmt} 图片，{width}×{height}"
    if ext not in TK_EXTENSIONS:
        return False, (f"不支持的图片格式：{ext or '（无扩展名）'}"
                       f"（当前环境没有 Pillow，仅支持 {'/'.join(TK_EXTENSIONS)}）")
    ok, detail = _magic_ok(p, ext)
    return (True, detail) if ok else (False, detail)


# --------------------------------------------------------------------------
# 纯 Python PNG 编码（IHDR 8bit truecolor+alpha → color type 6）
# --------------------------------------------------------------------------


def _chunk(tag: bytes, data: bytes) -> bytes:
    """PNG 数据块：长度 + 类型 + 数据 + CRC32(类型+数据)。"""
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(width: int, height: int, rgba: bytes) -> bytes:
    """纯 Python PNG 编码器（zlib + struct + crc32），输入 width*height*4 的 RGBA 字节。

    产出 8bit RGBA（color type 6，无隔行），每行前置 filter byte 0。
    """
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError(f"PNG 尺寸必须为正：{width}x{height}")
    raw = bytes(rgba)
    expect = width * height * 4
    if len(raw) != expect:
        raise ValueError(f"RGBA 字节数不对：期望 {expect}（{width}x{height}x4），实际 {len(raw)}")
    rows = bytearray()
    stride = width * 4
    for y in range(height):  # 每行加 filter byte 0（None）
        rows.append(0)
        rows += raw[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + _chunk(b"IEND", b""))


def _photo_from_options(master: Optional[tk.Misc], **options: Any) -> tk.PhotoImage:
    """统一创建 PhotoImage（显式传 master=None 时走 Tk 默认根窗口）。

    还没有 Tk 根窗口时 tkinter 抛的是英文 ``RuntimeError: Too early to create image``，
    这里统一换成中文的 ``WallpaperError``。
    """
    try:
        return tk.PhotoImage(master=master, **options)
    except RuntimeError as exc:  # 没有默认根窗口
        raise WallpaperError(f"创建 Tk 图片失败：{exc}（请先创建 tkinter.Tk() 根窗口）") from exc
    except tk.TclError as exc:   # 数据本身有问题
        raise WallpaperError(f"Tk 无法解码图片数据：{exc}") from exc


def photo_from_png(data: bytes, master: Optional[tk.Misc] = None) -> tk.PhotoImage:
    """把 PNG 字节 base64 后交给 tk.PhotoImage(data=...)，Tk 8.6 原生支持 PNG（含 alpha）。

    注意：返回的 PhotoImage 必须由调用方保存引用，否则会被 GC 掉、界面变空白。
    """
    payload = base64.b64encode(bytes(data)).decode("ascii")
    return _photo_from_options(master, data=payload)


# --------------------------------------------------------------------------
# 程序化生成的像素（纯 Python，不依赖 Pillow）
# --------------------------------------------------------------------------


def _solid_rgba(width: int, height: int, color: str, alpha: int) -> bytes:
    """单色 RGBA 字节。"""
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError(f"尺寸必须为正：{width}x{height}")
    r, g, b = hex_to_rgb(color)
    a = max(0, min(255, int(alpha)))
    return bytes((r, g, b, a)) * (width * height)


def _noise_rgba(size: int, alpha: int, seed: int) -> bytes:
    """纸纹噪声的 RGBA 字节：半透明白/黑细颗粒，同 seed 必然同结果。"""
    size = int(size)
    if size <= 0:
        raise ValueError(f"噪声尺寸必须为正：{size}")
    a_max = max(0, min(255, int(alpha)))
    rng = random.Random(int(seed))
    out = bytearray(size * size * 4)
    i = 0
    for _ in range(size * size):
        value = 255 if rng.random() < 0.5 else 0  # 白/黑颗粒各半
        a = rng.randint(0, a_max)                 # 明暗随机，整体很淡
        out[i] = value
        out[i + 1] = value
        out[i + 2] = value
        out[i + 3] = a
        i += 4
    return bytes(out)


def _vignette_rgba(size: int, strength: int) -> bytes:
    """径向暗角的 RGBA 字节：中心 alpha=0，越靠边越黑，四角达到 strength。"""
    size = int(size)
    if size <= 0:
        raise ValueError(f"暗角尺寸必须为正：{size}")
    strength = max(0, min(255, int(strength)))
    half = (size - 1) / 2.0
    max_r = math.hypot(half, half) or 1.0  # 中心到角的距离，保证四角 r=1
    out = bytearray(size * size * 4)
    i = 0
    for y in range(size):
        for x in range(size):
            r = math.hypot(x - half, y - half) / max_r
            a = int(round(strength * (r ** 1.6)))  # 1.6 次幂：中间更透、边缘压得更实
            out[i] = 0
            out[i + 1] = 0
            out[i + 2] = 0
            out[i + 3] = max(0, min(255, a))
            i += 4
    return bytes(out)


def solid_overlay(width: int, height: int, color: str, alpha: int,
                  master: Optional[tk.Misc] = None) -> tk.PhotoImage:
    """单色半透明覆盖层（RGBA PNG）。尺寸建议很小（如 4x4），调用方会用 zoom 放大铺满。"""
    return photo_from_png(png_bytes(width, height, _solid_rgba(width, height, color, alpha)), master)


def noise_tile(size: int = 64, alpha: int = 12, seed: int = 7,
               master: Optional[tk.Misc] = None) -> tk.PhotoImage:
    """程序化生成的纸纹噪声：半透明白/黑细颗粒，用于给背景加质感。必须确定性（同 seed 同结果）。"""
    return photo_from_png(png_bytes(size, size, _noise_rgba(size, alpha, seed)), master)


def vignette_tile(size: int = 128, strength: int = 70,
                  master: Optional[tk.Misc] = None) -> tk.PhotoImage:
    """径向暗角：中心完全透明，越靠边 alpha 越接近 strength（黑色）。"""
    return photo_from_png(png_bytes(size, size, _vignette_rgba(size, strength)), master)


# --------------------------------------------------------------------------
# Tk 后端：取色与整数缩放
# --------------------------------------------------------------------------


def _tk_get(photo: tk.PhotoImage, x: int, y: int) -> RGB:
    """读 Tk 图片某点颜色。

    ``PhotoImage.get()`` 在 Python 3 里可能返回 ``(r, g, b)`` 元组，也可能返回
    ``"r g b"`` 字符串（两者都兼容）；带 alpha 的图只返回 RGB，忽略 alpha。
    """
    raw = photo.get(x, y)
    if isinstance(raw, str):
        parts = raw.replace(",", " ").split()
        values = [int(round(float(p))) for p in parts[:3]]
    else:
        values: list[int] = []
        queue = list(raw) if isinstance(raw, (tuple, list)) else [raw]
        while queue and len(values) < 3:  # 容错：可能是嵌套序列
            item = queue.pop(0)
            if isinstance(item, (tuple, list)):
                queue = list(item) + queue
            else:
                values.append(int(round(float(item))))
    if len(values) < 3:
        raise WallpaperError(f"无法解析 Tk 像素值：{raw!r}")
    return (max(0, min(255, values[0])),
            max(0, min(255, values[1])),
            max(0, min(255, values[2])))


def _tk_predicted(n: int, z: int, s: int) -> int:
    """预测 zoom(z) 再 subsample(s) 之后的像素数：zoom 是精确乘法，subsample 是向上取整。"""
    return (n * z + s - 1) // s


def _tk_fit(photo: tk.PhotoImage, target: Size, cover: bool) -> tk.PhotoImage:
    """用整数 zoom/subsample 逼近目标尺寸。

    cover=True ：结果两个方向都 **≥** 目标（居中放置后由 Canvas 裁掉多余部分）；
    cover=False：结果两个方向都 **≤** 目标（contain / tile 用），且本来就装得下时不放大。
    搜索 (z, s) 组合时按「多出来的像素最少」挑选，避免白占内存。
    """
    w, h = photo.width(), photo.height()
    tw, th = target
    if not cover and w <= tw and h <= th:
        return photo  # 已经装得下：不放大（Tk 放大只是复制像素，没有画质收益）

    best: Optional[tuple[tuple[int, ...], int, int]] = None
    for s in range(1, 17):
        for z in range(1, 33):
            pw, ph = _tk_predicted(w, z, s), _tk_predicted(h, z, s)
            if cover:
                if pw < tw or ph < th:
                    continue
                score = (pw * ph,)              # 覆盖：超出面积越小越好
            else:
                if pw > tw or ph > th:
                    continue
                score = (tw * th - pw * ph,)    # 内嵌：离目标越近越好
            key = score + (z, s)
            if best is None or key < best[0]:
                best = (key, z, s)
    if best is None:  # 极端长宽比：退化成单次 zoom 或单次 subsample
        if cover:
            z = max(1, math.ceil(max(tw / w, th / h)))
            s = 1
        else:
            f = min(tw / w, th / h)
            z = 1
            s = max(1, math.ceil(1 / f)) if f > 0 else 1
    else:
        _, z, s = best

    img = photo
    if z > 1:
        img = img.zoom(z)        # 新对象：这里必须持有引用，否则会被 GC
    if s > 1:
        img = img.subsample(s)   # 同样是新对象
    return img


# --------------------------------------------------------------------------
# 壁纸
# --------------------------------------------------------------------------


class Wallpaper:
    """一张已加载/缩放/着色的壁纸。

    属性
    ----
    photo       缩放后的图（cover/stretch 下宽高都 >= 目标；contain/tile 下 <= 目标）
    size        请求的目标尺寸
    image_size  实际图片像素尺寸（等于 ``photo`` 的宽高）
    source_size 原图尺寸
    mode        模式
    backend     "pillow" / "tk"
    dim_in_photo  dim 是否已经混进 photo（只有 pillow + ``bake_dim=True`` 才为 True）
    """

    photo: tk.PhotoImage
    size: Size
    image_size: Size
    source_size: Size
    mode: Mode
    backend: str
    dim_in_photo: bool

    def __init__(self, path: object, size: Sequence[int], mode: Mode = "cover",
                 dim: float = 0.25, tint: str = "#000000",
                 master: Optional[tk.Misc] = None, bake_dim: bool = False) -> None:
        """加载并缩放壁纸；任何失败都抛 ``WallpaperError``（RuntimeError 子类，中文说明）。

        master / bake_dim 是可选扩展：不传时用 Tk 默认根窗口、dim 一律由覆盖层负责。
        """
        self.path = str(path)
        self.size = (int(size[0]), int(size[1]))
        self.mode = str(mode).strip().lower()
        self.dim = max(0.0, min(1.0, float(dim)))
        self.tint = str(tint)
        self.backend = backend()
        self.bake_dim = bool(bake_dim)
        self._master = master
        self._img: Optional[Any] = None      # pillow 后端的 RGB 图（tk 后端为 None）
        self._overlay: Optional[tk.PhotoImage] = None

        if self.size[0] <= 0 or self.size[1] <= 0:
            raise WallpaperError(f"壁纸目标尺寸无效：{self.size[0]}x{self.size[1]}")
        if self.mode not in MODES:
            raise WallpaperError(f"未知的壁纸模式：{mode!r}（可选 {' / '.join(MODES)}）")
        try:
            self._tint_rgb = hex_to_rgb(self.tint)
        except Exception as exc:
            raise WallpaperError(f"颜色值无法解析：{tint!r}（应形如 #101418）") from exc

        self.photo, self.source_size, self.image_size, self.dim_in_photo = self._load()

    # -- 加载 --------------------------------------------------------------

    def _load(self) -> tuple[tk.PhotoImage, Size, Size, bool]:
        if not os.path.isfile(self.path):
            raise WallpaperError(f"图片不存在：{self.path}")
        if self.backend == "pillow":
            return self._load_pillow()
        return self._load_tk()

    def _load_pillow(self) -> tuple[tk.PhotoImage, Size, Size, bool]:
        from PIL import ImageTk  # type: ignore[import-not-found]

        Image, ImageOps = _pil()  # type: ignore[misc]
        lanczos = getattr(Image, "LANCZOS", None) or Image.Resampling.LANCZOS
        try:
            with Image.open(self.path) as handle:
                src = handle.convert("RGB")   # 先转 RGB：PNG 带 alpha 也能取色/混合
        except Exception as exc:
            raise WallpaperError(f"无法打开图片：{self.path}（{exc}）") from exc

        tw, th = self.size
        try:
            if self.mode == "stretch":
                img = src.resize((tw, th), lanczos)
            elif self.mode == "cover":
                # 等比放大到覆盖目标后居中裁剪（多余部分直接丢掉，正好等于目标尺寸）
                img = ImageOps.fit(src, (tw, th), method=lanczos, centering=(0.5, 0.5))
            else:  # contain / tile：只缩小不放大
                img = src
                if src.width > tw or src.height > th:
                    img = src.copy()
                    img.thumbnail((tw, th), lanczos)
        except Exception as exc:
            raise WallpaperError(f"缩放图片失败：{self.path}（{exc}）") from exc

        dim_in_photo = False
        if self.bake_dim and self.dim > 0:
            img = Image.blend(img, Image.new("RGB", img.size, self._tint_rgb), self.dim)
            dim_in_photo = True

        try:
            photo = ImageTk.PhotoImage(img, master=self._master)
        except RuntimeError as exc:  # 没有默认根窗口
            raise WallpaperError(f"创建 Tk 图片失败：{exc}（请先创建 tkinter.Tk() 根窗口）") from exc
        except Exception as exc:
            raise WallpaperError(f"创建 Tk 图片失败：{exc}") from exc
        self._img = img
        return photo, (src.width, src.height), (img.width, img.height), dim_in_photo

    def _load_tk(self) -> tuple[tk.PhotoImage, Size, Size, bool]:
        ext = os.path.splitext(self.path)[1].lower()
        if ext not in TK_EXTENSIONS:
            raise WallpaperError(f"不支持的图片格式：{ext or '（无扩展名）'}"
                                 f"（当前环境没有 Pillow，仅支持 {'/'.join(TK_EXTENSIONS)}）")
        try:
            src = tk.PhotoImage(master=self._master, file=self.path)
        except RuntimeError as exc:  # 没有默认根窗口
            raise WallpaperError(f"创建 Tk 图片失败：{exc}（请先创建 tkinter.Tk() 根窗口）") from exc
        except tk.TclError as exc:   # 文件不是 Tk 能识别的图片 / 已损坏
            raise WallpaperError(f"无法打开图片：{self.path}（{exc}）") from exc

        # stretch 在 Tk 下也只能整数逼近，同样保证 >= 目标（多出的部分由 Canvas 裁掉）
        cover = self.mode in ("cover", "stretch")
        img = _tk_fit(src, self.size, cover=cover)
        return img, (src.width(), src.height()), (img.width(), img.height()), False

    # -- 取色 --------------------------------------------------------------

    def _pixel(self, x: int, y: int) -> RGB:
        w, h = self.image_size
        x = 0 if x < 0 else (w - 1 if x >= w else x)     # 越界 clamp
        y = 0 if y < 0 else (h - 1 if y >= h else y)
        if self._img is not None:
            px = self._img.getpixel((x, y))
            return (int(px[0]), int(px[1]), int(px[2]))
        return _tk_get(self.photo, x, y)

    def _view_offset(self) -> tuple[int, int]:
        """「目标/画布坐标」→「图片像素坐标」的偏移。

        cover 模式下 Tk 后端的图没有被裁剪、而是居中放置，可见区域比图小，
        所以要加上居中偏移；pillow 后端的图本身已经裁成目标大小，偏移是 0；
        contain/tile/stretch 都是左上角对齐，偏移同样是 0。
        有了它，``sample()`` / ``region_color()`` 的坐标系在两种后端下才一致。
        """
        if self.mode == "cover" and self._img is None:
            return (max(0, (self.image_size[0] - self.size[0]) // 2),
                    max(0, (self.image_size[1] - self.size[1]) // 2))
        return 0, 0

    def _visible_rect(self) -> tuple[int, int, int, int]:
        """图片像素坐标下的可见区域（闭区间）。"""
        ox, oy = self._view_offset()
        w, h = self.image_size
        return (ox, oy,
                min(w - 1, ox + self.size[0] - 1),
                min(h - 1, oy + self.size[1] - 1))

    def _clamp_canvas(self, x: int, y: int) -> tuple[int, int]:
        """把「目标/画布坐标」clamp 进目标尺寸内（越界取边缘）。"""
        w, h = self.size
        return (0 if x < 0 else (w - 1 if x >= w else x),
                0 if y < 0 else (h - 1 if y >= h else y))

    @staticmethod
    def _grid(x0: int, y0: int, x1: int, y1: int, n: int) -> list[tuple[int, int]]:
        """在闭区间内取 n×n 个格中心点（n=1 时取中点）。"""
        n = max(1, int(n))
        points: list[tuple[int, int]] = []
        for iy in range(n):
            y = y0 + int((iy + 0.5) * (y1 - y0 + 1) / n)
            for ix in range(n):
                x = x0 + int((ix + 0.5) * (x1 - x0 + 1) / n)
                points.append((x, y))
        return points

    def average_color(self) -> str:
        """可见区域的平均色，返回 '#rrggbb'。

        cover 下只有居中裁出来的那块是可见的：pillow 后端的图本身已经裁好并用
        ``resize((1,1))`` 求均值，Tk 后端则按可见矩形做 ≤400 点网格采样，
        所以两种后端给的是同一块区域的平均色（数值可能差几个色阶）。
        """
        if self._img is not None:
            Image = _pil()[0]  # type: ignore[index]
            lanczos = getattr(Image, "LANCZOS", None) or Image.Resampling.LANCZOS
            px = self._img.resize((1, 1), lanczos).getpixel((0, 0))
            return rgb_to_hex((int(px[0]), int(px[1]), int(px[2])))
        x0, y0, x1, y1 = self._visible_rect()
        pts = self._grid(x0, y0, x1, y1, 20)  # 400 点封顶：大图也不会卡
        r = sum(self._pixel(x, y)[0] for x, y in pts)
        g = sum(self._pixel(x, y)[1] for x, y in pts)
        b = sum(self._pixel(x, y)[2] for x, y in pts)
        n = len(pts)
        return rgb_to_hex((r // n, g // n, b // n))

    def sample(self, x: int, y: int) -> str:
        """取某点颜色（目标/画布坐标系，越界 clamp），返回 '#rrggbb'。

        cover 下会自动换算到居中裁剪出来的那块，因此两种后端的取色坐标一致。
        """
        cx, cy = self._clamp_canvas(int(x), int(y))
        ox, oy = self._view_offset()
        return rgb_to_hex(self._pixel(cx + ox, cy + oy))

    def region_color(self, x0: int, y0: int, x1: int, y1: int, samples: int = 5) -> str:
        """区域平均色（网格采样 samples x samples 个点），用于让侧栏/工具栏融入壁纸。

        坐标同样是目标/画布坐标系（和 ``sample()`` 一致，cover 下自动换算）。
        """
        ox, oy = self._view_offset()
        c0x, c0y = self._clamp_canvas(int(x0), int(y0))
        c1x, c1y = self._clamp_canvas(int(x1), int(y1))
        ax, bx = sorted((c0x + ox, c1x + ox))
        ay, by = sorted((c0y + oy, c1y + oy))
        pts = self._grid(ax, ay, bx, by, min(20, max(1, int(samples))))
        r = sum(self._pixel(x, y)[0] for x, y in pts)
        g = sum(self._pixel(x, y)[1] for x, y in pts)
        b = sum(self._pixel(x, y)[2] for x, y in pts)
        n = len(pts)
        return rgb_to_hex((r // n, g // n, b // n))

    # -- 放置与覆盖层 ------------------------------------------------------

    def place(self) -> tuple[str, int, int]:
        """返回 (anchor, x, y)，供 Canvas.create_image 使用，实现 cover/contain/stretch 的定位。

        cover: 居中裁剪（把图放在画布中心，anchor="center", x=w/2, y=h/2）
        contain/stretch/tile: anchor="nw", x=0, y=0
        （tile 模式由调用方自己平铺，这里返回 nw/0/0）
        """
        w, h = self.size
        if self.mode == "cover":
            return "center", w // 2, h // 2
        return "nw", 0, 0

    def dim_overlay(self) -> Optional[tk.PhotoImage]:
        """返回一个 4x4 的单色半透明覆盖层，用于把整张图压暗/染色；dim<=0 时返回 None。

        颜色取 tint，alpha = round(dim*255)。调用方会 zoom 到窗口大小后叠加。
        返回对象由本实例缓存持有（不会因为调用方忘记保存引用而被 GC），
        且当 ``dim_in_photo`` 为 True（Pillow 已把 dim 混进 photo）时返回 None，
        避免同一个 dim 生效两次。

        注意：调用方自己 ``zoom()`` 出来的那张放大覆盖层**必须自己保存引用**
        （挂到窗口对象上即可），否则 Tk 图片会被 GC 掉、叠加层直接消失。
        """
        if self.dim <= 0 or self.dim_in_photo:
            return None
        if self._overlay is None:
            self._overlay = solid_overlay(_OVERLAY_SIZE, _OVERLAY_SIZE, self.tint,
                                          int(round(self.dim * 255)), self._master)
        return self._overlay


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

_TEST_A = "#3366cc"
_TEST_B = "#cc6633"
_TEST_W, _TEST_H, _TEST_SPLIT = 160, 120, 100


def _write_test_png(path: str) -> None:
    """生成测试图：左侧 100px 为 #3366cc，右侧 60px 为 #cc6633（都是纯色，方便验证取色）。"""
    a = hex_to_rgb(_TEST_A)
    b = hex_to_rgb(_TEST_B)
    buf = bytearray()
    for _y in range(_TEST_H):
        for x in range(_TEST_W):
            r, g, bl = a if x < _TEST_SPLIT else b
            buf += bytes((r, g, bl, 255))
    with open(path, "wb") as handle:
        handle.write(png_bytes(_TEST_W, _TEST_H, bytes(buf)))


def _write_hband_png(path: str) -> None:
    """生成三条横向色带（上红 / 中绿 / 下蓝）的测试图，用来验证 cover 的坐标换算。"""
    top, mid, bottom = (200, 30, 30), (30, 180, 60), (40, 60, 220)
    buf = bytearray()
    for y in range(120):
        rgb = top if y < 40 else (mid if y < 80 else bottom)
        buf += bytes(rgb + (255,)) * 120
    with open(path, "wb") as handle:
        handle.write(png_bytes(120, 120, bytes(buf)))


def selfcheck(root: tk.Tk) -> list[tuple[str, bool, str]]:
    """在给定 root 下跑一遍用例，返回 (名称, 是否通过, 说明) 列表。"""
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        results.append((name, bool(ok), detail))
        return bool(ok)

    # --- 后端与扩展名 ---------------------------------------------------
    be = backend()
    have_pil = _pil() is not None
    check("后端识别", be in ("pillow", "tk") and (be == "pillow") == have_pil,
          f"backend()={be}，Pillow {'已安装' if have_pil else '未安装'}")

    exts = supported_extensions()
    expect = PILLOW_EXTENSIONS if be == "pillow" else TK_EXTENSIONS
    check("扩展名列表", exts == expect and all(e.startswith(".") and e == e.lower() for e in exts),
          f"{' '.join(exts)}")

    # --- PNG 编码器 -----------------------------------------------------
    rgba = bytes((0x33, 0x66, 0xCC, 0x80)) * 16
    data = png_bytes(4, 4, rgba)
    head = data[:8] == b"\x89PNG\r\n\x1a\n" and data[-8:-4] == b"IEND"  # 尾部：长度(4)+IEND(4)+CRC(4)
    ihdr = struct.unpack(">IIBBBBB", data[16:29])
    ph = photo_from_png(data, master=root)
    check("PNG 编码：签名/IHDR/IEND",
          head and ihdr == (4, 4, 8, 6, 0, 0, 0),
          f"signature+iend={head}，IHDR={ihdr}")
    check("PNG 编码：Tk 解码（含 alpha）",
          (ph.width(), ph.height()) == (4, 4) and _tk_get(ph, 1, 2) == (0x33, 0x66, 0xCC),
          f"{ph.width()}x{ph.height()}，像素={_tk_get(ph, 1, 2)}")

    if have_pil:
        import io
        from PIL import Image  # type: ignore[import-not-found]
        with Image.open(io.BytesIO(data)) as im:
            ok = im.size == (4, 4) and im.mode == "RGBA" and im.getpixel((3, 3)) == (0x33, 0x66, 0xCC, 0x80)
            detail = f"PIL 解析：{im.size} {im.mode} px={im.getpixel((3, 3))}"
        check("PNG 编码：PIL 解析", ok, detail)
    else:
        check("PNG 编码：PIL 解析", True, "本机没有 Pillow，跳过（已由 Tk 解码校验覆盖）")

    # --- 纸纹噪声 -------------------------------------------------------
    n1 = _noise_rgba(16, 12, 7)
    n2 = _noise_rgba(16, 12, 7)
    n3 = _noise_rgba(16, 12, 8)
    alphas = n1[3::4]
    tile = noise_tile(16, 12, 7, master=root)
    check("纸纹噪声：确定性 + 不超 alpha",
          n1 == n2 and n1 != n3 and len(n1) == 16 * 16 * 4 and max(alphas) <= 12
          and (tile.width(), tile.height()) == (16, 16) and min(alphas) == 0,
          f"同 seed 相同={n1 == n2}，换 seed 不同={n1 != n3}，alpha∈[{min(alphas)},{max(alphas)}]")

    # --- 暗角 -----------------------------------------------------------
    size_v = 64
    vig = _vignette_rgba(size_v, 70)

    def v_alpha(x: int, y: int) -> int:
        return vig[(y * size_v + x) * 4 + 3]

    center, mid, corner = v_alpha(32, 32), v_alpha(32, 10), v_alpha(63, 63)
    vtile = vignette_tile(size_v, 70, master=root)
    check("暗角：中心透明、四角最实",
          center == 0 and corner == 70 and center < mid < corner
          and (vtile.width(), vtile.height()) == (size_v, size_v),
          f"中心={center}，距中心 22px={mid}，角落={corner}")

    # --- 纯色覆盖层 -----------------------------------------------------
    solid = _solid_rgba(4, 4, "#36c", 128)  # 三位简写也要能解析
    ov = solid_overlay(4, 4, "#36c", 128, master=root)
    check("纯色覆盖层：颜色 + alpha",
          solid[:4] == bytes((0x33, 0x66, 0xCC, 128)) and set(solid[3::4]) == {128}
          and (ov.width(), ov.height()) == (4, 4) and _tk_get(ov, 2, 2) == (0x33, 0x66, 0xCC),
          f"RGBA(0,0)={tuple(solid[:4])}，PhotoImage={ov.width()}x{ov.height()}")

    # --- 真实 PNG 文件 + 四种模式 ---------------------------------------
    tmpdir = tempfile.mkdtemp(prefix="wp_selfcheck_")
    png_path = os.path.join(tmpdir, "wall.png")
    jpg_path = os.path.join(tmpdir, "fake.jpg")
    band_path = os.path.join(tmpdir, "bands.png")
    _write_test_png(png_path)
    _write_hband_png(band_path)
    with open(jpg_path, "wb") as handle:
        handle.write(b"not an image at all")

    def coords(prefix: str) -> None:
        """cover 的取色坐标：可见区域必须来自图中部（两种后端结果一致）。"""
        wp = Wallpaper(band_path, (120, 40), mode="cover", master=root)
        check(f"{prefix}cover 取色坐标：可见区域取自图中部",
              wp.sample(60, 0) == "#1eb43c" and wp.sample(60, 20) == "#1eb43c"
              and wp.sample(60, 39) == "#1eb43c" and wp.sample(60, 999) == "#1eb43c"
              and wp.region_color(0, 0, 119, 39) == "#1eb43c"
              and wp.average_color() == "#1eb43c",
              f"可见区上/中/下={wp.sample(60, 0)}/{wp.sample(60, 20)}/{wp.sample(60, 39)}，"
              f"越界={wp.sample(60, 999)}（clamp 到边缘），平均={wp.average_color()}，"
              f"图 {wp.image_size}，原图 {wp.source_size}")

    def modes(prefix: str) -> None:
        """四种模式的尺寸关系与取色（prefix 标注当前后端）。"""
        target = (100, 40)
        for mode in MODES:
            try:
                wp = Wallpaper(png_path, target, mode=mode, dim=0.25, tint="#101418", master=root)
            except Exception as exc:
                check(f"{prefix}{mode}：加载", False, f"{type(exc).__name__}: {exc}")
                continue
            w, h = wp.image_size
            if mode in ("cover", "stretch"):
                geo_ok = w >= target[0] and h >= target[1]
            else:
                geo_ok = w <= target[0] and h <= target[1]
            left_ok = wp.sample(0, 0) == _TEST_A
            right_ok = wp.sample(w - 1, 0) == _TEST_B
            band_a = wp.region_color(0, 0, 20, 10, samples=4)
            band_b = wp.region_color(w - 10, 0, w - 1, 10, samples=4)
            avg = wp.average_color()
            avg_rgb = hex_to_rgb(avg)
            rgb_a, rgb_b = hex_to_rgb(_TEST_A), hex_to_rgb(_TEST_B)
            avg_ok = (len(avg) == 7 and avg[0] == "#"
                      # 平均色必须落在两种取色之间（±1 容差，采样网格造成的取整）
                      and all(min(rgb_a[i], rgb_b[i]) - 1 <= avg_rgb[i] <= max(rgb_a[i], rgb_b[i]) + 1
                              for i in range(3)))
            want_place = ("center", 50, 20) if mode == "cover" else ("nw", 0, 0)
            check(f"{prefix}{mode}：尺寸/取色/定位",
                  geo_ok and left_ok and right_ok and band_a == _TEST_A and band_b == _TEST_B
                  and avg_ok and wp.source_size == (_TEST_W, _TEST_H)
                  and wp.place() == want_place and wp.backend == backend(),
                  f"图 {w}x{h}（目标 {target[0]}x{target[1]}），左={wp.sample(0, 0)} "
                  f"右={wp.sample(w - 1, 0)}，区域左={band_a} 右={band_b}，平均={avg}，"
                  f"原图={wp.source_size}，place={wp.place()}")

    modes("壁纸·")
    coords("壁纸·")
    if be == "pillow":  # 有 Pillow 时也强制跑一遍 Tk 回退路径
        force_backend("tk")
        try:
            check("强制回退：backend()=tk", backend() == "tk", f"backend()={backend()}")
            modes("壁纸(Tk回退)·")
            coords("壁纸(Tk回退)·")
            try:
                Wallpaper(jpg_path, (10, 10), mode="cover", master=root)
                check("Tk 回退：拒绝 .jpg 并给出中文原因", False, "居然没有抛异常")
            except WallpaperError as exc:
                msg = str(exc)
                check("Tk 回退：拒绝 .jpg 并给出中文原因",
                      "不支持的图片格式" in msg and ".jpg" in msg and "Pillow" in msg, msg)
            except Exception as exc:
                check("Tk 回退：拒绝 .jpg 并给出中文原因", False,
                      f"异常类型不对：{type(exc).__name__}: {exc}")
        finally:
            force_backend(None)

    # --- wall 参数与覆盖层 ----------------------------------------------
    wp2 = Wallpaper(png_path, (100, 40), mode="cover", dim=0.25, tint="#102030", master=root)
    ov2 = wp2.dim_overlay()
    check("dim_overlay：4x4 + tint 色 + alpha=round(dim*255)",
          ov2 is not None and (ov2.width(), ov2.height()) == (4, 4)
          and _tk_get(ov2, 0, 0) == (0x10, 0x20, 0x30)
          and int(round(0.25 * 255)) == 64 and wp2.dim_in_photo is False,
          f"{ov2.width()}x{ov2.height()}，色={_tk_get(ov2, 0, 0)}，alpha={int(round(0.25 * 255))}")
    wp3 = Wallpaper(png_path, (100, 40), mode="cover", dim=0.0, master=root)
    check("dim_overlay：dim=0 返回 None", wp3.dim_overlay() is None, "dim=0.0")

    if be == "pillow":
        wp4 = Wallpaper(png_path, (100, 40), mode="cover", dim=0.6, tint="#000000",
                        master=root, bake_dim=True)
        plain = Wallpaper(png_path, (100, 40), mode="cover", dim=0.6, master=root)
        baked = hex_to_rgb(wp4.average_color())
        raw = hex_to_rgb(plain.average_color())
        check("bake_dim：Pillow 直接把 dim 混进图里（覆盖层返回 None）",
              wp4.dim_in_photo and wp4.dim_overlay() is None
              and all(baked[i] <= raw[i] for i in range(3)),
              f"混合后={wp4.average_color()}，未混合={plain.average_color()}")
    else:
        check("bake_dim：无 Pillow 时退化为覆盖层", True, "Tk 后端没有像素混合，跳过")

    # --- can_open -------------------------------------------------------
    broken_path = os.path.join(tmpdir, "broken.png")  # 扩展名对、内容是垃圾
    with open(broken_path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    ok_png, msg_png = can_open(png_path)
    ok_jpg, msg_jpg = can_open(jpg_path)
    ok_broken, msg_broken = can_open(broken_path)
    ok_none, msg_none = can_open(os.path.join(tmpdir, "根本没有这张图.png"))
    broken_reason = ("损坏" in msg_broken or "不是" in msg_broken or "无法识别" in msg_broken)
    check("can_open：真图/假图/坏图/缺文件",
          ok_png and not ok_jpg and not ok_broken and not ok_none
          and "不存在" in msg_none and broken_reason
          and any(c in msg_jpg for c in ("不支持", "无法识别")),
          f"png→{msg_png} | jpg→{msg_jpg} | 坏 png→{msg_broken} | 缺→{msg_none}")
    try:
        Wallpaper(broken_path, (10, 10), master=root)
        check("坏图：加载时抛中文 WallpaperError", False, "居然没有抛异常")
    except WallpaperError as exc:
        check("坏图：加载时抛中文 WallpaperError", "无法打开图片" in str(exc), str(exc))

    # --- 错误路径 -------------------------------------------------------
    try:
        Wallpaper(os.path.join(tmpdir, "没有.png"), (10, 10), master=root)
        check("缺文件：抛中文 WallpaperError", False, "居然没有抛异常")
    except WallpaperError as exc:
        check("缺文件：抛中文 WallpaperError", "不存在" in str(exc) and isinstance(exc, RuntimeError),
              str(exc))
    try:
        Wallpaper(png_path, (10, 10), mode="乱来", master=root)
        check("未知模式：抛中文 WallpaperError", False, "居然没有抛异常")
    except WallpaperError as exc:
        check("未知模式：抛中文 WallpaperError", "模式" in str(exc), str(exc))
    try:
        png_bytes(2, 2, b"\x00" * 8)
        check("PNG 编码器：字节数校验", False, "居然没有抛异常")
    except ValueError as exc:
        check("PNG 编码器：字节数校验", "字节数" in str(exc), str(exc))

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    return results


def main() -> int:
    """命令行自检：``python -m pyreader.wallpaper``（打印 [PASS]/[FAIL] 与后端名）。"""
    root = tk.Tk()
    root.withdraw()
    be = backend()
    try:
        results = selfcheck(root)
    finally:
        root.destroy()
    print(f"壁纸模块自检 · 后端={be} · Python {sys.version.split()[0]} · Tk "
          f"{tk.TkVersion}")
    failed = 0
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""))
        failed += 0 if ok else 1
    print(f"共 {len(results)} 项，通过 {len(results) - failed} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

