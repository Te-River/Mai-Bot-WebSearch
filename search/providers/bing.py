"""Bing 搜索 provider（HTML SERP，免密钥）。

背景（来自参考实现的 CN 网络实网 benchmark）：HTML SERP 里**只有 Bing 还能返回
真实结果**，sogou / so.com / baidu 返回的是反爬壳，bing 国际版（``ensearch=1``）已死。
因此 Bing 是零配置默认路径的主力。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ...core.errors import ProviderError
from ...core.html_util import attr, node_text, parse_html
from ..query import protect_cjk_phrase
from ..types import SearchHit, SearchRequest

__all__ = ["BingProvider", "parse_bing_serp"]

ENDPOINT = "https://www.bing.com/search"
_SAFE_SEARCH = {True: "moderate", False: "off"}
# 摘要所在的候选选择器，按优先级尝试
_SNIPPET_SELECTORS = (".b_caption p", ".b_lineclamp2", ".b_algoSlug", "p")


def parse_bing_serp(html: str) -> list[SearchHit]:
    """从 Bing SERP HTML 解析结果列表。"""
    tree = parse_html(html)
    hits: list[SearchHit] = []
    for node in tree.css("li.b_algo"):
        link = node.css_first("h2 a")
        href = attr(link, "href")
        title = node_text(link)
        if not href or not title:
            continue
        snippet = ""
        for selector in _SNIPPET_SELECTORS:
            snippet = node_text(node.css_first(selector))
            if snippet:
                break
        hits.append(SearchHit(title=title, url=href, snippet=snippet, engine="bing"))
    return hits


class BingProvider:
    """Bing 网页搜索。"""

    name = "bing"
    kind = "html"
    requires_key = False

    def __init__(self, *, region: str = "zh-CN", language: str = "zh-Hans") -> None:
        self._region = region
        self._language = language

    def prepare(self, query: str) -> str:
        """中文查询加词组保护；英文查询原样通过。"""
        return protect_cjk_phrase(query)

    def build_url(self, query: str, request: SearchRequest) -> str:
        """构造 SERP 地址（多取一些，融合后再裁剪）。"""
        params = {
            "q": query,
            "count": str(max(10, min(request.max_results * 2, 30))),
            "setlang": self._language,
            "cc": (self._region.split("-")[-1] or "CN").upper(),
            "safesearch": _SAFE_SEARCH[bool(request.safe_search)],
        }
        return f"{ENDPOINT}?{urlencode(params)}"

    async def search(self, request: SearchRequest, http: Any) -> list[SearchHit]:
        """执行检索。

        Raises:
            ProviderError: 解析不到任何结果（通常意味着被反爬或页面结构变化）。
        """
        url = self.build_url(self.prepare(request.query), request)
        response = await http.fetch(url, breaker_key=self.name, max_bytes=524288)
        hits = parse_bing_serp(response.text)
        if not hits:
            raise ProviderError(self.name, "未解析到结果（可能被反爬或页面结构已变化）")
        return hits[: request.max_results]
