"""离线测试夹具。

这里刻意**复刻宿主的加载方式**（``spec_from_file_location`` + ``submodule_search_locations``，
见 MaiBot 的 ``plugin_runtime/runner/plugin_loader.py``）：把插件目录注册成一个合成包。
这样"缺少 ``__init__.py``""相对导入写错"这类问题会在 CI 直接暴露，而不是等装进麦麦才发现。

包在 conftest **导入时**就注册到 ``sys.modules``，因此测试模块可以直接
``from mai_websearch_under_test.core import ssrf``。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "mai_websearch_under_test"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_plugin_package() -> ModuleType:
    """按宿主的方式把插件目录加载为一个包。"""
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "plugin.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None, "无法为 plugin.py 构造模块 spec"
    assert spec.loader is not None, "plugin.py 的 loader 为空"
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin_module = _load_plugin_package()


@pytest.fixture(scope="session")
def plugin_package() -> ModuleType:
    """返回按宿主方式加载的插件包（供需要直接访问模型的测试使用）。"""
    return plugin_module


class FakeSend:
    """记录发送内容的最小 send 能力替身。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.images: list[tuple[str, str]] = []
        # 可切换成失败，用于覆盖"找到图但发不出去"的路径
        self.image_result = True

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        """记录一次文本发送。"""
        del kwargs
        self.texts.append((text, stream_id))
        return True

    async def image(self, image_data: str, stream_id: str, **kwargs: Any) -> bool:
        """记录一次图片发送。"""
        del kwargs
        self.images.append((image_data, stream_id))
        return self.image_result


class FakeTool:
    """记录工具定义查询。"""

    def __init__(self, definitions: list[dict[str, Any]] | None = None) -> None:
        self.definitions = definitions if definitions is not None else []
        self.calls = 0

    async def get_definitions(self) -> list[dict[str, Any]]:
        """返回预设的工具定义。"""
        self.calls += 1
        return list(self.definitions)


class FakeMessage:
    """最小 message 能力替身（默认查不到东西）。"""

    def __init__(self, detail: Any = None, error: Exception | None = None) -> None:
        self.detail = detail
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def get_by_id(self, message_id: str, **kwargs: Any) -> Any:
        """按 id 查消息。"""
        self.calls.append({"message_id": message_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.detail


class FakeLlm:
    """最小 llm 能力替身。"""

    def __init__(self, response: str = "总结结果", success: bool = True, error: Exception | None = None) -> None:
        self.response = response
        self.success = success
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate(self, prompt: Any, **kwargs: Any) -> dict[str, Any]:
        """返回预设的生成结果。"""
        self.calls.append({"prompt": prompt, **kwargs})
        if self.error is not None:
            raise self.error
        return {"success": self.success, "response": self.response, "model": "fake"}


class FakeContext:
    """只实现当前阶段用到的那部分 ctx。

    运行时是鸭子类型；注入路径与 Runner 一致（``MaiBotPlugin._set_context``）。
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger("test.mai_websearch")
        self.send = FakeSend()
        self.tool = FakeTool()
        self.message = FakeMessage()
        self.llm = FakeLlm()


@pytest.fixture
def plugin() -> Any:
    """返回已注入默认配置与假上下文的插件实例。"""
    instance = plugin_module.create_plugin()
    instance.set_plugin_config(instance.get_default_config())
    instance._set_context(FakeContext())
    return instance


@pytest.fixture(scope="session")
def read_fixture() -> Callable[[str], str]:
    """读取 ``tests/fixtures`` 下的文本文件。"""

    def _read(name: str) -> str:
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")

    return _read
