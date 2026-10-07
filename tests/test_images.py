"""图片通路测试：候选解析、过滤、去重、下载回退、管线状态。

核心取舍写在断言里：**缩略图优先**（``send.image`` 只接受 base64，下载不可避免）、
**三层去重**（URL / 内容哈希 / 重复窗口）、**下载失败要顺延下一张候选**。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mai_websearch_under_test.core.errors import NetworkError, ProviderError
from mai_websearch_under_test.core.http import FetchBytesResult
from mai_websearch_under_test.images.picker import (
    ImageHistory,
    ImagePicker,
    filter_candidates,
)
from mai_websearch_under_test.images.pipeline import ImageSearchPipeline
from mai_websearch_under_test.images.providers.bing_images import BingImagesProvider, parse_bing_images
from mai_websearch_under_test.images.providers.searxng_images import (
    SearxngImagesProvider,
    parse_searxng_images,
)
from mai_websearch_under_test.images.types import ImageCandidate, sniff_image_mime

# 1×1 PNG：足够用来验证魔数嗅探
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
TINY_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


# ------------------------------------------------------------------ 解析


class TestBingImagesParsing:
    """Bing 把结果 JSON 塞在 ``a.iusc`` 的 ``m`` 属性里。"""

    def test_parses_original_and_thumbnail(self, read_fixture: Any) -> None:
        candidates = parse_bing_images(read_fixture("bing_images.html"))
        assert len(candidates) == 2
        assert candidates[0].url == "https://img.example.com/cat-original.jpg"
        assert candidates[0].thumbnail_url == "https://tse.example.com/cat-thumb.jpg"
        assert candidates[0].title == "一只布偶猫"
        assert candidates[0].page_url == "https://page.example.com/cat"

    def test_handles_html_escaped_attribute(self, read_fixture: Any) -> None:
        """属性里的 ``&quot;`` 必须能被解回来（不同 lexbor 版本行为不同）。"""
        candidates = parse_bing_images(read_fixture("bing_images.html"))
        assert all(candidate.url for candidate in candidates)

    def test_skips_entries_without_original(self, read_fixture: Any) -> None:
        """只有缩略图的条目要跳过——原图缺失时无法确保质量。"""
        candidates = parse_bing_images(read_fixture("bing_images.html"))
        assert "no-original" not in " ".join(candidate.url for candidate in candidates)

    def test_skips_invalid_json(self, read_fixture: Any) -> None:
        """``m`` 不是 JSON 时不能抛异常，跳过即可。"""
        assert len(parse_bing_images(read_fixture("bing_images.html"))) == 2

    def test_returns_empty_on_antibot_shell(self) -> None:
        assert parse_bing_images("<html><body>请完成验证</body></html>") == []

    def test_build_url_carries_query_and_safe_search(self) -> None:
        url = BingImagesProvider(safe_search=True).build_url("布偶猫", limit=5)
        assert "%E5%B8%83%E5%81%B6%E7%8C%AB" in url
        assert "adlt=strict" in url
        assert "adlt=off" in BingImagesProvider(safe_search=False).build_url("x")

    def test_prepare_does_not_quote(self) -> None:
        """图片搜索加引号会显著减少结果。"""
        assert BingImagesProvider().prepare("布偶猫 可爱") == "布偶猫 可爱"

    async def test_raises_when_nothing_parsed(self) -> None:
        class FakeHttp:
            async def fetch(self, *args: Any, **kwargs: Any) -> Any:
                return type("R", (), {"text": "<html></html>"})()

        with pytest.raises(ProviderError):
            await BingImagesProvider().search("cat", FakeHttp())


class TestSearxngImagesParsing:
    def test_parses_candidates(self, read_fixture: Any) -> None:
        candidates = parse_searxng_images(read_fixture("searxng_images.json"))
        assert len(candidates) == 2  # 缺 img_src 的被跳过
        assert candidates[0].url == "https://img.example.com/a.png"
        assert candidates[0].thumbnail_url == "https://thumb.example.com/a.png"
        assert candidates[0].width == 800

    def test_parses_string_dimensions(self, read_fixture: Any) -> None:
        """SearXNG 有时把宽高返回成字符串。"""
        candidates = parse_searxng_images(read_fixture("searxng_images.json"))
        assert candidates[1].width == 120
        assert candidates[1].height == 80

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ProviderError, match="JSON"):
            parse_searxng_images("<html>not json</html>")

    def test_missing_results_raises(self) -> None:
        with pytest.raises(ProviderError, match="results"):
            parse_searxng_images('{"error":"format not enabled"}')

    def test_build_url_uses_images_category(self) -> None:
        url = SearxngImagesProvider(base_url="http://127.0.0.1:8888").build_url("cat")
        assert "categories=images" in url
        assert "format=json" in url

    async def test_not_configured_raises(self) -> None:
        with pytest.raises(ProviderError, match="未配置"):
            await SearxngImagesProvider(base_url="").search("cat", object())


# ------------------------------------------------------------------ 过滤与嗅探


class TestFilterCandidates:
    def test_drops_too_small_declared_size(self) -> None:
        candidates = [
            ImageCandidate(url="https://a/1.png", width=64, height=64),
            ImageCandidate(url="https://a/2.png", width=800, height=600),
        ]
        kept = filter_candidates(candidates, min_width=300, min_height=200)
        assert [candidate.url for candidate in kept] == ["https://a/2.png"]

    def test_keeps_unknown_size(self) -> None:
        """尺寸未知（0）时不预判，交给下载后的体积兜底。"""
        kept = filter_candidates([ImageCandidate(url="https://a/1.png")], min_width=300, min_height=200)
        assert len(kept) == 1

    def test_drops_empty_url(self) -> None:
        assert filter_candidates([ImageCandidate(url="")]) == []


class TestSniffImageMime:
    """不少 CDN 把图片标成 octet-stream，必须靠内容兜底。"""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (TINY_PNG, "image/png"),
            (TINY_JPEG, "image/jpeg"),
            (b"GIF89a" + b"\x00" * 16, "image/gif"),
            (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8, "image/webp"),
            (b"<html>not an image</html>", None),
            (b"", None),
        ],
    )
    def test_detects_by_magic(self, content: bytes, expected: str | None) -> None:
        assert sniff_image_mime(content) == expected


# ------------------------------------------------------------------ 重复窗口


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestImageHistory:
    def test_remembers_within_window(self) -> None:
        clock = _FakeClock()
        history = ImageHistory(window_seconds=60, clock=clock)
        assert history.is_recent("猫", "hash1") is False
        history.remember("猫", "hash1")
        assert history.is_recent("猫", "hash1") is True
        assert history.is_recent("狗", "hash1") is False, "不同关键词互不影响"

    def test_expires_after_window(self) -> None:
        clock = _FakeClock()
        history = ImageHistory(window_seconds=60, clock=clock)
        history.remember("猫", "hash1")
        clock.advance(61)
        assert history.is_recent("猫", "hash1") is False

    def test_zero_window_disables(self) -> None:
        history = ImageHistory(window_seconds=0)
        history.remember("猫", "hash1")
        assert history.is_recent("猫", "hash1") is False

    def test_bounded_query_count(self) -> None:
        """关键词数量必须有上限，否则长时间运行会内存泄漏。"""
        history = ImageHistory(window_seconds=60, max_queries=3)
        for index in range(20):
            history.remember(f"q{index}", f"h{index}")
        assert history.tracked_queries <= 3


# ------------------------------------------------------------------ 下载


class FakeImageHttp:
    """按 URL 返回字节或抛异常。"""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.keys: list[str] = []

    async def fetch_bytes(self, url: str, **kwargs: Any) -> FetchBytesResult:
        self.calls.append(url)
        self.keys.append(str(kwargs.get("breaker_key", "")))
        route = self.routes.get(url)
        if route is None:
            raise NetworkError("没有预设路由")
        if isinstance(route, Exception):
            raise route
        content, content_type, truncated = route
        return FetchBytesResult(
            status=200,
            url=url,
            content_type=content_type,
            content=content,
            truncated=truncated,
            elapsed_ms=1,
        )


def _picker(http: Any, **overrides: Any) -> ImagePicker:
    options: dict[str, Any] = {
        "http": http,
        "history": ImageHistory(window_seconds=0),
        "max_bytes": 1048576,
        "min_width": 0,
        "min_height": 0,
        "prefer_thumbnail": True,
        "timeout_seconds": 5.0,
    }
    options.update(overrides)
    return ImagePicker(**options)


class TestImagePicker:
    """``send.image`` 只接受 base64，所以下载是必经之路，回退逻辑必须稳。"""

    async def test_prefers_thumbnail(self) -> None:
        """缩略图优先是图片通路最大的一笔延迟优化。"""
        candidate = ImageCandidate(
            url="https://img.example.com/original.png",
            thumbnail_url="https://tse.example.com/thumb.png",
        )
        http = FakeImageHttp(
            {
                "https://tse.example.com/thumb.png": (TINY_PNG, "image/png", False),
                "https://img.example.com/original.png": (TINY_PNG * 100, "image/png", False),
            }
        )
        result = await _picker(http).pick("猫", [candidate])
        assert len(result.images) == 1
        assert result.images[0].source_url == "https://tse.example.com/thumb.png"
        assert http.calls == ["https://tse.example.com/thumb.png"], "不应下载原图"

    async def test_falls_back_to_original(self) -> None:
        """缩略图挂了要顺延到原图，而不是整条候选作废。"""
        candidate = ImageCandidate(
            url="https://img.example.com/original.png",
            thumbnail_url="https://tse.example.com/thumb.png",
        )
        http = FakeImageHttp(
            {
                "https://tse.example.com/thumb.png": NetworkError("缩略图 404"),
                "https://img.example.com/original.png": (TINY_PNG, "image/png", False),
            }
        )
        result = await _picker(http).pick("猫", [candidate])
        assert len(result.images) == 1
        assert result.images[0].source_url == "https://img.example.com/original.png"

    async def test_accepts_mislabeled_octet_stream(self) -> None:
        """CDN 常常把图片标成 octet-stream，靠魔数救回来。"""
        candidate = ImageCandidate(url="https://img.example.com/a.png")
        http = FakeImageHttp({"https://img.example.com/a.png": (TINY_PNG, "application/octet-stream", False)})
        result = await _picker(http).pick("猫", [candidate])
        assert result.images[0].mime_type == "image/png"

    async def test_rejects_html_error_page(self) -> None:
        """有的图床失败时返回 HTML 页面，不能当图片发出去。"""
        candidate = ImageCandidate(url="https://img.example.com/a.png")
        http = FakeImageHttp({"https://img.example.com/a.png": (b"<html>403</html>", "text/html", False)})
        result = await _picker(http).pick("猫", [candidate])
        assert result.images == []
        assert result.failures

    async def test_rejects_oversized(self) -> None:
        """超过体积上限的图片要跳过（截断的图片发出去是坏的）。"""
        candidate = ImageCandidate(url="https://img.example.com/big.png")
        http = FakeImageHttp({"https://img.example.com/big.png": (TINY_PNG, "image/png", True)})
        result = await _picker(http).pick("猫", [candidate])
        assert result.images == []
        assert "字节" in result.failures[0]

    async def test_dedupes_same_url_with_tracking_params(self) -> None:
        """同一张图的跟踪参数不同，不应被下两次。"""
        candidates = [
            ImageCandidate(url="https://img.example.com/a.png?utm_source=x"),
            ImageCandidate(url="https://img.example.com/a.png"),
        ]
        http = FakeImageHttp({"https://img.example.com/a.png?utm_source=x": (TINY_PNG, "image/png", False)})
        result = await _picker(http).pick("猫", candidates)
        assert len(result.images) == 1
        assert len(http.calls) == 1

    async def test_skips_recently_sent(self) -> None:
        """同一个关键词短期内不重复发同一张图。"""
        candidate = ImageCandidate(url="https://img.example.com/a.png")
        http = FakeImageHttp({"https://img.example.com/a.png": (TINY_PNG, "image/png", False)})
        picker = _picker(http, history=ImageHistory(window_seconds=60))

        first = await picker.pick("猫", [candidate])
        second = await picker.pick("猫", [candidate])

        assert len(first.images) == 1
        assert second.images == []
        assert second.skipped_repeats == 1

    async def test_respects_limit(self) -> None:
        candidates = [ImageCandidate(url=f"https://img.example.com/{i}.png") for i in range(5)]
        http = FakeImageHttp(
            {f"https://img.example.com/{i}.png": (TINY_PNG + bytes([i]), "image/png", False) for i in range(5)}
        )
        result = await _picker(http).pick("猫", candidates, limit=2)
        assert len(result.images) == 2

    async def test_failure_does_not_stop_later_candidates(self) -> None:
        candidates = [
            ImageCandidate(url="https://img.example.com/bad.png"),
            ImageCandidate(url="https://img.example.com/good.png"),
        ]
        http = FakeImageHttp(
            {
                "https://img.example.com/bad.png": NetworkError("挂了"),
                "https://img.example.com/good.png": (TINY_PNG, "image/png", False),
            }
        )
        result = await _picker(http).pick("猫", candidates)
        assert len(result.images) == 1
        assert result.failures

    async def test_breaker_key_is_scoped_per_host(self) -> None:
        """熔断键必须按主机取。

        实测 Bing 的缩略图 CDN（``ts*.mm.bing.net``）在部分网络下直接 ConnectError；
        如果整个图片通路共用一个熔断键，一次 CDN 故障会连累原图下载。
        """
        candidate = ImageCandidate(
            url="https://img.example.com/original.png",
            thumbnail_url="https://tse.example.com/thumb.png",
        )
        http = FakeImageHttp(
            {
                "https://tse.example.com/thumb.png": NetworkError("CDN 不可达"),
                "https://img.example.com/original.png": (TINY_PNG, "image/png", False),
            }
        )
        await _picker(http).pick("猫", [candidate])
        assert http.keys == ["image:tse.example.com", "image:img.example.com"]


# ------------------------------------------------------------------ 管线


class FakeImageProvider:
    def __init__(
        self,
        name: str,
        *,
        candidates: tuple[ImageCandidate, ...] = (),
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self._candidates = list(candidates)
        self._error = error
        self._delay = delay
        self.calls = 0

    async def search(self, query: str, http: Any, *, limit: int = 10) -> list[ImageCandidate]:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return list(self._candidates)


def _pipeline(providers: list[FakeImageProvider], http: Any, **overrides: Any) -> ImageSearchPipeline:
    options: dict[str, Any] = {
        "http": http,
        "providers": providers,
        "picker": _picker(http),
        "deadline_seconds": 5.0,
        "grace_seconds": 0.05,
    }
    options.update(overrides)
    return ImageSearchPipeline(**options)


def _ok_http() -> FakeImageHttp:
    return FakeImageHttp({"https://img.example.com/a.png": (TINY_PNG, "image/png", False)})


class TestImagePipeline:
    async def test_ok_status(self) -> None:
        provider = FakeImageProvider("bing_images", candidates=(ImageCandidate(url="https://img.example.com/a.png"),))
        outcome = await _pipeline([provider], _ok_http()).search("猫")
        assert outcome.status == "ok"
        assert outcome.ok is True
        assert len(outcome.images) == 1
        assert outcome.engine_status["bing_images"] == "ok:1"

    async def test_no_results_when_all_empty(self) -> None:
        """所有源都正常但没有候选。"""
        outcome = await _pipeline([FakeImageProvider("bing_images")], _ok_http()).search("猫")
        assert outcome.status == "no_results"
        assert outcome.ok is False

    async def test_all_failed_when_all_raise(self) -> None:
        """所有源都失败时必须明确区分于"没找到"。"""
        providers = [
            FakeImageProvider("bing_images", error=ProviderError("bing_images", "被反爬")),
            FakeImageProvider("searxng_images", error=ProviderError("searxng_images", "超时")),
        ]
        outcome = await _pipeline(providers, _ok_http()).search("猫")
        assert outcome.status == "all_failed"
        assert all(text.startswith("失败") for text in outcome.engine_status.values())

    async def test_no_unique_when_everything_repeats(self) -> None:
        provider = FakeImageProvider("bing_images", candidates=(ImageCandidate(url="https://img.example.com/a.png"),))
        http = _ok_http()
        pipeline = _pipeline([provider], http, picker=_picker(http, history=ImageHistory(window_seconds=60)))

        assert (await pipeline.search("猫")).status == "ok"
        second = await pipeline.search("猫")
        assert second.status == "no_unique"
        assert second.skipped_repeats == 1

    async def test_candidate_order_follows_provider_declaration(self) -> None:
        """完成顺序不确定，但合并顺序必须稳定（否则结果不可复现）。"""
        slow = FakeImageProvider(
            "slow",
            candidates=(ImageCandidate(url="https://img.example.com/a.png"),),
            delay=0.05,
        )
        fast = FakeImageProvider(
            "fast",
            candidates=(ImageCandidate(url="https://img.example.com/b.png"),),
        )
        http = FakeImageHttp(
            {
                "https://img.example.com/a.png": (TINY_PNG, "image/png", False),
                "https://img.example.com/b.png": (TINY_PNG + b"\x01", "image/png", False),
            }
        )
        outcome = await _pipeline([slow, fast], http, picker=_picker(http)).search("猫", limit=2)
        assert [image.source_url for image in outcome.images] == [
            "https://img.example.com/a.png",
            "https://img.example.com/b.png",
        ]

    async def test_no_providers_is_all_failed(self) -> None:
        outcome = await _pipeline([], _ok_http()).search("猫")
        assert outcome.status == "all_failed"
