"""``image_search`` 工具的离线测试。

关键契约：图片由插件**直接发送**到聊天流（模型无法自己贴图），
所以"发送是否成功"决定工具成功与否；失败必须给出可读原因。
"""

from __future__ import annotations

from typing import Any

from mai_websearch_under_test.core.errors import NetworkError
from mai_websearch_under_test.images.pipeline import ImageSearchOutcome
from mai_websearch_under_test.images.types import DownloadedImage


class FakeImagePipeline:
    """记录调用参数并返回预设结果。"""

    def __init__(self, outcome: ImageSearchOutcome | None = None, error: Exception | None = None) -> None:
        self.outcome = outcome
        self.error = error
        self.queries: list[str] = []
        self.limits: list[int] = []

    async def search(self, query: str, *, limit: int = 1) -> ImageSearchOutcome:
        self.queries.append(query)
        self.limits.append(limit)
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome


def _image(**overrides: Any) -> DownloadedImage:
    data: dict[str, Any] = {
        "content": b"fake-image-bytes",
        "mime_type": "image/png",
        "source_url": "https://img.example.com/cat.png",
        "engine": "bing_images",
        "title": "一只布偶猫",
        "page_url": "https://page.example.com/cat",
    }
    data.update(overrides)
    return DownloadedImage(**data)


def _outcome(**overrides: Any) -> ImageSearchOutcome:
    data: dict[str, Any] = {
        "query": "布偶猫",
        "status": "ok",
        "images": [_image()],
        "engine_status": {"bing_images": "ok:1"},
        "candidates_seen": 1,
        "elapsed_ms": 500,
    }
    data.update(overrides)
    return ImageSearchOutcome(**data)


# ------------------------------------------------------------------ 成功路径


async def test_sends_image_and_reports_what_was_sent(plugin: Any) -> None:
    """找到图片就必须真的发出去。"""
    plugin._image_pipeline = FakeImagePipeline(_outcome())
    result = await plugin.handle_image_search(query="布偶猫", stream_id="s1")

    assert result["success"] is True
    assert len(plugin.ctx.send.images) == 1
    _, stream_id = plugin.ctx.send.images[0]
    assert stream_id == "s1"
    assert "布偶猫" in result["content"]
    assert "https://page.example.com/cat" in result["content"]


async def test_passes_configured_limit(plugin: Any) -> None:
    """每次发几张由配置决定。"""
    config = plugin.get_default_config()
    config["images"]["max_images_per_call"] = 3
    plugin.set_plugin_config(config)
    pipeline = FakeImagePipeline(_outcome())
    plugin._image_pipeline = pipeline

    await plugin.handle_image_search(query="猫", stream_id="s1")
    assert pipeline.limits == [3]


async def test_strips_query(plugin: Any) -> None:
    pipeline = FakeImagePipeline(_outcome())
    plugin._image_pipeline = pipeline
    await plugin.handle_image_search(query="  布偶猫  ", stream_id="s1")
    assert pipeline.queries == ["布偶猫"]


# ------------------------------------------------------------------ 发送失败


async def test_without_stream_id_reports_send_failure(plugin: Any) -> None:
    """没有 stream_id 时无法发送，必须如实说明而不是谎报成功。"""
    plugin._image_pipeline = FakeImagePipeline(_outcome())
    result = await plugin.handle_image_search(query="猫", stream_id="")

    assert result["success"] is False
    assert plugin.ctx.send.images == []
    assert "没能发送" in result["content"]


async def test_send_returning_false_is_failure(plugin: Any) -> None:
    """宿主返回 False 时按失败处理。"""
    plugin.ctx.send.image_result = False
    plugin._image_pipeline = FakeImagePipeline(_outcome())
    result = await plugin.handle_image_search(query="猫", stream_id="s1")

    assert result["success"] is False
    assert "没能发送" in result["content"]


async def test_send_exception_does_not_propagate(plugin: Any) -> None:
    """发送抛异常也不能泄漏给宿主。"""

    async def boom(image_data: str, stream_id: str, **kwargs: Any) -> bool:
        raise RuntimeError("适配器炸了")

    plugin.ctx.send.image = boom
    plugin._image_pipeline = FakeImagePipeline(_outcome())
    result = await plugin.handle_image_search(query="猫", stream_id="s1")

    assert result["success"] is False
    assert "没能发送" in result["content"]


# ------------------------------------------------------------------ 失败状态


