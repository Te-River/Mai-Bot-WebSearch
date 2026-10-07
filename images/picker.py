"""图片候选过滤、去重与下载。

三个层次去重，缺一不可：

1. **URL 去重**：同一张图在不同引擎里的链接可能只差跟踪参数；
2. **内容哈希去重**：同一个图片可能有多个 CDN 地址（`sha256` 兜住）；
3. **重复窗口**：同一个关键词在窗口内不重复发同一张图——否则群里连问两次会看到一模一样的图。

下载必须**缩略图优先**：``send.image`` 只接受 base64（宿主实现已确认），
所以一定要下载；缩略图通常 20–60KB，原图常常数 MB。
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..core.errors import SearchError
from ..search.types import host_path_key
from .types import (
    IMAGE_MIME_TYPES,
    DownloadedImage,
    ImageCandidate,
    normalize_mime,
    sniff_image_mime,
)

__all__ = ["ImageHistory", "ImagePicker", "PickResult", "filter_candidates"]


def filter_candidates(
    candidates: Sequence[ImageCandidate],
    *,
    min_width: int = 0,
    min_height: int = 0,
) -> list[ImageCandidate]:
    """按声明的尺寸过滤候选。

    尺寸来自搜索引擎的声明（我们没有内置图片解码器去验证），
    作用是挡掉图标、表情与追踪像素这类明显不合格的候选。
    声明为 0（未知）的候选**不**因此被丢弃——宁可用下载后的体积兜底。
    """
    kept: list[ImageCandidate] = []
    for candidate in candidates:
        if not candidate.url:
            continue
        if min_width and candidate.width and candidate.width < min_width:
            continue
        if min_height and candidate.height and candidate.height < min_height:
            continue
        kept.append(candidate)
    return kept


class ImageHistory:
    """按关键词记录最近发过的图片哈希，避免重复刷屏。"""

    def __init__(
        self,
        *,
        window_seconds: float,
        max_queries: int = 200,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = max(0.0, window_seconds)
        self._max_queries = max(max_queries, 1)
        self._clock = clock
        self._entries: OrderedDict[str, deque[tuple[float, str]]] = OrderedDict()

    def is_recent(self, query: str, key: str) -> bool:
        """该关键词在窗口内是否已经发过这张图。"""
        if self._window <= 0:
            return False
        bucket = self._entries.get(query)
        if not bucket:
            return False
        now = self._clock()
        return any(entry_key == key and now - stamp < self._window for stamp, entry_key in bucket)

    def remember(self, query: str, key: str) -> None:
        """记录一次发送。"""
        if self._window <= 0:
            return
        bucket = self._entries.setdefault(query, deque(maxlen=64))
        bucket.append((self._clock(), key))
        self._entries.move_to_end(query)
        while len(self._entries) > self._max_queries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """清空记录。"""
        self._entries.clear()

    @property
    def tracked_queries(self) -> int:
        """当前跟踪的关键词数量（用于验证内存有界）。"""
        return len(self._entries)


@dataclass(slots=True)
class PickResult:
    """挑选与下载的结果。"""

    images: list[DownloadedImage] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skipped_repeats: int = 0


class ImagePicker:
    """把候选变成"可以发出去的图片"。"""

    def __init__(
        self,
        *,
        http: object,
        history: ImageHistory,
        max_bytes: int = 3145728,
        min_width: int = 0,
        min_height: int = 0,
        prefer_thumbnail: bool = True,
        timeout_seconds: float = 6.0,
    ) -> None:
        self._http = http
        self._history = history
        self._max_bytes = max_bytes
        self._min_width = min_width
        self._min_height = min_height
        self._prefer_thumbnail = prefer_thumbnail
        self._timeout = timeout_seconds

    def _urls_for(self, candidate: ImageCandidate) -> list[str]:
        """缩略图优先；把另一个地址作为回退。"""
        if self._prefer_thumbnail and candidate.thumbnail_url:
            ordered = [candidate.thumbnail_url, candidate.url]
        else:
            ordered = [candidate.url, candidate.thumbnail_url]
        seen: list[str] = []
        for url in ordered:
            if url and url not in seen:
                seen.append(url)
        return seen

    async def _download(self, candidate: ImageCandidate) -> DownloadedImage:
        """下载并校验一张图片；失败时抛 :class:`SearchError`。

        熔断键**按主机**取：实测 Bing 的缩略图 CDN（``ts*.mm.bing.net``）在部分网络下
        直接 ConnectError，若把整个图片通路共用一个熔断键，一次 CDN 故障会连累原图下载。
        按主机分开后，挂掉的 CDN 会在连续失败后快速跳过（熔断），直接走原图。
        """
        last_error: SearchError | None = None
        for url in self._urls_for(candidate):
            host = urlsplit(url).hostname or "unknown"
            try:
                response = await self._http.fetch_bytes(  # type: ignore[attr-defined]
                    url,
                    max_bytes=self._max_bytes,
                    timeout_seconds=self._timeout,
                    breaker_key=f"image:{host}",
                )
            except SearchError as exc:
                last_error = exc
                continue

            mime = normalize_mime(response.content_type)
            if mime not in IMAGE_MIME_TYPES:
                # 不少 CDN 用 octet-stream 发图片，靠魔数兜底
                mime = sniff_image_mime(response.content) or ""
            if not mime:
                last_error = SearchError(f"不是图片（{response.content_type or '未知类型'}）")
                continue
            if response.truncated:
                last_error = SearchError(f"图片超过 {self._max_bytes} 字节")
                continue

            return DownloadedImage(
                content=response.content,
                mime_type=mime,
                source_url=response.url or url,
                engine=candidate.engine,
                title=candidate.title,
                page_url=candidate.page_url,
                thumbnail_url=candidate.thumbnail_url,
                width=candidate.width,
                height=candidate.height,
            )
        raise last_error or SearchError("没有可用的图片地址")

    async def pick(
        self,
        query: str,
        candidates: Sequence[ImageCandidate],
        *,
        limit: int = 1,
    ) -> PickResult:
        """按顺序尝试候选，直到凑够 ``limit`` 张可用且不重复的图片。"""
        result = PickResult()
        tried: set[str] = set()

        for candidate in filter_candidates(
            candidates,
            min_width=self._min_width,
            min_height=self._min_height,
        ):
            location = host_path_key(candidate.url)
            if not location or location in tried:
                continue
            tried.add(location)

            try:
                image = await self._download(candidate)
            except SearchError as exc:
                result.failures.append(str(exc))
                continue

            digest = image.sha256
            if self._history.is_recent(query, digest):
                result.skipped_repeats += 1
                continue

            self._history.remember(query, digest)
            result.images.append(image)
            if len(result.images) >= max(1, limit):
                break

        return result
