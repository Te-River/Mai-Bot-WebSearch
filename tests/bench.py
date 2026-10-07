"""延迟门禁。

用法::

    python tests/bench.py            # 机器无关的门禁（CI 用这个）
    python tests/bench.py --strict   # 额外强制绝对时间预算（本地开发用）

门禁分两类：

* **机器无关**（始终强制）：Tier-1 重依赖不得进入加载路径；提前返回必须真的生效。
* **绝对时间**（仅 ``--strict``）：冷启动、导入耗时、编排 p50/p95。
  共享 CI runner 的绝对计时不可靠，把它当阻断门禁只会训练出"忽略红灯"的习惯。

真实网络的往返延迟由 ``tests/live_probe.py`` 负责——两者分开才能定位问题出在哪。
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

COLD_START_BUDGET_MS = 300.0
# 边际导入预算：宿主在激活插件**之前**就已经导入了 maibot_sdk 与 pydantic，
# 所以真正由我们引入的成本只有 httpx(~290ms) + selectolax(~4ms) + 自己的代码。
# 门禁盯的是这个边际值——测"全新解释器导入 plugin.py"会把宿主的账算到我们头上。
MARGINAL_IMPORT_BUDGET_MS = 450.0
P50_BUDGET_MS = 50.0  # 纯编排开销（不含网络）应当极低
P95_BUDGET_MS = 150.0
# 提前返回是机器无关门禁：慢引擎要睡 30 秒，能在这个预算内返回就说明取消生效了
EARLY_RETURN_BUDGET_MS = 2000.0
ITERATIONS = 200

# 这些库一旦出现在加载路径上，轻量化目标就破了
HEAVY_MODULES = ("trafilatura", "curl_cffi", "bs4", "lxml", "readability_lxml", "numpy")

_COLD_START_SNIPPET = """
import asyncio, importlib.util, json, logging, sys, time

root = sys.argv[1]
heavy = ("trafilatura", "curl_cffi", "bs4", "lxml", "readability_lxml", "numpy")

# 宿主在激活插件前已经导入过 SDK，先把它算作基线，才能量出**我们引入的边际成本**
started = time.perf_counter()
import maibot_sdk  # noqa: F401
baseline_ms = (time.perf_counter() - started) * 1000.0

started = time.perf_counter()
spec = importlib.util.spec_from_file_location(
    "bench_pkg", root + "/plugin.py", submodule_search_locations=[root]
)
module = importlib.util.module_from_spec(spec)
sys.modules["bench_pkg"] = module
spec.loader.exec_module(module)
marginal_ms = (time.perf_counter() - started) * 1000.0


class _Send:
    async def text(self, *args, **kwargs):
        return True


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("bench")
        self.send = _Send()


async def _main():
    plugin = module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(_Ctx())
    started = time.perf_counter()
    await plugin.on_load()
    on_load_ms = (time.perf_counter() - started) * 1000.0
    await plugin.on_unload()
    return on_load_ms


