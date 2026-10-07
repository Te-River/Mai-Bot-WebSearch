"""SearXNG 图片搜索（自建实例，JSON API）。

复用 ``engines.searxng`` 的实例地址，只是把 ``categories`` 换成 ``images``。
自建实例是唯一"无反爬、结果干净"的图片源，默认关闭（需要用户自建）。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from ...core.errors import ProviderError
from ..types import ImageCandidate

__all__ = ["SearxngImagesProvider", "parse_searxng_images"]

_SAFE_SEARCH = {True: "1", False: "0"}


def parse_searxng_images(payload: str) -> list[ImageCandidate]:
    """解析 SearXNG ``categories=images`` 的 JSON 响应。"""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ProviderError("searxng_images", f"响应不是合法 JSON：{exc}") from exc

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ProviderError("searxng_images", "响应缺少 results 字段（实例可能未开启 json 格式）")

    candidates: list[ImageCandidate] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("img_src") or "").strip()
        if not url:
            continue
        candidates.append(
            ImageCandidate(
                url=url,
                thumbnail_url=str(item.get("thumbnail_src") or "").strip(),
                title=str(item.get("title") or "").strip(),
                page_url=str(item.get("url") or "").strip(),
                engine="searxng_images",
                width=int(item.get("width") or 0) if str(item.get("width") or "").isdigit() else 0,
                height=int(item.get("height") or 0) if str(item.get("height") or "").isdigit() else 0,
                extra={"source_engine": str(item.get("engine") or "")},
            )
        )
    return candidates


class SearxngImagesProvider:
    """自建 SearXNG 的图片分类。"""

    name = "searxng_images"
    kind = "json"
    requires_key = False

    def __init__(self, *, base_url: str = "", language: str = "zh-CN", safe_search: bool = True) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._language = language or "zh-CN"
        self._safe_search = safe_search

    @property
    def configured(self) -> bool:
        """是否填了实例地址。"""
        return bool(self._base_url)

    def build_url(self, query: str, *, limit: int = 10) -> str:
        """构造图片搜索地址。"""
        params = {
            "q": query,
            "format": "json",
            "categories": "images",
            "language": self._language,
            "safesearch": _SAFE_SEARCH[bool(self._safe_search)],
        }
        return f"{self._base_url}/search?{urlencode(params)}"

    async def search(self, query: str, http: Any, *, limit: int = 10) -> list[ImageCandidate]:
        """执行图片检索。"""
        if not self.configured:
            raise ProviderError(self.name, "未配置实例地址（engines.searxng.base_url）")
        response = await http.fetch(
            self.build_url(query, limit=limit),
            breaker_key=self.name,
            max_bytes=262144,
            headers={"Accept": "application/json"},
        )
        return parse_searxng_images(response.text)[:limit]
