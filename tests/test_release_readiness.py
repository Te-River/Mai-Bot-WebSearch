"""发布门禁：能力声明一致性、组件注册、启动自检、二次总结接线。

这些检查是为了拦住"配置项存在但没接线""声明了能力却没用到""工具没注册上"这类
**测试全绿但发出去是坏的**问题——本项目已经真实发生过前两类。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from mai_websearch_under_test.search.router import SearchOutcome
from mai_websearch_under_test.search.types import SearchHit

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))

# ctx 的命名空间里，哪些**不是**能力（不需要声明）
NON_CAPABILITY_NAMESPACES = frozenset({"logger", "paths"})
# 宿主注册表里的能力命名空间
CAPABILITY_NAMESPACES = frozenset(
    {
        "send",
        "llm",
        "config",
        "database",
        "chat",
        "message",
        "person",
        "emoji",
        "frequency",
        "api",
        "component",
        "maisaka",
        "statistics",
        "render",
        "knowledge",
        "tool",
    }
)
_CTX_CALL_RE = re.compile(r"ctx\.([a-z_]+)\.([a-z_0-9]+)")

# 插件应当注册的组件
EXPECTED_TOOLS = frozenset({"web_search", "read_url", "image_search", "image_lookup"})
EXPECTED_COMMANDS = frozenset({"websearch"})


def _plugin_sources() -> list[Path]:
    """插件自身源码（不含测试——测试里的假 ctx 不代表真实能力需求）。"""
    return [
        path
        for path in PLUGIN_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and "tests" not in path.parts
    ]


def _used_capabilities() -> set[str]:
    """静态扫描代码里真实调用到的能力。"""
    used: set[str] = set()
    for path in _plugin_sources():
        for namespace, method in _CTX_CALL_RE.findall(path.read_text(encoding="utf-8")):
            if namespace in NON_CAPABILITY_NAMESPACES or namespace not in CAPABILITY_NAMESPACES:
                continue
            used.add(f"{namespace}.{method}")
    return used


# ------------------------------------------------------------------ 能力一致性


def test_scanner_finds_known_usages() -> None:
    """先验证扫描器本身有效——否则下面的断言可能因为"什么都没扫到"而假装通过。"""
    used = _used_capabilities()
    assert {"send.text", "send.image", "message.get_by_id", "llm.generate"} <= used


def test_every_used_capability_is_declared() -> None:
    """用到但没声明的能力会让宿主在运行时拒绝调用（比加载失败更难排查）。"""
    missing = sorted(_used_capabilities() - set(MANIFEST["capabilities"]))
    assert not missing, f"代码用到了未声明的能力：{missing}"


def test_no_unused_capability_declarations() -> None:
    """声明了却没用到的能力会白白扩大授权面。

    本项目真实踩过：``tool.get_definitions`` 与 ``llm.generate`` 声明了但功能没接线。
    """
    unused = sorted(set(MANIFEST["capabilities"]) - _used_capabilities())
    assert not unused, f"声明了但代码未使用的能力：{unused}"


# ------------------------------------------------------------------ 组件注册


def test_all_tools_are_registered(plugin: Any) -> None:
    """四条通路的组件必须都注册上——少一个就是少一条通路。"""
    components = plugin.get_components()
    names = {str(component.get("name") or "") for component in components}
    assert EXPECTED_TOOLS <= names, f"缺失的工具：{sorted(EXPECTED_TOOLS - names)}"
    assert EXPECTED_COMMANDS <= names, f"缺失的命令：{sorted(EXPECTED_COMMANDS - names)}"


def test_rpc_timeout_is_injected_at_top_level(plugin: Any) -> None:
    """``timeout_ms`` 必须在组件元数据**顶层**。

    宿主只读顶层；写在装饰器 kwargs 里会落进嵌套 metadata 而不被读取，
    慢查询会被 60 秒默认值截断成一次失败调用。
    """
    for component in plugin.get_components():
        metadata = component.get("metadata")
        assert isinstance(metadata, dict), component
        assert metadata.get("timeout_ms") == plugin.config.limits.rpc_timeout_seconds * 1000


def test_rpc_timeout_follows_config(plugin: Any) -> None:
    """改配置要能生效，不能被写死。"""
    config = plugin.get_default_config()
    config["limits"]["rpc_timeout_seconds"] = 40
    plugin.set_plugin_config(config)
    assert plugin.get_components()[0]["metadata"]["timeout_ms"] == 40_000


# ------------------------------------------------------------------ 启动自检


class TestToolNameConflictCheck:
    """与 ``google_search_plugin`` 共存时，同名工具会让 planner 选错。"""

    async def test_no_warning_without_conflict(self, plugin: Any, caplog: Any) -> None:
        plugin.ctx.tool.definitions = [{"name": "web_search", "definition": {}}]
        with caplog.at_level("WARNING"):
            await plugin._warn_on_tool_name_conflicts()
        assert "冲突" not in caplog.text

    async def test_warns_when_name_appears_twice(self, plugin: Any, caplog: Any) -> None:
        """同一个名字出现两次 = 两个插件都注册了它。"""
        plugin.ctx.tool.definitions = [
            {"name": "web_search", "definition": {"description": "我们的"}},
            {"name": "web_search", "definition": {"description": "别人的"}},
            {"name": "read_url", "definition": {}},
        ]
        with caplog.at_level("WARNING"):
            await plugin._warn_on_tool_name_conflicts()
        assert "web_search" in caplog.text
        assert "冲突" in caplog.text

    async def test_ignores_unrelated_names(self, plugin: Any, caplog: Any) -> None:
        plugin.ctx.tool.definitions = [
            {"name": "other_tool", "definition": {}},
            {"name": "other_tool", "definition": {}},
        ]
        with caplog.at_level("WARNING"):
            await plugin._warn_on_tool_name_conflicts()
        assert caplog.text == ""

    async def test_failure_never_breaks_loading(self, plugin: Any) -> None:
        """自检失败绝不能影响插件加载。"""
        plugin.ctx.tool = type("Boom", (), {"get_definitions": staticmethod(_raise)})()
        await plugin._warn_on_tool_name_conflicts()


async def _raise() -> Any:
    raise RuntimeError("宿主不支持")


# ------------------------------------------------------------------ 二次总结


def _outcome() -> SearchOutcome:
    return SearchOutcome(
        query="初音未来",
        hits=[SearchHit(title="初音未来 - 萌娘百科", url="https://x/1", snippet="虚拟歌手", rank=1)],
        engine_status={"bing": "ok:1"},
        elapsed_ms=100,
    )


class TestSummarize:
    """``[llm] summarize`` 是配置项，必须真的接线（本项目真实踩过）。"""

    async def test_off_by_default_does_not_call_llm(self, plugin: Any) -> None:
        plugin.ctx.llm.calls.clear()
        assert await plugin._maybe_summarize("原文") == "原文"
        assert plugin.ctx.llm.calls == []

    async def test_uses_configured_task_name(self, plugin: Any) -> None:
        """任务名必须来自配置，且类型上不可能是 vlm。"""
        config = plugin.get_default_config()
        config["llm"]["summarize"] = True
        config["llm"]["task_name"] = "tool_use"
        plugin.set_plugin_config(config)

        assert await plugin._maybe_summarize("原文") == "总结结果"
        assert plugin.ctx.llm.calls[0]["task_name"] == "tool_use"

    async def test_llm_failure_falls_back_to_original(self, plugin: Any) -> None:
        """总结失败要退回原文，而不是让整次搜索失败。"""
        config = plugin.get_default_config()
        config["llm"]["summarize"] = True
        plugin.set_plugin_config(config)
        plugin.ctx.llm.error = RuntimeError("模型不可用")

        assert await plugin._maybe_summarize("原文") == "原文"

    async def test_unsuccessful_result_falls_back(self, plugin: Any) -> None:
        config = plugin.get_default_config()
        config["llm"]["summarize"] = True
        plugin.set_plugin_config(config)
        plugin.ctx.llm.success = False
        plugin.ctx.llm.response = ""

        assert await plugin._maybe_summarize("原文") == "原文"

    async def test_empty_text_short_circuits(self, plugin: Any) -> None:
        config = plugin.get_default_config()
        config["llm"]["summarize"] = True
        plugin.set_plugin_config(config)
        assert await plugin._maybe_summarize("   ") == "   "
        assert plugin.ctx.llm.calls == []

    async def test_web_search_applies_summary(self, plugin: Any) -> None:
        """端到端：开启后 web_search 返回的是总结而不是原始列表。"""
        config = plugin.get_default_config()
        config["llm"]["summarize"] = True
        plugin.set_plugin_config(config)
        plugin._pipeline = _FakePipeline(_outcome())

        result = await plugin.handle_web_search(query="初音未来", stream_id="s")
        assert result["content"] == "总结结果"
        assert plugin.ctx.llm.calls

    async def test_web_search_without_hits_is_not_summarized(self, plugin: Any) -> None:
        """没搜到就没有可总结的内容，直接给出失败说明。"""
        config = plugin.get_default_config()
        config["llm"]["summarize"] = True
        plugin.set_plugin_config(config)
        plugin._pipeline = _FakePipeline(SearchOutcome(query="x", hits=[], engine_status={"bing": "ok:0"}))

        result = await plugin.handle_web_search(query="x", stream_id="s")
        assert result["success"] is False
        assert plugin.ctx.llm.calls == []


class _FakePipeline:
    def __init__(self, outcome: SearchOutcome) -> None:
        self.outcome = outcome

    async def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        del max_results
        return self.outcome


# ------------------------------------------------------------------ 仓库卫生


# 只匹配**注释里**的未完成标记
_MARKER_RE = re.compile(r"#.*\b(TODO|FIXME|XXX)\b")
# 检查器自身含有这些字面量与上面的正则（那行里就有 `#` 加标记），必须跳过以免自指
_CHECKER_FILE = Path(__file__).resolve()


@pytest.mark.parametrize("marker", ["TODO", "FIXME", "XXX"])
def test_no_unfinished_markers(marker: str) -> None:
    """发布前不应在注释里留下未完成的标记。"""
    offenders: list[str] = []
    for path in [*_plugin_sources(), *sorted((PLUGIN_ROOT / "tests").rglob("*.py"))]:
        if path.resolve() == _CHECKER_FILE:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _MARKER_RE.search(line)
            if match is not None and match.group(1) == marker:
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"发现 {marker} 标记：{offenders}"


def test_config_toml_is_gitignored() -> None:
    """运行时配置（可能含 API Key）绝不能进版本库。"""
    ignored = (PLUGIN_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/config.toml" in ignored


def test_license_matches_manifest() -> None:
    assert MANIFEST["license"].lower() in (PLUGIN_ROOT / "LICENSE").read_text(encoding="utf-8").lower()


def test_version_is_release_ready() -> None:
    """正式发布用 1.0.0，且 manifest 与代码必须一致。"""
    version_py = (PLUGIN_ROOT / "version.py").read_text(encoding="utf-8")
    assert '__version__ = "1.0.0"' in version_py
    assert MANIFEST["version"] == "1.0.0"
