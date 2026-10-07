
==============================================================================
  文件：pyreader/state.py
==============================================================================
"""极简状态容器（发布-订阅）。

与 Web 版 createStore 一一对应：所有视图只读 state，所有写入都走 set()，
订阅者按「渲染指纹」决定是否真正重绘。这样滚动、进度落盘这类高频写入
不会重建正文控件，也就不会丢掉滚动位置和选中状态。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

Listener = Callable[[Dict, Dict], None]


class Store:
    def __init__(self, initial: Optional[Dict] = None):
        self._state: Dict = dict(initial or {})
        self._listeners: List[Listener] = []

    @property
    def state(self) -> Dict:
        """只读视图（约定：调用方不要直接改内容）。"""
        return self._state

    def set(self, patch: Optional[Dict] = None, **kwargs) -> None:
        prev = self._state
        nxt = dict(prev)
        if patch:
            nxt.update(patch)
        if kwargs:
            nxt.update(kwargs)
        if all(nxt.get(k) == prev.get(k) for k in nxt) and set(nxt) == set(prev):
            return  # 无变化，不打扰订阅者
        self._state = nxt
        for fn in list(self._listeners):
            fn(nxt, prev)

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

