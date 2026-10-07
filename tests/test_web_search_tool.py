"""``web_search`` 工具的离线测试。

验收要点：工具**永远不抛异常**给宿主，失败时返回可读原因与已尝试的引擎。
"""

from __future__ import annotations

from typing import Any

import pytest

from mai_websearch_under_test.core.errors import NetworkError
from mai_websearch_under_test.search.router import SearchOutcome
from mai_websearch_under_test.search.types import SearchHit


class FakePipeline:
    """只实现 ``search`` 的假管线。"""

    def __init__(self, outcome: SearchOutcome | None = None, error: Exception | None = None) -> None:
        self.outcome = outcome
        self.error = error
        self.queries: list[str] = []

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        del max_results
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome


def _outcome(**overrides: Any) -> SearchOutcome:
    base: dict[str, Any] = {
        "query": "初音未来",
        "hits": [
            SearchHit(title="初音未来 - 萌娘百科", url="https://zh.moegirl.org.cn/x", snippet="虚拟歌手", rank=1),
            SearchHit(title="第二条", url="https://example.com/y", snippet="摘要二", rank=2),
        ],
        "engines": ["bing"],
        "engine_status": {"bing": "ok:2"},
        "elapsed_ms": 120,
    }
    base.update(overrides)
    return SearchOutcome(**base)


async def test_returns_titles_urls_and_snippets(plugin: Any) -> None:
    """成功时把标题、链接、摘要交给模型。"""
    plugin._pipeline = FakePipeline(_outcome())
    result = await plugin.handle_web_search(query="初音未来", stream_id="s")

    assert result["success"] is True
    assert "初音未来 - 萌娘百科" in result["content"]
    assert "https://zh.moegirl.org.cn/x" in result["content"]
    assert "虚拟歌手" in result["content"]


async def test_strips_query_whitespace(plugin: Any) -> None:
    """查询词首尾空白要被去掉。"""
    pipeline = FakePipeline(_outcome())
    plugin._pipeline = pipeline
    await plugin.handle_web_search(query="  初音未来  ", stream_id="s")
    assert pipeline.queries == ["初音未来"]


async def test_empty_query_returns_readable_message(plugin: Any) -> None:
    """空查询不发起网络请求。"""
    plugin._pipeline = FakePipeline(_outcome())
    result = await plugin.handle_web_search(query="   ", stream_id="s")
    assert result["success"] is False
    assert "关键词" in result["content"]


async def test_no_hits_explains_what_happened(plugin: Any) -> None:
    """没搜到也要说清尝试过哪些源——这是"绝不静默失败"的核心。"""
    plugin._pipeline = FakePipeline(
        _outcome(
            hits=[],
            engine_status={"bing": "失败 未解析到结果", "duckduckgo": "ok:0"},
        )
    )
    result = await plugin.handle_web_search(query="冷门词", stream_id="s")
    assert result["success"] is False
    assert "没有搜到" in result["content"]
    assert "bing" in result["content"]
    assert "建议" in result["content"]


async def test_all_engines_failed_suggests_proxy(plugin: Any) -> None:
    """全部源都失败时，提示指向网络/代理配置。"""
    plugin._pipeline = FakePipeline(
        _outcome(hits=[], engine_status={"bing": "失败 NetworkError", "duckduckgo": "失败 NetworkError"})
    )
    result = await plugin.handle_web_search(query="x", stream_id="s")
    assert "网络" in result["content"] or "代理" in result["content"]


async def test_network_error_is_converted_to_readable_text(plugin: Any) -> None:
    """异常不能泄漏给宿主变成难懂堆栈。"""
    plugin._pipeline = FakePipeline(error=NetworkError("ConnectError"))
    result = await plugin.handle_web_search(query="初音未来", stream_id="s")
    assert result["success"] is False
    assert "网络" in result["content"]
    assert "Traceback" not in result["content"]


async def test_unexpected_error_is_still_handled(plugin: Any) -> None:
    """哪怕是没预料到的异常，也要转成可读文案。"""
    plugin._pipeline = FakePipeline(error=RuntimeError("意外炸了"))
    result = await plugin.handle_web_search(query="x", stream_id="s")
    assert result["success"] is False
    assert result["content"]


async def test_pipeline_not_ready_returns_message(plugin: Any) -> None:
    """组件未就绪时给出可读提示（此时未调用 on_load）。"""
    assert plugin._pipeline is None
    result = await plugin.handle_web_search(query="x", stream_id="s")
    assert result["success"] is False
    assert "未就绪" in result["content"]


async def test_disabled_plugin_short_circuits(plugin: Any) -> None:
    """插件被禁用时不应发起请求。"""
    config = plugin.get_default_config()
    config["plugin"]["enabled"] = False
    plugin.set_plugin_config(config)
    pipeline = FakePipeline(_outcome())
    plugin._pipeline = pipeline

    result = await plugin.handle_web_search(query="x", stream_id="s")
    assert result["success"] is False
    assert pipeline.queries == []


async def test_long_output_is_truncated(plugin: Any) -> None:
    """超长结果必须截断，避免把上下文挤爆。"""
    hits = [
        SearchHit(title=f"标题 {index}", url=f"https://example.com/{index}", snippet="很长的摘要 " * 40, rank=index + 1)
        for index in range(30)
    ]
    plugin._pipeline = FakePipeline(_outcome(hits=hits))
    result = await plugin.handle_web_search(query="x", stream_id="s")
    assert len(result["content"]) <= plugin.config.llm.max_prompt_chars + 40


@pytest.mark.parametrize("field", ["content_items"])
async def test_tool_result_only_contains_text_for_phase_one(plugin: Any, field: str) -> None:
    """P1 的 web_search 是纯文本结果；图片走 content_items 的只有图片通路。"""
    plugin._pipeline = FakePipeline(_outcome())
    result = await plugin.handle_web_search(query="x", stream_id="s")
    assert field not in result
