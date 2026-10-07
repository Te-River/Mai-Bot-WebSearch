"""以图搜源（反查）引擎的公共接口。

**先读这段实测结论，再决定要不要加新引擎：**

===========================  ==========================================
引擎                          本环境实测结果（2026，直连）
===========================  ==========================================
ascii2d                      域名不可达（ConnectError）
IQDB                         域名不可达（ConnectError）
SauceNAO                     域名不可达（ConnectError）
Google Lens / TinEye         连接超时
Yandex                       只返回 1778 字符的壳页面，无结果
Bing 视觉搜索（URL 式）        降级成文本图片搜索，无 insightsToken
Bing 视觉搜索（上传式）        302 回自身 / multipart 400（需浏览器会话）
百度识图                      ``{"status":1,"msg":"Reject"}``（反爬）
===========================  ==========================================

也就是说：**在不少网络（含本项目实测环境）下，没有任何可用的反查引擎**。
因此本层的定位是"有就用、没有就明确告知"，而不是插件的主要能力路径：

* **图搜文**：主要靠宿主的官方多模态通道——把用户图片通过 ``content_items`` 交回宿主，
  由宿主按模型 ``visual`` 能力决定是否喂给模型。**这与反查引擎无关**。
* **图搜图**：主要靠"模型看图 → 提取关键词 → 调用 ``image_search``"这条链，
  也不依赖反查引擎。
* 反查引擎能在可用网络下补上"这张图出自哪部作品/哪个角色"这类**来源识别**。

因为爬取类引擎既不可达又需要反爬对抗，本层**只实现有公开文档的 JSON API**
（目前是 SauceNAO），不做 HTML 爬虫——不可验证的解析代码不该进仓库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["ReverseLookupResult", "ReverseProvider", "ReverseSource"]


@dataclass(slots=True)
class ReverseSource:
    """一条来源识别结果。"""

    title: str = ""
    author: str = ""
    similarity: float = 0.0
    index: str = ""
    urls: list[str] = field(default_factory=list)
    thumbnail: str = ""


@dataclass(slots=True)
class ReverseLookupResult:
    """一次反查的汇总。"""

    engine: str
    sources: list[ReverseSource] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        """是否拿到了来源信息。"""
        return bool(self.sources)


@runtime_checkable
class ReverseProvider(Protocol):
    """反查引擎协议。"""

    name: str
    requires_key: bool

    @property
    def configured(self) -> bool:
        """是否具备运行条件（通常是有没有 API Key）。"""
        ...

    async def lookup(self, image: Any, http: Any) -> ReverseLookupResult:
        """用图片（或图片地址）反查来源。"""
        ...
