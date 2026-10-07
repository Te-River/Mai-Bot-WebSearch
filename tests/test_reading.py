"""阅读管线测试：正文抽取、内容类型闸门、**每跳 SSRF 复检**。

安全要点：只校验初始 URL 是不够的——攻击者可以用公网地址 302 到 ``169.254.169.254``。
所以这里既有"直连内网被拒"的用例，也有"重定向到内网被拒"的用例。

注意：本文件里的 URL 一律使用**字面公网 IP**，避免测试真的去发 DNS 查询
（``ensure_public_url`` 对域名会解析，字面 IP 不会）。
"""

from __future__ import annotations

from typing import Any

import pytest

from mai_websearch_under_test.core.errors import (
    BlockedAddressError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
)
from mai_websearch_under_test.core.http import FetchResult
from mai_websearch_under_test.reading.extract import (
    ExtractionResult,
    extract_text,
    normalize_text,
    strategy_names,
)
from mai_websearch_under_test.reading.fetcher import is_readable_content_type, read_url
from mai_websearch_under_test.reading.types import ReadingResult, SpecialSource

PUBLIC_IP = "93.184.216.34"  # example.com 的历史地址，字面量因此不会触发 DNS
ARTICLE_URL = f"http://{PUBLIC_IP}/a"


def _response(
    *,
    status: int = 200,
    url: str = ARTICLE_URL,
    text: str = "",
    content_type: str = "text/html; charset=utf-8",
    headers: dict[str, str] | None = None,
    truncated: bool = False,
) -> FetchResult:
    return FetchResult(
        status=status,
        url=url,
        content_type=content_type,
        text=text,
        truncated=truncated,
        elapsed_ms=1,
        headers=headers or {},
    )


class ScriptedHttp:
    """按顺序返回预设响应的假 HTTP 客户端。"""

    def __init__(self, responses: list[FetchResult]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append({"url": url, **kwargs})
        assert self._responses, "没有更多预设响应了"
        return self._responses.pop(0)


# ------------------------------------------------------------------ 正文抽取


class TestExtract:
    """内置抽取是 Tier-1 库缺席时的主力，必须能剔噪声、保正文。"""

    def test_builtin_extracts_article(self, read_fixture: Any) -> None:
        result = extract_text(read_fixture("article.html"))
        assert result.strategy == "builtin"
        assert "第一段正文内容" in result.text
        assert "第二段正文内容" in result.text

    def test_builtin_drops_navigation_and_footer(self, read_fixture: Any) -> None:
        """导航、页脚、侧栏、脚本都不能混进正文。"""
        text = extract_text(read_fixture("article.html")).text
        for noise in ("首页导航", "关于我们", "站点大标题", "侧栏广告", "版权所有", "should-not-appear"):
            assert noise not in text, noise

    def test_builtin_keeps_word_spacing(self, read_fixture: Any) -> None:
        """内联标签不能把词语粘连。"""
        assert "加粗" in extract_text(read_fixture("article.html")).text

    def test_title_comes_from_title_tag(self, read_fixture: Any) -> None:
        assert extract_text(read_fixture("article.html")).title == "测试文章 - 示例站"

    def test_strategy_chain_prefers_earlier_entry(self) -> None:
        """链式降级：前面的策略有产出就不走后面的。"""
        calls: list[str] = []

        def first(html: str) -> tuple[str, str]:
            calls.append("first")
            return "T1", "来自 first 的正文"

        def second(html: str) -> tuple[str, str]:
            calls.append("second")
            return "T2", "来自 second 的正文"

        result = extract_text("<html></html>", strategies=[("first", first), ("second", second)])
        assert result.strategy == "first"
        assert result.text == "来自 first 的正文"
        assert calls == ["first"]

    def test_failing_strategy_falls_through(self) -> None:
        """单个策略抛异常必须让位，而不是让整次阅读失败。"""

        def broken(html: str) -> tuple[str, str]:
            raise RuntimeError("这个策略炸了")

        def fallback(html: str) -> tuple[str, str]:
            return "T", "兜底正文"

        result = extract_text("<html></html>", strategies=[("broken", broken), ("fallback", fallback)])
        assert result.strategy == "fallback"
        assert result.text == "兜底正文"

    def test_empty_strategy_does_not_win(self) -> None:
        """产出空文本的策略不算成功，否则会掩盖后面的可用策略。"""

        def empty(html: str) -> tuple[str, str]:
            return "T", "   "

        def real(html: str) -> tuple[str, str]:
            return "T", "真正文"

        assert extract_text("<html></html>", strategies=[("empty", empty), ("real", real)]).strategy == "real"

    def test_all_strategies_failing_reports_none(self) -> None:
        """全失败时返回空正文与 none，由上层给出可读提示。"""
        result = extract_text("<html></html>", strategies=[])
        assert result == ExtractionResult(title="", text="", strategy="none")

    def test_normalize_text_collapses_blank_lines(self) -> None:
        assert normalize_text("a\n\n\n\nb") == "a\n\nb"

    def test_builtin_is_always_available(self) -> None:
        """Tier-1 库是可选依赖，内置策略必须在任何环境都在链上。"""
        assert "builtin" in strategy_names()


# ------------------------------------------------------------------ 内容类型


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "text/html; charset=utf-8", "application/json", "text/plain", "", "application/xhtml+xml"],
)
def test_readable_content_types(content_type: str) -> None:
    """可读文本类型放行；空 Content-Type 也放行（不少站点不返回）。"""
    assert is_readable_content_type(content_type) is True


