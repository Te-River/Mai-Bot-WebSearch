"""入站图片获取测试。

段结构来自宿主源码（``plugin_runtime/host/message_utils.py``）::

    {"type": "image", "data": <平台URL>, "hash": <sha256>, "binary_data_base64": <b64>}

两级回退都必须覆盖：调用参数里的段（零额外 RPC）优先，
其次 ``ctx.message.get_by_id(include_binary_data=True)``，最后才自己下载 URL。
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from mai_websearch_under_test.core.errors import NetworkError, SearchError
from mai_websearch_under_test.core.http import FetchBytesResult
from mai_websearch_under_test.images.inbound import (
    InboundImage,
    acquire_inbound_image,
    image_from_message,
    image_segments,
    newest_image_message,
)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
PNG_B64 = base64.b64encode(PNG).decode("ascii")


def _message(*segments: dict[str, Any], message_id: str = "m1") -> dict[str, Any]:
    return {"message_id": message_id, "raw_message": list(segments)}


class FakeCtx:
    """只实现 ``message.get_by_id`` 与 ``message.get_recent``。"""

    def __init__(
        self,
        detail: Any = None,
        error: Exception | None = None,
        recent: Any = None,
        recent_error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.recent_calls: list[dict[str, Any]] = []
        self._detail = detail
        self._error = error
        self._recent = recent
        self._recent_error = recent_error
        self.message = self

    async def get_by_id(self, message_id: str, **kwargs: Any) -> Any:
        self.calls.append({"message_id": message_id, **kwargs})
        if self._error is not None:
            raise self._error
        return self._detail

    async def get_recent(self, chat_id: str, limit: int = 10) -> Any:
        self.recent_calls.append({"chat_id": chat_id, "limit": limit})
        if self._recent_error is not None:
            raise self._recent_error
        if self._recent is None:
            return {"success": True, "messages": []}
        return self._recent


class FakeBytesHttp:
    def __init__(self, content: bytes = PNG, content_type: str = "image/png", truncated: bool = False) -> None:
        self.calls: list[str] = []
        self._content = content
        self._content_type = content_type
        self._truncated = truncated

    async def fetch_bytes(self, url: str, **kwargs: Any) -> FetchBytesResult:
        del kwargs
        self.calls.append(url)
        return FetchBytesResult(
            status=200,
            url=url,
            content_type=self._content_type,
            content=self._content,
            truncated=self._truncated,
            elapsed_ms=1,
        )


# ------------------------------------------------------------------ 段解析


class TestImageSegments:
    def test_image_segments_filters_by_type(self) -> None:
        message = _message({"type": "text", "data": "你好"}, {"type": "image", "data": "u"})
        assert len(image_segments(message)) == 1

    def test_tolerates_missing_raw_message(self) -> None:
        assert image_segments({}) == []
        assert image_segments(None) == []

    def test_returns_empty_on_non_mapping(self) -> None:
        assert image_segments("nope") == []  # type: ignore[arg-type]


class TestImageFromMessage:
    def test_reads_base64(self) -> None:
        image = image_from_message(_message({"type": "image", "data": "https://x/1.png", "binary_data_base64": PNG_B64}))
        assert image is not None
        assert image.content == PNG
        assert image.url == "https://x/1.png"

    def test_reads_url_only(self) -> None:
        """只有 URL 时也算拿到了候选，稍后再下载。"""
        image = image_from_message(_message({"type": "image", "data": "https://x/1.png"}))
        assert image is not None
        assert image.has_bytes is False
        assert image.url == "https://x/1.png"

    def test_picks_last_image_when_message_has_several(self) -> None:
        """一条消息里有多张图时取**最后一张**（用户刚发的那张）。"""
        message = _message(
            {"type": "text", "data": "看这个"},
            {"type": "image", "data": "https://x/first.png"},
            {"type": "image", "data": "https://x/second.png"},
        )
        image = image_from_message(message)
        assert image is not None
        assert image.url == "https://x/second.png"

    def test_no_image_returns_none(self) -> None:
        assert image_from_message(_message({"type": "text", "data": "hi"})) is None

    def test_invalid_base64_falls_back_to_url(self) -> None:
        """坏 base64 不应让整条段作废。"""
        image = image_from_message(_message({"type": "image", "data": "https://x/1.png", "binary_data_base64": "!!!"}))
        assert image is not None
        assert image.has_bytes is False
        assert image.url == "https://x/1.png"

    def test_keeps_hash(self) -> None:
        image = image_from_message(_message({"type": "image", "data": "u", "hash": "abc123"}))
        assert image is not None
        assert image.sha256 == "abc123"

    def test_sniffs_mime_from_bytes(self) -> None:
        """段里没有 mime 字段，必须自己嗅探，否则会被当成默认的 jpeg 交给宿主。"""
        image = image_from_message(_message({"type": "image", "data": "u", "binary_data_base64": PNG_B64}))
        assert image is not None
        assert image.mime_type == "image/png"


# ------------------------------------------------------------------ 三级回退


class TestAcquireInboundImage:
    async def test_prefers_bytes_from_message(self) -> None:
        """第一级命中时不应触碰 ctx 或网络。"""
        ctx = FakeCtx()
        http = FakeBytesHttp()
        image = await acquire_inbound_image(
            message=_message({"type": "image", "data": "https://x/1.png", "binary_data_base64": PNG_B64}),
            ctx=ctx,
            http=http,
        )
        assert image.content == PNG
        assert ctx.calls == []
        assert http.calls == []

    async def test_downloads_when_only_url(self) -> None:
        """只有 URL 时自己下载（平台 URL 有时效）。"""
        http = FakeBytesHttp()
        image = await acquire_inbound_image(
            message=_message({"type": "image", "data": "https://x/1.png"}),
            http=http,
        )
        assert image.content == PNG
        assert http.calls == ["https://x/1.png"]

    async def test_falls_back_to_message_lookup(self) -> None:
        """消息里没带图片时，用 message_id 向宿主索取二进制。"""
        ctx = FakeCtx(detail=_message({"type": "image", "data": "u", "binary_data_base64": PNG_B64}))
        image = await acquire_inbound_image(
            message=_message({"type": "text", "data": "hi"}, message_id="m9"),
            ctx=ctx,
        )
        assert image.content == PNG
        assert ctx.calls[0]["message_id"] == "m9"
        assert ctx.calls[0]["include_binary_data"] is True, "必须显式索取二进制"

    async def test_lookup_failure_does_not_abort(self) -> None:
        """第二级失败要能继续退到下载，而不是直接抛错。"""
        ctx = FakeCtx(error=RuntimeError("宿主不支持"))
        http = FakeBytesHttp()
        image = await acquire_inbound_image(
            message=_message({"type": "image", "data": "https://x/1.png"}),
            ctx=ctx,
            http=http,
        )
        assert image.content == PNG

    async def test_no_image_anywhere_raises(self) -> None:
        with pytest.raises(SearchError) as info:
            await acquire_inbound_image(message=_message({"type": "text", "data": "hi"}), ctx=FakeCtx())
        assert "没找到你发的图" in info.value.user_message

    async def test_download_rejects_non_image(self) -> None:
        """平台 URL 有时会返回 HTML 错误页，不能当图片用。"""
        http = FakeBytesHttp(content=b"<html>403</html>", content_type="text/html")
        with pytest.raises(SearchError):
            await acquire_inbound_image(message=_message({"type": "image", "data": "https://x/1.png"}), http=http)

    async def test_download_rejects_oversized(self) -> None:
        http = FakeBytesHttp(truncated=True)
        with pytest.raises(SearchError):
            await acquire_inbound_image(message=_message({"type": "image", "data": "https://x/1.png"}), http=http)

    async def test_download_error_propagates_as_readable(self) -> None:
        class FailingHttp:
            async def fetch_bytes(self, url: str, **kwargs: Any) -> FetchBytesResult:
                raise NetworkError("ConnectError")

        with pytest.raises(NetworkError):
            await acquire_inbound_image(
                message=_message({"type": "image", "data": "https://x/1.png"}),
                http=FailingHttp(),
            )


# ------------------------------------------------------------------ 回溯最近消息


def _recent(*messages: dict[str, Any], success: bool = True) -> dict[str, Any]:
    return {"success": success, "messages": list(messages)}


def _image_message(message_id: str, timestamp: str, *, url: str = "https://x/1.png", with_bytes: bool = False) -> dict[str, Any]:
    segment: dict[str, Any] = {"type": "image", "data": url, "hash": "h-" + message_id}
    if with_bytes:
        segment["binary_data_base64"] = PNG_B64
    return {
        "message_id": message_id,
        "timestamp": timestamp,
        "raw_message": [{"type": "text", "data": "看这个"}, segment],
    }


class TestRecentFallback:
    """**真机踩到的核心场景**：用户先发图、再发文字问「这是什么」。

    planner 回的是**后一条纯文本消息**，所以"当前消息"里根本没有图片段，
    必须回溯最近消息才拿得到。
    """

    async def test_finds_image_in_previous_message(self) -> None:
        ctx = FakeCtx(
            recent=_recent(
                _image_message("mznbdsd", "17:52:34"),  # 17:52:34 发图
                {"message_id": "m3veuq4", "timestamp": "17:52:37", "raw_message": [{"type": "text", "data": "这是什么啊"}]},
            )
        )
        current = {"message_id": "m3veuq4", "raw_message": [{"type": "text", "data": "这是什么啊"}]}
        image = await acquire_inbound_image(message=current, ctx=ctx, chat_id="s1")

        assert image.url == "https://x/1.png"
        assert image.source == "message.get_recent"
        assert ctx.recent_calls[0]["chat_id"] == "s1"

    async def test_works_without_any_message_kwarg(self) -> None:
        """@Tool 的载荷里可能压根没有 message（只有 stream_id / chat_id）。"""
        ctx = FakeCtx(recent=_recent(_image_message("m1", "17:52:34", with_bytes=True)))
        image = await acquire_inbound_image(ctx=ctx, chat_id="s1")

        assert image.content == PNG
        assert image.mime_type == "image/png"

    async def test_prefers_newest_image(self) -> None:
        ctx = FakeCtx(
            recent=_recent(
                _image_message("old", "17:00:00", url="https://x/old.png", with_bytes=True),
                _image_message("new", "17:52:34", url="https://x/new.png"),
            ),
            detail=_image_message("new", "17:52:34", url="https://x/new.png", with_bytes=True),
        )
        image = await acquire_inbound_image(ctx=ctx, chat_id="s1", message_id="")

        assert image.url == "https://x/new.png", "要最新的那张，不是最早那张"

    async def test_fetches_bytes_for_recent_candidate(self) -> None:
        """get_recent 不带二进制（SDK 签名如此），要靠 get_by_id 补字节。"""
        ctx = FakeCtx(
            recent=_recent(_image_message("m1", "17:52:34")),
            detail=_image_message("m1", "17:52:34", with_bytes=True),
        )
        image = await acquire_inbound_image(ctx=ctx, chat_id="s1")

        assert image.content == PNG
        assert ctx.calls, "应该按 id 再取一次以拿到字节"
        assert ctx.calls[0]["include_binary_data"] is True

    async def test_prefers_nearer_source_over_older_one(self) -> None:
        """就近优先：当前消息有图时，不能拿更久之前那张来替代。"""
        current = {"message_id": "cur", "raw_message": [{"type": "image", "data": "https://x/current.png"}]}
        ctx = FakeCtx(recent=_recent(_image_message("old", "17:00:00", url="https://x/old.png", with_bytes=True)))
        image = await acquire_inbound_image(message=current, ctx=ctx, chat_id="s1", http=FakeBytesHttp())

        assert image.url == "https://x/current.png"

    async def test_downloads_when_only_url_available(self) -> None:
        """拿不到字节时退回下载平台 URL。"""
        ctx = FakeCtx(recent=_recent(_image_message("m1", "17:52:34")))
        http = FakeBytesHttp()
        image = await acquire_inbound_image(ctx=ctx, chat_id="s1", http=http)

        assert image.content == PNG
        assert http.calls == ["https://x/1.png"]

    async def test_explicit_message_id_wins(self) -> None:
        """模型明确给出 msg_id 时直接用它。"""
        ctx = FakeCtx(
            detail=_image_message("mznbdsd", "17:52:34", url="https://x/explicit.png", with_bytes=True),
            recent=_recent(_image_message("other", "17:00:00", url="https://x/other.png", with_bytes=True)),
        )
        image = await acquire_inbound_image(ctx=ctx, chat_id="s1", message_id="mznbdsd")

        assert image.url == "https://x/explicit.png"

    async def test_recent_failure_is_not_fatal(self) -> None:
        """回溯失败不应让工具崩掉。"""
        ctx = FakeCtx(recent_error=RuntimeError("宿主不支持"))
        with pytest.raises(SearchError):
            await acquire_inbound_image(ctx=ctx, chat_id="s1")

    async def test_unsuccessful_payload_is_ignored(self) -> None:
        ctx = FakeCtx(recent={"success": False, "error": "缺少必要参数 chat_id"})
        with pytest.raises(SearchError):
            await acquire_inbound_image(ctx=ctx, chat_id="s1")


class TestNewestImageMessage:
    def test_picks_by_timestamp_not_position(self) -> None:
        """不依赖列表顺序。"""
        payload = [
            _image_message("b", "200"),
            _image_message("a", "100"),
        ]
        chosen = newest_image_message(payload)
        assert chosen is not None
        assert chosen["message_id"] == "b"

    def test_returns_none_without_images(self) -> None:
        assert newest_image_message([{"message_id": "x", "raw_message": [{"type": "text", "data": "hi"}]}]) is None

    def test_tolerates_garbage(self) -> None:
        assert newest_image_message(None) is None
        assert newest_image_message("not a list") is None


# ------------------------------------------------------------------ 输出形态


class TestInboundImageOutput:
    def test_content_item_shape(self) -> None:
        """必须符合官方 content_items 约定（宿主据此把它拆成一条 user 图片消息）。"""
        image = InboundImage(source="message", content=PNG, mime_type="image/png")
        item = image.to_content_item()
        assert item["type"] == "image"
        assert item["mime_type"] == "image/png"
        assert base64.b64decode(item["data"]) == PNG

    def test_content_item_defaults_mime(self) -> None:
        """类型未知时给一个保守默认值，避免宿主拿到空 mime。"""
        item = InboundImage(source="message", content=PNG).to_content_item()
        assert item["mime_type"] == "image/jpeg"
