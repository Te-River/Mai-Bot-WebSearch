"""ascii2d 以图搜源（免密钥，ACG 语境最对口）。

**诚实声明：``ascii2d.net`` 在本项目实测网络下域名不可达（ConnectError），
所以这个 provider 的**请求契约**来自公开用法，**解析逻辑未经实网验证**。
它被写成"认不出结构就返回空"的形式，因此在你自己的网络上跑不通时
表现为"没搜到"，而不是把插件搞崩。跑 ``python tests/live_probe.py`` 可自查。

两种调用方式：
* 按图片地址：``GET /search/url/<urlencoded>``（免上传，最快）
* 按图片字节：``GET /search/file/``（multipart，字段名 ``file``）
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ...core.html_util import attr, node_text, parse_html
from ...core.ssrf import normalize_url
from . import ReverseLookupResult, ReverseSource

__all__ = ["Ascii2dProvider", "parse_ascii2d"]

DEFAULT_ENDPOINT = "https://ascii2d.net"

# 结果条目可能出现的容器类名（ascii2d 改版过多次，全部兜住）
_ITEM_SELECTORS = (
    "div.item-box",
    "div.row div.item-box",
    "div.search-result",
    "div.result-item",
)
_TITLE_SELECTORS = ("p.title", "span.title", "div.title", "a.title")
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _absolute(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return f"{DEFAULT_ENDPOINT}{href}"
    return href


def _pick(node: Any, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        found = node.css_first(selector)
        if found is not None:
            text = node_text(found)
            if text:
                return text
    return ""


def parse_ascii2d(html: str) -> ReverseLookupResult:
    """从 ascii2d 搜索结果页里提取来源条目。"""
    tree = parse_html(html)
    sources: list[ReverseSource] = []
    seen: set[str] = set()

    for selector in _ITEM_SELECTORS:
        for node in tree.css(selector):
            link = ""
            for anchor in node.css("a[href]"):
                candidate = _absolute(attr(anchor, "href") or "")
                if candidate.startswith("http"):
                    link = candidate
                    break
            if not link or link in seen:
                continue
            thumbnail = ""
            for image in node.css("img[src], img[data-src]"):
                thumbnail = _absolute(attr(image, "src") or attr(image, "data-src") or "")
                if thumbnail:
                    break
            text = node_text(node)
            match = _PERCENT_RE.search(text)
            sources.append(
                ReverseSource(
                    title=_pick(node, _TITLE_SELECTORS) or text[:80],
                    similarity=float(match.group(1)) if match else 0.0,
                    index="ascii2d",
                    urls=[normalize_url(link)],
                    thumbnail=thumbnail,
                )
            )
            seen.add(link)
        if sources:
            break

    sources.sort(key=lambda item: item.similarity, reverse=True)
    return ReverseLookupResult(engine="ascii2d", sources=sources)


class Ascii2dProvider:
    """ascii2d 以图搜源。"""

    name = "ascii2d"
    requires_key = False

    def __init__(self, *, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self._endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")

    @property
    def configured(self) -> bool:
        """免密钥，永远可用。"""
        return True

    def build_url(self, image_url: str) -> str:
        """按图片地址检索（免上传）。"""
        return f"{self._endpoint}/search/url/{quote(image_url, safe='')}"

    async def lookup(self, image: Any, http: Any) -> ReverseLookupResult:
        """反查来源。

        Raises:
            ProviderError: 既没有地址也没有字节。
        """
        image_url = getattr(image, "url", "") or ""
        content = getattr(image, "content", b"") or b""
        if image_url:
            response = await http.fetch(
                self.build_url(image_url),
                breaker_key=self.name,
                max_bytes=2097152,
                headers={"Accept": "text/html"},
            )
        elif content:
            response = await http.fetch(
                f"{self._endpoint}/search/file/",
                method="POST",
                files={"file": ("image.jpg", content, getattr(image, "mime_type", "") or "image/jpeg")},
                breaker_key=self.name,
                max_bytes=2097152,
                headers={"Accept": "text/html"},
            )
        else:
            from ...core.errors import ProviderError

            raise ProviderError(self.name, "既没有图片地址也没有图片字节")
        return parse_ascii2d(response.text)
