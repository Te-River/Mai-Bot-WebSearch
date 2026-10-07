"""SearXNG provider（自建实例，JSON API，免密钥）。

自建实例是**最稳定的路径**：没有反爬、结果干净、完全可控。需要实例在
``settings.yml`` 里开启 ``search.formats: [json]``。

默认关闭（``enabled = false``），因为没有实例时它没有意义。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from ...core.errors import ProviderError
from ..types import SearchHit, SearchRequest

__all__ = ["SearxngProvider", "parse_searxng_json"]

_FRESHNESS = {"day": "day", "week": "week", "month": "month", "year": "year"}
_SAFE_SEARCH = {True: "1", False: "0"}


def parse_searxng_json(payload: str) -> list[SearchHit]:
    """解析 SearXNG 的 ``format=json`` 响应。"""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ProviderError("searxng", f"响应不是合法 JSON：{exc}") from exc

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ProviderError("searxng", "响应缺少 results 字段（实例可能未开启 json 格式）")

    hits: list[SearchHit] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        hits.append(
            SearchHit(
                title=title,
                url=url,
                snippet=str(item.get("content") or "").strip(),
                engine="searxng",
                published_at=str(item.get("publishedDate") or ""),
                extra={"source_engine": str(item.get("engine") or "")},
            )
        )
    return hits


class SearxngProvider:
    """自建 SearXNG 实例。"""

    name = "searxng"
    kind = "json"
    requires_key = False

    def __init__(self, *, base_url: str = "", categories: str = "general", language: str = "zh-CN") -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._categories = categories or "general"
        self._language = language or "zh-CN"

    def prepare(self, query: str) -> str:
        """JSON 接口会把引号当字面量，因此**不做**词组保护。"""
        return query

    @property
    def configured(self) -> bool:
        """是否填了实例地址。"""
        return bool(self._base_url)

    def build_url(self, query: str, request: SearchRequest) -> str:
        """构造 ``/search?format=json`` 地址。"""
        params = {
            "q": query,
            "format": "json",
            "categories": self._categories,
            "language": self._language,
            "safesearch": _SAFE_SEARCH[bool(request.safe_search)],
        }
        if request.freshness in _FRESHNESS:
            params["time_range"] = _FRESHNESS[request.freshness]
        return f"{self._base_url}/search?{urlencode(params)}"

    async def search(self, request: SearchRequest, http: Any) -> list[SearchHit]:
        """执行检索。

        Raises:
            ProviderError: 未配置实例地址，或实例未开启 JSON 格式。
        """
        if not self.configured:
            raise ProviderError(self.name, "未配置实例地址（engines.searxng.base_url）")

        url = self.build_url(self.prepare(request.query), request)
        response = await http.fetch(
            url,
            breaker_key=self.name,
            max_bytes=262144,
            headers={"Accept": "application/json"},
        )
        return parse_searxng_json(response.text)[: request.max_results]
