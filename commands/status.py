"""``/websearch status`` 的输出构造。

组件装饰器必须挂在插件类上（SDK 扫描插件类），因此这里只放纯函数，便于离线单测。
"""

from __future__ import annotations

from ..config import WebSearchConfig
from ..reading.extract import strategy_names
from ..version import __version__

__all__ = ["build_status_text"]


def _mark(flag: bool) -> str:
    """把布尔开关渲染成对勾/叉号。"""
    return "✓" if flag else "✗"


def _describe_proxy(config: WebSearchConfig) -> str:
    """把代理模式渲染成一句人话。"""
    mode = config.network.proxy_mode
    if mode == "auto":
        return "auto（读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY）"
    if mode == "manual":
        return f"manual（{config.network.proxy or '未填写地址'}）"
    return "off（不使用代理）"


def build_status_text(config: WebSearchConfig) -> str:
    """构造 ``/websearch status`` 的输出文本。"""
    engines = config.engines
    enabled_engines = " ".join(
        f"{name}{_mark(flag)}"
        for name, flag in (
            ("bing", engines.bing.enabled),
            ("searxng", engines.searxng.enabled),
            ("moegirl", engines.moegirl.enabled),
            ("duckduckgo", engines.duckduckgo.enabled),
        )
    )
    visual = "开" if config.search.fetch_top_n else "关"
    summary = "开" if config.llm.summarize else "关"
    extractors = "/".join(strategy_names())
    images = config.images
    image_sources = " ".join(
        f"{name}{_mark(flag)}"
        for name, flag in (("bing", images.bing_enabled), ("searxng", images.searxng_enabled))
    )
    reverse = config.reverse
    reverse_engines = " ".join(
        f"{name}{_mark(flag)}"
        for name, flag in (("saucenao", reverse.saucenao_enabled),)
    )

    return "\n".join(
        [
            f"麦麦联网搜索 v{__version__}",
            f"状态：{'已启用' if config.plugin.enabled else '已禁用'}",
            f"代理：{_describe_proxy(config)}",
            f"引擎：{enabled_engines}",
            (
                f"搜索：结果 {config.search.max_results} 条，"
                f"硬截止 {config.search.global_deadline_seconds:g}s，"
                f"提前返回 {config.search.early_return_quorum} 个引擎，"
                f"正文抓取 {visual}"
            ),
            f"阅读：{'开' if config.reading.enabled else '关'}，抽取链路 {extractors}",
            (
                f"图片：{'开' if images.enabled else '关'}（{image_sources}），"
                f"每次 {images.max_images_per_call} 张，"
                f"缩略图优先 {_mark(images.prefer_thumbnail)}，"
                f"重复窗口 {images.repeat_window_minutes} 分钟"
            ),
            (
                f"以图搜源：{'开' if reverse.enabled else '关'}（{reverse_engines}），"
                f"回传模型观察 {_mark(reverse.preview_to_model)}"
            ),
            f"模型：总结 {summary}（任务 {config.llm.task_name}）",
            "图像理解：由麦麦按模型能力自动判定，本插件不调用 vlm 任务",
        ]
    )
