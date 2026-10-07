"""文搜图管线：多引擎扇出 → 候选合并 → 去重下载。

复用文本检索已有的延迟基础设施（``gather_early`` 的早返回与取消），
所以图片搜索同样受硬截止保护，不会因为某个图片源卡住而让用户干等。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..core.budget import gather_early
from .picker import ImagePicker
from .providers.bing_images import BingImagesProvider
from .providers.searxng_images import SearxngImagesProvider
from .types import DownloadedImage, ImageCandidate

__all__ = [
    "ImageProvider",
    "ImageSearchOutcome",
    "ImageSearchPipeline",
    "build_image_pipeline",
    "describe_failure",
]

# 状态与用户可见文案一一对应，避免上层再猜
ImageStatus = Literal["ok", "no_results", "no_unique", "all_failed"]


class ImageProvider(Protocol):
    """图片搜索引擎协议。"""

    name: str

    async def search(self, query: str, http: Any, *, limit: int = 10) -> list[ImageCandidate]:
        """返回候选图片。"""
        ...


@dataclass(slots=True)
class ImageSearchOutcome:
    """一次文搜图的结果与诊断信息。"""

    query: str
    status: ImageStatus
    images: list[DownloadedImage] = field(default_factory=list)
    engine_status: dict[str, str] = field(default_factory=dict)
    candidates_seen: int = 0
    skipped_repeats: int = 0
    failures: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        """是否拿到了可发送的图片。"""
        return bool(self.images)


class ImageSearchPipeline:
    """多引擎图片检索。"""

    def __init__(
        self,
        *,
        http: Any,
        providers: list[ImageProvider],
        picker: ImagePicker,
        deadline_seconds: float = 10.0,
        grace_seconds: float = 0.4,
    ) -> None:
        self._http = http
        self._providers = list(providers)
        self._picker = picker
        self._deadline = deadline_seconds
        self._grace = grace_seconds

    @property
    def providers(self) -> list[ImageProvider]:
        """当前启用的图片源。"""
        return list(self._providers)

    async def search(self, query: str, *, limit: int = 1) -> ImageSearchOutcome:
        """执行一次文搜图。"""
        started = time.monotonic()
        if not self._providers:
            return ImageSearchOutcome(query=query, status="all_failed", engine_status={})

        gathered = await gather_early(
            {provider.name: provider.search(query, self._http, limit=limit * 4) for provider in self._providers},
            quorum=1,
            grace_seconds=self._grace,
            deadline_seconds=self._deadline,
        )

        engine_status: dict[str, str] = {}
        candidates: list[ImageCandidate] = []
        # 按声明顺序合并，保证结果稳定（gather 的完成顺序是不确定的）
        for provider in self._providers:
            if provider.name in gathered.values:
                hits = gathered.values[provider.name]
                engine_status[provider.name] = f"ok:{len(hits)}"
                candidates.extend(hits)
            elif provider.name in gathered.errors:
                engine_status[provider.name] = f"失败 {gathered.errors[provider.name][:80]}"
            else:
                engine_status[provider.name] = "已取消"

        if not candidates:
            status: ImageStatus = "no_results"
            if engine_status and all(status_text.startswith("失败") for status_text in engine_status.values()):
                status = "all_failed"
            return ImageSearchOutcome(
                query=query,
                status=status,
                engine_status=engine_status,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        picked = await self._picker.pick(query, candidates, limit=limit)
        if picked.images:
            status = "ok"
        elif picked.skipped_repeats:
            status = "no_unique"
        else:
            status = "all_failed"

        return ImageSearchOutcome(
            query=query,
            status=status,
            images=picked.images,
            engine_status=engine_status,
            candidates_seen=len(candidates),
            skipped_repeats=picked.skipped_repeats,
            failures=picked.failures[:5],
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def build_image_pipeline(config: Any, http: Any, history: Any) -> ImageSearchPipeline:
    """按配置装配图片管线。"""
    images = config.images
    providers: list[ImageProvider] = []
    if images.bing_enabled:
        providers.append(BingImagesProvider(region=config.engines.bing.region, safe_search=images.safe_search))

    searxng = SearxngImagesProvider(
        base_url=config.engines.searxng.base_url,
        language=config.engines.searxng.language,
        safe_search=images.safe_search,
    )
    if images.searxng_enabled and searxng.configured:
        providers.append(searxng)

    picker = ImagePicker(
        http=http,
        history=history,
        max_bytes=images.max_bytes,
        min_width=images.min_width,
        min_height=images.min_height,
        prefer_thumbnail=images.prefer_thumbnail,
        timeout_seconds=config.network.timeout_seconds,
    )
    return ImageSearchPipeline(
        http=http,
        providers=providers,
        picker=picker,
        deadline_seconds=images.deadline_seconds,
    )


def describe_failure(outcome: ImageSearchOutcome) -> str:
    """把失败状态翻成一句用户能看懂的话。"""
    if outcome.status == "no_results":
        return f"没有找到「{outcome.query}」的图片。"
    if outcome.status == "no_unique":
        return f"「{outcome.query}」相关的图片最近都发过了，换一个说法试试。"
    if outcome.status == "all_failed":
        if outcome.failures:
            return f"找到了图片但都没能下载下来（{outcome.failures[0]}）。"
        return "所有图片源都失败了，通常是网络或代理问题。"
    return ""