async def test_no_results_message(plugin: Any) -> None:
    plugin._image_pipeline = FakeImagePipeline(_outcome(images=[], status="no_results"))
    result = await plugin.handle_image_search(query="不存在的图", stream_id="s1")

    assert result["success"] is False
    assert "没有找到" in result["content"]


async def test_no_unique_message(plugin: Any) -> None:
    """重复窗口命中时要说清原因，而不是笼统报错。"""
    plugin._image_pipeline = FakeImagePipeline(_outcome(images=[], status="no_unique", skipped_repeats=2))
    result = await plugin.handle_image_search(query="猫", stream_id="s1")

    assert result["success"] is False
    assert "发过了" in result["content"]


async def test_all_failed_mentions_engines(plugin: Any) -> None:
    plugin._image_pipeline = FakeImagePipeline(
        _outcome(images=[], status="all_failed", engine_status={"bing_images": "失败 被反爬"})
    )
    result = await plugin.handle_image_search(query="猫", stream_id="s1")

    assert result["success"] is False
    assert "bing_images" in result["content"]


async def test_download_failure_reason_is_surfaced(plugin: Any) -> None:
    """下载失败的具体原因要能看到，否则用户无从排查。"""
    plugin._image_pipeline = FakeImagePipeline(
        _outcome(images=[], status="all_failed", failures=["图片超过 3145728 字节"])
    )
    result = await plugin.handle_image_search(query="猫", stream_id="s1")
    assert "3145728" in result["content"]


# ------------------------------------------------------------------ 边界


async def test_empty_query_short_circuits(plugin: Any) -> None:
    pipeline = FakeImagePipeline(_outcome())
    plugin._image_pipeline = pipeline
    result = await plugin.handle_image_search(query="   ", stream_id="s1")

    assert result["success"] is False
    assert pipeline.queries == []


async def test_disabled_plugin_short_circuits(plugin: Any) -> None:
    config = plugin.get_default_config()
    config["plugin"]["enabled"] = False
    plugin.set_plugin_config(config)
    pipeline = FakeImagePipeline(_outcome())
    plugin._image_pipeline = pipeline

    result = await plugin.handle_image_search(query="猫", stream_id="s1")
    assert result["success"] is False
    assert pipeline.queries == []


async def test_disabled_images_section_short_circuits(plugin: Any) -> None:
    config = plugin.get_default_config()
    config["images"]["enabled"] = False
    plugin.set_plugin_config(config)
    pipeline = FakeImagePipeline(_outcome())
    plugin._image_pipeline = pipeline

    result = await plugin.handle_image_search(query="猫", stream_id="s1")
    assert result["success"] is False
    assert pipeline.queries == []


async def test_pipeline_not_ready(plugin: Any) -> None:
    assert plugin._image_pipeline is None
    result = await plugin.handle_image_search(query="猫", stream_id="s1")
    assert result["success"] is False
    assert "未就绪" in result["content"]


async def test_pipeline_exception_becomes_readable(plugin: Any) -> None:
    plugin._image_pipeline = FakeImagePipeline(error=NetworkError("ConnectTimeout"))
    result = await plugin.handle_image_search(query="猫", stream_id="s1")

    assert result["success"] is False
    assert "网络" in result["content"]
    assert "Traceback" not in result["content"]


# ------------------------------------------------------------------ 观察模式


async def test_preview_off_by_default(plugin: Any) -> None:
    """默认不把图片回传模型：base64 会显著增加载荷。"""
    plugin._image_pipeline = FakeImagePipeline(_outcome())
    result = await plugin.handle_image_search(query="猫", stream_id="s1")
    assert "content_items" not in result


async def test_preview_uses_official_content_items_channel(plugin: Any) -> None:
    """打开后走官方的 content_items 通道，绝不自己拼多模态 prompt。"""
    config = plugin.get_default_config()
    config["images"]["preview_to_model"] = True
    plugin.set_plugin_config(config)
    plugin._image_pipeline = FakeImagePipeline(_outcome())

    result = await plugin.handle_image_search(query="猫", stream_id="s1")

    items = result["content_items"]
    assert len(items) == 1
    assert items[0]["type"] == "image"
    assert items[0]["mime_type"] == "image/png"
    assert items[0]["data"]


async def test_preview_does_not_add_items_when_nothing_found(plugin: Any) -> None:
    config = plugin.get_default_config()
    config["images"]["preview_to_model"] = True
    plugin.set_plugin_config(config)
    plugin._image_pipeline = FakeImagePipeline(_outcome(images=[], status="no_results"))

    result = await plugin.handle_image_search(query="猫", stream_id="s1")
    assert "content_items" not in result
