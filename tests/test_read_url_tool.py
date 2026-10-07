"""``read_url`` 工具与"URL 转交阅读"的离线测试。

关键行为：``web_search`` 收到网址时应转为读正文——否则模型把 URL 当搜索词，
会得到一个语义完全无关的结果列表。
"""

from __future__ import annotations

from typing import Any

import pytest

from mai_websearch_under_test.core.errors import BlockedAddressError
from mai_websearch_under_test.reading.types import ReadingResult
from mai_websearch_under_test.search.router import SearchOutcome
from mai_websearch_under_test.search.types import SearchHit


class FakePipeline:
    """只记录被调用过的查询。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        del max_results
        self.queries.append(query)
        return SearchOutcome(
            query=query,
            hits=[SearchHit(title="搜索结果", url="https://example.com/r", snippet="摘要", rank=1)],
            engine_status={"bing": "ok:1"},
        )


def _reading(**overrides: Any) -> ReadingResult:
    data: dict[str, Any] = {
        "url": "https://example.com/a",
        "title": "页面标题",
        "text": "这是正文内容。",
        "strategy": "builtin",
        "hops": 1,
    }
    data.update(overrides)
    return ReadingResult(**data)


def _install_reader(plugin: Any, result: ReadingResult | Exception) -> list[str]:
    """把 ``_read_page`` 换成假实现，返回记录调用地址的列表。"""
    seen: list[str] = []

    async def fake(url: str) -> ReadingResult:
        seen.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    plugin._read_page = fake
    return seen


# ------------------------------------------------------------------ read_url 工具


async def test_read_url_renders_title_and_body(plugin: Any) -> None:
    """成功时返回标题、来源与正文。"""
    _install_reader(plugin, _reading())
    result = await plugin.handle_read_url(url="https://example.com/a", stream_id="s")

    assert result["success"] is True
    assert "《页面标题》" in result["content"]
    assert "这是正文内容" in result["content"]
    assert "https://example.com/a" in result["content"]


async def test_read_url_reports_extraction_strategy(plugin: Any) -> None:
    """诊断信息要带上用了哪条抽取策略与跳转次数。"""
    _install_reader(plugin, _reading(strategy="trafilatura", hops=2, truncated=True))
    content = (await plugin.handle_read_url(url="https://example.com/a", stream_id="s"))["content"]

    assert "trafilatura" in content
    assert "跳转 2 次" in content
    assert "截断" in content


async def test_read_url_empty_body_gives_actionable_message(plugin: Any) -> None:
    """抽不出正文时不能说"成功"，而要给出下一步建议。"""
    _install_reader(plugin, _reading(text=""))
    result = await plugin.handle_read_url(url="https://example.com/a", stream_id="s")

    assert result["success"] is False
    assert "建议" in result["content"]


async def test_read_url_empty_argument(plugin: Any) -> None:
    """没给网址时不发起请求。"""
    seen = _install_reader(plugin, _reading())
    result = await plugin.handle_read_url(url="   ", stream_id="s")

    assert result["success"] is False
    assert seen == []


async def test_read_url_blocked_address_is_readable(plugin: Any) -> None:
    """内网地址被拒时返回可读原因，而不是堆栈。"""
    _install_reader(plugin, BlockedAddressError("内网地址"))
    result = await plugin.handle_read_url(url="http://10.0.0.1/", stream_id="s")

    assert result["success"] is False
    assert "不能访问" in result["content"]
    assert "Traceback" not in result["content"]


async def test_read_url_respects_reading_switch(plugin: Any) -> None:
    """关闭阅读功能后不应再读网页。"""
    config = plugin.get_default_config()
    config["reading"]["enabled"] = False
    plugin.set_plugin_config(config)
    seen = _install_reader(plugin, _reading())

    result = await plugin.handle_read_url(url="https://example.com/a", stream_id="s")
    assert result["success"] is False
    assert seen == []


async def test_read_url_disabled_plugin(plugin: Any) -> None:
    """插件被禁用时短路。"""
    config = plugin.get_default_config()
    config["plugin"]["enabled"] = False
    plugin.set_plugin_config(config)
    seen = _install_reader(plugin, _reading())

    result = await plugin.handle_read_url(url="https://example.com/a", stream_id="s")
    assert result["success"] is False
    assert seen == []


# ------------------------------------------------------------------ URL 转交


@pytest.mark.parametrize(
    "query",
    ["https://example.com/a", "http://93.184.216.34/x", "www.example.com/a", "example.com/a?b=1"],
)
async def test_web_search_routes_urls_to_reading(plugin: Any, query: str) -> None:
    """收到网址时转为读正文，而不是拿它当搜索词。"""
    pipeline = FakePipeline()
    plugin._pipeline = pipeline
    seen = _install_reader(plugin, _reading())

    result = await plugin.handle_web_search(query=query, stream_id="s")

    assert seen == [query], "应当走阅读通路"
    assert pipeline.queries == [], "不应发起搜索"
    assert result["success"] is True
    assert "这是正文内容" in result["content"]


@pytest.mark.parametrize("query", ["baidu.com", "readme.md", "初音未来 是什么", "python asyncio 超时"])
async def test_web_search_keeps_non_urls_as_queries(plugin: Any, query: str) -> None:
    """光秃秃的域名与普通问句仍走搜索——保守判定，避免把词语误当网址。"""
    pipeline = FakePipeline()
    plugin._pipeline = pipeline
    seen = _install_reader(plugin, _reading())

    await plugin.handle_web_search(query=query, stream_id="s")

    assert pipeline.queries == [query]
    assert seen == []


async def test_web_search_does_not_route_when_reading_disabled(plugin: Any) -> None:
    """阅读被关闭时，网址也应退回搜索路径而不是报错。"""
    config = plugin.get_default_config()
    config["reading"]["enabled"] = False
    plugin.set_plugin_config(config)
    pipeline = FakePipeline()
    plugin._pipeline = pipeline

    await plugin.handle_web_search(query="https://example.com/a", stream_id="s")
    assert pipeline.queries == ["https://example.com/a"]
