"""SauceNAO 反查（公开 JSON API，需要 API Key）。

选它而不是 ascii2d / IQDB / Bing 视觉搜索的原因很简单：**它有公开文档的 JSON 接口**，
可以按规范实现并用固定响应做离线测试；那几个只有 HTML 页面且反爬，属于"不可验证的代码"。

⚠️ **本环境未验证**：``saucenao.com`` 在项目实测网络下域名不可达（ConnectError），
因此这个 provider 的联网行为没有实网证据，只有按官方文档实现的解析与单测。
AC 来源识别场景下它是公认最准的引擎，有 Key 且网络可达时值得启用。

API 文档：https://saucenao.com/user.php?page=search-api
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from ...core.errors import ProviderError
from . import ReverseLookupResult, ReverseSource

__all__ = ["SaucenaoProvider", "parse_saucenao"]

DEFAULT_ENDPOINT = "https://saucenao.com/search.php"

# header.status 的非零取值（官方文档）
_STATUS_MESSAGES = {
    -1: "无效的 API Key",
    -2: "超出配额（免费额度用尽）",
    -3: "请求过于频繁",
    -4: "无效的数据库编号",
    -5: "无效的输出类型",
}


def _similarity(raw: Any) -> float:
    """相似度是字符串，可能是空或非数字。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _first_non_empty(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按顺序取第一个非空字段（不同索引的字段名不一样）。"""
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def parse_saucenao(payload: str, *, min_similarity: float = 0.0) -> ReverseLookupResult:
    """解析 SauceNAO 的 ``output_type=2`` 响应。

    Raises:
        ProviderError: 响应不是合法 JSON 或结构异常。
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ProviderError("saucenao", f"响应不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("saucenao", "响应结构异常")

    header = data.get("header") if isinstance(data.get("header"), dict) else {}
    status = header.get("status", 0)
    if status not in (0, "0"):
        reason = _STATUS_MESSAGES.get(status, f"接口返回 status={status}")
        return ReverseLookupResult(engine="saucenao", error=reason)

    results = data.get("results")
    if not isinstance(results, list):
        raise ProviderError("saucenao", "响应缺少 results 字段")

    sources: list[ReverseSource] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        item_header = item.get("header") if isinstance(item.get("header"), dict) else {}
        item_data = item.get("data") if isinstance(item.get("data"), dict) else {}
        similarity = _similarity(item_header.get("similarity"))
        if similarity < min_similarity:
            continue
        urls = [str(url) for url in (item_data.get("ext_urls") or []) if url]
        sources.append(
            ReverseSource(
                title=_first_non_empty(item_data, ("title", "jp_name", "eng_name", "source")),
                author=_first_non_empty(item_data, ("author_name", "member_name", "creator", "artist")),
                similarity=similarity,
                index=str(item_header.get("index_name") or ""),
                urls=urls,
                thumbnail=str(item_header.get("thumbnail") or ""),
            )
        )

    sources.sort(key=lambda source: source.similarity, reverse=True)
    return ReverseLookupResult(engine="saucenao", sources=sources)


class SaucenaoProvider:
    """SauceNAO 以图搜源。"""

    name = "saucenao"
    requires_key = True

    def __init__(
        self,
        *,
        api_key: str = "",
        endpoint: str = DEFAULT_ENDPOINT,
        databases: str = "999",
        min_similarity: float = 50.0,
        numres: int = 5,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._endpoint = endpoint or DEFAULT_ENDPOINT
        self._databases = databases or "999"
        self._min_similarity = min_similarity
        self._numres = max(1, min(numres, 10))

    @property
    def configured(self) -> bool:
        """没有 API Key 就没法用。"""
        return bool(self._api_key)

    def supports(self, image: Any) -> bool:
        """地址和字节都能处理。"""
        return bool(getattr(image, "url", "") or getattr(image, "content", b""))

    def build_query_url(self, image_url: str) -> str:
        """用图片地址反查（免上传，最快）。"""
        params = {
            "output_type": "2",
            "api_key": self._api_key,
            "db": self._databases,
            "numres": str(self._numres),
            "url": image_url,
        }
        return f"{self._endpoint}?{urlencode(params)}"

    def build_form(self) -> dict[str, str]:
        """用图片字节反查时的表单字段（``file`` 由调用方以 multipart 补上）。"""
        return {
            "output_type": "2",
            "api_key": self._api_key,
            "db": self._databases,
            "numres": str(self._numres),
        }

    async def lookup(self, image: Any, http: Any) -> ReverseLookupResult:
        """反查来源。

        优先用图片地址（免上传）；只有字节时才走 multipart 上传。

        Raises:
            ProviderError: 未配置 API Key。
        """
        if not self.configured:
            raise ProviderError(self.name, "未配置 API Key（reverse.saucenao_api_key）")

        image_url = getattr(image, "url", "") or ""
        content = getattr(image, "content", b"") or b""

        if image_url:
            response = await http.fetch(
                self.build_query_url(image_url),
                breaker_key=self.name,
                max_bytes=524288,
                headers={"Accept": "application/json"},
            )
        elif content:
            response = await http.fetch(
                self._endpoint,
                method="POST",
                data=self.build_form(),
                files={"file": ("image.jpg", content, getattr(image, "mime_type", "") or "image/jpeg")},
                breaker_key=self.name,
                max_bytes=524288,
                headers={"Accept": "application/json"},
            )
        else:
            raise ProviderError(self.name, "既没有图片地址也没有图片字节")

        return parse_saucenao(response.text, min_similarity=self._min_similarity)
