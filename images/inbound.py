"""入站图片获取（图搜图 / 图搜文的第一步）。

宿主把 ``ImageComponent`` 序列化成（源码 ``plugin_runtime/host/message_utils.py``）::

    {"type": "image", "data": <平台URL>, "hash": <sha256>, "binary_data_base64": <b64>}

因此有两级回退（都在本模块内）：

1. 工具调用 ``kwargs["message"]["raw_message"]`` 里直接带的图片段——零额外 RPC，最快；
2. ``ctx.message.get_by_id(..., include_binary_data=True)`` 显式索取二进制。

拿到 URL 但没有字节时再自行下载一次（平台 URL 有时效，且不一定公网可达）。

**刻意不做 ``Images`` 表兜底**：那张表里的 ``full_path`` 是"项目内相对路径"，
插件拿不到宿主项目根目录，按它拼路径既脆弱又可能读错文件——不如明确失败。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.errors import SearchError
from .types import IMAGE_MIME_TYPES, normalize_mime, sniff_image_mime

__all__ = ["InboundImage", "acquire_inbound_image", "image_from_message", "image_segments"]

# 入站图片也可能很大（用户随手发的高清图），下载时按这个上限截断
DEFAULT_INBOUND_MAX_BYTES = 3145728


@dataclass(slots=True)
class InboundImage:
    """从消息里拿到的用户图片。"""

    source: str
    url: str = ""
    content: bytes = b""
    mime_type: str = ""
    sha256: str = ""

    @property
    def has_bytes(self) -> bool:
        """是否已经拿到二进制。"""
        return bool(self.content)

    def to_base64(self) -> str:
        """转成 base64（``send.image`` 与 ``content_items`` 都需要）。"""
        import base64

        return base64.b64encode(self.content).decode("ascii")

    def to_content_item(self) -> dict[str, str]:
        """转成官方 ``content_items`` 条目，让宿主按模型能力决定是否喂给模型。"""
        return {
            "type": "image",
            "data": self.to_base64(),
            "mime_type": self.mime_type or "image/jpeg",
            "name": "user-image",
            "description": "用户发来的图片",
        }


def image_segments(message: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """取出消息里的图片段（保持原顺序）。"""
    if not isinstance(message, Mapping):
        return []
    raw = message.get("raw_message")
    if not isinstance(raw, Sequence):
        return []
    return [segment for segment in raw if isinstance(segment, Mapping) and segment.get("type") == "image"]


def image_from_message(message: Mapping[str, Any] | None, *, source: str = "message") -> InboundImage | None:
    """从消息字典里取出第一张图片（可能是 URL，也可能是字节）。"""
    for segment in image_segments(message):
        content = segment.get("binary_data_base64")
        url = str(segment.get("data") or "")
        result = InboundImage(source=source, url=url, sha256=str(segment.get("hash") or ""))
        if isinstance(content, str) and content:
            import base64

            try:
                result.content = base64.b64decode(content, validate=True)
            except (ValueError, TypeError):
                result.content = b""
        if result.content or result.url:
            # 段里没有 mime 字段，但我们手上有字节 —— 嗅探出来，
            # 否则下游会把它当成默认的 image/jpeg 交给宿主或模型。
            if result.content and not result.mime_type:
                result.mime_type = sniff_image_mime(result.content) or ""
            return result
    return None


async def _download(url: str, *, http: Any, max_bytes: int, timeout_seconds: float) -> tuple[bytes, str]:
    """下载平台图片并校验它确实是图片。"""
    response = await http.fetch_bytes(url, max_bytes=max_bytes, timeout_seconds=timeout_seconds, breaker_key="inbound")
    mime = normalize_mime(response.content_type)
    if mime not in IMAGE_MIME_TYPES:
        mime = sniff_image_mime(response.content) or ""
    if not mime:
        raise SearchError(f"拿到的不是图片（{response.content_type or '未知类型'}）")
    if response.truncated:
        raise SearchError(f"图片超过 {max_bytes} 字节")
    return response.content, mime


async def acquire_inbound_image(
    *,
    message: Mapping[str, Any] | None = None,
    ctx: Any = None,
    stream_id: str = "",
    http: Any = None,
    max_bytes: int = DEFAULT_INBOUND_MAX_BYTES,
    timeout_seconds: float = 8.0,
) -> InboundImage:
    """取得用户图片的字节。

    顺序：调用参数里的消息段 → ``ctx.message.get_by_id`` → 用平台 URL 自行下载。

    Raises:
        SearchError: 消息里没有图片，或所有途径都拿不到字节。
    """
    candidate = image_from_message(message)
    if candidate is not None and candidate.has_bytes:
        return candidate

    message_id = ""
    if isinstance(message, Mapping):
        message_id = str(message.get("message_id") or "")

    if ctx is not None and message_id:
        try:
            detail = await ctx.message.get_by_id(  # type: ignore[attr-defined]
                message_id,
                stream_id=stream_id,
                include_binary_data=True,
            )
        except Exception:  # noqa: BLE001 - 这一级拿不到就退到下载，不要中断整个工具
            detail = None
        fetched = image_from_message(detail, source="message.get_by_id")
        if fetched is not None and fetched.has_bytes:
            return fetched
        if candidate is None and fetched is not None:
            candidate = fetched

    if candidate is not None and candidate.url and http is not None:
        content, mime = await _download(
            candidate.url,
            http=http,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
        candidate.content = content
        candidate.mime_type = mime
        return candidate

    raise SearchError(
        "这条消息里没有可用的图片",
        user_message="我没拿到你发的那张图，可以再发一次吗？",
    )
