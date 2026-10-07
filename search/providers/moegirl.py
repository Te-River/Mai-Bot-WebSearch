"""萌娘百科 provider（专用 JSON 通路）。

**实测结论（2026，chrome UA）决定了这里的实现方式：**

===========================  ==========================================
入口                          结果
===========================  ==========================================
``GET /初音未来``（文章 HTML）   403
``index.php?search=``         403
``api.php?action=raw``        403
``action=query&list=search``  ``action-notallowed``
``action=parse``              ``action-notallowed``
``rest.php/v1/...``           认证墙
``action=opensearch``         **可用**（返回标题候选）
``prop=extracts&explaintext`` **可用**（返回纯文本正文）
===========================  ==========================================

因此本 provider **只走两次 JSON 调用，绝不抓 HTML**——抓 403 只会白烧预算并污染熔断器。

另一个必须处理的限制：``opensearch`` 是**标题前缀匹配**，对自然语言零召回
（实测 ``search=初音未来是什么`` 返回空）。所以需要 ``site:`` 兜底：
用通用引擎定位条目 URL，再从 URL 路径还原标题（萌娘百科的路径就是标题）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from ...core.errors import ProviderError
from ...reading.types import ReadingResult, SpecialSource
from ..query import prepare_query, search_subject
from ..types import SearchHit, SearchRequest

__all__ = [
    "MOEGIRL_DOMAINS",
    "MoegirlProvider",
    "is_moegirl_url",
    "moegirl_special_source",
    "moegirl_title_from_url",
    "parse_extracts",
    "parse_opensearch",
]

MOEGIRL_DOMAINS = ("moegirl.org.cn",)

# 单条结果的摘要字符上限
_SNIPPET_BUDGET = 600

# 标题定位器：给定查询，返回候选条目标题（由接线层注入，内部通常走通用引擎 + site:）
TitleLocator = Callable[[str, Any], Awaitable[list[str]]]


def is_moegirl_url(url: str) -> bool:
    """是否萌娘百科（含 mzh 镜像）的地址。"""
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in MOEGIRL_DOMAINS)


def moegirl_title_from_url(url: str) -> str:
    """从文章 URL 还原条目标题。

    萌娘百科的文章路径**就是标题**（``/初音未来``、``/初音未来(世界计划)``），
    所以拿到 URL 就等于拿到了标题，不需要额外一次 API 调用。
    同时兼容 ``/wiki/X`` 与 ``index.php?title=X`` 两种常见形态。
    """
    parts = urlsplit(url or "")
    if parts.path.endswith("/index.php"):
        from urllib.parse import parse_qs

        return (parse_qs(parts.query).get("title") or [""])[0].strip()

    path = unquote(parts.path or "").lstrip("/")
    if path.startswith("wiki/"):
        path = path[len("wiki/") :]
    return path.strip()


def parse_opensearch(payload: str) -> list[str]:
    """解析 ``action=opensearch`` 的响应。

    格式为 ``[query, [titles], [descriptions], [urls]]``；
    对自然语言查询这里会是空列表——这是预期行为，由 ``site:`` 兜底接住。
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ProviderError("moegirl", f"opensearch 响应不是合法 JSON：{exc}") from exc
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        raise ProviderError("moegirl", "opensearch 响应结构异常")
    return [str(title).strip() for title in data[1] if str(title).strip()]


