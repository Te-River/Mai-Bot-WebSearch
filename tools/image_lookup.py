"""``image_lookup`` 的结果渲染（图搜图 / 图搜文）。

这个工具的定位要说清楚：**它不假装自己有反查能力**。
反查引擎在多数网络下不可用（见 ``images/reverse/__init__.py`` 的实测表），
所以它的主要价值是把"图片已经拿到、模型可以直接观察、需要相似图就顺势用 image_search"
这条链路打通并讲明白。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

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

    **这里踩过一个真实的坑**：早退条件曾经是"完成一个算一个"（quorum=1），
    结果某个引擎因为"输入形式不匹配"**瞬间失败**，它的完成立刻触发了早退，
    把还在跑、真正可能能用的引擎全部取消了——最终只报告了那一个失败。

    现在改成三条：
    1. 先按 ``supports()`` 过滤掉处理不了当前输入的引擎（不参与，也不报失败）；
    2. 只有拿到**成功结果**才早退；一路失败就一路等到 deadline；
    3. 到达 deadline 后取消仍在跑的，并把它们记成"没在时限内返回"。
    """
    if not providers:
        return []

    usable = [provider for provider in providers if _supports(provider, image)]
    if not usable:
        return []

    async def run(provider: Any) -> ReverseLookupResult:
        try:
            return await provider.lookup(image, http)
        except Exception as exc:  # noqa: BLE001 - 单个引擎失败只记录，不影响其它引擎
            return ReverseLookupResult(engine=str(getattr(provider, "name", "?")), error=describe_error(exc))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(deadline_seconds, 0.0)
    tasks = {str(provider.name): asyncio.ensure_future(run(provider)) for provider in usable}
    collected: dict[str, ReverseLookupResult] = {}

    pending = set(tasks.values())
    while pending:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        done, pending = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        for task in done:
            name = next(key for key, value in tasks.items() if value is task)
            collected[name] = task.result()
        # 只有"成功"才值得早退；全是失败就继续等其它引擎
        if any(result.ok for result in collected.values()):
            break
        if grace_seconds > 0 and pending:
            await asyncio.sleep(min(grace_seconds, max(0.0, deadline - loop.time())))

    for name, task in tasks.items():
        if task.done():
            collected.setdefault(name, task.result())
        else:
            task.cancel()
            collected.setdefault(name, ReverseLookupResult(engine=name, error="没在时限内返回"))

    return [collected[str(provider.name)] for provider in usable if str(provider.name) in collected]


def _supports(provider: Any, image: InboundImage) -> bool:
    """引擎是否处理得了这张图；没声明 ``supports`` 的一律当作可以。"""
    checker = getattr(provider, "supports", None)
    if checker is None:
        return True
    try:
        return bool(checker(image))
    except Exception:  # noqa: BLE001 - supports 判断失败不该拖垮整次反查
        return True


def render_lookup(
    *,
    image: InboundImage,
    results: Sequence[ReverseLookupResult],
    previewed: bool,
) -> str:
    """渲染反查结果与后续指引。"""
    size_kb = max(1, len(image.content) // 1024) if image.content else 0
    if image.content:
        lines = [f"已拿到用户发来的图片（{size_kb} KB，{image.mime_type or '未知类型'}）。"]
    else:
        # 只有平台 URL、没拿到字节：还能用于以图搜源（部分引擎按 URL 查），
        # 但**不能**把图交给模型观察——必须说清楚，不能假装拿到了。
        lines = [
            "只拿到了这张图的平台链接，没能取到图片本身（平台 URL 有时效且不保证公网可达），"
            "因此我无法直接观察画面内容。"
        ]

    found = [result for result in results if result.ok]
    errors = [result for result in results if result.error]
    if not results:
        lines.append(
            "没有可用于这张图的反查引擎（可在配置里开启更多引擎；Yandex 只支持按图片链接反查）。"
        )
    for result in found:
        lines.append(f"以图搜源（{result.engine}）：")
        for source in result.sources[:_MAX_SOURCES]:
            parts = [f"相似度 {source.similarity:.1f}%"] if source.similarity else []
            if source.title:
                parts.append(f"作品/标题：{source.title}")
            if source.author:
                parts.append(f"作者：{source.author}")
            if source.index:
                parts.append(f"图库：{source.index}")
            if not parts:
                parts.append("(无标题信息)")
            lines.append("  - " + "｜".join(parts))
            for url in source.urls[:2]:
                lines.append(f"    来源：{url}")
    for result in errors:
        lines.append(f"以图搜源（{result.engine}）失败：{result.error}")

    if not found:
        # 全部引擎都没搜到：明确告诉模型"这是环境问题，不要重试"，避免它反复调工具
        lines.append(
            "所有以图搜源引擎都没能返回结果（多为网络不可达或被反爬）。"
            "**不要重复调用本工具**——请直接依据你看到的图像内容回答用户，"
            "不认识就说不认识，不要编造来源。"
        )
    else:
        lines.append("以上是反查到的来源信息；请据此回答，并保留来源链接。")

    if previewed and image.has_bytes:
        lines.append("我已经把这张图交给你观察，可直接依据图像内容回答用户。")

    if not found:
        return "\n".join(lines)

    lines.append(
        "如果用户还想要**新的相似图片**（而不是这张图的出处），"
        "请依据图像内容提取关键词后调用 image_search。"
    )
    return "\n".join(lines)
