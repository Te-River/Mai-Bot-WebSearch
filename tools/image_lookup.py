"""``image_lookup`` 的结果渲染（图搜图 / 图搜文）。

这个工具的定位要说清楚：**它不假装自己有反查能力**。
反查引擎在多数网络下不可用（见 ``images/reverse/__init__.py`` 的实测表），
所以它的主要价值是把"图片已经拿到、模型可以直接观察、需要相似图就顺势用 image_search"
这条链路打通并讲明白。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..core.budget import gather_early
from ..core.errors import describe_error
from ..images.inbound import InboundImage
from ..images.reverse import ReverseLookupResult

__all__ = ["render_lookup", "run_reverse_lookup"]

_MAX_SOURCES = 3


async def run_reverse_lookup(
    providers: Sequence[Any],
    image: InboundImage,
    http: Any,
    *,
    deadline_seconds: float = 8.0,
    grace_seconds: float = 0.3,
) -> list[ReverseLookupResult]:
    """并发跑所有反查引擎，失败转成可见的错误项（而不是抛出去）。

    反查是"锦上添花"，任何一个引擎失败都不该让整个工具失败。
    """
    if not providers:
        return []

    async def run(provider: Any) -> ReverseLookupResult:
        try:
            return await provider.lookup(image, http)
        except Exception as exc:  # noqa: BLE001 - 单个引擎失败只记录，不影响其它引擎
            return ReverseLookupResult(engine=str(getattr(provider, "name", "?")), error=describe_error(exc))

    gathered = await gather_early(
        {str(provider.name): run(provider) for provider in providers},
        quorum=1,
        grace_seconds=grace_seconds,
        deadline_seconds=deadline_seconds,
    )
    ordered: list[ReverseLookupResult] = []
    for provider in providers:
        name = str(provider.name)
        if name in gathered.values:
            ordered.append(gathered.values[name])
        elif name in gathered.errors:
            ordered.append(ReverseLookupResult(engine=name, error=gathered.errors[name]))
    return ordered


def render_lookup(
    *,
    image: InboundImage,
    results: Sequence[ReverseLookupResult],
    previewed: bool,
) -> str:
    """渲染反查结果与后续指引。"""
    size_kb = max(1, len(image.content) // 1024) if image.content else 0
    lines = [f"已拿到用户发来的图片（{size_kb} KB，{image.mime_type or '未知类型'}）。"]

    found = [result for result in results if result.ok]
    errors = [result for result in results if result.error]
    if not results:
        lines.append(
            "没有可用的以图搜源引擎（可在配置的 reverse 段启用 SauceNAO 并填写 API Key）。"
        )
    for result in found:
        lines.append(f"以图搜源（{result.engine}）：")
        for source in result.sources[:_MAX_SOURCES]:
            parts = [f"相似度 {source.similarity:.1f}%"]
            if source.title:
                parts.append(f"作品/标题：{source.title}")
            if source.author:
                parts.append(f"作者：{source.author}")
            if source.index:
                parts.append(f"图库：{source.index}")
            lines.append("  - " + "｜".join(parts))
            for url in source.urls[:2]:
                lines.append(f"    来源：{url}")
    for result in errors:
        lines.append(f"以图搜源（{result.engine}）失败：{result.error}")

    if previewed:
        lines.append("我已经把这张图交给你观察，请直接依据图像内容回答用户。")
    else:
        lines.append("如果你需要重新观察图像内容，请把 reverse.preview_to_model 打开。")

    lines.append(
        "如果需要找相似图片：先依据你看到的图像内容提取关键词，再用 image_search 搜索——"
        "本工具不做以图搜图，不要重复调用它。"
    )
    return "\n".join(lines)
