"""结果融合：归一化 → host+path 去重 → 加权 RRF。

用 RRF（Reciprocal Rank Fusion）而不是加权分数求和的理由：各引擎的分数量纲完全不同
（有的给 0~1 相似度，有的给 BM25，有的根本没有分数），**只用排名**才可比。

``fuse`` 的输出顺序是确定的：同分时按"首次出现的引擎顺序 + 引擎内排名"稳定排序，
这样同样的输入永远得到同样的输出，便于测试与缓存。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .types import SearchHit, host_path_key

__all__ = ["DEFAULT_WEIGHTS", "WEIGHTS_BY_CLASS", "fuse"]

# RRF 平滑常数：越大越削弱"排名第一"的优势。60 是业界常用值。
RRF_K = 60

DEFAULT_WEIGHTS: dict[str, float] = {
    "bing": 0.4,
    "moegirl": 0.3,
    "searxng": 0.3,
    "duckduckgo": 0.2,
}

WEIGHTS_BY_CLASS: dict[str, dict[str, float]] = {
    "general": {"bing": 0.4, "searxng": 0.3, "duckduckgo": 0.2, "moegirl": 0.1},
    "cjk": {"bing": 0.5, "searxng": 0.3, "duckduckgo": 0.2, "moegirl": 0.2},
    "acg": {"moegirl": 0.5, "bing": 0.3, "searxng": 0.2, "duckduckgo": 0.1},
    "dev-ecosystem": {"bing": 0.4, "searxng": 0.3, "duckduckgo": 0.2},
    "error-code": {"bing": 0.4, "searxng": 0.3, "duckduckgo": 0.2},
}


def fuse(
    hits_by_engine: Mapping[str, Sequence[SearchHit]],
    *,
    weights: Mapping[str, float] | None = None,
    limit: int = 8,
    rrf_k: int = RRF_K,
) -> list[SearchHit]:
    """把多个引擎的结果融合成一个去重后的排名列表。

    同一条结果（host+path 相同）会被合并：保留首次出现的标题与摘要，
    非空摘要优先，并记录所有命中它的引擎。
    """
    weight_table = weights if weights is not None else DEFAULT_WEIGHTS
    scores: dict[str, float] = {}
    merged: dict[str, SearchHit] = {}
    engines_by_key: dict[str, list[str]] = {}
    first_seen: dict[str, int] = {}
    order = 0

    for engine, hits in hits_by_engine.items():
        weight = float(weight_table.get(engine, 0.1))
        for index, hit in enumerate(hits):
            key = host_path_key(hit.url)
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + weight / (rrf_k + index + 1)

            if key not in merged:
                merged[key] = SearchHit(
                    title=hit.title,
                    url=hit.url,
                    snippet=hit.snippet,
                    engine=engine,
                    published_at=hit.published_at,
                    extra=dict(hit.extra),
                )
                first_seen[key] = order
                order += 1
            else:
                existing = merged[key]
                if not existing.snippet and hit.snippet:
                    existing.snippet = hit.snippet
                if not existing.title and hit.title:
                    existing.title = hit.title

            engines_by_key.setdefault(key, []).append(engine)

    ranked = sorted(scores, key=lambda key: (-scores[key], first_seen[key]))
    output: list[SearchHit] = []
    for position, key in enumerate(ranked[: max(limit, 0)], start=1):
        hit = merged[key]
        hit.rank = position
        hit.extra["engines"] = ",".join(engines_by_key[key])
        output.append(hit)
    return output
