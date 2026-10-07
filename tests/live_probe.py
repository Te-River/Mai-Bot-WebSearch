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
    """探测反查引擎的**真实可用性与解析效果**（不只是连通性）。

    这节是给"反查解析器未经本网络验证"准备的校准工具：它会先取一张真实图片，
    再逐个跑通配好的反查引擎，报告每个引擎是"搜到几条""解析到结构"还是"不可达/被反爬"。
    **换到可达网络后重跑这一节**，把输出（或 ``--dump`` 存下的响应）发回，就能校准解析器。
    """
    config = module.config.WebSearchConfig()
    proxy = module.core.http.resolve_proxy(config.network.proxy_mode, config.network.proxy)
    http = module.core.http.HttpClient(proxy=proxy, timeout_seconds=15.0)

    print("\n=== 以图搜源：取一张测试图 ===")
    test_image = None
    try:
        # 注意：包是以合成名加载的，这里只能走已加载模块的属性，不能写绝对导入
        bi = module.images.providers.bing_images
        provider = bi.BingImagesProvider()
        resp = await http.fetch(provider.build_url(IMAGE_QUERIES[0], limit=3), max_bytes=1048576)
        cands = bi.parse_bing_images(resp.text)
        if cands:
            raw = await http.fetch_bytes(cands[0].url, max_bytes=3145728, timeout_seconds=15.0)
            test_image = module.images.inbound.InboundImage(
                source="probe",
                url=cands[0].url,
                content=raw.content,
                mime_type=raw.content_type or "image/jpeg",
            )
            print(f"  测试图就绪: {len(raw.content) // 1024} KB  {cands[0].url[:60]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  取图失败（改用最小 PNG 继续探测）: {type(exc).__name__}: {str(exc)[:60]}")

    if test_image is None:
        # 降级也要继续：探针的价值在于"跑通每个引擎并报告"，不能因为取图失败就不跑
        test_image = module.images.inbound.InboundImage(
            source="probe",
            content=bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
            ),
            mime_type="image/png",
        )
        print("  使用 1x1 占位 PNG（引擎会拒收，但足以验证连通性与错误路径）")

    print("\n=== 以图搜源引擎：真实调用 ===")
    try:
        providers = module.images.reverse.build_reverse_providers(config)
        for provider in providers:
            started = time.perf_counter()
            try:
                result = await provider.lookup(test_image, http)
                elapsed = (time.perf_counter() - started) * 1000
                if result.ok:
                    top = result.sources[0]
                    print(
                        f"  OK   {provider.name:14s} {elapsed:6.0f} ms  {len(result.sources)} 条  "
                        f"top: {(top.title or top.urls[0] if top.urls else '')[:40]}"
                    )
                else:
                    print(f"  EMPTY{'':10s} {provider.name:14s} {elapsed:6.0f} ms  {result.error or '结构未识别'}")
            except Exception as exc:  # noqa: BLE001
                elapsed = (time.perf_counter() - started) * 1000
                print(f"  FAIL {provider.name:14s} {elapsed:6.0f} ms  {type(exc).__name__}: {str(exc)[:45]}")
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
