"""配置模型校验。

宿主通过构造默认实例来生成 ``config.toml``，因此**任何缺少默认值的字段都会导致启动失败**。
"""

from __future__ import annotations

from types import ModuleType

import pytest
from maibot_sdk.config import build_plugin_default_config
from pydantic import ValidationError


def test_every_field_has_a_default(plugin_package: ModuleType) -> None:
    """build_plugin_default_config 会在字段缺默认值时抛错——这正是宿主的启动路径。"""
    defaults = build_plugin_default_config(plugin_package.config.WebSearchConfig)
    assert defaults["plugin"]["config_version"]
    assert defaults["plugin"]["enabled"] is True


def test_root_has_plugin_section_with_config_version(plugin_package: ModuleType) -> None:
    """SDK 的常量表明根模型必须有 plugin 段且含 config_version。"""
    config = plugin_package.config.WebSearchConfig()
    assert config.plugin.config_version == "1.0.0"


def test_all_sections_exist(plugin_package: ModuleType) -> None:
    """配置分段齐全，WebUI 才能渲染出完整表单。"""
    config = plugin_package.config.WebSearchConfig()
    for name in ("plugin", "network", "engines", "search", "reading", "llm", "limits", "perf"):
        assert hasattr(config, name), f"缺少配置段 {name}"


def test_latency_defaults_match_the_budget(plugin_package: ModuleType) -> None:
    """默认值必须直接满足延迟预算，而不是靠用户去调。"""
    search = plugin_package.config.WebSearchConfig().search
    assert search.fetch_top_n == 0, "默认不抓正文（快路径）"
    assert search.global_deadline_seconds <= 6.0, "搜索硬截止不得超过 6 秒"
    assert search.per_engine_timeout_seconds <= 3.0


def test_summarize_is_off_by_default(plugin_package: ModuleType) -> None:
    """工具结果会回到麦麦自己的对话模型，默认不再二次调用 LLM。"""
    assert plugin_package.config.WebSearchConfig().llm.summarize is False


def test_llm_task_name_cannot_be_vlm(plugin_package: ModuleType) -> None:
    """类型层面禁止 vlm：vlm 任务留空时宿主不会回退，硬编码它会在部分部署上失败。

    图像理解一律通过 Tool 的 content_items 交回宿主，由宿主按模型 visual 能力自动判定。
    """
    with pytest.raises(ValidationError):
        plugin_package.config.LlmSection(task_name="vlm")


def test_ssrf_guard_is_on_by_default(plugin_package: ModuleType) -> None:
    """SSRF 防护默认开启。"""
    assert plugin_package.config.WebSearchConfig().reading.block_private_hosts is True


def test_moegirl_defaults(plugin_package: ModuleType) -> None:
    """萌娘百科默认开启，且默认只取简介（快）。"""
    moegirl = plugin_package.config.WebSearchConfig().engines.moegirl
    assert moegirl.enabled is True
    assert moegirl.extract_mode == "intro"
    assert moegirl.api_base.endswith("/api.php")


def test_duckduckgo_is_disabled_by_default(plugin_package: ModuleType) -> None:
    """实网探针：html.duckduckgo.com 三次请求全部 ConnectTimeout，因此默认关闭。

    保持关闭也意味着默认搜索路径不会被一个必然超时的源拖慢。
    """
    assert plugin_package.config.WebSearchConfig().engines.duckduckgo.enabled is False


def test_searxng_is_disabled_by_default(plugin_package: ModuleType) -> None:
    """SearXNG 需要用户自建实例，没有地址时开启只会报错。"""
    searxng = plugin_package.config.WebSearchConfig().engines.searxng
    assert searxng.enabled is False
    assert searxng.base_url == ""


def test_proxy_defaults_to_auto(plugin_package: ModuleType) -> None:
    """代理默认 auto：读环境变量，这是"连不上网"最常见的一步修复。"""
    assert plugin_package.config.WebSearchConfig().network.proxy_mode == "auto"


def test_concurrency_is_conservative(plugin_package: ModuleType) -> None:
    """插件与其它插件共享同一个 Runner 子进程，默认并发必须保守。"""
    perf = plugin_package.config.WebSearchConfig().perf
    assert perf.max_inflight_searches <= 3
    assert perf.per_stream_cooldown_seconds > 0
