"""SauceNAO 反查测试（离线，用固定响应）。

⚠️ 本 provider 的**联网行为在本项目实测网络下无法验证**（域名不可达），
所以这里覆盖的是"按官方文档实现的解析与请求构造"——这部分是可验证的。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mai_websearch_under_test.core.errors import ProviderError
from mai_websearch_under_test.images.inbound import InboundImage
from mai_websearch_under_test.images.reverse import ReverseLookupResult
from mai_websearch_under_test.images.reverse.saucenao import SaucenaoProvider, parse_saucenao


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeHttp:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.text)


# ------------------------------------------------------------------ 解析


class TestParseSaucenao:
    def test_parses_and_sorts_by_similarity(self, read_fixture: Any) -> None:
        result = parse_saucenao(read_fixture("saucenao.json"), min_similarity=50.0)
        assert result.ok is True
        assert [round(source.similarity, 1) for source in result.sources] == [92.2, 88.0]

    def test_maps_fields(self, read_fixture: Any) -> None:
        result = parse_saucenao(read_fixture("saucenao.json"), min_similarity=50.0)
        top = result.sources[0]
        assert top.title == "初音ミク"
        assert top.author == "画师A"  # member_name 兜底
        assert top.index == "Pixiv"
        assert top.urls == ["https://www.pixiv.net/artworks/123"]

    def test_author_falls_back_to_author_name(self, read_fixture: Any) -> None:
        """不同图库的字段名不一样（member_name / author_name / creator）。"""
        result = parse_saucenao(read_fixture("saucenao.json"), min_similarity=50.0)
        assert result.sources[1].author == "画师C"

    def test_filters_low_similarity(self, read_fixture: Any) -> None:
        assert len(parse_saucenao(read_fixture("saucenao.json"), min_similarity=90.0).sources) == 1

    def test_keeps_all_when_threshold_zero(self, read_fixture: Any) -> None:
        assert len(parse_saucenao(read_fixture("saucenao.json"), min_similarity=0.0).sources) == 3

    def test_nonzero_status_becomes_error_not_exception(self) -> None:
        """配额用尽之类是**预期内的失败**，应作为可见的错误项返回。"""
        payload = json.dumps({"header": {"status": -2}, "results": []})
        result = parse_saucenao(payload)
        assert result.ok is False
        assert "配额" in result.error

    def test_unknown_status_mentions_code(self) -> None:
        payload = json.dumps({"header": {"status": -99}, "results": []})
        assert "-99" in parse_saucenao(payload).error

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ProviderError, match="JSON"):
            parse_saucenao("<html>blocked</html>")

    def test_missing_results_raises(self) -> None:
        with pytest.raises(ProviderError, match="results"):
            parse_saucenao('{"header":{"status":0}}')

    def test_tolerates_missing_data_fields(self) -> None:
        """字段缺失是常态，不能让整次反查失败。"""
        payload = json.dumps(
            {"header": {"status": 0}, "results": [{"header": {"similarity": "70"}, "data": {}}]}
        )
        result = parse_saucenao(payload)
        assert len(result.sources) == 1
        assert result.sources[0].title == ""
        assert result.sources[0].similarity == 70.0

    def test_empty_results_is_not_an_error(self) -> None:
        payload = json.dumps({"header": {"status": 0}, "results": []})
        result = parse_saucenao(payload)
        assert result.ok is False
        assert result.error == ""


# ------------------------------------------------------------------ provider


class TestSaucenaoProvider:
    def test_requires_api_key(self) -> None:
        assert SaucenaoProvider(api_key="").configured is False
        assert SaucenaoProvider(api_key="abc").configured is True

    def test_query_url_carries_required_params(self) -> None:
        url = SaucenaoProvider(api_key="k", databases="999").build_query_url("https://img/x.jpg")
        assert "output_type=2" in url
        assert "api_key=k" in url
        assert "db=999" in url
        assert "url=https%3A%2F%2Fimg%2Fx.jpg" in url

    async def test_lookup_without_key_raises(self) -> None:
        with pytest.raises(ProviderError, match="API Key"):
            await SaucenaoProvider(api_key="").lookup(InboundImage(source="t", url="https://x/1.png"), FakeHttp())

    async def test_lookup_prefers_url_over_upload(self, read_fixture: Any) -> None:
        """有图片地址就用 URL 反查，省掉一次 multipart 上传。"""
        http = FakeHttp(read_fixture("saucenao.json"))
        image = InboundImage(source="t", url="https://img/x.jpg", content=b"bytes")
        result = await SaucenaoProvider(api_key="k").lookup(image, http)

        assert result.ok is True
        call = http.calls[0]
        assert "url=" in call["url"], "应当把图片地址带进查询串"
        assert call.get("method") != "POST", "有地址时不该走上传"

    async def test_lookup_uploads_when_no_url(self, read_fixture: Any) -> None:
        """平台 URL 有时效，只有字节时必须走 multipart 上传。"""
        http = FakeHttp(read_fixture("saucenao.json"))
        image = InboundImage(source="t", content=b"image-bytes", mime_type="image/png")
        result = await SaucenaoProvider(api_key="k").lookup(image, http)

        assert result.ok is True
        call = http.calls[0]
        assert call["method"] == "POST"
        assert call["data"]["output_type"] == "2"
        assert "file" in call["files"]

    async def test_lookup_with_neither_raises(self) -> None:
        with pytest.raises(ProviderError, match="既没有"):
            await SaucenaoProvider(api_key="k").lookup(InboundImage(source="t"), FakeHttp())

    async def test_passes_breaker_key(self, read_fixture: Any) -> None:
        http = FakeHttp(read_fixture("saucenao.json"))
        await SaucenaoProvider(api_key="k").lookup(InboundImage(source="t", url="https://x/1.png"), http)
        assert http.calls[0]["breaker_key"] == "saucenao"


def test_reverse_lookup_result_ok_semantics() -> None:
    """只有拿到来源才算 ok；带错误信息的结果不算。"""
    assert ReverseLookupResult(engine="x").ok is False
    assert ReverseLookupResult(engine="x", error="失败了").ok is False
