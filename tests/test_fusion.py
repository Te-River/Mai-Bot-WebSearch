"""融合测试：host+path 去重、加权 RRF 排序、确定性。"""

from __future__ import annotations

from mai_websearch_under_test.search.fusion import DEFAULT_WEIGHTS, fuse
from mai_websearch_under_test.search.types import SearchHit, host_path_key


def _hit(url: str, *, title: str = "标题", snippet: str = "", engine: str = "bing") -> SearchHit:
    return SearchHit(title=title, url=url, snippet=snippet, engine=engine)


class TestHostPathKey:
    """去重键必须忽略跟踪参数与 www 前缀，否则同一页面会被算成多条。"""

    def test_ignores_query_and_scheme(self) -> None:
        assert host_path_key("https://example.com/a?utm_source=x") == host_path_key("http://example.com/a")

    def test_ignores_www_prefix(self) -> None:
        assert host_path_key("https://www.example.com/a") == host_path_key("https://example.com/a")

    def test_ignores_trailing_slash(self) -> None:
        assert host_path_key("https://example.com/a/") == host_path_key("https://example.com/a")

    def test_distinguishes_different_paths(self) -> None:
        assert host_path_key("https://example.com/a") != host_path_key("https://example.com/b")


def test_merges_same_page_from_multiple_engines() -> None:
    """同一页面的标题/摘要应合并，并记录所有命中它的引擎。"""
    fused = fuse(
        {
            "bing": [_hit("https://www.example.com/a?utm_source=x", title="T", snippet="", engine="bing")],
            "duckduckgo": [_hit("https://example.com/a", title="T", snippet="S2", engine="duckduckgo")],
        },
        weights={"bing": 1.0, "duckduckgo": 1.0},
        limit=5,
    )
    assert len(fused) == 1
    assert fused[0].extra["engines"] == "bing,duckduckgo"
    assert fused[0].snippet == "S2"  # 空摘要被另一引擎补齐


def test_weight_decides_order_between_engines() -> None:
    """权重高的一方排在前面。"""
    hits = {
        "bing": [_hit("https://b.example/1", engine="bing")],
        "duckduckgo": [_hit("https://d.example/1", engine="duckduckgo")],
    }
    assert fuse(hits, weights={"bing": 1.0, "duckduckgo": 0.1}, limit=5)[0].engine == "bing"
    assert fuse(hits, weights={"bing": 0.1, "duckduckgo": 1.0}, limit=5)[0].engine == "duckduckgo"


def test_rank_order_follows_engine_ranking() -> None:
    """同一引擎内，排名靠前的保持靠前，并写入最终 rank。"""
    hits = [_hit(f"https://e.example/{index}", title=f"T{index}") for index in range(3)]
    fused = fuse({"bing": hits}, weights={"bing": 1.0}, limit=3)
    assert [hit.url for hit in fused] == [f"https://e.example/{index}" for index in range(3)]
    assert [hit.rank for hit in fused] == [1, 2, 3]


def test_agreement_beats_a_single_first_place() -> None:
    """两个引擎都给出的结果，应胜过只有单个引擎排第一的结果——这就是融合的价值。"""
    fused = fuse(
        {
            "bing": [
                _hit("https://only.example/y", title="Y", engine="bing"),
                _hit("https://shared.example/x", title="X", engine="bing"),
            ],
            "duckduckgo": [_hit("https://shared.example/x", title="X", engine="duckduckgo")],
        },
        weights={"bing": 1.0, "duckduckgo": 1.0},
        limit=5,
    )
    assert fused[0].url == "https://shared.example/x"


def test_respects_limit() -> None:
    """裁剪到请求条数。"""
    hits = [_hit(f"https://e.example/{index}") for index in range(10)]
    assert len(fuse({"bing": hits}, weights={"bing": 1.0}, limit=3)) == 3


def test_is_deterministic() -> None:
    """同输入同输出，否则缓存与测试都无从谈起。"""
    hits = {
        "bing": [_hit("https://a.example/1", engine="bing"), _hit("https://b.example/1", engine="bing")],
        "duckduckgo": [_hit("https://c.example/1", engine="duckduckgo")],
    }
    first = [hit.url for hit in fuse(hits, weights={"bing": 0.4, "duckduckgo": 0.2}, limit=5)]
    second = [hit.url for hit in fuse(hits, weights={"bing": 0.4, "duckduckgo": 0.2}, limit=5)]
    assert first == second


def test_skips_hits_without_usable_url() -> None:
    """没有主机名的 URL（空串、相对路径）都不是可用结果，必须丢弃。"""
    fused = fuse({"bing": [_hit(""), _hit("not-a-url"), _hit("https://ok.example/1")]}, weights={"bing": 1.0}, limit=5)
    assert [hit.url for hit in fused] == ["https://ok.example/1"]


def test_host_path_key_empty_without_host() -> None:
    """去重键对无主机 URL 返回空串，供上层判定丢弃。"""
    assert host_path_key("") == ""
    assert host_path_key("/relative/path") == ""


def test_handles_empty_input() -> None:
    """没有任何引擎结果时返回空列表。"""
    assert fuse({}, limit=5) == []
    assert fuse({"bing": []}, limit=5) == []


def test_default_weights_cover_first_phase_engines() -> None:
    """默认权重必须覆盖已实现的引擎，否则会被当成 0.1 兜底而失去调优意义。"""
    for engine in ("bing", "searxng", "duckduckgo", "moegirl"):
        assert engine in DEFAULT_WEIGHTS
