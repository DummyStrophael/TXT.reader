
==============================================================================
  文件：pyreader/textproc.py
==============================================================================
"""纯文本处理层：编码探测、分段、章节识别、阅读统计。

这一层完全不依赖 tkinter，也不读写任何文件，因此可以单独导入和单元测试。
输入永远是「一段文本」，输出永远是「一组排版块 + 章节表」。
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from typing import Iterable, List

# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """一个排版块。kind 决定它在阅读区里用哪个 Text tag 渲染。"""

    kind: str  # 'h1' 文首大标题 | 'meta' 元信息行 | 'h2' 章节标题 | 'p' 正文段落
    text: str


@dataclass(frozen=True)
class Chapter:
    """一条目录项。block_index 指回 Article.blocks 里的下标。"""

    title: str
    block_index: int


@dataclass
class Article:
    """一本书渲染所需的全部内容（不含业务状态）。"""

    blocks: List[Block] = field(default_factory=list)
    chapters: List[Chapter] = field(default_factory=list)
    chars: int = 0
    minutes: int = 0

    @property
    def paragraphs(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "p")


# --------------------------------------------------------------------------
# 编码探测
# --------------------------------------------------------------------------

# 中文环境最常见的两种非 UTF-8 编码：GB18030（GBK/GB2312 的超集）与 UTF-16。
_CANDIDATES = ("utf-8", "gb18030")


def _sniff_utf16(data: bytes) -> str:
    """无 BOM 的 UTF-16 猜测。

    UTF-16 文本里大量字符的高字节是 0x00（ASCII 区间尤其明显），据此看
    「奇数位还是偶数位更容易出 0x00」就能判断字节序；判不出来就返回空串。
    中文 UTF-16 的 0x00 很少，这里不强行猜测，避免把正常文本猜坏。
    """
    if len(data) < 8:
        return ""
    even = data[0::2]
    odd = data[1::2]
    n_even = even.count(0)
    n_odd = odd.count(0)
    ratio_odd = n_odd / max(1, len(odd))
    ratio_even = n_even / max(1, len(even))
    if ratio_odd > 0.30 and n_odd > n_even * 3:
        return "utf-16-le"
    if ratio_even > 0.30 and n_even > n_odd * 3:
        return "utf-16-be"
    return ""


def decode_bytes(data: bytes) -> "tuple[str, str]":
    """把文件字节解码成文本，返回 (文本, 编码名)。

    顺序：BOM → 无 BOM 的 UTF-16 猜测 → 严格 UTF-8 → GB18030 → 有损 UTF-8 兜底。
    「严格」是关键：宁可解码失败也不要静默产生乱码。
    """
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8"), "UTF-8"
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[len(codecs.BOM_UTF16_LE):].decode("utf-16-le"), "UTF-16LE"
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[len(codecs.BOM_UTF16_BE):].decode("utf-16-be"), "UTF-16BE"

    sniffed = _sniff_utf16(data)
    if sniffed:
        try:
            return data.decode(sniffed), "UTF-16LE" if sniffed.endswith("le") else "UTF-16BE"
        except UnicodeDecodeError:
            pass

    for enc in _CANDIDATES:
        try:
            return data.decode(enc), enc.upper() if enc == "gb18030" else "UTF-8"
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "UTF-8（有损）"


def guess_title(filename: str, content: str) -> str:
    """书名：优先文件名（去掉扩展名），其次正文第一行短句。"""
    stem = ""
    if filename:
        base = filename.replace("\\", "/").rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0].strip() if "." in base else base.strip()
    if stem:
        return stem[:60]
    for line in content.splitlines():
        line = line.strip()
        if line and len(line) <= 40:
            return line[:60]
    return "未命名文本"


# --------------------------------------------------------------------------
# 分段
# --------------------------------------------------------------------------

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")


def split_paragraphs(text: str) -> List[str]:
    """把整本书切成段落。

    两种版式自适应：
    * 空行占比 >= 5%：按空行分段（Markdown、西文散文），行内换行视为软换行并拼接；
    * 否则一行一段（多数中文 txt 的硬折行）。
    标题行永远独占一段，绝不被拼进正文。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blanks = sum(1 for l in lines if not l.strip())
    spaced = blanks / max(1, len(lines)) >= 0.05

    out: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        if not buf:
            return
        s = ""
        for line in buf:
            if not s:
                s = line
                continue
            # 中文硬折行直接拼接；西文用空格连接
            s += line if (_CJK.search(s[-1]) and _CJK.match(line[0])) else " " + line
        if s.strip():
            out.append(s.strip())
        buf.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if spaced:
                flush()
            continue
        if is_heading(line):
            flush()
            out.append(line.strip())
            continue
        buf.append(line)
        if not spaced:
            flush()
    flush()
    return out


