"""以图搜源引擎的离线测试（三个免密钥 provider + 装配）。

**重要**：这些引擎的域名在本项目实测网络下不可达，**解析器的真实 DOM 结构未经实网验证**。
这里的用例是按各站公开结构写的**合成夹具**，作用是：
1. 保证解析逻辑本身没写错（选择器、字段映射、容错、排序）；
2. 保证"认不出结构就返回空、不抛异常"这条底线成立。
换到可达网络后用 ``python tests/live_probe.py`` 校准解析器。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mai_websearch_under_test.config import WebSearchConfig
from mai_websearch_under_test.core.errors import ProviderError
from mai_websearch_under_test.images.inbound import InboundImage
from mai_websearch_under_test.images.reverse import build_reverse_providers
from mai_websearch_under_test.images.reverse.ascii2d import Ascii2dProvider, parse_ascii2d
from mai_websearch_under_test.images.reverse.bing_visual import BingVisualProvider, parse_bing_visual
from mai_websearch_under_test.images.reverse.iqdb import IqdbProvider, parse_iqdb
from mai_websearch_under_test.images.reverse.yandex import YandexProvider, parse_yandex

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


class FakeResponse:
    def __init__(self, text: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.text = text
        self.status = status
        self.headers = headers or {}
        self.content_type = "text/html"


class FakeHttp:
    def __init__(self, text: str = "", *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._text = text
        self._status = status
        self._headers = headers
        self.calls: list[dict[str, Any]] = []

    async def fetch(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self._text, self._status, self._headers)


# 合成夹具：按各站公开结构手写，非抓取所得
ASCII2D_HTML = """
<html><body>
<div class="row">
  <div class="item-box">
    <a href="https://www.pixiv.net/artworks/999">
      <img src="https://img1.pixiv.net/img-master/1.jpg" />
    </a>
    <p class="title">初音ミク 立ち絵</p>
    <div class="similarity"><span>92.15</span>%</div>
  </div>
  <div class="item-box">
    <a href="https://danbooru.donmai.us/posts/1234">
      <img src="//img.donmai.us/thumb/1.jpg" />
    </a>
    <p class="title">hatsune miku (原创)</p>
    <div class="similarity"><span>71.30</span>%</div>
  </div>
</div>
</body></html>
"""

IQDB_HTML = """
<html><body><div id="pages">
  <div class="result">
    <span class="image"><a href="https://iqdb.org/hit/1"><img src="/thumbs/1.jpg"></a></span>
    <span class="info">
      <span class="similarity">95.20%</span>
      <span class="source"><a href="https://gelbooru.com/index.php?page=post&amp;s=view&amp;id=5">Gelbooru</a></span>
    </span>
  </div>
  <div class="result">
    <span class="image"><a href="https://iqdb.org/hit/2"><img src="/thumbs/2.jpg"></a></span>
    <span class="info">
      <span class="similarity">55.10%</span>
      <span class="source"><a href="https://konachan.net/post/9">Konachan</a></span>
    </span>
  </div>
