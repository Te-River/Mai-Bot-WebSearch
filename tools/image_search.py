"""``image_search`` 的结果渲染。

图片本身由插件直接发到聊天流（``send.image`` 只接受 base64，模型无法代劳），
所以这里渲染的是**给模型看的说明**：发了什么、从哪来的、失败时为什么。
"""

from __future__ import annotations

from ..images.pipeline import ImageSearchOutcome, describe_failure

__all__ = ["render_image_outcome"]


def render_image_outcome(outcome: ImageSearchOutcome, *, sent: int = 0) -> str:
    """渲染文搜图结果。

    Args:
        outcome: 管线结果。
        sent: 实际成功发送到聊天流的张数（发送可能单独失败）。
    """
    if not outcome.images:
        lines = [describe_failure(outcome)]
        if outcome.engine_status:
            lines.append("图片源状态：" + "；".join(f"{name} → {text}" for name, text in outcome.engine_status.items()))
        return "\n".join(lines)

    first = outcome.images[0]
    if sent <= 0:
        return "\n".join(
            [
                f"找到了「{outcome.query}」的图片，但没能发送到聊天里。",
                f"来源：{first.page_url or first.source_url}",
                "可能的原因：适配器不支持发送图片，或图片超出了平台限制。",
            ]
        )

    lines = [f"已为用户发送 {sent} 张「{outcome.query}」的图片。"]
    if first.title:
        lines.append(f"图片标题：{first.title}")
    if first.page_url:
        lines.append(f"来源页面：{first.page_url}")
    lines.append(f"来源引擎：{first.engine}（{first.mime_type}，{first.size_bytes // 1024} KB）")
    lines.append("你可以据此向用户说明这张图的内容；不要重复发送同一张图。")
    return "\n".join(lines)
