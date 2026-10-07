"""检索层的公共类型与 provider 协议。

provider 只负责"构造请求 + 把响应解析成本地 hit 列表"，不关心排序与融合；
解析函数接受 ``str``（HTML/JSON 文本）而不是 response 对象，
这样 fixtures 可以直接喂字符串做离线单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

__all__ = [
    "ProviderKind",
    "SearchHit",
    "SearchProvider",
    "SearchRequest",
    "host_path_key",
]

ProviderKind = Literal["html", "json"]


@dataclass(slots=True)
class SearchRequest:
    """一次已归一化的检索请求。"""

    query: str
    max_results: int = 8
    language: str = "zh-CN"
    region: str = "zh-CN"
    safe_search: bool = True
    freshness: str = ""


@dataclass(slots=True)
class SearchHit:
    """归一化后的单条结果。"""

    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    rank: int = 0
    published_at: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SearchProvider(Protocol):
    """搜索引擎适配器协议。"""

    name: str
    kind: ProviderKind
    requires_key: bool

    def prepare(self, query: str) -> str:
        """把用户查询转换成该引擎需要的形态（CJK 保护、限定符折叠等）。"""
        ...

    async def search(self, request: SearchRequest, http: Any) -> list[SearchHit]:
        """执行检索并返回归一化结果。"""
        ...


def host_path_key(url: str) -> str:
    """去重键：主机 + 路径（忽略 query、末尾斜杠与 ``www.`` 前缀）。

    同一个页面在多个引擎里常带不同的跟踪参数，按 host+path 去重才能真正合并。
    **没有主机名的 URL 返回空串**（相对路径、解析残缺的条目都不是可用的结果链接）。
    """
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "/").rstrip("/") or "/"
    return f"{host}{path}"
