"""``_manifest.json`` 的静态校验。

宿主侧的 ``ManifestValidator`` 是严格模式：字段多余、id 格式、版本号、URL 格式、
以及**声明了未注册的能力名**都会导致插件被拒绝加载。这类错误在真实部署里表现为
"插件根本不出现"，所以必须在 CI 里先拦一道。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST: dict = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))

# 宿主 ``src/plugin_runtime/capabilities/registry.py`` 注册的能力名。
# 依据官方文档的 73 项清单；文档清单遗漏了 ``chat.get_avatar``（api-reference 明确要求声明），故补入。
KNOWN_CAPABILITIES = frozenset(
    {
        # send (7)
        "send.text",
        "send.emoji",
        "send.image",
        "send.forward",
        "send.hybrid",
        "send.command",
        "send.custom",
        # llm (5)
        "llm.generate",
        "llm.generate_with_tools",
        "llm.embed",
        "llm.transcribe_audio",
        "llm.get_available_models",
        # config (3)
        "config.get",
        "config.get_plugin",
        "config.get_all",
        # database (5)
        "database.query",
        "database.save",
        "database.get",
        "database.delete",
        "database.count",
        # chat (7)
        "chat.get_all_streams",
        "chat.get_group_streams",
        "chat.get_private_streams",
        "chat.open_session",
        "chat.get_stream_by_group_id",
        "chat.get_stream_by_user_id",
        "chat.get_avatar",
        # message (6)
        "message.get_by_time",
        "message.get_by_time_in_chat",
        "message.get_by_id",
        "message.get_recent",
        "message.count_new",
        "message.build_readable",
        # person (3)
        "person.get_id",
        "person.get_value",
        "person.get_id_by_name",
        # emoji (8)
        "emoji.get_by_description",
        "emoji.get_random",
        "emoji.get_count",
        "emoji.get_emotions",
        "emoji.get_all",
        "emoji.get_info",
        "emoji.register",
        "emoji.delete",
        # frequency (3)
        "frequency.get_current_talk_value",
        "frequency.set_adjust",
        "frequency.get_adjust",
        # api (4)
        "api.call",
        "api.get",
        "api.list",
        "api.replace_dynamic",
        # component (11)
        "component.get_all_plugins",
        "component.get_plugin_info",
        "component.get_plugin_config_schema",
        "component.update_plugin_config",
        "component.list_loaded_plugins",
        "component.list_registered_plugins",
        "component.enable",
        "component.disable",
        "component.load_plugin",
        "component.unload_plugin",
        "component.reload_plugin",
        # maisaka (2)
        "maisaka.context.append",
        "maisaka.proactive.trigger",
        # statistics (7)
        "statistics.local.models",
        "statistics.local.model_trend",
        "statistics.local.token_trend",
        "statistics.local.token_distribution",
        "statistics.local.message_trend",
        "statistics.local.tool_trend",
        "statistics.local.online_time_trend",
        # 其他 (3)
        "render.html2png",
        "knowledge.search",
        "tool.get_definitions",
    }
)

REQUIRED_KEYS = frozenset(
    {
        "manifest_version",
        "id",
        "version",
        "name",
        "description",
        "author",
        "license",
        "urls",
        "host_application",
        "sdk",
        "capabilities",
        "i18n",
    }
)
OPTIONAL_KEYS = frozenset(
    {
        "dependencies",
        "plugin_type",
        "display",
        # 宿主 PluginManifest 模型确实定义了这两个字段（默认空），因此合法
        "llm_providers",
        "changelog",
    }
)
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def test_manifest_version_is_two() -> None:
    """manifest 协议版本当前固定为 2。"""
    assert MANIFEST["manifest_version"] == 2


def test_id_matches_host_pattern() -> None:
    """id 必须匹配宿主的 ID 正则。"""
    assert ID_PATTERN.match(MANIFEST["id"]), MANIFEST["id"]


def test_versions_are_strict_semver() -> None:
    """插件版本与两个兼容区间都必须是严格三段式。"""
    assert SEMVER_PATTERN.match(MANIFEST["version"]), MANIFEST["version"]
    for section in ("host_application", "sdk"):
        block = MANIFEST[section]
        for key in ("min_version", "max_version"):
            assert SEMVER_PATTERN.match(block[key]), f"{section}.{key}={block[key]}"
        assert block["min_version"] <= block["max_version"]


def test_urls_are_http() -> None:
    """author.url 与 urls.* 都必须是 http(s) URL。"""
    assert MANIFEST["author"]["url"].startswith(("http://", "https://"))
    for value in MANIFEST["urls"].values():
        assert value.startswith(("http://", "https://")), value


def test_capabilities_are_all_registered() -> None:
    """声明了未注册的能力名会被 Runner 拒绝激活——这是最高频的"插件不出现"原因。"""
    declared = MANIFEST["capabilities"]
    assert declared, "capabilities 不能为空"
    assert len(set(declared)) == len(declared), "capabilities 不允许重复"
    unknown = sorted(set(declared) - KNOWN_CAPABILITIES)
    assert not unknown, f"声明了未注册的能力名：{unknown}"


def test_no_extra_keys() -> None:
    """ManifestValidator 禁止未声明的多余字段。"""
    extra = sorted(set(MANIFEST) - REQUIRED_KEYS - OPTIONAL_KEYS)
    assert not extra, f"manifest 中存在多余字段：{extra}"


def test_plugin_type_is_valid() -> None:
    """plugin_type 必须是官方枚举值之一。"""
    valid = {
        "adapter",
        "tool",
        "provider",
        "management",
        "data",
        "media",
        "game",
        "integration",
        "extension",
        "other",
    }
    assert MANIFEST.get("plugin_type", "extension") in valid


def test_version_matches_python_module() -> None:
    """manifest 的 version 与 code 里的 __version__ 必须一致。"""
    version_py = (ROOT / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', version_py)
    assert match is not None, "version.py 中未找到 __version__"
    assert match.group(1) == MANIFEST["version"]


def test_license_file_matches_manifest() -> None:
    """LICENSE 与 manifest 的 license 声明必须一致。"""
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert MANIFEST["license"].lower() in license_text.lower()


def test_default_locale_is_zh_cn() -> None:
    """用户可见文本为简体中文。"""
    assert MANIFEST["i18n"]["default_locale"] == "zh-CN"
