"""网页读取：手动跟随重定向 + 每跳 SSRF 复检 + 正文抽取。

**手动跟随重定向是安全要求而非风格选择**：只校验初始 URL 的话，
攻击者可以用一个公网地址 302 到 ``169.254.169.254`` 把防护绕过去。
因此每一跳都要重新走一遍 :func:`ensure_public_url`。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..core.errors import (
    ContentTooLargeError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
)
from ..core.ssrf import ensure_public_url
from .extract import extract_text
from .types import ReadingResult, SpecialSource

__all__ = ["READABLE_CONTENT_TYPES", "is_readable_content_type", "read_url"]

# 只读文本类内容；图片、视频、PDF 等一律拒绝（有专门的图片通路）
READABLE_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
    "text/xml",
    "application/xml",
    "text/markdown",
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def is_readable_content_type(content_type: str) -> bool:
    """空 ``Content-Type`` 视为可读（不少站点不返回）；否则必须在白名单内。"""
    normalized = (content_type or "").split(";")[0].strip().lower()
    if not normalized:
        return True
    return any(normalized == allowed or normalized.endswith(f"+{allowed.split('/')[-1]}") for allowed in READABLE_CONTENT_TYPES)


def _find_special(url: str, sources: Sequence[SpecialSource]) -> SpecialSource | None:
    for source in sources:
        if source.matches(url):
            return source
    return None


async def read_url(
    url: str,
    *,
    http: Any,
    max_bytes: int = 1048576,
    timeout_seconds: float = 8.0,
    max_content_length: int = 3000,
    max_redirects: int = 5,
    block_private_hosts: bool = True,
    special_sources: Sequence[SpecialSource] = (),
) -> ReadingResult:
    """读取一个 URL 并抽取正文。

    Args:
        url: 目标地址。
        http: :class:`~..core.http.HttpClient` 实例（或测试替身）。
        max_bytes: 单页最多读取的字节数。
        timeout_seconds: 单跳超时。
        max_content_length: 返回正文的最大字符数。
        max_redirects: 最多跟随多少跳。
        block_private_hosts: 是否启用 SSRF 防护（仅本机自建服务才应关闭）。
        special_sources: 站点专用通路（如萌娘百科的 JSON 接口）。

    Raises:
        BlockedAddressError: 目标或任一跳指向内网。
        TooManyRedirectsError: 跳转次数超限。
        UnsupportedContentTypeError: 不是可读文本。
        HttpStatusError / NetworkError: 由 HTTP 层抛出。
    """
    started = time.monotonic()

    special = _find_special(url, special_sources)
    if special is not None:
        result = await special.fetch(url, http)
        # 专用通路同样受正文长度约束，否则一条百科正文可能几万字
        if len(result.text) > max_content_length:
            result.text = f"{result.text[:max_content_length]}…"
            result.truncated = True
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    allow_private = not block_private_hosts
    current = await ensure_public_url(url, allow_private=allow_private)
    hops = 0

    while True:
        response = await http.fetch(
            current,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            follow_redirects=False,
        )
        if response.status in _REDIRECT_STATUSES:
            location = (response.headers or {}).get("location", "")
            if not location:
                # 没有 Location 的重定向只能当作最终响应处理
                break
            hops += 1
            if hops > max_redirects:
                raise TooManyRedirectsError(max_redirects)
            current = await ensure_public_url(urljoin(current, location), allow_private=allow_private)
            continue
        break

    if not is_readable_content_type(response.content_type):
        raise UnsupportedContentTypeError(response.content_type)

    extracted = extract_text(response.text)
    if response.truncated and not extracted.text:
        raise ContentTooLargeError(max_bytes)

    text = extracted.text
    clipped = False
    if len(text) > max_content_length:
        text = f"{text[:max_content_length]}…"
        clipped = True

    return ReadingResult(
        url=response.url or current,
        title=extracted.title,
        text=text,
        strategy=extracted.strategy,
        truncated=response.truncated or clipped,
        content_type=response.content_type,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        hops=hops,
    )


def host_of(url: str) -> str:
    """取主机名（小写），供站点专用通路的匹配函数使用。"""
    return (urlsplit(url).hostname or "").lower()
