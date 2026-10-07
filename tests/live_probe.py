"""实网探针：用**真实管线**验证端到端行为，并实测各源可用性。

用法::

    python tests/live_probe.py                     # 默认查询集
    python tests/live_probe.py 初音未来 python      # 自定义查询

不参与 CI（依赖外网、结果随对方改版而变化）。它的作用是：

* 验证真实网络下"分类 → 扇出 → 融合 → 兜底"整条链路能跑通；
* 让默认引擎集合与融合权重由**实测**决定，而不是靠猜；
* 对方改版后重跑一次即可知道该关掉哪个源。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_QUERIES = (
    "初音未来",
    "初音未来是什么",  # 自然语言 → 触发萌娘百科的 site: 兜底
    "python asyncio 超时",
    "ModuleNotFoundError: No module named requests",
)

# 阅读通路的验证目标：一个普通站点 + 一个只有 JSON 接口可用的站点
READING_TARGETS = (
    "https://example.com/",
    "https://zh.moegirl.org.cn/初音未来",
)

# 文搜图的验证目标
IMAGE_QUERIES = ("布偶猫", "赛博朋克城市")

# 反查引擎的连通性检查目标（域名级探测，不需要图片）
REVERSE_HOSTS = (
    ("ascii2d", "https://ascii2d.net/"),
    ("iqdb", "https://iqdb.org/"),
    ("saucenao", "https://saucenao.com/"),
    ("yandex", "https://yandex.com/images/"),
    ("google-lens", "https://lens.google.com/"),
    ("tineye", "https://tineye.com/"),
    ("baidu-graph", "https://graph.baidu.com/"),
)


def _load_package() -> Any:
    """按宿主方式加载插件包。"""
    spec = importlib.util.spec_from_file_location(
        "live_probe_pkg",
        ROOT / "plugin.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_probe_pkg"] = module
    spec.loader.exec_module(module)
    return module


async def _probe_pipeline(module: Any, queries: tuple[str, ...]) -> None:
    """用真实管线跑查询：这是端到端验证，不是单点探测。"""
    config = module.config.WebSearchConfig()
    proxy = module.core.http.resolve_proxy(config.network.proxy_mode, config.network.proxy)
    print(f"代理：{proxy or '(直连)'}")

    http = module.core.http.HttpClient(proxy=proxy, timeout_seconds=config.network.timeout_seconds)
    pipeline = module.search.router.build_search_pipeline(config, http, module.core.budget.ConcurrencyGate(3))
    print(f"引擎：{[provider.name for provider in pipeline.providers]}\n")

    try:
        for query in queries:
            started = time.perf_counter()
            outcome = await pipeline.search(query)
            elapsed = (time.perf_counter() - started) * 1000
            print(f"--- {query!r} ---")
            print(f"  {elapsed:7.0f} ms  {len(outcome.hits)} 条  {outcome.engine_status}")
            for hit in outcome.hits[:3]:
                print(f"    {hit.rank}. [{hit.extra.get('engines', hit.engine)}] {hit.title[:50]}")
                print(f"       {hit.url[:90]}")
            print()

        # 缓存命中：高频场景下这是最重要的延迟优化
        started = time.perf_counter()
        cached = await pipeline.search(queries[0])
        print(f"缓存命中：{(time.perf_counter() - started) * 1000:.1f} ms  from_cache={cached.from_cache}")
    finally:
        await http.aclose()


async def _probe_reading(module: Any) -> None:
    """验证阅读通路：普通站点走抽取链，萌娘百科走专用 JSON 通路。"""
    config = module.config.WebSearchConfig()
    proxy = module.core.http.resolve_proxy(config.network.proxy_mode, config.network.proxy)
    http = module.core.http.HttpClient(proxy=proxy, timeout_seconds=config.network.timeout_seconds)
    pipeline = module.search.router.build_search_pipeline(config, http, module.core.budget.ConcurrencyGate(3))

    provider = pipeline.provider_by_name("moegirl")
    special = [module.search.providers.moegirl.moegirl_special_source(provider)] if provider else []

    print("\n=== 阅读通路（正文抽取链路：" + "/".join(module.reading.extract.strategy_names()) + "）===")
    try:
        for target in READING_TARGETS:
            try:
                result = await module.reading.fetcher.read_url(
                    target,
                    http=http,
                    max_content_length=config.reading.max_content_length,
                    special_sources=special,
                )
                print(f"  ✓ {result.elapsed_ms:6d} ms  [{result.strategy}]  {result.title[:40]!r}")
                print(f"      {len(result.text)} 字符  {result.text[:70]!r}")
            except Exception as exc:  # noqa: BLE001 - 探针要打印所有失败
                print(f"  ✗ {target}  {type(exc).__name__}: {exc}")
    finally:
        await http.aclose()


async def _probe_images(module: Any) -> None:
    """验证文搜图：候选解析 → 缩略图下载 → 可发送的 base64。

    重点是看**实际下载的字节数**：``send.image`` 只接受 base64，
    缩略图优先能把这个数字压到几十 KB，这是图片通路最关键的一笔开销。
    """
    config = module.config.WebSearchConfig()
    proxy = module.core.http.resolve_proxy(config.network.proxy_mode, config.network.proxy)
    http = module.core.http.HttpClient(proxy=proxy, timeout_seconds=config.network.timeout_seconds)
    history = module.images.picker.ImageHistory(window_seconds=0)
    pipeline = module.images.pipeline.build_image_pipeline(config, http, history)

    print(f"\n=== 图片通路（源：{[provider.name for provider in pipeline.providers]}）===")
    try:
        for query in IMAGE_QUERIES:
            try:
                outcome = await pipeline.search(query, limit=1)
                print(f"  [{outcome.status}] {outcome.elapsed_ms:6d} ms  {query!r}  候选 {outcome.candidates_seen}")
                print(f"      {outcome.engine_status}")
                for image in outcome.images:
                    print(
                        f"      → {image.mime_type}  {image.size_bytes // 1024} KB  "
                        f"base64 {len(image.to_base64()) // 1024} KB  {image.source_url[:70]}"
                    )
                for failure in outcome.failures:
                    print(f"      失败：{failure}")
            except Exception as exc:  # noqa: BLE001 - 探针要打印所有失败
                print(f"  ✗ {query!r}  {type(exc).__name__}: {exc}")
    finally:
        await http.aclose()


async def _probe_reverse(module: Any) -> None:
    """探测反查引擎的连通性。

    实测结论（2026，直连）：**多数网络下没有任何可用的反查引擎**，
    所以图搜文/图搜图的主路径是"宿主多模态通道 + 模型提取关键词后用 image_search"，
    反查只是可选增益。换网络后重跑这一节即可知道自己能不能用。
    """
    config = module.config.WebSearchConfig()
    proxy = module.core.http.resolve_proxy(config.network.proxy_mode, config.network.proxy)
    http = module.core.http.HttpClient(proxy=proxy, timeout_seconds=12.0)

    print("\n=== 以图搜源引擎连通性 ===")
    try:
        for name, url in REVERSE_HOSTS:
            started = time.perf_counter()
            try:
                response = await http.fetch(url, max_bytes=32768)
                elapsed = (time.perf_counter() - started) * 1000
                # 能连上不代表能用：还要看返回的是不是壳页面
                note = "（返回内容很短，可能是壳页面/需登录）" if len(response.text) < 4096 else ""
                print(f"  OK   {name:12s} {elapsed:6.0f} ms  {response.status}  {len(response.text)} 字符{note}")
            except Exception as exc:  # noqa: BLE001 - 探针要打印所有失败
                elapsed = (time.perf_counter() - started) * 1000
                print(f"  FAIL {name:12s} {elapsed:6.0f} ms  {type(exc).__name__}: {str(exc)[:45]}")
    finally:
        await http.aclose()


def main(argv: list[str]) -> int:
    """入口。"""
    if hasattr(sys.stdout, "reconfigure"):
        # Windows 控制台默认 GBK，中文会直接抛 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    queries = tuple(argv[1:]) or DEFAULT_QUERIES
    module = _load_package()
    asyncio.run(_probe_pipeline(module, queries))
    asyncio.run(_probe_reading(module))
    asyncio.run(_probe_images(module))
    asyncio.run(_probe_reverse(module))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
