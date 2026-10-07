"""引擎路由与扇出。

按查询类型选择引擎集合与融合权重，并发扇出，达到 quorum 后**提前返回并取消剩余请求**。
本模块**不导入**插件配置模型（只按属性读取配置对象），因此可以脱离 SDK 单独单测。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ..core.budget import ConcurrencyGate, gather_early
from ..core.cache import TTLCache
from ..core.errors import EmptyQueryError, SearchError, TooManyRequestsError
from .fusion import DEFAULT_WEIGHTS, WEIGHTS_BY_CLASS, fuse
from .providers.bing import BingProvider
from .providers.duckduckgo import DuckDuckGoProvider
from .providers.moegirl import (
    MOEGIRL_DOMAINS,
    MoegirlProvider,
    is_moegirl_url,
    moegirl_title_from_url,
)
from .providers.searxng import SearxngProvider
from .query import PreparedQuery, contains_cjk, prepare_query
from .types import SearchHit, SearchProvider, SearchRequest

__all__ = [
    "QueryClass",
    "SearchOutcome",
    "SearchPipeline",
    "build_search_pipeline",
    "classify_query",
]

QueryClass = Literal["error-code", "dev-ecosystem", "acg", "cjk", "general"]

# 报错/异常文本：这类查询的权威答案通常在开发者社区
_ERROR_CODE_RE = re.compile(
    r"(ERR_[A-Z0-9_]+|\bException\b|Traceback|stack\s*trace|panic:|segfault|"
    r"\bTypeError\b|\bReferenceError\b|\bSyntaxError\b|\bModuleNotFoundError\b|"
    r"cannot\s+read\s+\w+|undefined\s+is\s+not|no\s+such\s+(?:module|file))",
    re.IGNORECASE,
)
_DEV_RE = re.compile(
    r"\b(github|gitlab|stackoverflow|npm|pip|pypi|docker|kubernetes|python|javascript|"
    r"typescript|rust|golang|java|api|regex|sql|json|linux|windows)\b",
    re.IGNORECASE,
)
# ACG 语境：命中则提升萌娘百科权重
_ACG_RE = re.compile(
    r"(萌娘|动漫|番剧|声优|轻小说|galgame|vtuber|虚拟主播|初音|东方|fate|原神|明日方舟|"
    r"碧蓝|崩坏|星穹铁道|vocaloid|anime|manga|seiyuu|cosplay)",
    re.IGNORECASE,
)


def classify_query(query: str) -> QueryClass:
    """把查询归类，用于选择引擎集合与融合权重。

    优先级：报错文本 > 开发者语境 > ACG 语境 > 中文 > 通用。
    """
    text = query or ""
    if _ERROR_CODE_RE.search(text):
        return "error-code"
    if _DEV_RE.search(text):
        return "dev-ecosystem"
    if _ACG_RE.search(text):
        return "acg"
    if contains_cjk(text):
        return "cjk"
    return "general"


@dataclass(slots=True)
class SearchOutcome:
    """一次检索的完整结果与诊断信息。"""

    query: str
    hits: list[SearchHit] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)
    engine_status: dict[str, str] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    deadline_hit: bool = False
    elapsed_ms: int = 0
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        """是否有结果。"""
        return bool(self.hits)

    @property
    def failed_engines(self) -> list[str]:
        """失败的引擎名列表。"""
        return [name for name, status in self.engine_status.items() if not status.startswith("ok")]


def _short_reason(text: str, limit: int = 90) -> str:
    """把异常描述压成一行短说明。"""
    compact = " ".join((text or "").split())
    return compact if len(compact) <= limit else f"{compact[:limit]}…"


class SearchPipeline:
    """多引擎检索管线。"""

    def __init__(
        self,
        *,
        http: Any,
        providers: Sequence[SearchProvider],
        max_results: int = 8,
        deadline_seconds: float = 6.0,
        quorum: int = 2,
        grace_seconds: float = 0.8,
        cache_ttl_seconds: int = 600,
        safe_search: bool = True,
        freshness: str = "",
        gate: ConcurrencyGate | None = None,
        cache: TTLCache[str, SearchOutcome] | None = None,
        weights_by_class: Mapping[str, Mapping[str, float]] | None = None,
    ) -> None:
        self._http = http
        self._providers = list(providers)
        self._max_results = max_results
        self._deadline = deadline_seconds
        self._quorum = quorum
        self._grace = grace_seconds
        self._safe_search = safe_search
        self._freshness = freshness
        self._gate = gate or ConcurrencyGate(3)
        self._cache = cache or TTLCache(maxsize=200, ttl_seconds=cache_ttl_seconds)
        self._weights = dict(weights_by_class) if weights_by_class else WEIGHTS_BY_CLASS

    @property
    def providers(self) -> list[SearchProvider]:
        """当前可用引擎（已按配置过滤）。"""
        return list(self._providers)

    @property
    def gate(self) -> ConcurrencyGate:
        """并发闸门（诊断用）。"""
        return self._gate

    @property
    def cache(self) -> TTLCache[str, SearchOutcome]:
        """查询缓存（诊断用）。"""
        return self._cache

    def provider_by_name(self, name: str) -> SearchProvider | None:
        """按名字取引擎（接线层用它拿萌娘百科的专用阅读通路）。"""
        for provider in self._providers:
            if provider.name == name:
                return provider
        return None

    def weights_for(self, query: str) -> Mapping[str, float]:
        """取该查询类别对应的融合权重。"""
        return self._weights.get(classify_query(query), DEFAULT_WEIGHTS)

    def select(self, prepared: PreparedQuery) -> list[SearchProvider]:
        """按查询选择要跑的引擎。

        ``site:`` 限定命中某引擎的域名时，只跑那个引擎——这既是性能优化，
        也是"指定站内搜索"的正确语义。
        """
        if prepared.site:
            scoped = [p for p in self._providers if _site_matches(prepared.site, getattr(p, "domains", ()))]
            if scoped:
                return scoped
        return list(self._providers)

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        """执行一次检索。

        Raises:
            EmptyQueryError: 查询为空。
            TooManyRequestsError: 并发已满（背压，快速失败而不是排队）。
        """
        prepared = prepare_query(query)
        if not prepared.text:
            raise EmptyQueryError()

        limit = max_results or self._max_results
        cache_key = f"{prepared.text}|{prepared.site}|{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return replace(cached, from_cache=True)

        if not self._gate.try_acquire():
            raise TooManyRequestsError()

        started = time.monotonic()
        try:
            providers = self.select(prepared)
            request = SearchRequest(
                query=prepared.text,
                max_results=max(limit * 2, limit),
                safe_search=self._safe_search,
                freshness=self._freshness,
            )
            awaitables = {provider.name: provider.search(request, self._http) for provider in providers}
            gathered = await gather_early(
                awaitables,
                quorum=min(self._quorum, len(awaitables)) or 1,
                grace_seconds=self._grace,
                deadline_seconds=self._deadline,
            )
        finally:
            self._gate.release()

        engine_status: dict[str, str] = {
            name: f"ok:{len(hits)}" for name, hits in gathered.values.items()
        }
        for name, reason in gathered.errors.items():
            engine_status[name] = f"失败 {_short_reason(reason)}"

        fused = fuse(
            gathered.values,
            weights=self.weights_for(prepared.text),
            limit=limit,
        )
        outcome = SearchOutcome(
            query=prepared.text,
            hits=fused,
            engines=[provider.name for provider in providers],
            engine_status=engine_status,
            cancelled=gathered.cancelled,
            deadline_hit=gathered.deadline_hit,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        if outcome.hits:
            self._cache.set(cache_key, outcome)
        return outcome


def _site_matches(site: str, domains: Sequence[str]) -> bool:
    """``site:`` 是否落在该引擎负责的域名集合里。"""
    normalized = (site or "").lower().lstrip(".")
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in domains)


def _build_title_locator(locator_provider: SearchProvider | None) -> Any:
    """构造"用通用引擎定位萌娘百科条目"的兜底定位器。

    ``opensearch`` 是标题前缀匹配、对自然语言零召回（已实测），
    因此先用通用引擎查 ``site:zh.moegirl.org.cn <查询>``，
    再从结果 URL 的路径还原条目名——**萌娘百科的路径就是标题**，不需要额外 API 调用。
    """
    if locator_provider is None:
        return None

    async def locate(query: str, http: Any) -> list[str]:
        request = SearchRequest(query=f"site:{MOEGIRL_DOMAINS[0]} {query}", max_results=5)
        try:
            hits = await locator_provider.search(request, http)
        except SearchError:
            return []
        titles: list[str] = []
        for hit in hits:
            if not is_moegirl_url(hit.url):
                continue
            title = moegirl_title_from_url(hit.url)
            if title and title not in titles:
                titles.append(title)
        return titles

    return locate


def build_search_pipeline(config: Any, http: Any, gate: ConcurrencyGate) -> SearchPipeline:
    """按插件配置装配检索管线（配置对象只按属性读取，避免耦合 SDK）。

    每个引擎都必须受配置开关约束：实网探针证明 DuckDuckGo 在部分网络下完全不可达，
    无条件启用只会白白拖慢搜索并污染诊断信息。
    """
    engines = config.engines
    providers: list[SearchProvider] = []

    bing: BingProvider | None = None
    if engines.bing.enabled:
        bing = BingProvider(region=engines.bing.region, language=engines.bing.language)
        providers.append(bing)

    searxng = SearxngProvider(
        base_url=engines.searxng.base_url,
        categories=engines.searxng.categories,
        language=engines.searxng.language,
    )
    if engines.searxng.enabled and searxng.configured:
        providers.append(searxng)

    if engines.duckduckgo.enabled:
        providers.append(DuckDuckGoProvider(region=engines.duckduckgo.region))

    if engines.moegirl.enabled and engines.moegirl.api_base:
        # 兜底定位优先用最可靠的通用引擎
        locator: SearchProvider | None = None
        if bing is not None:
            locator = bing
        elif searxng.configured:
            locator = searxng
        providers.append(
            MoegirlProvider(
                api_base=engines.moegirl.api_base,
                extract_mode=engines.moegirl.extract_mode,
                polite_ua=engines.moegirl.polite_ua,
                site_fallback=engines.moegirl.site_fallback,
                title_locator=_build_title_locator(locator),
            )
        )

    return SearchPipeline(
        http=http,
        providers=providers,
        max_results=config.search.max_results,
        deadline_seconds=config.search.global_deadline_seconds,
        quorum=config.search.early_return_quorum,
        grace_seconds=config.search.grace_window_ms / 1000.0,
        cache_ttl_seconds=config.search.cache_ttl_seconds,
        safe_search=config.search.safe_search,
        gate=gate,
    )
