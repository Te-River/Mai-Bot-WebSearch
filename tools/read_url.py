"""``read_url`` 的结果渲染。

与 ``web_search`` 一致：失败必须说清原因，不返回空内容。
"""

from __future__ import annotations

from ..reading.types import ReadingResult

__all__ = ["render_reading"]

# 抽取策略名 → 给用户看的说法
_STRATEGY_LABELS = {
    "trafilatura": "trafilatura",
    "readability": "readability",
    "builtin": "内置抽取",
    "moegirl-extracts": "萌娘百科接口",
    "none": "未能抽取",
}


def render_reading(result: ReadingResult, *, max_chars: int = 6000) -> str:
    """把阅读结果渲染成给模型阅读的文本。"""
    if not result.text.strip():
        return (
            f"我打开了 {result.url}，但没能从里面提取出正文。\n"
            "建议：换一个具体的内容页链接，或直接用 web_search 搜索相关主题。"
        )

    label = _STRATEGY_LABELS.get(result.strategy, result.strategy or "未知")
    notes = [f"正文抽取：{label}"]
    if result.hops:
        notes.append(f"跳转 {result.hops} 次")
    if result.truncated:
        notes.append("内容已截断")

    header = f"《{result.title}》" if result.title else "网页内容"
    body = result.text
    if len(body) > max_chars:
        body = f"{body[:max_chars]}…（正文过长已截断）"

    return "\n".join(
        [
            header,
            f"来源：{result.url}",
            f"（{'；'.join(notes)}）",
            "",
            body,
        ]
    )
