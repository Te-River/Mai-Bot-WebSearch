"""图片通路的公共类型与判定辅助。"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

__all__ = [
    "IMAGE_MIME_TYPES",
    "DownloadedImage",
    "ImageCandidate",
    "normalize_mime",
    "sniff_image_mime",
]

# 可发送且可被模型观察的图片类型
IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"})

# 各图片格式的魔数：不少 CDN 会把图片标成 application/octet-stream，靠内容兜底
_MAGIC_PREFIXES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_mime(content: bytes) -> str | None:
    """从内容头判断图片类型；不是已知图片则返回 ``None``。"""
    if len(content) < 12:
        return None
    for prefix, mime in _MAGIC_PREFIXES:
        if content.startswith(prefix):
            return mime
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return None


def normalize_mime(content_type: str) -> str:
    """去掉 charset 等参数并小写。"""
    return (content_type or "").split(";")[0].strip().lower()


@dataclass(slots=True)
class ImageCandidate:
    """图片搜索引擎给出的一条候选。"""

    url: str
    thumbnail_url: str = ""
    title: str = ""
    page_url: str = ""
    engine: str = ""
    width: int = 0
    height: int = 0
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DownloadedImage:
    """下载并校验通过的图片。"""

    content: bytes
    mime_type: str
    source_url: str
    engine: str
    title: str = ""
    page_url: str = ""
    thumbnail_url: str = ""
    width: int = 0
    height: int = 0

    @property
    def size_bytes(self) -> int:
        """字节数。"""
        return len(self.content)

    @property
    def sha256(self) -> str:
        """内容哈希，用于跨查询去重。"""
        return hashlib.sha256(self.content).hexdigest()

    def to_base64(self) -> str:
        """转成 ``ctx.send.image`` 需要的 base64。"""
        return base64.b64encode(self.content).decode("ascii")

    def to_content_item(self) -> dict[str, str]:
        """转成 Tool 返回值的 ``content_items`` 条目。

        官方机制：宿主会把它拆成"文本 Tool Result + 一条普通 user 图片消息"，
        再由宿主按模型 ``visual`` 能力决定是否真的带图——**不要自己拼多模态 prompt**。
        """
        return {
            "type": "image",
            "data": self.to_base64(),
            "mime_type": self.mime_type,
            "name": self.title or "image",
            "description": self.title,
        }
