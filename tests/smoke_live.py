"""端到端冒烟测试：按宿主方式加载插件，然后用**真实网络**跑四条通路。

用法::

    python tests/smoke_live.py             # 全部
    python tests/smoke_live.py --offline   # 只检查加载与组件注册

与 ``bench.py`` 的分工：bench 管**离线延迟门禁**；本脚本管**组装好的插件是不是真的能用**——
它走的是插件自己的工具处理函数（配置 → 处理器 → 管线 → 渲染），而不只是各层管线。

它同时可以当作你在自己麦麦环境里的一次性验收脚本：
把插件放进 ``plugins/`` 后直接运行即可看到四条通路的真实结果。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


class SmokeCtx:
    """最小可用 ctx：记录发送内容，其余能力返回空结果。"""

    def __init__(self) -> None:
        self.logger = logging.getLogger("smoke")
        self.texts: list[str] = []
        self.images: list[str] = []
        self.send = self
        self.tool = self
        self.message = self
        self.llm = self

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        del stream_id, kwargs
        self.texts.append(text)
        return True

    async def image(self, image_data: str, stream_id: str, **kwargs: Any) -> bool:
        del stream_id, kwargs
        self.images.append(image_data)
        return True

    async def get_definitions(self) -> list[dict[str, Any]]:
        return []

    async def get_by_id(self, message_id: str, **kwargs: Any) -> Any:
        del message_id, kwargs
        return None

    async def generate(self, prompt: Any, **kwargs: Any) -> dict[str, Any]:
        del prompt, kwargs
        return {"success": False, "response": "", "model": "smoke"}


def _load_package() -> Any:
    """按宿主方式加载插件包。"""
    spec = importlib.util.spec_from_file_location(
        "smoke_pkg",
        ROOT / "plugin.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_pkg"] = module
    spec.loader.exec_module(module)
    return module


def _report(label: str, passed: bool, detail: str = "") -> bool:
    mark = "[OK]  " if passed else "[FAIL]"
    print(f"  {mark} {label}" + (f"  — {detail}" if detail else ""))
    return passed


async def _run(module: Any, *, offline: bool) -> int:
    failures = 0
    plugin = module.create_plugin()
    plugin.set_plugin_config(plugin.get_default_config())
    ctx = SmokeCtx()
    plugin._set_context(ctx)

    print("== 加载与注册 ==")
    started = time.perf_counter()
    try:
        await plugin.on_load()
        on_load_ms = (time.perf_counter() - started) * 1000
        failures += not _report("on_load 未抛异常", True, f"{on_load_ms:.1f} ms")
    except Exception as exc:  # noqa: BLE001 - 冒烟脚本要报告所有失败
        failures += not _report("on_load", False, f"{type(exc).__name__}: {exc}")

    names = {str(component.get("name") or "") for component in plugin.get_components()}
    expected = {"web_search", "read_url", "image_search", "image_lookup", "websearch"}
    missing = sorted(expected - names)
    failures += not _report("组件注册齐全", not missing, f"缺失 {missing}" if missing else f"{len(names)} 个")

    if offline:
        print("\n（--offline：跳过联网部分）")
        await plugin.on_unload()
        return 1 if failures else 0

    print("\n== ① 文搜文 ==")
    try:
        result = await plugin.handle_web_search(query="初音未来", stream_id="smoke")
        text = str(result.get("content") or "")
        failures += not _report(
            "web_search 返回内容",
            bool(result.get("success")) and len(text) > 0,
            f"{len(text)} 字符｜{text.splitlines()[0][:40] if text else ''}",
        )
    except Exception as exc:  # noqa: BLE001
        failures += not _report("web_search", False, f"{type(exc).__name__}: {exc}")

    print("\n== 阅读 ==")
    try:
        result = await plugin.handle_read_url(url="https://example.com/", stream_id="smoke")
        text = str(result.get("content") or "")
        failures += not _report(
            "read_url 取到正文",
            bool(result.get("success")),
            f"{len(text)} 字符｜{text.splitlines()[0][:40] if text else ''}",
        )
    except Exception as exc:  # noqa: BLE001
        failures += not _report("read_url", False, f"{type(exc).__name__}: {exc}")

    print("\n== ② 文搜图 ==")
    try:
        before = len(ctx.images)
        result = await plugin.handle_image_search(query="布偶猫", stream_id="smoke")
        sent = len(ctx.images) - before
        text = str(result.get("content") or "")
        failures += not _report(
            "image_search 发出图片",
            sent > 0,
            f"发送 {sent} 张｜{len(text)} 字符说明",
        )
    except Exception as exc:  # noqa: BLE001
        failures += not _report("image_search", False, f"{type(exc).__name__}: {exc}")

    print("\n== ③④ 图搜图 / 图搜文 ==")
    try:
        result = await plugin.handle_image_lookup(
            message={"message_id": "smoke", "raw_message": [{"type": "text", "data": "这是什么"}]},
            stream_id="smoke",
        )
        text = str(result.get("content") or "")
        # 没有真图时预期"可读失败"，这本身就是在验证错误路径
        failures += not _report(
            "image_lookup 给出可读结果（无图时预期失败但不崩）",
            bool(text) and "Traceback" not in text,
            text.splitlines()[0][:50] if text else "",
        )
    except Exception as exc:  # noqa: BLE001
        failures += not _report("image_lookup", False, f"{type(exc).__name__}: {exc}")

    print("\n== 卸载 ==")
    try:
        await plugin.on_unload()
        failures += not _report("on_unload 未抛异常", True)
    except Exception as exc:  # noqa: BLE001
        failures += not _report("on_unload", False, f"{type(exc).__name__}: {exc}")

    return 1 if failures else 0


def main(argv: list[str]) -> int:
    """入口。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # 日志走 stdout：否则 PowerShell 会把 stderr 渲染成刺眼的错误块
    logging.basicConfig(level=logging.INFO, format="    %(levelname)s %(message)s", stream=sys.stdout)
    offline = "--offline" in argv
    module = _load_package()
    code = asyncio.run(_run(module, offline=offline))
    print("\n冒烟结果：" + ("通过 [OK]" if code == 0 else "有失败项 [FAIL]"))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
