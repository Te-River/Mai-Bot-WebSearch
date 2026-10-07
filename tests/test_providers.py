"""provider 解析测试（完全离线，用录制的 fixtures）。

这些解析器是最容易因对方改版而失效的部分，因此每条断言都对应一个具体的失败模式。
"""

from __future__ import annotations

from typing import Any

import pytest

from mai_websearch_under_test.core.errors import ProviderError
from mai_websearch_under_test.search.providers.bing import BingProvider, parse_bing_serp
from mai_websearch_under_test.search.providers.duckduckgo import (
    DuckDuckGoProvider,
    parse_ddg_html,
    unwrap_ddg_url,
)
from mai_websearch_under_test.search.providers.searxng import (
    SearxngProvider,
    parse_searxng_json,
)
from mai_websearch_under_test.search.types import SearchRequest


class FakeResponse:
    """provider 只依赖 ``.text``。"""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeHttp:
    """返回固定正文的假 HTTP 客户端。"""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return FakeResponse(self.text)


# ------------------------------------------------------------------ Bing


class TestBingParsing:
    """Bing 是零配置默认路径的主力，解析必须稳。"""

    def test_parses_all_blocks(self, read_fixture: Any) -> None:
        hits = parse_bing_serp(read_fixture("bing_serp.html"))
        assert len(hits) == 3
        assert "初音未来" in hits[0].title
        assert hits[0].snippet
        assert all(hit.engine == "bing" for hit in hits)

    def test_keeps_word_spacing_across_inline_tags(self, read_fixture: Any) -> None:
        """``<b>`` 等内联标签不能把单词粘起来（lexbor 的默认行为会）。"""
        hits = parse_bing_serp(read_fixture("bing_serp.html"))
        assert hits[1].title == "Hello World 示例页"

    def test_tolerates_missing_snippet(self, read_fixture: Any) -> None:
        """有的条目没有摘要，不能因此丢掉整条结果。"""
        hits = parse_bing_serp(read_fixture("bing_serp.html"))
        assert hits[2].snippet == ""
        assert hits[2].url.endswith("/no-snippet")

    def test_returns_empty_on_antibot_shell(self) -> None:
        """反爬壳/验证页必须解析成 0 条，而不是伪造结果。"""
        assert parse_bing_serp("<html><body>请完成验证</body></html>") == []

    def test_build_url_encodes_query_and_options(self) -> None:
        """查询要 URL 编码，安全搜索要生效。"""
        url = BingProvider().build_url(
            "初音未来",
            SearchRequest(query="初音未来", max_results=5, safe_search=False),
        )
        assert "%E5%88%9D%E9%9F%B3" in url
        assert "safesearch=off" in url
        assert "count=" in url

    def test_prepare_protects_cjk_phrase(self) -> None:
        """中文查询走词组保护。"""
        assert BingProvider().prepare("初音未来 演唱会") == '"初音未来" 演唱会'

    async def test_raises_provider_error_on_empty_parse(self) -> None:
        """解析不到结果必须显式失败，让熔断器与诊断能看到。"""
        http = FakeHttp("<html><body>blocked</body></html>")
        with pytest.raises(ProviderError):
            await BingProvider().search(SearchRequest(query="x"), http)

    async def test_passes_breaker_key(self) -> None:
        """必须带上 breaker_key，否则熔断器对它无效。"""
        http = FakeHttp("")
        with pytest.raises(ProviderError):
            await BingProvider().search(SearchRequest(query="x"), http)
        assert http.calls[0]["breaker_key"] == "bing"


# ------------------------------------------------------------------ DuckDuckGo


class TestDuckDuckGo:
    """DDG 返回的是跳转链接，解包错了会让去重与展示全部失准。"""

    def test_unwraps_redirect_link(self) -> None:
        assert (
            unwrap_ddg_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Falpha&rut=x")
            == "https://example.org/alpha"
        )

    def test_keeps_plain_link(self) -> None:
        assert unwrap_ddg_url("https://example.net/beta") == "https://example.net/beta"

    def test_handles_empty_link(self) -> None:
        assert unwrap_ddg_url("") == ""

    def test_parses_fixture_and_unwraps(self, read_fixture: Any) -> None:
        hits = parse_ddg_html(read_fixture("ddg_html.html"))
        assert len(hits) == 2
        assert hits[0].url == "https://example.org/alpha"
        assert hits[0].snippet
        assert hits[1].url == "https://example.net/beta"

    async def test_uses_post_form(self) -> None:
        """html 端点用 POST 更稳定。"""
        http = FakeHttp("")
        with pytest.raises(ProviderError):
            await DuckDuckGoProvider().search(SearchRequest(query="x"), http)
        assert http.calls[0]["method"] == "POST"
        assert "q" in http.calls[0]["data"]


# ------------------------------------------------------------------ SearXNG


class TestSearxng:
    """SearXNG 走 JSON，无解析风险，但必须处理未配置与格式未开启。"""

    def test_parses_json_results(self, read_fixture: Any) -> None:
        hits = parse_searxng_json(read_fixture("searxng.json"))
        assert len(hits) == 2  # 缺 url 的那条被跳过
        assert hits[0].title == "初音未来 - 萌娘百科"
        assert hits[0].extra["source_engine"] == "bing"
        assert hits[0].published_at

    def test_raises_on_invalid_json(self) -> None:
        with pytest.raises(ProviderError, match="JSON"):
            parse_searxng_json("<html>not json</html>")

    def test_raises_when_results_missing(self) -> None:
        """实例没开 json 格式时会返回别的结构，必须明确报错。"""
        with pytest.raises(ProviderError, match="results"):
            parse_searxng_json('{"error": "format not enabled"}')

    def test_not_configured_without_base_url(self) -> None:
        assert SearxngProvider(base_url="").configured is False
        assert SearxngProvider(base_url="http://127.0.0.1:8888/").configured is True

    def test_build_url_requests_json(self) -> None:
        url = SearxngProvider(base_url="http://127.0.0.1:8888").build_url("x", SearchRequest(query="x"))
        assert "format=json" in url

    def test_prepare_does_not_add_quotes(self) -> None:
        """JSON 接口把引号当字面量，不能做词组保护。"""
        assert SearxngProvider(base_url="http://x").prepare("初音未来 演唱会") == "初音未来 演唱会"

    async def test_raises_when_not_configured(self) -> None:
        with pytest.raises(ProviderError, match="未配置"):
            await SearxngProvider(base_url="").search(SearchRequest(query="x"), FakeHttp(""))
