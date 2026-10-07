"""URL 规范化与 SSRF 防护测试。

抓取用户给的任意 URL 是本插件最大的攻击面，这些用例是安全底线。
"""

from __future__ import annotations

import pytest

from mai_websearch_under_test.core.errors import BlockedAddressError
from mai_websearch_under_test.core.ssrf import ensure_public_url, ip_block_reason, normalize_url


def test_strips_fragment_and_tracking_params() -> None:
    """fragment 与跟踪参数都应被去掉。"""
    assert normalize_url("https://Example.COM/a/b?utm_source=x&spm=y&id=1#frag") == "https://example.com/a/b?id=1"


def test_lowercases_host_and_fills_root_path() -> None:
    """主机小写；空路径补成根路径。"""
    assert normalize_url("https://EXAMPLE.com") == "https://example.com/"


def test_drops_default_ports() -> None:
    """默认端口应被省略，非默认端口保留。"""
    assert normalize_url("http://example.com:80/x") == "http://example.com/x"
    assert normalize_url("https://example.com:443/") == "https://example.com/"
    assert normalize_url("https://example.com:8443/x") == "https://example.com:8443/x"


def test_keeps_meaningful_query_params() -> None:
    """有意义的查询参数必须保留。"""
    assert "q=" in normalize_url("https://example.com/s?q=hello%20world&utm_medium=z")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "data:text/html,<b>x</b>",
        "javascript:alert(1)",
        "gopher://example.com/",
        "",
    ],
)
def test_rejects_non_http_schemes(url: str) -> None:
    """非 http(s) 协议一律拒绝。"""
    with pytest.raises(BlockedAddressError):
        normalize_url(url)


def test_rejects_credentials_in_url() -> None:
    """URL 中携带账号密码会绕过主机判定，必须拒绝。"""
    with pytest.raises(BlockedAddressError):
        normalize_url("https://user:pass@example.com/")


@pytest.mark.parametrize(
    ("ip", "reason"),
    [
        ("127.0.0.1", "回环地址"),
        ("::1", "回环地址"),
        ("10.1.2.3", "内网地址"),
        ("192.168.1.1", "内网地址"),
        ("172.16.5.5", "内网地址"),
        ("169.254.169.254", "链路本地地址"),
        ("0.0.0.0", "未指定地址"),
        ("8.8.8.8", None),
        ("1.1.1.1", None),
    ],
)
def test_ip_block_reason(ip: str, reason: str | None) -> None:
    """内网 / 回环 / 元数据地址必须被识别，公网地址放行。"""
    assert ip_block_reason(ip) == reason


async def test_rejects_literal_metadata_address() -> None:
    """云元数据地址是最典型的 SSRF 目标。"""
    with pytest.raises(BlockedAddressError):
        await ensure_public_url("http://169.254.169.254/latest/meta-data/")


async def test_rejects_domain_resolving_to_private() -> None:
    """域名解析到内网也必须拒绝（DNS 指向内网的经典绕过）。"""

    async def resolver(host: str) -> list[str]:
        return ["192.168.0.10"]

    with pytest.raises(BlockedAddressError):
        await ensure_public_url("https://evil.example/", resolver=resolver)


async def test_rejects_when_any_resolved_address_is_private() -> None:
    """多 A 记录里只要有一个内网地址就拒绝（否则可被轮询命中）。"""

    async def resolver(host: str) -> list[str]:
        return ["93.184.216.34", "10.0.0.1"]

    with pytest.raises(BlockedAddressError):
        await ensure_public_url("https://mixed.example/", resolver=resolver)


async def test_allows_public_domain() -> None:
    """全部解析到公网则放行。"""

    async def resolver(host: str) -> list[str]:
        return ["93.184.216.34"]

    assert await ensure_public_url("https://example.com/x", resolver=resolver) == "https://example.com/x"


async def test_literal_public_ip_skips_dns() -> None:
    """字面公网 IP 不应触发 DNS 解析（省一次查询，也避免被解析器拖慢）。"""
    called = False

    async def resolver(host: str) -> list[str]:
        nonlocal called
        called = True
        return []

    assert await ensure_public_url("http://93.184.216.34/", resolver=resolver)
    assert called is False


async def test_reports_dns_failure() -> None:
    """解析失败要给出明确原因，而不是放任请求发出去。"""

    async def resolver(host: str) -> list[str]:
        raise OSError("name resolution failed")

    with pytest.raises(BlockedAddressError, match="解析失败"):
        await ensure_public_url("https://nx.example/", resolver=resolver)


async def test_allow_private_bypasses_guard() -> None:
    """本机自建服务（如 127.0.0.1 上的 SearXNG）需要显式开关才放行。"""
    url = "http://127.0.0.1:8888/search?q=x"
    assert await ensure_public_url(url, allow_private=True) == url
    with pytest.raises(BlockedAddressError):
        await ensure_public_url(url)
