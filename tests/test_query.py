"""查询处理测试：CJK 词组保护、站点限定、空白归一。"""

from __future__ import annotations

import pytest

from mai_websearch_under_test.search.query import (
    PreparedQuery,
    contains_cjk,
    extract_site,
    normalize_whitespace,
    prepare_query,
    protect_cjk_phrase,
    search_subject,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("初音未来", True),
        ("Hello World", False),
        ("python 报错", True),
        ("", False),
        ("ひらがな", True),
    ],
)
def test_contains_cjk(text: str, expected: bool) -> None:
    """中日韩字符识别。"""
    assert contains_cjk(text) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  a   b  ", "a b"),
        ("a\n\tb", "a b"),
        ("", ""),
    ],
)
def test_normalize_whitespace(raw: str, expected: str) -> None:
    """空白折叠。"""
    assert normalize_whitespace(raw) == expected


class TestProtectCjkPhrase:
    """中文 SERP 会把 token 跨页自由匹配，把核心词加引号能显著收敛结果。"""

    def test_protects_the_token_with_most_cjk(self) -> None:
        assert protect_cjk_phrase("初音未来 演唱会 门票") == '"初音未来" 演唱会 门票'

    def test_leaves_existing_quotes_alone(self) -> None:
        """不和用户自己的引号打架。"""
        assert protect_cjk_phrase('"初音未来" 演唱会') == '"初音未来" 演唱会'
        assert protect_cjk_phrase("“初音未来” 演唱会") == "“初音未来” 演唱会"

    def test_leaves_single_token_alone(self) -> None:
        """单词查询没有短语边界可加。"""
        assert protect_cjk_phrase("初音未来") == "初音未来"

    def test_leaves_pure_ascii_alone(self) -> None:
        """英文查询不需要这层保护。"""
        assert protect_cjk_phrase("hello world") == "hello world"

    def test_ignores_single_cjk_char_token(self) -> None:
        """核心词不足两个汉字时不臆造短语边界。"""
        assert protect_cjk_phrase("的 python") == "的 python"


class TestExtractSite:
    """``site:`` 限定是萌娘百科兜底路径的关键。"""

    def test_extracts_site_and_strips_it(self) -> None:
        assert extract_site("初音未来 site:zh.moegirl.org.cn") == ("zh.moegirl.org.cn", "初音未来")

    def test_case_insensitive(self) -> None:
        assert extract_site("hello SITE:example.com") == ("example.com", "hello")

    def test_no_site_returns_empty(self) -> None:
        assert extract_site("初音未来") == ("", "初音未来")

    def test_handles_quoted_site(self) -> None:
        assert extract_site('x site:"example.com"') == ("example.com", "x")


def test_prepare_query_marks_quoted() -> None:
    """已带引号的查询需要被标记（部分引擎要区别对待）。"""
    assert prepare_query('"初音未来" site:a.com') == PreparedQuery(text='"初音未来"', site="a.com", quoted=True)


def test_prepare_query_plain() -> None:
    """普通查询。"""
    assert prepare_query("初音未来") == PreparedQuery(text="初音未来", site="", quoted=False)


class TestSearchSubject:
    """按标题检索的站点需要剥掉疑问成分。

    实测：``site:zh.moegirl.org.cn 初音未来是什么`` 在 Bing 上找不到萌娘百科页面，
    而剥成 ``初音未来`` 后 opensearch 能直接命中条目。
    """

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("初音未来是什么", "初音未来"),
            ("初音未来是谁", "初音未来"),
            ("初音未来是谁呢", "初音未来"),
            ("初音未来怎么样？", "初音未来"),
            ("什么是初音未来", "初音未来"),
            ("请问初音未来是谁", "初音未来"),
            ("介绍一下初音未来", "初音未来"),
            ("初音未来 是什么", "初音未来"),
        ],
    )
    def test_strips_question_words(self, query: str, expected: str) -> None:
        assert search_subject(query) == expected

    @pytest.mark.parametrize("query", ["初音未来", "python asyncio 超时", "什么是", "是什么", ""])
    def test_leaves_harmless_queries_alone(self, query: str) -> None:
        """没有疑问成分、或剥完不足两个字时原样返回，不臆造主题。"""
        assert search_subject(query) == query
