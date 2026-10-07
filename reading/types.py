"""阅读层的公共类型。

放在独立模块是为了避免 ``reading`` 与 ``search.providers`` 互相导入形成环。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["ReadingResult", "SpecialFetcher", "SpecialSource"]


@dataclass(slots=True)
class ReadingResult:
    """一次"读网页"的结果。"""

    url: str
    title: str
    text: str
    strategy: str = ""
    truncated: bool = False
    content_type: str = ""
    elapsed_ms: int = 0
    hops: int = 0


SpecialFetcher = Callable[[str, Any], Awaitable[ReadingResult]]


@dataclass(slots=True)
class SpecialSource:
    """某些站点有专用接口，直接抓 HTML 只会拿到 403。

    典型例子是萌娘百科：文章 HTML 与 ``action=raw`` 都是 403，
    但 ``prop=extracts`` 能返回干净的纯文本正文。命中 ``matches`` 时优先走 ``fetch``。
    """

    name: str
    matches: Callable[[str], bool]
    fetch: SpecialFetcher