</div></body></html>
"""

YANDEX_BEM = json.dumps(
    {
        "serp-list": {
            "serp-item": [
                {
                    "data": {
                        "dups": [{"origUrl": "https://www.pixiv.net/artworks/77", "title": "Miku fanart"}]
                    }
                }
            ]
        }
    }
)
YANDEX_HTML = f'<html><body><div class="serp-item" data-bem=\'{YANDEX_BEM}\'></div></body></html>'

BING_HTML = (
    "<html><head><script>IG(\"insights\", "
    + json.dumps(
        {
            "PagesIncluding": [
                {"contentUrl": "https://example.com/post-a", "name": "来源页 A", "thumbnailUrl": "https://t/a.jpg"},
                {"contentUrl": "https://example.org/post-b", "name": "来源页 B"},
            ]
        }
    )
    + ");</script></head><body></body></html>"
)


# ------------------------------------------------------------------ ascii2d


class TestAscii2d:
    def test_parses_items(self) -> None:
        result = parse_ascii2d(ASCII2D_HTML)
        assert result.ok is True
        assert len(result.sources) == 2

    def test_sorts_by_similarity(self) -> None:
        result = parse_ascii2d(ASCII2D_HTML)
        assert result.sources[0].similarity > result.sources[1].similarity

    def test_maps_title_and_url(self) -> None:
        top = parse_ascii2d(ASCII2D_HTML).sources[0]
        assert "初音" in top.title
        assert top.urls[0].startswith("https://www.pixiv.net/")

    def test_makes_protocol_relative_thumbnail_absolute(self) -> None:
        second = parse_ascii2d(ASCII2D_HTML).sources[1]
        assert second.thumbnail.startswith("https://")

    def test_unknown_layout_returns_empty(self) -> None:
        assert parse_ascii2d("<html><body>nothing</body></html>").sources == []

    def test_garbage_does_not_raise(self) -> None:
        for junk in ("", "not html", "<div class='item-box'></div>"):
            assert parse_ascii2d(junk).ok in (True, False)


class TestAscii2dProvider:
    def test_keyless_always_configured(self) -> None:
        assert Ascii2dProvider().configured is True
        assert Ascii2dProvider().requires_key is False

    def test_build_url_encodes(self) -> None:
        url = Ascii2dProvider().build_url("https://img/x.jpg?a=1")
        assert url.startswith("https://ascii2d.net/search/url/")
        assert "%3A%2F%2F" in url

    async def test_prefers_url_over_upload(self) -> None:
        http = FakeHttp(ASCII2D_HTML)
        await Ascii2dProvider().lookup(InboundImage(source="t", url="https://x/1.png", content=PNG), http)
        # 有地址时走 GET（免上传），因此没有显式 method 键
        assert http.calls[0].get("method", "GET") == "GET"
        assert http.calls[0]["url"].startswith("https://ascii2d.net/search/url/")

    async def test_uploads_when_no_url(self) -> None:
        http = FakeHttp(ASCII2D_HTML)
        await Ascii2dProvider().lookup(InboundImage(source="t", content=PNG), http)
        assert http.calls[0]["method"] == "POST"
        assert "file" in http.calls[0]["files"]

    async def test_requires_something(self) -> None:
        with pytest.raises(ProviderError):
            await Ascii2dProvider().lookup(InboundImage(source="t"), FakeHttp())


# ------------------------------------------------------------------ IQDB


class TestIqdb:
    def test_parses_results(self) -> None:
        result = parse_iqdb(IQDB_HTML)
        assert result.ok is True
        assert len(result.sources) == 2

    def test_reads_similarity_and_source(self) -> None:
        top = parse_iqdb(IQDB_HTML).sources[0]
        assert top.similarity == 95.2
        assert "Gelbooru" in top.index
        assert "gelbooru.com" in top.urls[0]

    def test_drops_iqdb_internal_links(self) -> None:
        """内部详情页不是来源，要过滤掉。"""
        for source in parse_iqdb(IQDB_HTML).sources:
            assert "iqdb.org" not in source.urls[0]

    def test_unknown_layout_returns_empty(self) -> None:
        assert parse_iqdb("<html>empty</html>").sources == []


class TestIqdbProvider:
    async def test_prefers_file_upload(self) -> None:
        """IQDB 用文件提交最稳；有字节时优先上传。"""
        http = FakeHttp(IQDB_HTML)
        await IqdbProvider().lookup(InboundImage(source="t", content=PNG), http)
        assert http.calls[0]["method"] == "POST"
        assert "file" in http.calls[0]["files"]

    async def test_falls_back_to_url(self) -> None:
        http = FakeHttp(IQDB_HTML)
        await IqdbProvider().lookup(InboundImage(source="t", url="https://x/1.png"), http)
        assert http.calls[0]["data"] == {"url": "https://x/1.png"}

    async def test_requires_something(self) -> None:
        with pytest.raises(ProviderError):
            await IqdbProvider().lookup(InboundImage(source="t"), FakeHttp())


# ------------------------------------------------------------------ Yandex


class TestYandex:
    def test_parses_data_bem(self) -> None:
        result = parse_yandex(YANDEX_HTML)
        assert result.ok is True
        assert any("pixiv" in url for source in result.sources for url in source.urls)

    def test_falls_back_to_raw_urls(self) -> None:
        html = '<html><script>var t = {"origUrl":"https://fallback.example/post"};</script></html>'
        result = parse_yandex(html)
        assert result.ok is True
        assert "fallback.example" in result.sources[0].urls[0]

    def test_shell_page_returns_empty(self) -> None:
        """反爬壳页面不应被当成有结果，也不该抛异常。"""
        assert parse_yandex("<html><body>please enable js</body></html>").sources == []

    async def test_url_only_engine(self) -> None:
        with pytest.raises(ProviderError):
            await YandexProvider().lookup(InboundImage(source="t", content=PNG), FakeHttp())


# ------------------------------------------------------------------ Bing 视觉


class TestBingVisual:
    def test_parses_pages_including(self) -> None:
        result = parse_bing_visual(BING_HTML)
        assert result.ok is True
        assert any("post-a" in url for source in result.sources for url in source.urls)

    def test_detects_anti_bot_page(self) -> None:
        """没有视觉搜索特征时应明确报错，而不是"假装搜到了"。"""
        result = parse_bing_visual("<html><body>robot check</body></html>")
        assert result.ok is False
        assert "反爬" in result.error


class TestBingVisualProvider:
    async def test_missing_redirect_is_visible_error(self) -> None:
        http = FakeHttp("", headers={})
        result = await BingVisualProvider().lookup(InboundImage(source="t", content=PNG), http)
        assert result.ok is False
        assert "跳转" in result.error

    async def test_redirect_without_token_is_rejected(self) -> None:
        http = FakeHttp("", headers={"location": "https://www.bing.com/images/search?q=cat"})
        result = await BingVisualProvider().lookup(InboundImage(source="t", content=PNG), http)
        assert result.ok is False
        assert "insightsToken" in result.error

    async def test_requires_something(self) -> None:
        with pytest.raises(ProviderError):
            await BingVisualProvider().lookup(InboundImage(source="t"), FakeHttp())


# ------------------------------------------------------------------ 装配


class TestRegistry:
    def test_keyless_engines_enabled_by_default(self) -> None:
        names = {provider.name for provider in build_reverse_providers(WebSearchConfig())}
        assert {"ascii2d", "iqdb", "bing-visual"} <= names

    def test_yandex_opt_in(self) -> None:
        """Yandex 对脚本常需会话，默认关闭。"""
        assert "yandex" not in {p.name for p in build_reverse_providers(WebSearchConfig())}

    def test_saucenao_opt_in_and_kept_even_without_key(self) -> None:
        """开了但没填 Key 也要保留，让它真实报出"未配置 Key"。"""
        config = WebSearchConfig()
        config.reverse.saucenao_enabled = True
        providers = {p.name: p for p in build_reverse_providers(config)}
        assert "saucenao" in providers
        assert providers["saucenao"].configured is False

    def test_disabling_an_engine_removes_it(self) -> None:
        config = WebSearchConfig()
        config.reverse.ascii2d_enabled = False
        assert "ascii2d" not in {p.name for p in build_reverse_providers(config)}
