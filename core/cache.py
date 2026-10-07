"""带 TTL 与容量上限的 LRU 缓存。

三类缓存（查询 / 正文 / 图片哈希）**都必须有界**：插件与其它插件共享同一个 Runner 子进程，
无界缓存会随着长时间运行拖垮宿主。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

__all__ = ["TTLCache"]

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """LRU + TTL 缓存。

    ``maxsize`` 与 ``ttl_seconds`` 共同约束内存占用；``ttl_seconds <= 0`` 时退化为不缓存。
    """

    def __init__(
        self,
        *,
        maxsize: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize 必须为正整数")
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: K) -> V | None:
        """取缓存；过期或不存在返回 ``None``。"""
        item = self._data.get(key)
        if item is None:
            self._misses += 1
            return None
        expire_at, value = item
        if self._clock() >= expire_at:
            del self._data[key]
            self._misses += 1
            return None
        self._data.move_to_end(key)
        self._hits += 1
        return value

    def set(self, key: K, value: V) -> None:
        """写缓存；``ttl_seconds <= 0`` 时直接忽略。"""
        if self._ttl <= 0:
            return
        self._data[key] = (self._clock() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)
            self._evictions += 1

    def clear(self) -> None:
        """清空缓存（不清统计）。"""
        self._data.clear()

    def stats(self) -> dict[str, int]:
        """返回命中/未命中/淘汰计数，供 ``/websearch status`` 展示。"""
        return {
            "size": len(self._data),
            "maxsize": self._maxsize,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
        }

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return self.get(key) is not None  # type: ignore[arg-type]
