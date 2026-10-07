"""Yandex 以图搜源（免密钥，通用，URL 式最好用）。

**诚实声明**：本项目实测时 ``yandex.com/images/`` 能连通，但 ``/images/search?rpt=imageview``
只返回一个约 1.7KB 的壳页面（疑似反爬/需会话），因此**结果解析未经实网验证**。
Yandex 的结果结构变动频繁，这里同时尝试两条路：
1. ``data-bem`` 属性里的 JSON（``serp-item``）；
2. 退而求其次，直接从 HTML 里抓图片缩略图与外链。
认不出就返回空，不抛异常。跑 ``python tests/live_probe.py`` 可自查。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from ...core.html_util import attr, parse_html
from ...core.ssrf import normalize_url
from . import ReverseLookupResult, ReverseSource

__all__ = ["YandexProvider", "parse_yandex"]

DEFAULT_ENDPOINT = "https://yandex.com"
_URL_IN_JSON = re.compile(r'"(?:origUrl|url|href)":"(https?:[^"]+)"')
_TITLE_IN_JSON = re.compile(r'"(?:title|snippet|description)":"([^"]{1,200})"')


def _from_data_bem(tree: Any, base: str) -> list[ReverseSource]:
    """尝试解析 serp-item 的 data-bem JSON。"""
    sources: list[ReverseSource] = []
    for node in tree.css("[data-bem*=\"serp-item\"]"):
        raw = attr(node, "data-bem") or ""
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        blocks = payload if isinstance(payload, list) else [payload]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for item in _iter_dicts(block):
                url = _first(item, ("origUrl", "url", "href", "img_href"))
                if not url or not str(url).startswith("http"):
                    continue
                sources.append(
                    ReverseSource(
                        title=_first(item, ("title", "snippet", "description", "text")),
                        index="yandex",
                        urls=[normalize_url(str(url))],
                        thumbnail=str(_first(item, ("thumb", "preview", "img_src")) or ""),
                    )
                )
                if len(sources) >= 20:
                    return sources
    del base
    return sources


def _iter_dicts(payload: Any) -> list[dict[str, Any]]:
    """深度优先收集所有字典节点，便于在嵌套 JSON 里找链接。"""
    found: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack and len(found) < 400:
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


def _from_raw_html(html: str, base: str) -> list[ReverseSource]:
    """退化路径：直接从 HTML 抓外链。"""
    sources: list[ReverseSource] = []
    seen: set[str] = set()
    for match in _URL_IN_JSON.finditer(html):
        url = match.group(1).replace("\\/", "/")
        if url in seen:
            continue
        seen.add(url)
        sources.append(ReverseSource(index="yandex", urls=[normalize_url(url)]))
        if len(sources) >= 15:
            break
    for title_match in _TITLE_IN_JSON.finditer(html):
        if sources:
            sources[0].title = sources[0].title or title_match.group(1)
            break
    del base
    return sources


def parse_yandex(html: str, *, endpoint: str = DEFAULT_ENDPOINT) -> ReverseLookupResult:
    """提取 Yandex 结果。"""
    tree = parse_html(html)
    sources = _from_data_bem(tree, endpoint)
    if not sources:
        sources = _from_raw_html(html, endpoint)
    return ReverseLookupResult(engine="yandex", sources=sources)


class YandexProvider:
    """Yandex 以图搜源。"""

    name = "yandex"
    requires_key = False

    def __init__(self, *, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self._endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")

    @property
    def configured(self) -> bool:
        """免密钥，永远可用。"""
        return True

    def supports(self, image: Any) -> bool:
        """只支持按图片地址反查——没有 URL 就直接不参与，省得跑一次再报错。"""
        return bool(getattr(image, "url", ""))

    def build_url(self, image_url: str) -> str:
        return f"{self._endpoint}/images/search?rpt=imageview&url={quote(image_url, safe='')}"

    async def lookup(self, image: Any, http: Any) -> ReverseLookupResult:
        """反查来源。

        Raises:
            ProviderError: 既没有地址也没有字节。
        """
        image_url = getattr(image, "url", "") or ""
        if not image_url:
            from ...core.errors import ProviderError

            raise ProviderError(self.name, "Yandex 只支持按图片地址反查")
        response = await http.fetch(
            self.build_url(image_url),
            breaker_key=self.name,
            max_bytes=4194304,
            headers={"Accept": "text/html"},
        )
        return parse_yandex(response.text, endpoint=self._endpoint)
