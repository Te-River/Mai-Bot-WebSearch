"""``web_search`` 的结果渲染。

工具处理函数必须挂在插件类上（SDK 扫描插件类），所以这里只放纯函数：
把 :class:`SearchOutcome` 渲染成给模型阅读的文本。

原则：**失败必须说清原因与已尝试的引擎**，绝不返回空内容让模型自己猜。
"""

from __future__ import annotations

from ..search.router import SearchOutcome

__all__ = ["render_failure", "render_outcome", "render_success"]

_MAX_TITLE = 120
_MAX_SNIPPET = 300


def _clip(text: str, limit: int) -> str:
    """截断过长文本。"""
    compact = " ".join((text or "").split())
    return compact if len(compact) <= limit else f"{compact[:limit]}…"


def render_success(outcome: SearchOutcome) -> str:
    """渲染命中的结果列表。"""
    lines = [f"关于「{outcome.query}」的搜索结果（{len(outcome.hits)} 条）："]
    for hit in outcome.hits:
        lines.append(f"{hit.rank}. {_clip(hit.title, _MAX_TITLE)}")
        lines.append(f"   来源：{hit.url}")
        if hit.snippet:
            lines.append(f"   摘要：{_clip(hit.snippet, _MAX_SNIPPET)}")
    if outcome.deadline_hit:
        lines.append("（达到时间上限，已返回先到的结果）")
    return "\n".join(lines)


def render_failure(outcome: SearchOutcome) -> str:
    """渲染"没搜到"的情形，并给出可执行的下一步建议。"""
    lines = [f"没有搜到「{outcome.query}」的结果。"]
    if outcome.engine_status:
        lines.append("已尝试的搜索源：" + "；".join(f"{name} → {status}" for name, status in outcome.engine_status.items()))

    hints: list[str] = []
    if all(not status.startswith("ok") for status in outcome.engine_status.values()) and outcome.engine_status:
        hints.append("所有搜索源都失败了，通常是网络或代理问题（检查插件配置的 network 段）")
    else:
        hints.append("换个更具体的关键词，或直接给出一个网址让我读正文")
    if outcome.deadline_hit:
        hints.append("本次达到时间上限，稍后可重试")
    lines.append("建议：" + "；".join(hints))
    return "\n".join(lines)


def render_outcome(outcome: SearchOutcome, *, max_chars: int = 4000) -> str:
    """统一入口：按是否有结果选择渲染方式，并做总长度保护。"""
    text = render_success(outcome) if outcome.hits else render_failure(outcome)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n…（结果过长已截断）"
