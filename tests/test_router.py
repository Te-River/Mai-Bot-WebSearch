"""检索管线测试：分类、站点定向、融合、缓存、背压、故障隔离。

用假的 provider 驱动，不碰网络——这样"早返回""背压""缓存"这些机制才测得准。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mai_websearch_under_test.core.budget import ConcurrencyGate
from mai_websearch_under_test.core.errors import EmptyQueryError, ProviderError, TooManyRequestsError
from mai_websearch_under_test.search.query import PreparedQuery
from mai_websearch_under_test.search.router import (
    SearchOutcome,
    SearchPipeline,
    classify_query,
)
from mai_websearch_under_test.search.types import SearchHit, SearchRequest


class FakeProvider:
    """可配置延迟 / 失败 / 命中的假引擎。"""

    kind = "json"
    requires_key = False

    def __init__(
        self,
        name: str,
        *,
        hits: tuple[SearchHit, ...] = (),
        delay: float = 0.0,
        error: Exception | None = None,
        domains: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.domains = domains
        self.calls = 0
        self._hits = list(hits)
        self._delay = delay
        self._error = error

    def prepare(self, query: str) -> str:
        return query

    async def search(self, request: SearchRequest, http: Any) -> list[SearchHit]:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return list(self._hits)


class FakeHttp:
    """管线只把 http 透传给 provider；测试里不应该真的发请求。"""

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("测试不应发起真实 HTTP 请求")


def _hit(url: str, *, title: str = "标题", engine: str = "fake") -> SearchHit:
    return SearchHit(title=title, url=url, snippet="摘要", engine=engine)


def _pipeline(providers: list[FakeProvider], **overrides: Any) -> SearchPipeline:
    options: dict[str, Any] = {
        "http": FakeHttp(),
        "providers": providers,
        "max_results": 8,
        "deadline_seconds": 5.0,
        "quorum": 2,
        "grace_seconds": 0.05,
    }
    options.update(overrides)
    return SearchPipeline(**options)


# ------------------------------------------------------------------ 分类


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ModuleNotFoundError: no module named requests", "error-code"),
        ("Traceback most recent call last", "error-code"),
        ("python 列表推导式", "dev-ecosystem"),
        ("docker compose 端口映射", "dev-ecosystem"),
        ("初音未来的声优是谁", "acg"),
        ("原神 新版本", "acg"),
        ("今天天气怎么样", "cjk"),
        ("hello world", "general"),
    ],
)
def test_classify_query(query: str, expected: str) -> None:
    """分类决定融合权重，必须稳定可预测。"""
    assert classify_query(query) == expected


def test_classification_priority_prefers_error_code() -> None:
    """带报错的查询即使含 ACG 关键词，也应走开发者语境。"""
    assert classify_query("原神 启动报错 Traceback") == "error-code"


def test_weights_lookup_falls_back_to_default() -> None:
    """未知类别要退回默认权重，不能 KeyError。"""
    pipeline = _pipeline([FakeProvider("bing")])
    assert pipeline.weights_for("hello")["bing"] == pytest.approx(0.4)


# ------------------------------------------------------------------ 引擎选择


def test_site_operator_scopes_to_matching_provider() -> None:
    """``site:`` 命中某引擎的域名时只跑它——既是性能优化也是正确语义。"""
    moegirl = FakeProvider("moegirl", domains=("moegirl.org.cn",))
    bing = FakeProvider("bing")
    pipeline = _pipeline([moegirl, bing])  # type: ignore[list-item]
    selected = pipeline.select(PreparedQuery(text="初音未来", site="zh.moegirl.org.cn"))
    assert [provider.name for provider in selected] == ["moegirl"]


def test_site_operator_falls_back_to_all_when_unknown() -> None:
    """没有引擎认领该域名时不能把自己锁死成 0 个引擎。"""
    pipeline = _pipeline([FakeProvider("bing")])
    selected = pipeline.select(PreparedQuery(text="x", site="unknown.example"))
    assert [provider.name for provider in selected] == ["bing"]


# ------------------------------------------------------------------ 检索


async def test_search_fuses_results_from_multiple_engines() -> None:
    """多引擎结果应被融合并记录每个引擎的状态。"""
    pipeline = _pipeline(
        [
            FakeProvider("bing", hits=(_hit("https://a.example/1", title="A", engine="bing"),)),
            FakeProvider("duckduckgo", hits=(_hit("https://b.example/1", title="B", engine="duckduckgo"),)),
        ]
    )
    outcome = await pipeline.search("初音未来")
    assert isinstance(outcome, SearchOutcome)
    assert outcome.ok is True
    assert {hit.url for hit in outcome.hits} == {"https://a.example/1", "https://b.example/1"}
    assert outcome.engine_status["bing"] == "ok:1"
    assert outcome.failed_engines == []


async def test_failure_of_one_engine_does_not_break_others() -> None:
    """故障隔离：一个源挂了，其它源的结果照常返回。"""
    pipeline = _pipeline(
        [
            FakeProvider("bing", hits=(_hit("https://a.example/1", engine="bing"),)),
            FakeProvider("duckduckgo", error=ProviderError("duckduckgo", "被反爬")),
        ]
    )
    outcome = await pipeline.search("初音未来")
    assert outcome.ok is True
    assert outcome.failed_engines == ["duckduckgo"]
    assert "被反爬" in outcome.engine_status["duckduckgo"]


async def test_all_engines_failing_is_reported_not_swallowed() -> None:
    """全部失败时必须留下可解释的状态，绝不静默返回空。"""
    pipeline = _pipeline(
        [
            FakeProvider("bing", error=ProviderError("bing", "网络断了")),
            FakeProvider("duckduckgo", error=ProviderError("duckduckgo", "超时")),
        ]
    )
    outcome = await pipeline.search("初音未来")
    assert outcome.ok is False
    assert set(outcome.failed_engines) == {"bing", "duckduckgo"}
    assert all(status.startswith("失败") for status in outcome.engine_status.values())


async def test_cache_prevents_second_network_round() -> None:
    """高频场景下，重复查询必须命中缓存。"""
    provider = FakeProvider("bing", hits=(_hit("https://a.example/1", engine="bing"),))
    pipeline = _pipeline([provider])

    first = await pipeline.search("初音未来")
    second = await pipeline.search("初音未来")

    assert first.from_cache is False
    assert second.from_cache is True
    assert provider.calls == 1
    assert [hit.url for hit in second.hits] == [hit.url for hit in first.hits]


async def test_failed_search_is_not_cached() -> None:
    """失败不进缓存，否则一次网络抖动会被"记住"十分钟。"""
    provider = FakeProvider("bing", error=ProviderError("bing", "临时故障"))
    pipeline = _pipeline([provider])

    await pipeline.search("初音未来")
    await pipeline.search("初音未来")
    assert provider.calls == 2


async def test_empty_query_raises() -> None:
    """空查询要明确报错，而不是发一次无意义的网络请求。"""
    pipeline = _pipeline([FakeProvider("bing")])
    with pytest.raises(EmptyQueryError):
        await pipeline.search("   ")


async def test_slow_engine_is_cancelled_after_quorum() -> None:
    """早返回机制在管线层面生效：慢引擎被取消并记录。"""
    fast = FakeProvider("bing", hits=(_hit("https://a.example/1", engine="bing"),))
    slow = FakeProvider("duckduckgo", delay=30.0)
    pipeline = _pipeline([fast, slow], quorum=1, grace_seconds=0.05)

    outcome = await asyncio.wait_for(pipeline.search("初音未来"), timeout=2.0)
    assert outcome.ok is True
    assert "duckduckgo" in outcome.cancelled
    assert outcome.deadline_hit is False


async def test_deadline_marks_outcome() -> None:
    """到达硬截止要留下痕迹，让用户知道结果是"先到的那些"。"""
    pipeline = _pipeline(
        [FakeProvider("bing", delay=30.0), FakeProvider("duckduckgo", delay=30.0)],
        deadline_seconds=0.05,
        quorum=2,
        grace_seconds=5.0,
    )
    outcome = await pipeline.search("初音未来")
    assert outcome.deadline_hit is True
    assert len(outcome.cancelled) == 2


async def test_backpressure_rejects_instead_of_queueing() -> None:
    """并发满时应快速失败，而不是把请求堆在队列里让用户等更久。"""
    release = asyncio.Event()

    class BlockingProvider(FakeProvider):
        async def search(self, request: SearchRequest, http: Any) -> list[SearchHit]:
            self.calls += 1
            await release.wait()
            return []

    pipeline = _pipeline([BlockingProvider("slow")], gate=ConcurrencyGate(1), deadline_seconds=5.0)
    task = asyncio.create_task(pipeline.search("第一个"))
    await asyncio.sleep(0.02)

    with pytest.raises(TooManyRequestsError):
        await pipeline.search("第二个")

    release.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert pipeline.gate.inflight == 0


async def test_gate_is_released_on_failure() -> None:
    """异常路径也必须释放并发位，否则插件会永久卡死。"""
    pipeline = _pipeline([FakeProvider("bing", error=ProviderError("bing", "炸"))], gate=ConcurrencyGate(1))
    await pipeline.search("初音未来")
    assert pipeline.gate.inflight == 0


def test_select_ignores_providers_without_domains_declaration() -> None:
    """未声明 domains 的引擎不参与站点定向，避免误伤。"""
    pipeline = _pipeline([FakeProvider("bing")])
    assert [provider.name for provider in pipeline.select(PreparedQuery(text="x", site="a.com"))] == ["bing"]


# ------------------------------------------------------------------ 引擎开关


def test_default_pipeline_contains_only_measured_working_engines() -> None:
    """零配置下只应启用实测可用的引擎：Bing + 萌娘百科。

    DuckDuckGo 实测不可达（默认关）、SearXNG 需要自建实例（默认关），
    因此默认路径必须既不被拖慢，也不产生噪音失败。
    """
    from mai_websearch_under_test.config import WebSearchConfig
    from mai_websearch_under_test.search.router import build_search_pipeline

    pipeline = build_search_pipeline(WebSearchConfig(), object(), ConcurrencyGate(3))
    assert [provider.name for provider in pipeline.providers] == ["bing", "moegirl"]


def test_enabling_engines_adds_them_to_pipeline() -> None:
    """开关打开后引擎应进入管线。"""
    from mai_websearch_under_test.config import WebSearchConfig
    from mai_websearch_under_test.search.router import build_search_pipeline

    config = WebSearchConfig()
    config.engines.duckduckgo.enabled = True
    pipeline = build_search_pipeline(config, object(), ConcurrencyGate(3))
    assert [provider.name for provider in pipeline.providers] == ["bing", "duckduckgo", "moegirl"]


def test_searxng_requires_base_url_even_when_enabled() -> None:
    """只把 enabled 打开但没填地址，不应装配出一个必然报错的引擎。"""
    from mai_websearch_under_test.config import WebSearchConfig
    from mai_websearch_under_test.search.router import build_search_pipeline

    config = WebSearchConfig()
    config.engines.bing.enabled = False
    config.engines.moegirl.enabled = False
    config.engines.searxng.enabled = True
    pipeline = build_search_pipeline(config, object(), ConcurrencyGate(3))
    assert pipeline.providers == []
