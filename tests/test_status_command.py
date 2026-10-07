"""``/websearch`` 命令的离线测试。

Command 处理函数必须返回三元组 ``(success: bool, response: str, weight: int)``。
"""

from __future__ import annotations

from typing import Any

import pytest


async def test_status_returns_triple(plugin: Any) -> None:
    """返回契约：三元组且 success 为 True。"""
    result = await plugin.handle_websearch(stream_id="stream-1", matched_groups={})
    assert isinstance(result, tuple) and len(result) == 3
    success, response, weight = result
    assert success is True
    assert isinstance(response, str) and response
    assert isinstance(weight, int)


async def test_status_sends_to_stream(plugin: Any) -> None:
    """有 stream_id 时必须把文本发出去，而不只是返回给调用方。"""
    await plugin.handle_websearch(stream_id="stream-1", matched_groups={})
    texts = plugin.ctx.send.texts
    assert len(texts) == 1
    text, stream_id = texts[0]
    assert stream_id == "stream-1"
    assert "麦麦联网搜索" in text


async def test_status_mentions_no_vlm_dependency(plugin: Any) -> None:
    """状态输出要明确告知：图像理解由宿主自动判定，本插件不依赖 vlm 任务。"""
    _, response, _ = await plugin.handle_websearch(stream_id="s", matched_groups={})
    assert "vlm" in response


async def test_unknown_subcommand_fails_gracefully(plugin: Any) -> None:
    """未知子命令返回失败而不是抛异常。"""
    success, response, weight = await plugin.handle_websearch(
        stream_id="stream-1", matched_groups={"action": "bogus"}
    )
    assert success is False
    assert "bogus" in response
    assert weight == 1


async def test_missing_stream_id_does_not_send(plugin: Any) -> None:
    """没有 stream_id 时只返回文本，不调用 send。"""
    success, response, _ = await plugin.handle_websearch(stream_id="", matched_groups={})
    assert success is True
    assert response
    assert plugin.ctx.send.texts == []


@pytest.mark.parametrize("action", ["status", "STATUS", "s"])
async def test_status_aliases(plugin: Any, action: str) -> None:
    """status 与其简写、大小写都应被接受。"""
    success, _, _ = await plugin.handle_websearch(stream_id="s", matched_groups={"action": action})
    assert success is True
