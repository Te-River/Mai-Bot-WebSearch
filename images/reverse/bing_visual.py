"""Bing 视觉搜索（免密钥，通用）。

**诚实声明**：本项目实测时 Bing 的 ``/images/searchbyimage/upload`` 对脚本请求
返回 302 自环或 400（疑似需要浏览器会话参数），**整条链路未经实网验证**。
这里仍按公开用法实现，并把"拿不到 insightsToken"当作可预期失败而非异常。

流程：``POST /images/searchbyimage/upload``（图片字节）→ 302 到带 ``insightsToken``
的结果页 → 解析该页。结果页里视觉搜索的载荷通常在 ``IG``/``insightsToken`` 相关的
脚本 JSON 里，同时有普通图片块；两条路都试，认不出就返回空。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...core.html_util import attr, parse_html
from ...core.ssrf import normalize_url
from . import ReverseLookupResult, ReverseSource

__all__ = ["BingVisualProvider", "parse_bing_visual"]

DEFAULT_ENDPOINT = "https://www.bing.com"
_PAGES_RE = re.compile(r'PagesIncluding|VisualSearch|\"insightsToken\"')


def _iter_dicts(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack and len(found) < 600:
        current = stack.pop()
        if isinstance(current, dict):
            found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _first(block: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if block.get(key):
            return block[key]
    return None


def _from_embedded_json(html: str) -> list[ReverseSource]:
    """从页面里的多个 JSON 脚本块中提取来源页与相似图。"""
    sources: list[ReverseSource] = []
    seen: set[str] = set()
    for match in re.finditer(r'IG\("[^"]+",\s*(\{.*?\})\s*\)\s*;?\s*</script>', html, re.DOTALL):
        raw = match.group(1)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for block in _iter_dicts(payload):
            url = _first(block, ("contentUrl", "hostPageUrl", "webSearchUrl", "url", "murl"))
            if not url or not str(url).startswith("http"):
                continue
            cleaned = str(url).replace("\\/", "/")
            if cleaned in seen:
                continue
            seen.add(cleaned)
            sources.append(
                ReverseSource(
                    title=str(_first(block, ("name", "title", "snippet")) or ""),
                    index="bing-visual",
                    urls=[normalize_url(cleaned)],
                    thumbnail=str(_first(block, ("thumbnailUrl", "thumbnail", "turl")) or ""),
                )
            )
            if len(sources) >= 20:
                return sources
    return sources


def _from_image_blocks(html: str) -> list[ReverseSource]:
    """退化路径：抓结果页里的图片块。"""
    tree = parse_html(html)
    sources: list[ReverseSource] = []
    seen: set[str] = set()
    for node in tree.css("a.iusc[href], .img_cont[href], a[href]"):
        href = attr(node, "href") or ""
        m = re.search(r'"murl":"(https?[^"]+)"', attr(node, "m") or "")
        url = m.group(1) if m else href
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        sources.append(ReverseSource(index="bing-visual", urls=[normalize_url(url.replace("\\/", "/"))]))
        if len(sources) >= 15:
            break
    return sources


def parse_bing_visual(html: str) -> ReverseLookupResult:
    """解析 Bing 视觉搜索结果页。"""
    if not _PAGES_RE.search(html) and "insightsToken" not in html:
        # 没有视觉搜索特征——可能返回了首页或反爬页
        return ReverseLookupResult(engine="bing-visual", error="结果页不含视觉搜索数据（可能被反爬）")
    sources = _from_embedded_json(html)
    if not sources:
        sources = _from_image_blocks(html)
    return ReverseLookupResult(engine="bing-visual", sources=sources)


class BingVisualProvider:
    """Bing 视觉搜索。"""

    name = "bing-visual"
    requires_key = False

    def __init__(self, *, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self._endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")

    @property
    def configured(self) -> bool:
        """免密钥，永远可用。"""
        return True

    def supports(self, image: Any) -> bool:
        """地址和字节都能处理。"""
        return bool(getattr(image, "url", "") or getattr(image, "content", b""))

    def _results_url(self, location: str) -> str:
        if location.startswith("http"):
            return location
        return f"{self._endpoint}{location}"

    async def lookup(self, image: Any, http: Any) -> ReverseLookupResult:
        """上传并解析视觉搜索结果。

        Raises:
            ProviderError: 既没有地址也没有字节。
        """
        image_url = getattr(image, "url", "") or ""
        content = getattr(image, "content", b"") or b""
        if not image_url and not content:
            from ...core.errors import ProviderError

            raise ProviderError(self.name, "既没有图片地址也没有图片字节")

        # 第一步：提交图片，换取带 insightsToken 的结果页 URL
        if content:
            upload = await http.fetch(
                f"{self._endpoint}/images/searchbyimage/upload",
                method="POST",
                files={"imgurl": ("image.jpg", content, getattr(image, "mime_type", "") or "image/jpeg")},
                data={"cbir": "sbi", "imageBin": ""},
                follow_redirects=False,
                breaker_key=self.name,
                max_bytes=65536,
                headers={"Accept": "text/html", "Referer": f"{self._endpoint}/images/"},
            )
        else:
            upload = await http.fetch(
                f"{self._endpoint}/images/searchbyimage?cbir=sbi&imgurl={image_url}",
                follow_redirects=False,
                breaker_key=self.name,
                max_bytes=65536,
                headers={"Accept": "text/html", "Referer": f"{self._endpoint}/images/"},
            )

        location = (upload.headers or {}).get("location", "")
        if not location:
            return ReverseLookupResult(engine="bing-visual", error="上传后没有拿到结果页跳转")
        query = parse_qs(urlsplit(self._results_url(location)).query)
        if "insightstoken" not in {key.lower() for key in query}:
            return ReverseLookupResult(engine="bing-visual", error="跳转链接里没有 insightsToken（可能被反爬）")

        # 第二步：抓取并解析结果页
        page = await http.fetch(
            self._results_url(location),
            breaker_key=self.name,
            max_bytes=4194304,
            headers={"Accept": "text/html"},
        )
        return parse_bing_visual(page.text)
