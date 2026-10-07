
==============================================================================
  文件：pyreader/storage.py
==============================================================================
"""持久化层：SQLite 书库 + JSON 设置。

对应 Web 版的 IndexedDB + localStorage：
  * 书库用 SQLite（标准库 sqlite3，WAL 模式），正文直接存库，
    元数据与正文分表查询——书架只读元数据，正文按需读取；
  * 设置用 JSON 小文件，原子写入（临时文件 + replace），避免半截文件。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

APP_DIR_NAME = "PyTextReader"


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BookMeta:
    """书架元数据：不含正文，用于列表渲染。"""

    id: str
    title: str
    size: int
    encoding: str
    added_at: float
    last_read_at: float
    progress: float
    progress_updated_at: float
    char_offset: int = 0        # 首行可见字符在全书中的下标（排版无关的精确位置）

    @property
    def percent(self) -> int:
        return int(round(self.progress * 100))

    @property
    def finished(self) -> bool:
        return self.progress >= 0.999

    @property
    def percent_text(self) -> str:
        if self.finished:
            return "已读完"
        return f"{self.percent}%" if self.percent > 0 else "未开始"


@dataclass(frozen=True)
class Book:
    meta: BookMeta
    content: str


def _row_to_meta(row: sqlite3.Row) -> BookMeta:
    keys = row.keys()
    return BookMeta(
        id=row["id"], title=row["title"], size=row["size"], encoding=row["encoding"],
        added_at=row["added_at"], last_read_at=row["last_read_at"],
        progress=row["progress"], progress_updated_at=row["progress_updated_at"],
        char_offset=int(row["char_offset"]) if "char_offset" in keys else 0,
    )


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def portable_dir() -> Path:
    """程序自身所在目录：打包后是 exe 所在目录，源码运行时是项目根目录。"""
    if getattr(sys, "frozen", False):          # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve()
    # 源码布局是 <项目>/pyreader/storage.py
    return here.parent.parent if here.parent.name == "pyreader" else here.parent


def _user_data_dir() -> Path:
    """传统用户目录（便携目录不可写时的退路，也是老数据的迁移来源）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def _writable(path: Path) -> bool:
    """能不能在这个目录里建文件（Program Files、只读盘、无权限共享盘会失败）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def default_data_dir() -> Path:
    """数据目录：默认放「程序目录\\data」（便携，跟着文件夹一起搬）。

    优先级：--data-dir > 环境变量 PYREADER_HOME > 程序目录\\data > 用户目录。
    程序目录不可写时自动回退到用户目录，不影响使用。
    """
    override = os.environ.get("PYREADER_HOME")
    if override:
        return Path(override).expanduser()
    portable = portable_dir() / "data"
    if _writable(portable):
        return portable
    return _user_data_dir()


def migrate_legacy_data(new_dir: Path) -> bool:
    """把老位置（%APPDATA%\\PyTextReader）的数据搬到便携目录，只在新目录还空着时做一次。

    是「复制」而不是「移动」：老目录原样保留，出问题还能找回来。
    """
    try:
        if new_dir.resolve() == _user_data_dir().resolve():
            return False
        if (new_dir / "library.db").exists():
            return False
        legacy = _user_data_dir()
        if not (legacy / "library.db").exists():
            return False
        new_dir.mkdir(parents=True, exist_ok=True)
        for name in ("library.db", "library.db-wal", "library.db-shm", "settings.json"):
            src = legacy / name
            if src.exists():
                shutil.copy2(src, new_dir / name)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# 书库
# --------------------------------------------------------------------------


class Library:
    """SQLite 书库。所有写操作立即提交，避免异常退出丢数据。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS books (
        id                   TEXT PRIMARY KEY,
        title                TEXT NOT NULL,
        content              TEXT NOT NULL,
        size                 INTEGER NOT NULL DEFAULT 0,
        encoding             TEXT NOT NULL DEFAULT 'UTF-8',
        added_at             REAL NOT NULL,
        last_read_at         REAL NOT NULL,
        progress             REAL NOT NULL DEFAULT 0,
        progress_updated_at  REAL NOT NULL DEFAULT 0,
        char_offset          INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_books_last_read ON books(last_read_at DESC);
    """

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(self.SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库补列：char_offset 是后来加的精确阅读位置。"""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(books)")}
        if "char_offset" not in cols:
            self.conn.execute("ALTER TABLE books ADD COLUMN char_offset INTEGER NOT NULL DEFAULT 0")

    # -- 读 -----------------------------------------------------------------

    def all_meta(self) -> List[BookMeta]:
        rows = self.conn.execute(
            "SELECT * FROM books ORDER BY last_read_at DESC, added_at DESC"
        ).fetchall()
        return [_row_to_meta(r) for r in rows]

    def get(self, book_id: str) -> Optional[Book]:
        row = self.conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            return None
        return Book(meta=_row_to_meta(row), content=row["content"])

    def get_meta(self, book_id: str) -> Optional[BookMeta]:
        book = self.get(book_id)
        return book.meta if book else None

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"])

    def get_by_title(self, title: str) -> List[BookMeta]:
        rows = self.conn.execute(
            "SELECT * FROM books WHERE title = ? ORDER BY added_at DESC", (title,)
        ).fetchall()
        return [_row_to_meta(r) for r in rows]

    # -- 写 -----------------------------------------------------------------

    def add(self, title: str, content: str, encoding: str = "UTF-8", size: Optional[int] = None) -> BookMeta:
        now = time.time()
        meta = BookMeta(
            id=uuid.uuid4().hex[:12], title=title or "未命名文本",
            size=size if size is not None else len(content.encode("utf-8")),
            encoding=encoding, added_at=now, last_read_at=now,
            progress=0.0, progress_updated_at=0.0,
        )
        self.conn.execute(
            "INSERT INTO books (id, title, content, size, encoding, added_at, last_read_at, progress, progress_updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (meta.id, meta.title, content, meta.size, meta.encoding,
             meta.added_at, meta.last_read_at, meta.progress, meta.progress_updated_at),
        )
        self.conn.commit()
        return meta

    def remove(self, book_id: str) -> None:
        self.conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        self.conn.commit()

    def set_progress(self, book_id: str, ratio: float, char_offset: int = 0) -> None:
        """ratio 是 0~1 的已读比例（视口底部字符 / 总字符），char_offset 是精确的字符下标。"""
        self.conn.execute(
            "UPDATE books SET progress = ?, char_offset = ?, progress_updated_at = ? WHERE id = ?",
            (max(0.0, min(1.0, float(ratio))), max(0, int(char_offset)), time.time(), book_id),
        )
        self.conn.commit()

    def touch(self, book_id: str) -> None:
        self.conn.execute("UPDATE books SET last_read_at = ? WHERE id = ?", (time.time(), book_id))
        self.conn.commit()

    def rename(self, book_id: str, title: str) -> None:
        self.conn.execute("UPDATE books SET title = ? WHERE id = ?", (title, book_id))
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------

