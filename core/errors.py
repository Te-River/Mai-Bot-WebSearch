"""统一异常与用户可读文案。

设计原则：网络异常必须让用户看见原因，**绝不静默返回空结果**。
所以每个异常都带一个 ``user_message``，由工具层直接呈现给聊天窗口。
"""

from __future__ import annotations

import asyncio

__all__ = [
    "BlockedAddressError",
    "CircuitOpenError",
    "ContentTooLargeError",
    "EmptyQueryError",
    "HttpStatusError",
    "NetworkError",
    "ProviderError",
    "RateLimitedError",
    "SearchError",
    "TooManyRedirectsError",
    "TooManyRequestsError",
    "UnsupportedContentTypeError",
    "describe_error",
]


class SearchError(Exception):
    """本插件所有可预期错误的基类。"""

    def __init__(self, message: str, *, user_message: str = "") -> None:
        super().__init__(message)
        self.user_message = user_message or message


class EmptyQueryError(SearchError):
    """查询为空。"""

    def __init__(self) -> None:
        super().__init__("查询为空", user_message="请告诉我要搜什么")


class BlockedAddressError(SearchError):
    """URL 协议不被允许，或指向内网 / 回环 / 链路本地 / 云元数据地址。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"地址被拒绝：{reason}", user_message=f"这个地址不能访问（{reason}）")


class RateLimitedError(SearchError):
    """被本地令牌桶限流。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"本地限流：{key}", user_message="请求太频繁了，稍等一下再试")


class CircuitOpenError(SearchError):
    """熔断器打开，该引擎暂时不可用。"""

    def __init__(self, key: str, retry_after: float) -> None:
        super().__init__(
            f"熔断中：{key}（{retry_after:.0f}s 后半开）",
            user_message="这个搜索源刚连续失败，已暂时跳过",
        )


class ProviderError(SearchError):
    """某个引擎自身失败（解析不出结果、返回异常页面等）。"""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider


class NetworkError(SearchError):
    """出站网络失败。"""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            user_message=f"网络请求失败（{message}）。如果麦麦需要通过代理出网，请在插件配置的 network 段填写代理",
        )


class HttpStatusError(SearchError):
    """HTTP 状态码异常。"""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status}：{url}", user_message=f"对方返回了 HTTP {status}")


class ContentTooLargeError(SearchError):
    """响应体超过配置上限。"""

    def __init__(self, limit: int) -> None:
        super().__init__(f"内容超过 {limit} 字节", user_message="这个页面太大了，已放弃读取")


class TooManyRequestsError(SearchError):
    """并发已满（背压）：快速失败，而不是把请求堆在队列里让用户等更久。"""

    def __init__(self) -> None:
        super().__init__("并发已满", user_message="当前搜索任务较多，请稍后再试")


class TooManyRedirectsError(SearchError):
    """重定向次数超过上限。

    手动跟随重定向的目的就是**每一跳都重新做 SSRF 校验**，
    因此这个上限既是资源保护，也是防止用重定向链把校验绕过去的兜底。
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"重定向超过 {limit} 跳", user_message="这个链接跳转太多次了，已放弃")


class UnsupportedContentTypeError(SearchError):
    """响应的内容类型不是可读文本。"""

    def __init__(self, content_type: str) -> None:
        super().__init__(
            f"不支持的内容类型：{content_type or '(空)'}",
            user_message="这个链接不是网页正文（可能是文件或媒体），我没法读",
        )


def describe_error(exc: BaseException) -> str:
    """把任意异常转成一句可以发给用户的话。"""
    if isinstance(exc, SearchError) and exc.user_message:
        return exc.user_message
    if isinstance(exc, TimeoutError):
        return "请求超时了"
    if isinstance(exc, asyncio.CancelledError):
        return "请求被取消了"
    return f"出错了：{exc}"
