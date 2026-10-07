"""DuckDuckGo provider（html 端点，免密钥）。

``html.duckduckgo.com`` 用 POST 更稳定；返回的结果链接是 DDG 的跳转链接
（``//duckduckgo.com/l/?uddg=<urlencoded>``），必须解包成真实地址，
否则去重与展示都会出错。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ...core.errors import ProviderError
from ...core.html_util import attr, node_text, parse_html
from ..query import protect_cjk_phrase
from ..types import SearchHit, SearchRequest

__all__ = ["DuckDuckGoProvider", "parse_ddg_html", "unwrap_ddg_url"]

ENDPOINT = "https://html.duckduckgo.com/html/"
_FRESHNESS = {"day": "d", "week": "w", "month": "m", "year": "y"}
_SNIPPET_SELECTORS = ("a.result__snippet", ".result__snippet", "td.result-snippet")


def unwrap_ddg_url(href: str) -> str:
    """把 DDG 的跳转链接解包成真实地址；普通链接原样返回。"""
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    parts = urlsplit(href)
    if "duckduckgo.com" not in (parts.hostname or ""):
        return href
    target = parse_qs(parts.query).get("uddg")
    if not target:
        return href
    return unquote(target[0])


def parse_ddg_html(html: str) -> list[SearchHit]:
    """解析 DDG html 端点的结果。

    只用一个类选择器：写成 ``div.result, div.web-result`` 会让同时带这两个类的节点
    被匹配两次（lexbor 不做去重），结果数量直接翻倍。
    """
    tree = parse_html(html)
    nodes = tree.css("div.result")
    if not nodes:
        nodes = tree.css("div.web-result")
    hits: list[SearchHit] = []
    for node in nodes:
        link = node.css_first("a.result__a") or node.css_first("a.result-link")
        href = unwrap_ddg_url(attr(link, "href"))
        title = node_text(link)
        if not href or not title:
            continue
        snippet = ""
        for selector in _SNIPPET_SELECTORS:
            snippet = node_text(node.css_first(selector))
            if snippet:
                break
        hits.append(SearchHit(title=title, url=href, snippet=snippet, engine="duckduckgo"))
    return hits


class DuckDuckGoProvider:
    """DuckDuckGo 网页搜索。"""

    name = "duckduckgo"
    kind = "html"
    requires_key = False

    def __init__(self, *, region: str = "cn-zh") -> None:
        self._region = region

    def prepare(self, query: str) -> str:
        """中文查询加词组保护。"""
        return protect_cjk_phrase(query)

    async def search(self, request: SearchRequest, http: Any) -> list[SearchHit]:
        """执行检索（POST 表单）。"""
        data = {
            "q": self.prepare(request.query),
            "kl": self._region,
        }
        if request.freshness in _FRESHNESS:
            data["df"] = _FRESHNESS[request.freshness]

        response = await http.fetch(
            ENDPOINT,
            method="POST",
            data=data,
            headers={
                "Referer": "https://html.duckduckgo.com/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            breaker_key=self.name,
            max_bytes=524288,
        )
        hits = parse_ddg_html(response.text)
        if not hits:
            raise ProviderError(self.name, "未解析到结果（可能被反爬或页面结构已变化）")
        return hits[: request.max_results]
