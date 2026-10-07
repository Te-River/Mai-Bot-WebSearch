"""IQDB 以图搜源（免密钥，ACG 图库聚合）。

**诚实声明**：``iqdb.org`` 在本项目实测网络下域名不可达，解析逻辑**未经实网验证**，
同样按"认不出结构就返回空"的方式写。跑 ``python tests/live_probe.py`` 可自查。

IQDB 既支持按地址（``url=``）也支持按文件（``file=``）提交，POST 到根路径。
结果页里每个来源是一个 ``div.result``：含相似度百分比、图库名（source）、
原图缩略图与详情链接。
"""

from __future__ import annotations

import re
from typing import Any

from ...core.html_util import attr, node_text, parse_html
from ...core.ssrf import normalize_url
from . import ReverseLookupResult, ReverseSource

__all__ = ["IqdbProvider", "parse_iqdb"]

DEFAULT_ENDPOINT = "https://iqdb.org"
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _absolute(href: str, endpoint: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return f"{endpoint}{href}"
    return href


def parse_iqdb(html: str, *, endpoint: str = DEFAULT_ENDPOINT) -> ReverseLookupResult:
    """提取 IQDB 结果页里的来源条目。"""
    tree = parse_html(html)
    sources: list[ReverseSource] = []
    seen: set[str] = set()

    for node in tree.css("div.result, div.resulttable > div"):
        title = ""
        link = ""
        for anchor in node.css("a[href]"):
            candidate = _absolute(attr(anchor, "href") or "", endpoint)
            if candidate.startswith("http") and "iqdb.org" not in candidate:
                link = candidate
                title = node_text(anchor) or title
                break
        if not link or link in seen:
            continue
        thumbnail = ""
        for image in node.css("img[src], img[data-src]"):
            thumbnail = _absolute(attr(image, "src") or attr(image, "data-src") or "", endpoint)
            if thumbnail:
                break
        index = ""
        for source_node in node.css("span.source a, .source a, span.source"):
            index = node_text(source_node)
            if index:
                break
        match = _PERCENT_RE.search(node_text(node))
        sources.append(
            ReverseSource(
                title=title or node_text(node)[:80],
                similarity=float(match.group(1)) if match else 0.0,
                index=index or "iqdb",
                urls=[normalize_url(link)],
                thumbnail=thumbnail,
            )
        )
        seen.add(link)

    sources.sort(key=lambda item: item.similarity, reverse=True)
    return ReverseLookupResult(engine="iqdb", sources=sources)


class IqdbProvider:
    """IQDB 以图搜源。"""

    name = "iqdb"
    requires_key = False

    def __init__(self, *, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self._endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")

    @property
    def configured(self) -> bool:
        """免密钥，永远可用。"""
        return True

    async def lookup(self, image: Any, http: Any) -> ReverseLookupResult:
        """反查来源。

        Raises:
            ProviderError: 既没有地址也没有字节。
        """
        image_url = getattr(image, "url", "") or ""
        content = getattr(image, "content", b"") or b""
        if content:
            response = await http.fetch(
                f"{self._endpoint}/",
                method="POST",
                files={"file": ("image.jpg", content, getattr(image, "mime_type", "") or "image/jpeg")},
                breaker_key=self.name,
                max_bytes=2097152,
                headers={"Accept": "text/html"},
            )
        elif image_url:
            response = await http.fetch(
                f"{self._endpoint}/",
                method="POST",
                data={"url": image_url},
                breaker_key=self.name,
                max_bytes=2097152,
                headers={"Accept": "text/html"},
            )
        else:
            from ...core.errors import ProviderError

            raise ProviderError(self.name, "既没有图片地址也没有图片字节")
        return parse_iqdb(response.text, endpoint=self._endpoint)
