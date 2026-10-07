"""入站图片获取（图搜图 / 图搜文的第一步）。

**这里踩过两个真机才暴露的坑，都是"消息形态假设错了"：**

1. **工具调用里没有 ``message``。** 宿主的 ``@Command`` 会注入 ``message``（含 ``raw_message``），
   但 ``@Tool`` 的载荷只有模型参数 +
   ``stream_id`` / ``chat_id`` / ``group_id`` / ``user_id`` / ``platform``
   （见 ``plugin_runtime/component_query.py::_build_tool_invocation_payload``）。
   所以"从 kwargs['message'] 里读图片"在工具里**永远拿不到东西**。

2. **图片和提问往往是两条消息。** 真机日志：用户先发图（``msg_id=mznbdsd``），
   3 秒后发"这是什么啊"（``msg_id=m3veuq4``）；planner 回的是**后一条纯文本**，
   于是"当前消息"里根本没有图片段。

因此取图顺序是：

1. 调用参数里自带的 ``message``（``@Command`` 路径，零 RPC）；
2. 模型显式指定的 ``message_id``；
3. **``message.get_recent`` 回溯最近消息，取最新的一张图**
   （SDK 签名 ``get_recent(chat_id, limit)`` 不带二进制，所以找到后可能还要按 id 取一次字节）；
4. 只有平台 URL 时自己下载（URL 有时效，且不一定公网可达）。

**刻意不做 ``Images`` 表兜底**：那张表里的 ``full_path`` 是"项目内相对路径"，
插件拿不到宿主项目根目录，按它拼路径既脆弱又可能读错文件——不如明确失败。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.errors import SearchError
from .types import IMAGE_MIME_TYPES, normalize_mime, sniff_image_mime

__all__ = ["InboundImage", "acquire_inbound_image", "image_from_message", "image_segments"]

# 入站图片也可能很大（用户随手发的高清图），下载时按这个上限截断
DEFAULT_INBOUND_MAX_BYTES = 3145728

# 回溯最近多少条消息找图。图片与提问通常紧挨着，10 条足够；
# 再大就会把很久以前发的图当成"这张图"。
DEFAULT_RECENT_LIMIT = 10


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
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    return [segment for segment in raw if isinstance(segment, Mapping) and segment.get("type") == "image"]


def _decode_binary(segment: Mapping[str, Any]) -> bytes:
    """解出段里的 base64（坏数据按"没有字节"处理，不抛）。"""
    raw = segment.get("binary_data_base64")
    if not isinstance(raw, str) or not raw:
        return b""
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return b""


def image_from_message(message: Mapping[str, Any] | None, *, source: str = "message") -> InboundImage | None:
    """从消息字典里取出**最后一张**图片（可能是 URL，也可能是字节）。"""
    segments = image_segments(message)
    for segment in reversed(segments):
        result = InboundImage(source=source, url=str(segment.get("data") or ""), sha256=str(segment.get("hash") or ""))
        result.content = _decode_binary(segment)
        if result.content or result.url:
            # 段里没有 mime 字段，但我们手上有字节 —— 嗅探出来，
            # 否则下游会把它当成默认的 image/jpeg 交给宿主或模型。
            if result.content:
                result.mime_type = sniff_image_mime(result.content) or ""
            return result
    return None


def _message_timestamp(message: Mapping[str, Any]) -> float:
    """取消息时间戳（宿主给的是字符串形式的秒）。

    解析不了时返回 ``-1.0``，让排序退化成"按列表位置，靠后更新"。
    """
    try:
        return float(message.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        return -1.0


def newest_image_message(messages: Any) -> Mapping[str, Any] | None:
    """从消息列表里挑出**最新的一条带图消息**。

    不依赖列表顺序：先按 ``timestamp`` 比较；时间戳缺失或并列时按位置取靠后的
    （宿主返回的是时间升序，"靠后"即更新）。
    """
    if not isinstance(messages, Sequence) or isinstance(messages, str):
        return None
    best: tuple[tuple[float, int], Mapping[str, Any]] | None = None
    for index, item in enumerate(messages):
        if not isinstance(item, Mapping) or not image_segments(item):
            continue
        key = (_message_timestamp(item), index)
        if best is None or key > best[0]:
            best = (key, item)
    return best[1] if best is not None else None


async def _by_id(ctx: Any, message_id: str, chat_id: str) -> Mapping[str, Any] | None:
    """按 id 取单条消息（含二进制）。失败返回 ``None``，不影响其它途径。"""
    if not message_id:
        return None
    try:
        detail = await ctx.message.get_by_id(
            message_id,
            stream_id=chat_id,
            chat_id=chat_id,
            include_binary_data=True,
        )
    except Exception:  # noqa: BLE001 - 这一级拿不到就退到下一级
        return None
    if isinstance(detail, Mapping) and detail.get("success") is False:
        return None
    return detail if isinstance(detail, Mapping) else None


async def _from_recent(ctx: Any, chat_id: str, limit: int) -> Mapping[str, Any] | None:
    """回溯最近消息，返回最新的一条带图消息。"""
    if not chat_id or limit <= 0:
        return None
    try:
        payload = await ctx.message.get_recent(chat_id, limit=limit)
    except Exception:  # noqa: BLE001 - 回溯失败不影响"至少把已知的图交出去"
        return None
    if not isinstance(payload, Mapping) or payload.get("success") is False:
        return None
    return newest_image_message(payload.get("messages"))


async def _download(url: str, *, http: Any, max_bytes: int, timeout_seconds: float) -> tuple[bytes, str]:
    """下载平台图片并校验它确实是图片。"""
    response = await http.fetch_bytes(
        url, max_bytes=max_bytes, timeout_seconds=timeout_seconds, breaker_key="inbound"
    )
    mime = normalize_mime(response.content_type)
    if mime not in IMAGE_MIME_TYPES:
        mime = sniff_image_mime(response.content) or ""
    if not mime:
        raise SearchError(f"拿到的不是图片（{response.content_type or '未知类型'}）")
    if response.truncated:
        raise SearchError(f"图片超过 {max_bytes} 字节")
    return response.content, mime


async def _upgrade(
    ctx: Any,
    message: Mapping[str, Any] | None,
    candidate: InboundImage,
    chat_id: str,
) -> InboundImage:
    """给只有 URL 的候选补取二进制。

    ``get_recent`` 的 SDK 签名不带 ``include_binary_data``，所以从它拿到的图片段
    只有 ``data``（平台 URL）与 ``hash``；要字节得再按 id 取一次。
    """
    if candidate.has_bytes or ctx is None:
        return candidate
    message_id = str((message or {}).get("message_id") or "")
    if not message_id:
        return candidate
    detail = await _by_id(ctx, message_id, chat_id)
    if detail is None:
        return candidate
    richer = image_from_message(detail, source=f"{candidate.source}+get_by_id")
    return richer if richer is not None and richer.has_bytes else candidate


async def acquire_inbound_image(
    *,
    message: Mapping[str, Any] | None = None,
    ctx: Any = None,
    chat_id: str = "",
    stream_id: str = "",
    message_id: str = "",
    http: Any = None,
    max_bytes: int = DEFAULT_INBOUND_MAX_BYTES,
    timeout_seconds: float = 8.0,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
) -> InboundImage:
    """取得用户图片的字节。

    优先级：**第一个能拿到图片的来源就算它**（就近优先——宁可当前消息的原图，
    也不要更久之前那张）��然后尽力把它的二进制补齐。

    Raises:
        SearchError: 所有途径都拿不到图片；``user_message`` 给出可读提示。
    """
    chat = chat_id or stream_id
    explicit = str(message_id or "")

    async def _source_message() -> tuple[Mapping[str, Any] | None, str]:
        return (message, "message") if isinstance(message, Mapping) else (None, "")

    async def _source_explicit() -> tuple[Mapping[str, Any] | None, str]:
        if not explicit:
            return None, ""
        return await _by_id(ctx, explicit, chat), "message.get_by_id"

    async def _source_current_id() -> tuple[Mapping[str, Any] | None, str]:
        """当前消息自带 id（@Command 路径）：按 id 补一次二进制。"""
        if explicit or not isinstance(message, Mapping):
            return None, ""
        current_id = str(message.get("message_id") or "")
        if not current_id:
            return None, ""
        return await _by_id(ctx, current_id, chat), "message.get_by_id"

    async def _source_recent() -> tuple[Mapping[str, Any] | None, str]:
        return await _from_recent(ctx, chat, recent_limit), "message.get_recent"

    # 惰性求值：第一级拿到字节就直接返回，不为后面的来源白跑 RPC
    for source in (_source_message, _source_explicit, _source_current_id, _source_recent):
        detail, label = await source()
        if detail is None:
            continue
        candidate = image_from_message(detail, source=label)
        if candidate is None:
            continue
        candidate = await _upgrade(ctx, detail, candidate, chat)
        if candidate.has_bytes:
            return candidate
        # 只剩平台 URL：自己下载（URL 有时效，且不一定公网可达）
        if http is not None and candidate.url:
            content, mime = await _download(
                candidate.url, http=http, max_bytes=max_bytes, timeout_seconds=timeout_seconds
            )
            candidate.content = content
            candidate.mime_type = mime
        return candidate

    raise SearchError(
        f"最近 {recent_limit} 条消息里没有可用的图片",
        user_message="没找到你发的图，是不是没发上来？",
    )