DEFAULT_SETTINGS: Dict = {
    "font_size": 19,
    "line_height": 1.9,
    "page_width": 720,
    "font_family": "serif",      # serif | sans
    "theme": "paper",            # paper | sepia | green | gray | night | ink | custom
    "custom_bg": "#faf8f5",
    "custom_fg": "#2b2b2b",
    "eye_care": False,
    "indent": True,
    "shelf_open": True,
    "sidebar": None,             # None | 'toc' | 'settings'
    "window": "",                # 记住窗口尺寸 "960x680+80+60"（逻辑像素）
    # --- 壁纸与质感 ---------------------------------------------------
    "wallpaper_on": False,
    "wallpaper_path": "",
    "wallpaper_mode": "cover",   # cover | contain | tile | stretch
    "wallpaper_dim": 0.28,       # 壁纸向主题底色融合/压暗的比例
    "wallpaper_panel": 0.18,     # 外壳（工具栏/书架/侧栏）向壁纸靠拢的比例
    "texture_on": True,          # 程序化纸纹
    "texture_strength": 10,      # 纸纹透明度 0~40
    "immersive": False,          # 沉浸模式（隐藏书架与侧栏）
}


class Settings:
    """JSON 设置文件。读取时与默认值合并，缺字段不会导致崩溃。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict:
        data: Dict = {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError):
            pass  # 损坏就用默认值，不阻塞启动
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
        return merged

    def save(self, settings: Dict) -> None:
        payload = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass  # 磁盘不可写时只影响持久化，不影响本次阅读

