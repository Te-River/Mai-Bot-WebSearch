"""查询处理：CJK 词组保护、站点限定、空白归一。

规则来自参考实现的实网经验：**中文 SERP 会把多词查询的 token 跨页自由匹配**，
导致结果严重跑偏。把含中文最多的那个词加引号可以显著收敛。

注意：引号只对 HTML SERP 有意义，JSON 查询 API 会把引号当字面量——
所以由各 provider 自己决定是否调用 ``protect_cjk_phrase``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "PreparedQuery",
    "contains_cjk",
    "extract_site",
    "normalize_whitespace",
    "prepare_query",
    "protect_cjk_phrase",
    "search_subject",
]

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_SITE_RE = re.compile(r"\bsite:(\S+)", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

# 疑问成分：用于给"按标题检索"的站点（如萌娘百科）剥出一个可匹配的主题词。
# 按长度倒序处理，否则"是什么"会被"什么"先吃掉一半。
_QUESTION_SUFFIXES = tuple(
    sorted(
        (
            "是什么意思",
            "是什么",
            "是哪个",
            "是谁",
            "是啥",
            "怎么样",
            "怎样",
            "为什么",
            "怎么",
            "多少",
            "哪个",
            "什么",
            "吗",
            "呢",
            "吧",
            "啊",
            "呀",
        ),
        key=len,
        reverse=True,
    )
)
_QUESTION_PREFIXES = tuple(
    sorted(
        ("什么是", "我想知道", "想问一下", "问一下", "了解一下", "介绍一下", "请问"),
        key=len,
        reverse=True,
    )
)
_TRAILING_PUNCTUATION = "?？!！。.~～ ,，、"


@dataclass(slots=True)
class PreparedQuery:
    """拆分后的查询。"""

    text: str
    site: str = ""
    quoted: bool = False


def contains_cjk(text: str) -> bool:
    """是否包含中日韩字符。"""
    return bool(_CJK_RE.search(text or ""))


def normalize_whitespace(query: str) -> str:
    """折叠空白并去掉首尾空格。"""
    return _WS_RE.sub(" ", (query or "").strip())


def extract_site(query: str) -> tuple[str, str]:
    """拆出 ``site:`` 限定符，返回 ``(站点, 去掉限定符的查询)``。

    站点限定是萌娘百科兜底路径的关键：``opensearch`` 对自然语言零召回，
    需要先用通用引擎查 ``site:zh.moegirl.org.cn`` 定位标题。
    """
    match = _SITE_RE.search(query or "")
    if not match:
        return "", normalize_whitespace(query)
    site = match.group(1).strip().strip("'\"")
    rest = normalize_whitespace(_SITE_RE.sub(" ", query))
    return site, rest


def protect_cjk_phrase(query: str) -> str:
    """把多词中文查询里"最像核心短语"的那个词加双引号。

    已有引号、无 CJK、单词查询、核心词不足 2 个汉字时都原样返回
    —— 不臆造短语边界，也不和用户自己的引号打架。
    """
    if '"' in query or "“" in query:
        return query
    if not contains_cjk(query):
        return query
    tokens = [token for token in query.split() if token]
    if len(tokens) < 2:
        return query

    core_index = -1
    core_length = 0
    for index, token in enumerate(tokens):
        length = len(_CJK_RE.findall(token))
        if length > core_length:
            core_length = length
            core_index = index
    if core_index < 0 or core_length < 2:
        return query

    protected = list(tokens)
    protected[core_index] = f'"{protected[core_index]}"'
    return " ".join(protected)


def prepare_query(query: str) -> PreparedQuery:
    """归一化查询并拆出站点限定。"""
    site, text = extract_site(query)
    return PreparedQuery(text=text, site=site, quoted='"' in text or "“" in text)


def search_subject(query: str) -> str:
    """剥掉疑问成分，抽出查询的"主题"。

    用途是**按标题检索的站点**（萌娘百科的 ``opensearch`` 就是标题前缀匹配）。
    实测：``site:zh.moegirl.org.cn 初音未来是什么`` 在 Bing 上找不到萌娘百科页面，
    而 ``初音未来`` 能直接命中条目标题——自然语言问句必须先剥掉疑问成分。

    剥不出东西（或剥完不足 2 个字）时原样返回，不臆造主题。
    """
    text = normalize_whitespace(query).strip(_TRAILING_PUNCTUATION)
    if not text:
        return normalize_whitespace(query)

    changed = True
    while changed and len(text) > 2:
        changed = False
        for prefix in _QUESTION_PREFIXES:
            if text.startswith(prefix) and len(text) > len(prefix) + 1:
                text = text[len(prefix) :].strip(_TRAILING_PUNCTUATION)
                changed = True
                break
        for suffix in _QUESTION_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text = text[: -len(suffix)].strip(_TRAILING_PUNCTUATION)
                changed = True
                break

    text = text.strip()
    return text if len(text) >= 2 else normalize_whitespace(query)
