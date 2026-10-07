"""``image_lookup`` 工具的离线测试。

这个工具的定位很明确：**不假装自己有反查能力**。
实测多数网络下反查引擎不可用，所以它的主要职责是——
拿到用户图片、在可用时补上来源信息、并把"要找相似图请用 image_search"讲清楚。
"""

from __future__ import annotations

import base64
from typing import Any

from mai_websearch_under_test.core.errors import ProviderError
from mai_websearch_under_test.images.inbound import InboundImage
from mai_websearch_under_test.images.reverse import ReverseLookupResult, ReverseSource
from mai_websearch_under_test.tools.image_lookup import render_lookup, run_reverse_lookup

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
PNG_B64 = base64.b64encode(PNG).decode("ascii")


def _message_with_image() -> dict[str, Any]:
    return {
        "message_id": "m1",
        "raw_message": [{"type": "image", "data": "https://x/1.png", "binary_data_base64": PNG_B64}],
    }


class FakeProvider:
    """假反查引擎。"""

    requires_key = True

    def __init__(self, name: str, *, sources: tuple[ReverseSource, ...] = (), error: Exception | None = None) -> None:
        self.name = name
        self._sources = list(sources)
        self._error = error
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def lookup(self, image: Any, http: Any) -> ReverseLookupResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return ReverseLookupResult(engine=self.name, sources=list(self._sources))


# ------------------------------------------------------------------ 编排


class TestRunReverseLookup:
    async def test_no_providers_returns_empty(self) -> None:
        assert await run_reverse_lookup([], InboundImage(source="t", content=PNG), None) == []

    async def test_orders_by_declaration(self) -> None:
        """完成顺序不确定，输出顺序必须稳定。"""
        slow = FakeProvider("slow", sources=(ReverseSource(title="S"),))
        fast = FakeProvider("fast", sources=(ReverseSource(title="F"),))
        results = await run_reverse_lookup([slow, fast], InboundImage(source="t", content=PNG), None)
        assert [result.engine for result in results] == ["slow", "fast"]

    async def test_provider_exception_becomes_visible_error(self) -> None:
        """单个引擎失败只记录，不影响其它引擎，也不抛给宿主。"""
        broken = FakeProvider("broken", error=ProviderError("broken", "未配置 API Key"))
        good = FakeProvider("good", sources=(ReverseSource(title="X", similarity=90.0),))
        results = await run_reverse_lookup([broken, good], InboundImage(source="t", content=PNG), None)

        assert results[0].ok is False
        assert "API Key" in results[0].error
        assert results[1].ok is True


# ------------------------------------------------------------------ 渲染


class TestRenderLookup:
    def _image(self) -> InboundImage:
        return InboundImage(source="message", content=PNG, mime_type="image/png")

    def test_reports_no_engine_available(self) -> None:
        """没有引擎时必须说清楚，并给出下一步，而不是沉默。"""
        text = render_lookup(image=self._image(), results=[], previewed=False)
        assert "没有可用的以图搜源引擎" in text
        assert "image_search" in text

    def test_renders_sources(self) -> None:
        result = ReverseLookupResult(
            engine="saucenao",
            sources=[ReverseSource(title="初音ミク", author="画师A", similarity=92.2, index="Pixiv",
                                   urls=["https://www.pixiv.net/artworks/123"])],
        )
        text = render_lookup(image=self._image(), results=[result], previewed=False)
        assert "92.2%" in text
        assert "初音ミク" in text
        assert "画师A" in text
        assert "https://www.pixiv.net/artworks/123" in text

    def test_renders_engine_error(self) -> None:
        result = ReverseLookupResult(engine="saucenao", error="超出配额")
        text = render_lookup(image=self._image(), results=[result], previewed=False)
        assert "超出配额" in text

    def test_guides_to_image_search(self) -> None:
        """必须明确告诉模型"找相似图用 image_search"，避免它反复调用本工具。"""
        text = render_lookup(image=self._image(), results=[], previewed=True)
        assert "image_search" in text
        assert "不要重复调用" in text

    def test_mentions_preview_state(self) -> None:
        assert "交给你观察" in render_lookup(image=self._image(), results=[], previewed=True)
        assert "preview_to_model" in render_lookup(image=self._image(), results=[], previewed=False)


# ------------------------------------------------------------------ 工具处理函数


class TestHandleImageLookup:
    async def test_returns_guidance_without_engines(self, plugin: Any) -> None:
        """没有配置反查引擎时，仍然要把图片链路走通并给出指引。"""
        plugin._http = object()
        result = await plugin.handle_image_lookup(message=_message_with_image(), stream_id="s1")

        assert result["success"] is True
        assert "image_search" in result["content"]
        assert "content_items" not in result, "默认不回传图片（模型通常已经看到）"

    async def test_preview_adds_content_item(self, plugin: Any) -> None:
        config = plugin.get_default_config()
        config["reverse"]["preview_to_model"] = True
        plugin.set_plugin_config(config)
        plugin._http = object()

        result = await plugin.handle_image_lookup(message=_message_with_image(), stream_id="s1")

        assert len(result["content_items"]) == 1
        assert result["content_items"][0]["type"] == "image"
        assert result["content_items"][0]["mime_type"] == "image/png"

    async def test_includes_reverse_results(self, plugin: Any) -> None:
        plugin._http = object()
        plugin._reverse_providers = [
            FakeProvider("saucenao", sources=(ReverseSource(title="初音ミク", similarity=95.0),))
        ]
        result = await plugin.handle_image_lookup(message=_message_with_image(), stream_id="s1")

        assert "初音ミク" in result["content"]
        assert plugin._reverse_providers[0].calls == 1

    async def test_reverse_failure_does_not_fail_tool(self, plugin: Any) -> None:
        """反查失败不能拖垮"把图交给模型"这条主线。"""
        plugin._http = object()
        plugin._reverse_providers = [FakeProvider("saucenao", error=RuntimeError("炸了"))]
        result = await plugin.handle_image_lookup(message=_message_with_image(), stream_id="s1")

        assert result["success"] is True
        assert "失败" in result["content"]

    async def test_missing_image_reports_readably(self, plugin: Any) -> None:
        plugin._http = object()
        result = await plugin.handle_image_lookup(
            message={"message_id": "m1", "raw_message": [{"type": "text", "data": "你好"}]},
            stream_id="s1",
        )
        assert result["success"] is False
        assert "再发一次" in result["content"]

    async def test_disabled_plugin_short_circuits(self, plugin: Any) -> None:
        config = plugin.get_default_config()
        config["plugin"]["enabled"] = False
        plugin.set_plugin_config(config)
        result = await plugin.handle_image_lookup(message=_message_with_image(), stream_id="s1")
        assert result["success"] is False

    async def test_disabled_reverse_section_short_circuits(self, plugin: Any) -> None:
        config = plugin.get_default_config()
        config["reverse"]["enabled"] = False
        plugin.set_plugin_config(config)
        result = await plugin.handle_image_lookup(message=_message_with_image(), stream_id="s1")
        assert result["success"] is False

    async def test_acquisition_error_is_readable(self, plugin: Any) -> None:
        """拿不到图片时给出可读提示（此处：只有 URL 且没有可用的 HTTP 客户端）。"""
        plugin._http = None
        message = {"message_id": "m1", "raw_message": [{"type": "image", "data": "https://x/1.png"}]}
        result = await plugin.handle_image_lookup(message=message, stream_id="s1")

        assert result["success"] is False
        assert "再发一次" in result["content"]
        assert "Traceback" not in result["content"]
