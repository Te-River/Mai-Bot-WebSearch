"""出站 HTTP 客户端。

一个长生命周期的 ``httpx.AsyncClient``（keep-alive 连接池）常驻，省掉每次查询的
TCP+TLS 握手（100–300ms）；配合令牌桶与熔断器实现"错了就快速失败，不重试不等待"。

客户端**惰性创建**：``on_load`` 只登记配置、不做任何 I/O，满足 ≤300ms 的加载预算。

两条读取路径：``fetch`` 返回解码后的文本，``fetch_bytes`` 返回原始字节（图片下载用）。
两者共用同一套代理/限流/熔断/体积上限逻辑。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import CircuitOpenError, HttpStatusError, NetworkError, RateLimitedError, SearchError
from .ratelimit import CircuitBreaker, TokenBucket

__all__ = ["DEFAULT_USER_AGENTS", "FetchBytesResult", "FetchResult", "HttpClient", "resolve_proxy"]

DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)

_CHARSET_RE = re.compile(r"charset=[\"']?([\w\-]+)", re.IGNORECASE)

# 代理环境变量，按优先级排列（含小写形式，Windows 上大小写不敏感但保持显式更安全）
_PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


def resolve_proxy(mode: str, proxy: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """按配置与环境变量解析实际使用的代理地址。

    ``mode="auto"`` 会读取 ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY``——
    "麦麦连不上网"最常见的一步修复就在这里。
    """
    if mode == "off":
        return None
    if mode == "manual":
        return (proxy or "").strip() or None
    source: Mapping[str, str] = env if env is not None else os.environ
    for key in _PROXY_ENV_KEYS:
        value = source.get(key)
        if value and value.strip():
            return value.strip()
    return None


@dataclass(slots=True)
class FetchResult:
    """一次文本抓取的结果。"""

    status: int
    url: str
    content_type: str
    text: str
    truncated: bool
    elapsed_ms: int
    # 读取响应头是手动跟随重定向的前提（需要 Location）
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class FetchBytesResult:
    """一次二进制抓取的结果（图片下载用）。"""

    status: int
    url: str
    content_type: str
    content: bytes
    truncated: bool
    elapsed_ms: int


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """流式读取并在超过上限时立即断开，避免大文件把内存打爆。"""
    chunks: list[bytes] = []
    size = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        remaining = max_bytes - size
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            truncated = True
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), truncated


def _decode(raw: bytes, content_type: str) -> str:
    """按声明字符集解码；未声明时先试 UTF-8，再退 GB18030（大量中文站点不声明 charset）。"""
    match = _CHARSET_RE.search(content_type or "")
    if match:
        try:
            return raw.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


class HttpClient:
    """共享的出站 HTTP 客户端。"""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout_seconds: float = 10.0,
        verify_tls: bool = True,
        user_agents: list[str] | None = None,
        rate_per_second: float = 2.0,
        burst: float = 4.0,
        breaker: CircuitBreaker | None = None,
        max_connections: int = 8,
    ) -> None:
        self._proxy = proxy
        self._timeout = timeout_seconds
        self._verify_tls = verify_tls
        self._user_agents = tuple(user_agents) if user_agents else DEFAULT_USER_AGENTS
        self._limiter = TokenBucket(rate_per_second=rate_per_second, burst=burst)
        self._breaker = breaker or CircuitBreaker()
        self._max_connections = max_connections
        self._client: httpx.AsyncClient | None = None
        self._ua_index = 0

    @property
    def breaker(self) -> CircuitBreaker:
        """熔断器（供诊断命令读取健康状况）。"""
        return self._breaker

    def _pick_user_agent(self) -> str:
        agent = self._user_agents[self._ua_index % len(self._user_agents)]
        self._ua_index += 1
        return agent

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # 不启用 HTTP/2：需要额外的 h2 依赖，而 keep-alive 已经拿到了主要收益
            self._client = httpx.AsyncClient(
                proxy=self._proxy,
                verify=self._verify_tls,
                limits=httpx.Limits(
                    max_connections=self._max_connections,
                    max_keepalive_connections=max(2, self._max_connections // 2),
                ),
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
            )
        return self._client

    async def _request(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str] | None,
        max_bytes: int,
        timeout_seconds: float | None,
        breaker_key: str,
        follow_redirects: bool,
        data: Mapping[str, Any] | None,
        files: Any,
        json_body: Any,
        accept: str,
    ) -> tuple[int, str, str, bytes, bool, int, Mapping[str, str]]:
        """执行请求并返回原始字节（文本/二进制两条路径共用）。"""
        host = urlsplit(url).hostname or "unknown"
        if breaker_key and not self._breaker.allow(breaker_key):
            raise CircuitOpenError(breaker_key, self._breaker.retry_after(breaker_key))
        if not self._limiter.try_consume(host):
            raise RateLimitedError(host)

        client = await self._ensure_client()
        merged_headers: dict[str, str] = {
            "User-Agent": self._pick_user_agent(),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": accept,
        }
        if headers:
            merged_headers.update(headers)

        started = time.monotonic()
        try:
            async with client.stream(
                method,
                url,
                headers=merged_headers,
                timeout=timeout_seconds or self._timeout,
                follow_redirects=follow_redirects,
                data=data,
                files=files,
                json=json_body,
            ) as response:
                status = response.status_code
                content_type = response.headers.get("content-type", "")
                final_url = str(response.url)
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                if status >= 400:
                    raise HttpStatusError(status, final_url)
                raw, truncated = await _read_capped(response, max_bytes)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._record(breaker_key, ok=False)
            raise NetworkError(type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            self._record(breaker_key, ok=False)
            raise NetworkError(type(exc).__name__) from exc
        except SearchError:
            self._record(breaker_key, ok=False)
            raise

        self._record(breaker_key, ok=True)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return status, final_url, content_type, raw, truncated, elapsed_ms, response_headers

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        max_bytes: int = 524288,
        timeout_seconds: float | None = None,
        breaker_key: str = "",
        follow_redirects: bool = True,
        data: Mapping[str, Any] | None = None,
        files: Any = None,
        json_body: Any = None,
    ) -> FetchResult:
        """抓取一个 URL 并返回解码后的文本。

        Raises:
            CircuitOpenError: 该 ``breaker_key`` 处于熔断状态。
            RateLimitedError: 该主机触发本地令牌桶限流。
            HttpStatusError: 返回 4xx/5xx。
            NetworkError: 连接、TLS 或超时失败。
        """
        status, final_url, content_type, raw, truncated, elapsed_ms, response_headers = await self._request(
            url,
            method=method,
            headers=headers,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            breaker_key=breaker_key,
            follow_redirects=follow_redirects,
            data=data,
            files=files,
            json_body=json_body,
            accept="text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        )
        return FetchResult(
            status=status,
            url=final_url,
            content_type=content_type,
            text=_decode(raw, content_type),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
            headers=response_headers,
        )

    async def fetch_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = 3145728,
        timeout_seconds: float | None = None,
        breaker_key: str = "",
        follow_redirects: bool = True,
    ) -> FetchBytesResult:
        """抓取一个 URL 并返回**原始字节**（图片等二进制内容）。

        文本路径会对字节做字符集解码，二进制走那条路会被破坏，所以必须分开。
        """
        status, final_url, content_type, raw, truncated, elapsed_ms, _ = await self._request(
            url,
            method="GET",
            headers=headers,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            breaker_key=breaker_key,
            follow_redirects=follow_redirects,
            data=None,
            files=None,
            json_body=None,
            accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        )
        return FetchBytesResult(
            status=status,
            url=final_url,
            content_type=content_type,
            content=raw,
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    def _record(self, breaker_key: str, *, ok: bool) -> None:
        if not breaker_key:
            return
        if ok:
            self._breaker.record_success(breaker_key)
        else:
            self._breaker.record_failure(breaker_key)

    async def aclose(self) -> None:
        """关闭连接池；卸载与配置热更新时必须调用，避免残留句柄。"""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
