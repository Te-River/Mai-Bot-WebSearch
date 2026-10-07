"""URL 规范化与 SSRF 防护。

抓取用户给的任意 URL 是典型 SSRF 面：必须拒绝回环 / 内网 / 链路本地 / 云元数据地址。
字面 IP 直接判定；域名则解析后**对每一个解析结果**判定，避免 DNS 指向内网。

注意：重定向每一跳都要重新调用 ``ensure_public_url``（由调用方在手动跟随重定向时保证），
否则攻击者可以用一个公网 URL 302 到 ``169.254.169.254``。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import BlockedAddressError

__all__ = [
    "ALLOWED_SCHEMES",
    "TRACKING_PARAMS",
    "ensure_public_url",
    "ip_block_reason",
    "looks_like_url",
    "normalize_url",
]

ALLOWED_SCHEMES = frozenset({"http", "https"})

# 单 token 的"带路径的域名"，例如 example.com/a、mzh.moegirl.org.cn/wiki/x
_DOTTED_HOST_WITH_PATH_RE = re.compile(r"^[\w-]+(?:\.[\w-]+)+(?:/[^\s]*)?$")


def looks_like_url(text: str) -> bool:
    """判断用户给的字符串是否应被当作 URL 而非搜索词。

    刻意保守，避免把普通词误判成网址：

    * 带 ``http(s)://`` 前缀 → 是；
    * ``www.`` 开头 → 是；
    * "带点的域名 **且** 有路径" → 是（``example.com/a``）；
    * 光秃秃的 ``baidu.com``、``readme.md`` → 否，交给搜索引擎更稳妥。
    """
    candidate = (text or "").strip()
    if not candidate or " " in candidate:
        return False
    lowered = candidate.lower()
    if lowered.startswith(("http://", "https://")):
        return True
    if lowered.startswith("www."):
        return True
    if "/" not in candidate:
        return False
    return bool(_DOTTED_HOST_WITH_PATH_RE.match(candidate))

# 常见跟踪参数：读网页时去掉，既提高缓存命中率，也少给对方送分析数据
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "spm",
        "share_source",
        "share_medium",
        "fbclid",
        "gclid",
        "yclid",
        "from_source",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}

Resolver = Callable[[str], Awaitable[list[str]]]


def ip_block_reason(ip_text: str) -> str | None:
    """判定一个 IP 字面量是否应被拒绝；允许时返回 ``None``。"""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return None
    if ip.is_unspecified:
        return "未指定地址"
    if ip.is_loopback:
        return "回环地址"
    if ip.is_link_local:
        # 覆盖 169.254.169.254（云元数据服务）
        return "链路本地地址"
    if ip.is_private:
        return "内网地址"
    if ip.is_multicast:
        return "组播地址"
    if ip.is_reserved:
        return "保留地址"
    return None


def normalize_url(url: str, *, strip_tracking: bool = True) -> str:
    """规范化 URL：校验协议、去 fragment、去跟踪参数、主机小写、去默认端口。

    Raises:
        BlockedAddressError: 协议不被允许、缺少主机名，或 URL 中携带账号密码。
    """
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedAddressError(f"不支持的协议 {scheme or '(空)'}")

    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedAddressError("缺少主机名")
    if parts.username or parts.password:
        raise BlockedAddressError("URL 中不允许携带账号密码")

    # IPv6 需要重新加回方括号
    netloc = f"[{host}]" if ":" in host else host
    port = parts.port
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"

    query = parts.query
    if strip_tracking and query:
        pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
        query = urlencode(pairs)

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


async def _resolve_default(host: str) -> list[str]:
    """默认解析器：在线程里做 DNS，避免阻塞事件循环。"""
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
    return [str(info[4][0]) for info in infos]


async def ensure_public_url(url: str, *, resolver: Resolver | None = None, allow_private: bool = False) -> str:
    """规范化并确认目标不是内网地址，返回可安全抓取的 URL。

    Args:
        url: 待检查的 URL。
        resolver: 可注入的解析器（测试用）；默认走真实 DNS。
        allow_private: 仅用于本机自建服务（如 127.0.0.1 上的 SearXNG）。

    Raises:
        BlockedAddressError: 协议不允许，或主机解析到被封禁的地址。
    """
    normalized = normalize_url(url)
    if allow_private:
        return normalized

    host = urlsplit(normalized).hostname or ""

    literal_reason = ip_block_reason(host)
    if literal_reason is not None:
        raise BlockedAddressError(f"{host} 是{literal_reason}")

    # 字面 IP 无需解析；域名必须解析后逐个检查
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return normalized

    resolve = resolver or _resolve_default
    try:
        addresses = await resolve(host)
    except OSError as exc:
        raise BlockedAddressError(f"域名解析失败（{exc}）") from exc

    if not addresses:
        raise BlockedAddressError(f"{host} 无法解析")

    for address in addresses:
        reason = ip_block_reason(address)
        if reason is not None:
            raise BlockedAddressError(f"{host} 解析到{reason} {address}")
    return normalized
