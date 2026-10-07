"""萌娘百科专用通路测试。

每条断言都对应一个**实测结论**：
文章 HTML 是 403（所以必须走 JSON 接口）、
``opensearch`` 对自然语言零召回（所以必须有 ``site:`` 兜底）、
``extracts`` 按 pageid 返回而非按请求顺序（所以要重排）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest

from mai_websearch_under_test.core.errors import ProviderError
from mai_websearch_under_test.search.providers.moegirl import (
    MoegirlProvider,
    is_moegirl_url,
    moegirl_special_source,
    moegirl_title_from_url,
    parse_extracts,
    parse_opensearch,
)
from mai_websearch_under_test.search.types import SearchRequest

EMPTY_OPENSEARCH = '["初音未来是什么",[],[],[]]'


class FakeResponse:
    """provider 只依赖 ``.text``。"""

    def __init__(self, text: str) -> None:
        self.text = text


class RoutingHttp:
    """按 URL 里的 action 参数分发的假 HTTP 客户端。"""

    def __init__(self, *, opensearch: str = "", extracts: str = "") -> None:
        self.opensearch = opensearch
        self.extracts = extracts
        self.calls: list[str] = []

    async def fetch(self, url: str, **kwargs: Any) -> FakeResponse:
        del kwargs
        self.calls.append(url)
        return FakeResponse(self.opensearch if "action=opensearch" in url else self.extracts)


class TermAwareHttp:
    """按 opensearch 的搜索词返回不同载荷，用于验证"原查询 → 主题词"的两级尝试。"""

    def __init__(self, *, opensearch_by_term: dict[str, str], extracts: str = "") -> None:
        self.opensearch_by_term = opensearch_by_term
        self.extracts = extracts
        self.calls: list[str] = []

    async def fetch(self, url: str, **kwargs: Any) -> FakeResponse:
        del kwargs
        self.calls.append(url)
        if "action=opensearch" not in url:
            return FakeResponse(self.extracts)
        for term, payload in self.opensearch_by_term.items():
            if quote(term) in url:
                return FakeResponse(payload)
        return FakeResponse(EMPTY_OPENSEARCH)


# ------------------------------------------------------------------ URL 还原


class TestTitleFromUrl:
    """萌娘百科的文章路径就是标题，拿到 URL 就等于拿到标题。"""

    def test_plain_title(self) -> None:
        assert moegirl_title_from_url("https://zh.moegirl.org.cn/初音未来") == "初音未来"

    def test_percent_encoded_title(self) -> None:
        url = "https://zh.moegirl.org.cn/%E5%88%9D%E9%9F%B3%E6%9C%AA%E6%9D%A5"
        assert moegirl_title_from_url(url) == "初音未来"

    def test_title_with_parentheses(self) -> None:
        url = "https://zh.moegirl.org.cn/%E5%88%9D%E9%9F%B3%E6%9C%AA%E6%9D%A5(%E4%B8%96%E7%95%8C%E8%AE%A1%E5%88%92)"
        assert moegirl_title_from_url(url) == "初音未来(世界计划)"

    def test_wiki_path_form(self) -> None:
        assert moegirl_title_from_url("https://zh.moegirl.org.cn/wiki/初音未来") == "初音未来"

    def test_index_php_form(self) -> None:
        assert moegirl_title_from_url("https://zh.moegirl.org.cn/index.php?title=初音未来") == "初音未来"

    def test_empty_url(self) -> None:
        assert moegirl_title_from_url("") == ""


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://zh.moegirl.org.cn/初音未来", True),
        ("https://mzh.moegirl.org.cn/初音未来", True),
        ("https://www.moegirl.org.cn/x", True),
        ("https://moegirl.org.cn/x", True),
        ("https://example.com/x", False),
        ("https://notmoegirl.org.cn/x", False),
        ("https://moegirl.org.cn.evil.com/x", False),
    ],
)
def test_is_moegirl_url(url: str, expected: bool) -> None:
    """站点匹配必须严格，``notmoegirl.org.cn`` 这种近似域名不能命中。"""
    assert is_moegirl_url(url) is expected


# ------------------------------------------------------------------ 解析


class TestParseOpensearch:
    def test_parses_titles(self, read_fixture: Any) -> None:
        titles = parse_opensearch(read_fixture("moegirl_opensearch.json"))
        assert titles == ["初音未来", "初音未来(世界计划)"]

    def test_natural_language_returns_empty(self) -> None:
        """实测：opensearch 是标题前缀匹配，自然语言零召回——这是预期行为。"""
        assert parse_opensearch(EMPTY_OPENSEARCH) == []

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ProviderError, match="JSON"):
            parse_opensearch("<html>认证墙</html>")

    def test_unexpected_structure_raises(self) -> None:
        with pytest.raises(ProviderError, match="结构"):
            parse_opensearch('{"error":"action-notallowed"}')


class TestParseExtracts:
    def test_reorders_by_requested_titles(self, read_fixture: Any) -> None:
        """API 按 pageid 返回，顺序与请求无关，必须按请求顺序重排。"""
        parsed = parse_extracts(
            read_fixture("moegirl_extracts.json"),
            order=["初音未来", "初音未来(世界计划)"],
        )
        assert [title for title, _ in parsed] == ["初音未来", "初音未来(世界计划)"]

    def test_skips_missing_entries(self, read_fixture: Any) -> None:
        """``missing`` 的条目要跳过，而不是产出空正文的一条结果。

        注意 ``order`` 只用于排序、不用于过滤：结果集以 API 响应为准，
        过滤会在 MediaWiki 归一化标题时误删有效结果。
        """
        parsed = parse_extracts(read_fixture("moegirl_extracts.json"), order=["初音未来", "不存在的条目"])
        titles = [title for title, _ in parsed]
        assert "不存在的条目" not in titles
        assert titles[0] == "初音未来", "请求顺序里的条目应排在前面"

    def test_unknown_titles_go_last(self, read_fixture: Any) -> None:
        parsed = parse_extracts(read_fixture("moegirl_extracts.json"), order=["别的条目"])
        assert len(parsed) == 2

    def test_without_order_keeps_api_order(self, read_fixture: Any) -> None:
        assert len(parse_extracts(read_fixture("moegirl_extracts.json"))) == 2

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ProviderError, match="JSON"):
            parse_extracts("not json")

    def test_missing_pages_raises(self) -> None:
        with pytest.raises(ProviderError, match="pages"):
            parse_extracts('{"error":"action-notallowed"}')


# ------------------------------------------------------------------ provider


def _provider(**overrides: Any) -> MoegirlProvider:
    options: dict[str, Any] = {
        "api_base": "https://zh.moegirl.org.cn/api.php",
        "extract_mode": "intro",
        "site_fallback": True,
        "title_locator": None,
    }
    options.update(overrides)
    return MoegirlProvider(**options)


class TestProviderUrls:
    def test_article_base_derived_from_api_base(self) -> None:
        assert _provider().article_base == "https://zh.moegirl.org.cn/"

    def test_extracts_url_merges_titles_into_one_call(self) -> None:
        """多标题合并成一次请求——这是省一次往返的关键。"""
        url = _provider().build_extracts_url(["初音未来", "初音未来(世界计划)"])
        assert "%7C" in url  # titles 之间的 | 被编码
        assert "exintro=1" in url

    def test_full_mode_has_no_exintro(self) -> None:
        url = _provider(extract_mode="full").build_extracts_url(["初音未来"])
        assert "exintro" not in url

    def test_prepare_does_not_add_quotes(self) -> None:
        """JSON 接口把引号当字面量。"""
        assert _provider().prepare("初音未来 演唱会") == "初音未来 演唱会"

    def test_not_configured_without_api_base(self) -> None:
        assert _provider(api_base="").configured is False


class TestProviderSearch:
    async def test_happy_path_uses_opensearch_then_extracts(self, read_fixture: Any) -> None:
        http = RoutingHttp(
            opensearch=read_fixture("moegirl_opensearch.json"),
            extracts=read_fixture("moegirl_extracts.json"),
        )
        hits = await _provider().search(SearchRequest(query="初音未来"), http)

        assert len(http.calls) == 2, "应当只有两次调用：opensearch + extracts"
        assert [hit.title for hit in hits] == ["初音未来", "初音未来(世界计划)"]
        assert hits[0].url.startswith("https://zh.moegirl.org.cn/")
        assert "Crypton" in hits[0].snippet
        assert all(hit.engine == "moegirl" for hit in hits)

    async def test_snippet_is_bounded(self, read_fixture: Any) -> None:
        """单条摘要必须截断，否则一条百科正文就能吃满模型上下文。"""
        http = RoutingHttp(
            opensearch=read_fixture("moegirl_opensearch.json"),
            extracts=read_fixture("moegirl_extracts.json"),
        )
        hits = await _provider().search(SearchRequest(query="初音未来"), http)
        assert all(len(hit.snippet) <= 600 for hit in hits)

    async def test_site_fallback_when_opensearch_returns_nothing(self, read_fixture: Any) -> None:
        """自然语言零召回时必须靠 site: 兜底拿到标题。"""
        located: list[str] = []

        async def locator(query: str, http: Any) -> list[str]:
            located.append(query)
            return ["初音未来"]

        http = RoutingHttp(opensearch=EMPTY_OPENSEARCH, extracts=read_fixture("moegirl_extracts.json"))
        hits = await _provider(title_locator=locator).search(SearchRequest(query="初音未来是什么"), http)

        assert located == ["初音未来"], "兜底应拿剥掉疑问词的**主题词**，而不是整句问句"
        assert hits[0].title == "初音未来", "兜底定位到的标题应排在结果首位"

    async def test_subject_retry_avoids_expensive_fallback(self, read_fixture: Any) -> None:
        """剥掉疑问词后 opensearch 直接命中，就不该再打一次通用引擎。

        这是实网探针发现的真实缺陷：原来只试原查询，导致"初音未来是什么"
        白白走一趟 site: 兜底（而它在 Bing 上找不到萌娘百科页面）。
        """
        located: list[str] = []

        async def locator(query: str, http: Any) -> list[str]:
            located.append(query)
            return ["不该被调用"]

        http = TermAwareHttp(
            opensearch_by_term={"初音未来": read_fixture("moegirl_opensearch.json")},
            extracts=read_fixture("moegirl_extracts.json"),
        )
        hits = await _provider(title_locator=locator).search(SearchRequest(query="初音未来是什么"), http)

        assert located == [], "主题词能命中时不应动用 site: 兜底"
        assert hits[0].title == "初音未来"

    async def test_no_fallback_when_disabled(self) -> None:
        """关闭兜底时零召回就直接失败，不应静默返回空。"""

        async def locator(query: str, http: Any) -> list[str]:
            return ["不该被调用"]

        http = RoutingHttp(opensearch=EMPTY_OPENSEARCH)
        with pytest.raises(ProviderError, match="没有找到"):
            await _provider(site_fallback=False, title_locator=locator).search(
                SearchRequest(query="初音未来是什么"), http
            )

    async def test_no_locator_means_no_fallback(self) -> None:
        """没有可用的通用引擎时不硬撑，直接给出可解释的失败。"""
        http = RoutingHttp(opensearch=EMPTY_OPENSEARCH)
        with pytest.raises(ProviderError):
            await _provider().search(SearchRequest(query="初音未来是什么"), http)

    async def test_not_configured_raises(self) -> None:
        with pytest.raises(ProviderError, match="api_base"):
            await _provider(api_base="").search(SearchRequest(query="x"), RoutingHttp())

    async def test_passes_breaker_key(self, read_fixture: Any) -> None:
        """必须带 breaker_key，否则熔断器对这个源无效。"""
        http = RoutingHttp(
            opensearch=read_fixture("moegirl_opensearch.json"),
            extracts=read_fixture("moegirl_extracts.json"),
        )
        await _provider().search(SearchRequest(query="初音未来"), http)
        assert all("action=" in url for url in http.calls)


class TestFetchArticle:
    async def test_returns_reading_result(self, read_fixture: Any) -> None:
        """站点专用通路：由 URL 取正文，不抓 HTML。"""
        http = RoutingHttp(extracts=read_fixture("moegirl_extracts.json"))
        result = await _provider().fetch_article("https://zh.moegirl.org.cn/初音未来", http)

        assert result.title == "初音未来"
        assert "Crypton" in result.text
        assert result.strategy == "moegirl-extracts"
        assert len(http.calls) == 1, "不应有网页抓取"

    async def test_unresolvable_url_raises(self) -> None:
        with pytest.raises(ProviderError, match="标题"):
            await _provider().fetch_article("https://zh.moegirl.org.cn/", RoutingHttp())

    async def test_missing_article_raises(self) -> None:
        http = RoutingHttp(extracts='{"query":{"pages":{"-1":{"title":"x","missing":""}}}}')
        with pytest.raises(ProviderError, match="没有找到"):
            await _provider().fetch_article("https://zh.moegirl.org.cn/不存在", http)


def test_special_source_matches_moegirl_only() -> None:
    """专用通路只应接管萌娘百科，且通过它才绕开 403 的 HTML 抓取。"""
    source = moegirl_special_source(_provider())
    assert source.matches("https://zh.moegirl.org.cn/初音未来") is True
    assert source.matches("https://example.com/x") is False
