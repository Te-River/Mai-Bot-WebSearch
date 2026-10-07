"""Bing 图片搜索（HTML，免密钥）。

结果内嵌在 ``<a class="iusc" m="{...}">`` 的 ``m`` 属性里，是一段 JSON：
``murl``（原图）、``turl``（缩略图）、``t``（标题）、``purl``（来源页）。

**缩略图优先**：``send.image`` 只接受 base64（已确认宿主实现），所以必须下载；
而缩略图通常 20–60KB，原图常常数 MB——这是图片通路最大的一笔延迟优化。
"""

from __future__ import annotations

import html as html_module
import json
from typing import Any
from urllib.parse import urlencode

from ...core.errors import ProviderError
from ...core.html_util import attr, parse_html
from ..types import ImageCandidate

__all__ = ["BingImagesProvider", "parse_bing_images"]

ENDPOINT = "https://www.bing.com/images/search"
_SAFE_SEARCH = {True: "strict", False: "off"}


def _loads_loose(raw: str) -> dict[str, Any]:
    """解析 ``m`` 属性。

    lexbor 通常会解码属性里的 HTML 实体，但不同版本/不同页面可能留下 ``&quot;``，
    所以再兜一次 :func:`html.unescape`。
    """
    if not raw:
        return {}
    for candidate in (raw, html_module.unescape(raw)):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def parse_bing_images(html: str) -> list[ImageCandidate]:
    """从 Bing 图片搜索页解析候选。"""
    tree = parse_html(html)
    candidates: list[ImageCandidate] = []
    for node in tree.css("a.iusc"):
        data = _loads_loose(attr(node, "m"))
        original = str(data.get("murl") or "").strip()
        if not original:
            continue
        candidates.append(
            ImageCandidate(
                url=original,
                thumbnail_url=str(data.get("turl") or "").strip(),
                title=str(data.get("t") or "").strip(),
                page_url=str(data.get("purl") or attr(node, "href") or "").strip(),
                engine="bing_images",
            )
        )
    return candidates


class BingImagesProvider:
    """Bing 图片搜索。"""

    name = "bing_images"
    kind = "html"
    requires_key = False

    def __init__(self, *, region: str = "zh-CN", safe_search: bool = True) -> None:
        self._region = region
        self._safe_search = safe_search

    def prepare(self, query: str) -> str:
        """图片搜索不做词组保护：SERP 的引号会显著减少图片结果。"""
        return query

    def build_url(self, query: str, *, limit: int = 10) -> str:
        """构造图片搜索地址。"""
        params = {
            "q": query,
            "first": "1",
            "count": str(max(10, min(limit * 3, 35))),
            "adlt": _SAFE_SEARCH[bool(self._safe_search)],
            "form": "HDRSC2",
            "cc": (self._region.split("-")[-1] or "CN").upper(),
        }
        return f"{ENDPOINT}?{urlencode(params)}"

    async def search(self, query: str, http: Any, *, limit: int = 10) -> list[ImageCandidate]:
        """执行图片检索。

        Raises:
            ProviderError: 解析不到任何候选（通常意味着被反爬或页面结构变化）。
        """
        response = await http.fetch(
            self.build_url(self.prepare(query), limit=limit),
            breaker_key=self.name,
            max_bytes=1048576,
        )
        candidates = parse_bing_images(response.text)
        if not candidates:
            raise ProviderError(self.name, "未解析到图片（可能被反爬或页面结构已变化）")
        return candidates[:limit]