@pytest.mark.parametrize("content_type", ["image/png", "application/pdf", "video/mp4", "application/octet-stream"])
def test_unreadable_content_types(content_type: str) -> None:
    """二进制内容一律拒绝——图片有专门的通路。"""
    assert is_readable_content_type(content_type) is False


# ------------------------------------------------------------------ 读取与安全


async def test_reads_and_extracts(read_fixture: Any) -> None:
    """正常路径：抓取 → 抽取 → 返回正文。"""
    http = ScriptedHttp([_response(text=read_fixture("article.html"))])
    result = await read_url(ARTICLE_URL, http=http)
    assert "第一段正文内容" in result.text
    assert result.strategy == "builtin"
    assert result.hops == 0
    assert http.calls[0]["follow_redirects"] is False, "必须手动跟随重定向，才能逐跳校验"


async def test_follows_redirect_manually() -> None:
    """302 要跟随，并记下跳数。"""
    http = ScriptedHttp(
        [
            _response(status=302, headers={"location": f"http://{PUBLIC_IP}/final"}),
            _response(url=f"http://{PUBLIC_IP}/final", text="<html><body><p>最终页面正文</p></body></html>"),
        ]
    )
    result = await read_url(ARTICLE_URL, http=http)
    assert result.hops == 1
    assert result.url == f"http://{PUBLIC_IP}/final"
    assert "最终页面正文" in result.text


async def test_redirect_to_private_address_is_blocked() -> None:
    """**核心安全用例**：公网地址 302 到内网，必须在第二跳被拦下。"""
    http = ScriptedHttp([_response(status=302, headers={"location": "http://127.0.0.1/secret"})])
    with pytest.raises(BlockedAddressError):
        await read_url(ARTICLE_URL, http=http)
    assert len(http.calls) == 1, "被拦下后不应继续请求"


async def test_redirect_to_metadata_address_is_blocked() -> None:
    """云元数据地址是 SSRF 的头号目标。"""
    http = ScriptedHttp([_response(status=302, headers={"location": "http://169.254.169.254/latest/meta-data/"})])
    with pytest.raises(BlockedAddressError):
        await read_url(ARTICLE_URL, http=http)


async def test_too_many_redirects() -> None:
    """重定向链必须有上限。"""
    responses = [
        _response(status=302, headers={"location": f"http://{PUBLIC_IP}/hop{index}"}) for index in range(10)
    ]
    http = ScriptedHttp(responses)
    with pytest.raises(TooManyRedirectsError):
        await read_url(ARTICLE_URL, http=http, max_redirects=3)


async def test_private_target_is_blocked_upfront() -> None:
    """直连内网在第一跳就该被拒。"""
    http = ScriptedHttp([])
    with pytest.raises(BlockedAddressError):
        await read_url("http://10.0.0.5/admin", http=http)
    assert http.calls == []


async def test_unsupported_content_type_rejected() -> None:
    """PDF/图片等不应被当作正文读。"""
    http = ScriptedHttp([_response(content_type="application/pdf", text="%PDF-1.4")])
    with pytest.raises(UnsupportedContentTypeError):
        await read_url(ARTICLE_URL, http=http)


async def test_long_text_is_clipped() -> None:
    """正文按配置截断，并打上截断标记。"""
    body = "<html><body><article><p>" + "很长的正文。" * 200 + "</p></article></body></html>"
    http = ScriptedHttp([_response(text=body)])
    result = await read_url(ARTICLE_URL, http=http, max_content_length=100)
    assert len(result.text) <= 101
    assert result.truncated is True


async def test_special_source_is_used_and_clipped() -> None:
    """站点专用通路命中时不应发起网页抓取，且同样受长度约束。"""
    calls: list[str] = []

    async def fetch(url: str, http: Any) -> ReadingResult:
        calls.append(url)
        return ReadingResult(url=url, title="专用通路", text="专用正文" * 100, strategy="special")

    source = SpecialSource(name="fake", matches=lambda url: url.endswith("/special"), fetch=fetch)
    http = ScriptedHttp([])
    result = await read_url(f"http://{PUBLIC_IP}/special", http=http, special_sources=[source], max_content_length=50)

    assert calls == [f"http://{PUBLIC_IP}/special"]
    assert http.calls == [], "命中专用通路时不应再抓网页"
    assert result.strategy == "special"
    assert len(result.text) <= 51
    assert result.truncated is True


async def test_truncated_without_text_raises_content_too_large() -> None:
    """页面被体积上限截断且抽不出正文时，要给出明确原因而不是空结果。"""
    from mai_websearch_under_test.core.errors import ContentTooLargeError

    http = ScriptedHttp([_response(text="<html><body></body></html>", truncated=True)])
    with pytest.raises(ContentTooLargeError):
        await read_url(ARTICLE_URL, http=http)
