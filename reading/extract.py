"""正文抽取：trafilatura → readability → 内置降级。

两个 Tier-1 库（trafilatura / readability-lxml）**不在 manifest 依赖里**，
装了就用、没装就退到内置实现——这是"轻量"与"质量"之间的显式取舍：

* 内置实现是**廉价且可预测**的 O(n) 噪声剔除（不做 readability 打分），
  对大多数新闻/博客/百科页面够用；
* 想要更强的正文识别能力，用户可以 ``pip install trafilatura readability-lxml``。

所有策略都只接受 HTML 字符串、返回 ``(title, text)``，因此可以用 fixtures 离线单测。
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..core.html_util import node_text, parse_html

__all__ = ["ExtractionResult", "Strategy", "available_strategies", "extract_text"]

Strategy = Callable[[str], "tuple[str, str] | None"]

# 结构性噪声：导航、页脚、侧栏、评论、脚本
_NOISE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "svg",
    "button",
    "[role=navigation]",
    "[role=banner]",
    "[role=complementary]",
    "[aria-hidden=true]",
)

# 语义化正文容器，按优先级尝试
_CONTENT_SELECTORS = (
    "article",
    "main",
    "[role=main]",
    "#content",
    ".content",
    ".post-content",
    ".article-content",
    ".markdown-body",
    "body",
)

# 连续 3 个以上换行折成 2 个，避免正文里出现大片空行
_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass(slots=True)
class ExtractionResult:
    """抽取结果与所用策略（策略名会出现在诊断信息里）。"""

    title: str
    text: str
    strategy: str


def _page_title(html: str) -> str:
    """尽力取页面标题：``<title>`` → ``og:title`` → ``<h1>``。"""
    tree = parse_html(html)
    for selector in ("title", "meta[property='og:title']", "h1"):
        node = tree.css_first(selector)
        if node is None:
            continue
        if selector.startswith("meta"):
            value = node.attributes.get("content", "")
        else:
            value = node_text(node)
        value = (value or "").strip()
        if value:
            return value
    return ""


def normalize_text(text: str) -> str:
    """折叠多余空行与行内空白。"""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _extract_trafilatura(html: str) -> tuple[str, str] | None:
    """Tier-1：trafilatura（正文识别质量最好）。"""
    import trafilatura

    text = trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if not text:
        return None
    return _page_title(html), text


def _extract_readability(html: str) -> tuple[str, str] | None:
    """Tier-1：readability-lxml（先定位正文容器，再转纯文本）。"""
    from readability import Document

    document = Document(html)
    summary = document.summary(html_partial=True)
    if not summary:
        return None
    text = node_text(parse_html(summary).css_first("body") or parse_html(summary).root, separator="\n")
    if not text.strip():
        return None
    return (document.short_title() or _page_title(html)), text


def _extract_builtin(html: str) -> tuple[str, str] | None:
    """内置降级：剔除结构性噪声后取语义容器文本。

    刻意不做 readability 那样的打分与候选比较——那需要遍历大量节点
    （最坏 O(n·depth)），与大页面上的 CPU 预算冲突。
    """
    tree = parse_html(html)
    for selector in _NOISE_SELECTORS:
        for node in tree.css(selector):
            node.decompose()

    for selector in _CONTENT_SELECTORS:
        node = tree.css_first(selector)
        if node is None:
            continue
        text = node_text(node, separator="\n")
        if text.strip():
            return _page_title(html), text
    return None


def available_strategies() -> list[tuple[str, Strategy]]:
    """按可用性构造策略链。重依赖只在真正抽取时才 import。"""
    chain: list[tuple[str, Strategy]] = []
    if importlib.util.find_spec("trafilatura") is not None:
        chain.append(("trafilatura", _extract_trafilatura))
    if importlib.util.find_spec("readability") is not None:
        chain.append(("readability", _extract_readability))
    chain.append(("builtin", _extract_builtin))
    return chain


def strategy_names() -> list[str]:
    """当前环境可用的策略名（供诊断命令展示）。"""
    return [name for name, _ in available_strategies()]


def extract_text(html: str, *, strategies: Sequence[tuple[str, Strategy]] | None = None) -> ExtractionResult:
    """依次尝试策略链，返回第一个产出非空正文的结果。

    Args:
        html: 页面 HTML。
        strategies: 可注入的策略链（测试用）；默认按可用性自动构造。
    """
    chain = list(strategies) if strategies is not None else available_strategies()
    fallback_title = _page_title(html)
    for name, strategy in chain:
        try:
            produced = strategy(html)
        except Exception:  # noqa: BLE001 - 单个策略失败必须让位给下一个
            continue
        if not produced:
            continue
        title, text = produced
        normalized = normalize_text(text)
        if normalized:
            return ExtractionResult(title=title or fallback_title, text=normalized, strategy=name)
    return ExtractionResult(title=fallback_title, text="", strategy="none")
