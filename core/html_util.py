"""HTML 解析小工具（lexbor 后端）。

selectolax 1.0 **移除**了 Modest 后端（``selectolax.parser`` 会直接抛 ImportError），
必须使用 ``selectolax.lexbor``。lexbor 是单 wheel、C 实现，比 lxml+bs4 轻且快。
"""

from __future__ import annotations

from typing import Any

from selectolax.lexbor import LexborHTMLParser

__all__ = ["attr", "node_text", "parse_html"]


def parse_html(html: str) -> LexborHTMLParser:
    """构造解析树。"""
    return LexborHTMLParser(html or "")


def node_text(node: Any, *, separator: str = " ") -> str:
    """提取节点文本。

    必须传 ``separator``：默认行为会把内联标签直接拼接，
    ``Hello <b>World</b>`` 会变成 ``HelloWorld``，标题里的空格就丢了。
    """
    if node is None:
        return ""
    return node.text(separator=separator, strip=True)


def attr(node: Any, name: str, default: str = "") -> str:
    """读取属性，缺失时返回默认值。"""
    if node is None:
        return default
    return node.attributes.get(name, default) or default