# --------------------------------------------------------------------------
# 章节识别
# --------------------------------------------------------------------------
# 原则：宁可少认，不可把正文当标题。
#   * 命中：第一章 / 第一章 起点 / 第 2 章：缘起 / Chapter 3 - The Fall /
#           序章 / 前言 / 尾声 / 番外 / Markdown 的 # 标题
#   * 排除：含「。」的句子、以逗号分号结尾的行、超长行、
#           以及「第一章的内容主要讲述了…」这类虚词开头的正文。
CH_NUM = r"[0-9零一二三四五六七八九十百千两]{1,6}"
CH_HEAD = rf"(?:第\s*{CH_NUM}\s*[章节回卷篇部幕]|Chapter\s+[0-9IVXLC]+)"
CH_KW = r"(?:序章|序言|自序|前言|引子|楔子|尾声|终章|后记|番外|附录)"
CH_TAIL = r"(?:|[\s:：、.．·\-—－][^\s].{0,19}|[^\s的了是在有为与和及所将把就都很].{0,5})"
CHAPTER_RE = re.compile(rf"^(?:#{{1,3}}\s+\S.{{0,44}}|{CH_HEAD}{CH_TAIL}|{CH_KW}{CH_TAIL})$", re.I)

_SENTENCE_TAIL = re.compile(r"[，；,;]$")


def is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 46:
        return False
    if not CHAPTER_RE.match(s):
        return False
    if s.startswith("#") and re.match(r"^#{1,3}\s", s):
        return True  # Markdown 标题是显式的
    return "。" not in s and not _SENTENCE_TAIL.search(s)


def clean_heading(line: str) -> str:
    return re.sub(r"^#{1,6}\s*", "", line.strip()).strip()


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def reading_minutes(chars: int) -> int:
    """中文按 ~420 字/分钟估算。"""
    return max(1, round(chars / 420))


def build_article(title: str, content: str, size: int = 0, encoding: str = "UTF-8") -> Article:
    """把原始文本编译成排版块序列 + 章节表。"""
    chars = len(content)
    art = Article(chars=chars, minutes=reading_minutes(chars))
    art.blocks.append(Block("h1", title))
    meta = f"{chars:,} 字 · 约 {art.minutes} 分钟 · {encoding} · {human_size(size or len(content.encode('utf-8')))}"
    art.blocks.append(Block("meta", meta))
    art.chapters.append(Chapter("开头", 0))  # 目录第一项指向文首

    for para in split_paragraphs(content):
        idx = len(art.blocks)
        if is_heading(para):
            art.blocks.append(Block("h2", clean_heading(para)))
            art.chapters.append(Chapter(clean_heading(para), idx))
        else:
            art.blocks.append(Block("p", para))
    return art


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1048576:.2f} MB"


def iter_plain_text(article: Article) -> Iterable[str]:
    """导出纯文本（供「另存为」等扩展使用）。"""
    for block in article.blocks:
        if block.kind in ("h1", "h2", "meta"):
            yield block.text
        else:
            yield block.text