on_load_ms = asyncio.run(_main())
loaded = sorted({name.split(".")[0] for name in sys.modules} & set(heavy))
print(json.dumps({
    "baseline_ms": baseline_ms,
    "marginal_ms": marginal_ms,
    "on_load_ms": on_load_ms,
    "heavy": loaded,
}))
"""


def _measure_cold_start() -> dict[str, Any]:
    """在全新解释器里测冷启动，避免被本进程已导入的模块污染。"""
    completed = subprocess.run(
        [sys.executable, "-c", _COLD_START_SNIPPET, str(ROOT)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _load_package() -> Any:
    """在当前进程里按宿主方式加载插件包。"""
    spec = importlib.util.spec_from_file_location(
        "bench_inproc",
        ROOT / "plugin.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_inproc"] = module
    spec.loader.exec_module(module)
    return module


class _InstantProvider:
    """零延迟假引擎，用来测纯编排开销。"""

    kind = "json"
    requires_key = False

    def __init__(self, name: str) -> None:
        self.name = name

    def prepare(self, query: str) -> str:
        return query

    async def search(self, request: Any, http: Any) -> list[Any]:
        return []


class _SlowProvider(_InstantProvider):
    """永远不返回的假引擎，用来验证提前返回真的生效。"""

    async def search(self, request: Any, http: Any) -> list[Any]:
        import asyncio

        await asyncio.sleep(30)
        return []


def _measure_pipeline_overhead(module: Any) -> list[float]:
    """测管线编排的 p50/p95（不含网络）。"""
    import asyncio

    pipeline = module.search.router.SearchPipeline(
        http=object(),
        providers=[_InstantProvider("bing"), _InstantProvider("duckduckgo")],
        deadline_seconds=6.0,
        quorum=2,
        grace_seconds=0.0,
        cache_ttl_seconds=0,
    )

    async def _run() -> list[float]:
        samples: list[float] = []
        for index in range(ITERATIONS):
            started = time.perf_counter()
            await pipeline.search(f"查询-{index}")
            samples.append((time.perf_counter() - started) * 1000.0)
        return samples

    return asyncio.run(_run())


def _measure_early_return(module: Any) -> float:
    """一个快引擎 + 一个慢引擎：应在宽限窗口附近返回，而不是等 30 秒。"""
    import asyncio

    pipeline = module.search.router.SearchPipeline(
        http=object(),
        providers=[_InstantProvider("bing"), _SlowProvider("slow")],
        deadline_seconds=30.0,
        quorum=1,
        grace_seconds=0.05,
        cache_ttl_seconds=0,
    )

    async def _run() -> float:
        started = time.perf_counter()
        await pipeline.search("提前返回")
        return (time.perf_counter() - started) * 1000.0

    return asyncio.run(_run())


def _measure_import_breakdown(modules: tuple[str, ...]) -> dict[str, float]:
    """分别测量各依赖的导入耗时，用于判断"重"在哪里。"""
    result: dict[str, float] = {}
    for name in modules:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import time; t=time.perf_counter(); import {name}; print((time.perf_counter()-t)*1000)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            result[name] = float(completed.stdout.strip().splitlines()[-1])
    return result


def main() -> int:
    """执行全部门禁并打印报告。"""
    strict = "--strict" in sys.argv
    if hasattr(sys.stdout, "reconfigure"):
        # Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    failures: list[str] = []
    advisories: list[str] = []

    cold = _measure_cold_start()
    print("== 冷启动 ==")
    print(f"  SDK 基线导入    {cold['baseline_ms']:.1f} ms（宿主已付，不计入我们）")
    print(f"  边际导入        {cold['marginal_ms']:.1f} ms（预算 {MARGINAL_IMPORT_BUDGET_MS:.0f} ms）")
    print(f"  on_load         {cold['on_load_ms']:.1f} ms（硬预算 {COLD_START_BUDGET_MS:.0f} ms）")
    if cold["on_load_ms"] > COLD_START_BUDGET_MS:
        advisories.append(f"on_load {cold['on_load_ms']:.1f}ms 超出 {COLD_START_BUDGET_MS:.0f}ms 预算")
    if cold["marginal_ms"] > MARGINAL_IMPORT_BUDGET_MS:
        advisories.append(f"边际导入 {cold['marginal_ms']:.1f}ms 超出 {MARGINAL_IMPORT_BUDGET_MS:.0f}ms 预算")
    # 机器无关：重依赖绝不允许出现在加载路径上
    if cold["heavy"]:
        failures.append(f"加载路径上出现了 Tier-1 重依赖：{cold['heavy']}")
    else:
        print("  Tier-1 重依赖   未导入 [OK]")

    breakdown = _measure_import_breakdown(("httpx", "selectolax", "pydantic", "maibot_sdk"))
    print("\n== 导入成本拆解（各自独立解释器）==")
    for name, milliseconds in breakdown.items():
        print(f"  {name:<14}{milliseconds:7.1f} ms")

    module = _load_package()
    samples = _measure_pipeline_overhead(module)
    p50 = statistics.median(samples)
    p95 = sorted(samples)[int(len(samples) * 0.95) - 1]
    print("\n== 管线编排开销（不含网络）==")
    print(f"  p50             {p50:.2f} ms（预算 {P50_BUDGET_MS:.0f} ms）")
    print(f"  p95             {p95:.2f} ms（预算 {P95_BUDGET_MS:.0f} ms）")
    if p50 > P50_BUDGET_MS:
        advisories.append(f"编排 p50 {p50:.2f}ms 超出预算")
    if p95 > P95_BUDGET_MS:
        advisories.append(f"编排 p95 {p95:.2f}ms 超出预算")

    early = _measure_early_return(module)
    print("\n== 提前返回 ==")
    print(f"  慢引擎被取消后返回 {early:.1f} ms（预算 {EARLY_RETURN_BUDGET_MS:.0f} ms，慢引擎需 30s）")
    # 机器无关：慢引擎睡 30s，能在这个预算内返回就证明取消真的生效
    if early > EARLY_RETURN_BUDGET_MS:
        failures.append(f"提前返回耗时 {early:.1f}ms 超出 {EARLY_RETURN_BUDGET_MS:.0f}ms——慢引擎没有被真正取消")

    print()
    for item in advisories:
        level = "FAIL" if strict else "WARN"
        print(f"  [{level}] {item}")
        if strict:
            failures.append(item)
    if advisories and not strict:
        print("  （绝对时间预算需加 --strict 才会阻断；CI 只强制机器无关门禁）")

    if failures:
        print("\n门禁未通过：")
        for item in failures:
            print(f"  [FAIL] {item}")
        return 1
    print("门禁全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