def parse_extracts(payload: str, order: Sequence[str] = ()) -> list[tuple[str, str]]:
    """解析 ``prop=extracts`` 的响应，按请求顺序返回 ``(title, text)``。

    API 以 pageid 为键返回，顺序与请求无关，因此需要按 ``order`` 重排，
    否则融合排序会把次要条目排到前面。不存在的标题会带 ``missing`` 字段，直接跳过。

    ``order`` **只用于排序，不用于过滤**：结果集以响应为准。
    过滤会在 MediaWiki 归一化标题（下划线/空格、重定向）时把有效结果误删。
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ProviderError("moegirl", f"extracts 响应不是合法 JSON：{exc}") from exc

    pages = (data.get("query") or {}).get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, dict):
        raise ProviderError("moegirl", "extracts 响应缺少 pages 字段")

    collected: list[tuple[str, str]] = []
    for page in pages.values():
        if not isinstance(page, dict) or page.get("missing") is not None:
            continue
        title = str(page.get("title") or "").strip()
        text = str(page.get("extract") or "").strip()
        if title and text:
            collected.append((title, text))

    if not order:
        return collected

    lowered = [item.strip().lower() for item in order]

    def rank(item: tuple[str, str]) -> int:
        name = item[0].strip().lower()
        return lowered.index(name) if name in lowered else len(lowered)

    return sorted(collected, key=rank)


def _article_url(article_base: str, title: str) -> str:
    """由条目标题构造文章地址。"""
    return f"{article_base}{quote(title)}"


class MoegirlProvider:
    """萌娘百科专用检索。"""

    name = "moegirl"
    kind = "json"
    requires_key = False
    domains = MOEGIRL_DOMAINS

    def __init__(
        self,
        *,
        api_base: str = "https://zh.moegirl.org.cn/api.php",
        extract_mode: str = "intro",
        polite_ua: str = "",
        site_fallback: bool = True,
        title_locator: TitleLocator | None = None,
        max_titles: int = 3,
    ) -> None:
        self._api_base = (api_base or "").strip()
        self._extract_mode = extract_mode if extract_mode in {"intro", "full"} else "intro"
        self._polite_ua = polite_ua
        self._site_fallback = site_fallback
        self._title_locator = title_locator
        self._max_titles = max(1, max_titles)

    @property
    def configured(self) -> bool:
        """是否填了 API 地址。"""
        return bool(self._api_base)

    @property
    def article_base(self) -> str:
        """文章根地址（由 ``api.php`` 反推）。"""
        base = self._api_base.rsplit("/api.php", 1)[0]
        return f"{base}/" if not base.endswith("/") else base

    def prepare(self, query: str) -> str:
        """JSON 接口把引号当字面量，因此不做词组保护。"""
        return query

    def build_opensearch_url(self, query: str, *, limit: int = 5) -> str:
        """构造标题检索地址。"""
        params = {"action": "opensearch", "search": query, "limit": str(limit), "format": "json"}
        return f"{self._api_base}?{urlencode(params)}"

    def build_extracts_url(self, titles: Sequence[str]) -> str:
        """构造正文抽取地址；**多标题合并成一次调用**，省一次往返。"""
        params: dict[str, str] = {
            "action": "query",
            "prop": "extracts",
            "explaintext": "1",
            "titles": "|".join(titles),
            "format": "json",
        }
        if self._extract_mode == "intro":
            params["exintro"] = "1"
        return f"{self._api_base}?{urlencode(params)}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._polite_ua:
            headers["User-Agent"] = self._polite_ua
        return headers

    async def _opensearch(self, query: str, http: Any) -> list[str]:
        response = await http.fetch(
            self.build_opensearch_url(query),
            breaker_key=self.name,
            max_bytes=131072,
            headers=self._headers(),
        )
        return parse_opensearch(response.text)

    async def _extracts(self, titles: Sequence[str], http: Any) -> list[tuple[str, str]]:
        if not titles:
            return []
        response = await http.fetch(
            self.build_extracts_url(titles),
            breaker_key=self.name,
            max_bytes=524288,
            headers=self._headers(),
        )
        return parse_extracts(response.text, order=titles)

    async def resolve_titles(self, query: str, http: Any) -> list[str]:
        """得到候选条目标题，按代价从低到高分三级尝试。

        1. 直接用原查询做 opensearch（标题类查询第一步就命中）；
        2. **剥掉疑问成分**后再试一次——自然语言问句实测零召回，而剥出主题词能直接命中
           （``初音未来是什么`` → ``初音未来``），代价只是一次同接口调用；
        3. 仍无结果时才动用 ``site:`` 兜底（要打一次通用引擎，最贵）。
        """
        titles = await self._opensearch(query, http)
        if titles:
            return titles

        subject = search_subject(query)
        if subject and subject != query:
            titles = await self._opensearch(subject, http)
            if titles:
                return titles

        if not self._site_fallback or self._title_locator is None:
            return []
        return await self._title_locator(subject or query, http)

    async def search(self, request: SearchRequest, http: Any) -> list[SearchHit]:
        """执行检索。

        每条候选条目产出一条结果：标题为条目名，摘要为正文（按 ``max_results`` 裁剪字符数）。
        """
        if not self.configured:
            raise ProviderError(self.name, "未配置 api_base")

        parsed = prepare_query(request.query)
        titles = await self.resolve_titles(parsed.text, http)
        if not titles:
            raise ProviderError(self.name, "没有找到对应条目")

        # 单条摘要上限：避免一条百科正文就把模型的上下文吃满
        articles = await self._extracts(titles[: self._max_titles], http)
        if not articles:
            raise ProviderError(self.name, "条目存在但没有正文")

        return [
            SearchHit(
                title=title,
                url=_article_url(self.article_base, title),
                snippet=text[:_SNIPPET_BUDGET],
                engine=self.name,
                extra={"wiki": "moegirl"},
            )
            for title, text in articles
        ]

    async def fetch_article(self, url: str, http: Any) -> ReadingResult:
        """站点专用通路：由 URL 取正文（``read_url`` 命中萌娘百科时走这里）。

        文章 HTML 是 403，所以这里**不抓网页**，而是从路径还原标题后调 ``extracts``。
        """
        title = moegirl_title_from_url(url)
        if not title:
            raise ProviderError(self.name, "无法从链接中识别条目标题")

        articles = await self._extracts([title], http)
        if not articles:
            raise ProviderError(self.name, f"没有找到条目「{title}」")
        resolved, text = articles[0]
        return ReadingResult(
            url=url,
            title=resolved,
            text=text,
            strategy="moegirl-extracts",
            content_type="application/json",
            hops=1,
        )


def moegirl_special_source(provider: MoegirlProvider) -> SpecialSource:
    """把 provider 包装成阅读层的站点专用通路。"""
    return SpecialSource(
        name="moegirl",
        matches=is_moegirl_url,
        fetch=provider.fetch_article,
    )
