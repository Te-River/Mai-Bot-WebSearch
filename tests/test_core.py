"""core 基础设施测试：缓存、限流、熔断、冷却、并发闸门、延迟预算。"""

from __future__ import annotations

import asyncio

import pytest

from mai_websearch_under_test.core.budget import ConcurrencyGate, gather_early
from mai_websearch_under_test.core.cache import TTLCache
from mai_websearch_under_test.core.ratelimit import CircuitBreaker, Cooldown, TokenBucket


class FakeClock:
    """可手动推进的时钟，让限流/熔断测试不必真的等待。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ------------------------------------------------------------------ 缓存


def test_cache_hit_and_miss() -> None:
    """基本读写与统计。"""
    cache: TTLCache[str, int] = TTLCache(maxsize=2, ttl_seconds=10)
    assert cache.get("a") is None
    cache.set("a", 1)
    assert cache.get("a") == 1
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_cache_expires_after_ttl() -> None:
    """过期即失效，边界处（恰好到期）也算过期。"""
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(maxsize=2, ttl_seconds=10, clock=clock)
    cache.set("a", 1)
    clock.advance(9.9)
    assert cache.get("a") == 1
    clock.advance(0.2)
    assert cache.get("a") is None


def test_cache_evicts_least_recently_used() -> None:
    """容量满时淘汰最久未使用项，保证内存有界。"""
    cache: TTLCache[str, int] = TTLCache(maxsize=2, ttl_seconds=10)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # a 变成最近使用
    cache.set("c", 3)  # 应淘汰 b
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.stats()["evictions"] == 1


def test_cache_disabled_when_ttl_not_positive() -> None:
    """ttl <= 0 表示不缓存（配置项 cache_ttl_seconds=0 的语义）。"""
    cache: TTLCache[str, int] = TTLCache(maxsize=2, ttl_seconds=0)
    cache.set("a", 1)
    assert cache.get("a") is None
    assert len(cache) == 0


def test_cache_rejects_invalid_maxsize() -> None:
    """容量必须为正，否则缓存无意义。"""
    with pytest.raises(ValueError):
        TTLCache(maxsize=0, ttl_seconds=1)


# ------------------------------------------------------------------ 令牌桶


def test_token_bucket_allows_burst_then_blocks() -> None:
    """先用完突发额度，再按速率恢复。"""
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=1.0, burst=2.0, clock=clock)
    assert bucket.try_consume("h") is True
    assert bucket.try_consume("h") is True
    assert bucket.try_consume("h") is False
    clock.advance(1.0)
    assert bucket.try_consume("h") is True


def test_token_bucket_is_per_key() -> None:
    """不同主机各自计数。"""
    bucket = TokenBucket(rate_per_second=1.0, burst=1.0)
    assert bucket.try_consume("a.example") is True
    assert bucket.try_consume("b.example") is True


def test_token_bucket_bounds_tracked_keys() -> None:
    """key 数量必须有上限，否则长时间运行会内存泄漏。"""
    bucket = TokenBucket(rate_per_second=1.0, burst=1.0, max_keys=3)
    for index in range(20):
        bucket.try_consume(f"host-{index}")
    assert bucket.tracked_keys <= 3


def test_token_bucket_rejects_invalid_rate() -> None:
    """速率必须为正。"""
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=0, burst=1)


# ------------------------------------------------------------------ 熔断器


def test_breaker_opens_after_threshold() -> None:
    """连续失败达到阈值即熔断。"""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, open_seconds=300, clock=clock)
    assert breaker.allow("bing") is True
    breaker.record_failure("bing")
    assert breaker.allow("bing") is True
    breaker.record_failure("bing")
    assert breaker.allow("bing") is False
    assert breaker.retry_after("bing") == pytest.approx(300)


def test_breaker_half_opens_after_cooldown() -> None:
    """冷却结束后放行**一个**探测请求，而不是全部放行。"""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=60, clock=clock)
    breaker.record_failure("x")
    assert breaker.allow("x") is False
    clock.advance(60)
    assert breaker.allow("x") is True
    assert breaker.allow("x") is False  # 探测在途


def test_breaker_probe_failure_reopens_cooldown() -> None:
    """探测失败要重新计时，避免在坏源上反复打。"""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=60, clock=clock)
    breaker.record_failure("x")
    clock.advance(60)
    assert breaker.allow("x") is True
    breaker.record_failure("x")
    assert breaker.allow("x") is False
    clock.advance(59)
    assert breaker.allow("x") is False


def test_breaker_success_closes() -> None:
    """探测成功即恢复。"""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=60, clock=clock)
    breaker.record_failure("x")
    clock.advance(60)
    assert breaker.allow("x") is True
    breaker.record_success("x")
    assert breaker.allow("x") is True


def test_breaker_snapshot_reports_state() -> None:
    """诊断命令需要能读到熔断状态。"""
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=60, clock=FakeClock())
    breaker.record_failure("bing")
    snapshot = breaker.snapshot()
    assert snapshot["bing"]["open"] is True
    assert snapshot["bing"]["cooldown_left"] > 0


# ------------------------------------------------------------------ 冷却


def test_cooldown_blocks_within_interval() -> None:
    """同一聊天流在冷却窗口内应被拦下。"""
    clock = FakeClock()
    cooldown = Cooldown(interval_seconds=5, clock=clock)
    assert cooldown.remaining("s1") == 0
    cooldown.mark("s1")
    assert cooldown.remaining("s1") == pytest.approx(5)
    clock.advance(5)
    assert cooldown.remaining("s1") == 0
    assert cooldown.remaining("s2") == 0  # 互不影响


def test_cooldown_disabled_when_zero() -> None:
    """interval=0 表示不限流。"""
    cooldown = Cooldown(interval_seconds=0)
    cooldown.mark("s1")
    assert cooldown.remaining("s1") == 0


# ------------------------------------------------------------------ 并发闸门


def test_gate_implements_backpressure() -> None:
    """满了就快速失败，而不是让调用方排队等待。"""
    gate = ConcurrencyGate(2)
    assert gate.try_acquire() is True
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False
    assert gate.inflight == 2
    gate.release()
    assert gate.try_acquire() is True


def test_gate_release_is_idempotent_at_zero() -> None:
    """多余的 release 不应把计数压成负数。"""
    gate = ConcurrencyGate(1)
    gate.release()
    assert gate.inflight == 0


# ------------------------------------------------------------------ 提前返回


async def _after(delay: float, value: str) -> str:
    await asyncio.sleep(delay)
    return value


async def test_gather_early_returns_everything_when_fast() -> None:
    """都很快时不应取消任何人。"""
    result = await gather_early(
        {"a": _after(0.01, "A"), "b": _after(0.02, "B")},
        quorum=2,
        grace_seconds=0.05,
        deadline_seconds=1.0,
    )
    assert result.values == {"a": "A", "b": "B"}
    assert result.cancelled == []
    assert result.deadline_hit is False


async def test_gather_early_cancels_slow_engine_after_quorum() -> None:
    """这是"快"的核心机制：够用了就取消慢的。"""

    async def never() -> str:
        await asyncio.sleep(30)
        return "X"

    result = await gather_early(
        {"fast": _after(0.01, "F"), "slow": never()},
        quorum=1,
        grace_seconds=0.05,
        deadline_seconds=30.0,
    )
    assert result.values == {"fast": "F"}
    assert result.cancelled == ["slow"]
    assert result.deadline_hit is False


async def test_gather_early_grace_window_lets_slower_engine_in() -> None:
    """宽限窗口的意义：避免"最快的两个恰好是最差的"。"""
    result = await gather_early(
        {"fast": _after(0.01, "F"), "better": _after(0.05, "B")},
        quorum=1,
        grace_seconds=0.2,
        deadline_seconds=5.0,
    )
    assert result.values == {"fast": "F", "better": "B"}


async def test_gather_early_respects_deadline() -> None:
    """硬截止必须先于一切：宁可返回部分结果也不能挂死。"""

    async def slow() -> str:
        await asyncio.sleep(30)
        return "S"

    result = await gather_early(
        {"a": slow(), "b": slow()},
        quorum=1,
        grace_seconds=5.0,
        deadline_seconds=0.05,
    )
    assert result.deadline_hit is True
    assert sorted(result.cancelled) == ["a", "b"]
    assert result.values == {}


async def test_gather_early_captures_errors_without_failing_others() -> None:
    """单个引擎失败不能影响其它引擎。"""

    async def boom() -> str:
        raise RuntimeError("炸了")

    result = await gather_early(
        {"ok": _after(0.01, "V"), "bad": boom()},
        quorum=2,
        grace_seconds=0.05,
        deadline_seconds=1.0,
    )
    assert result.values == {"ok": "V"}
    assert "RuntimeError" in result.errors["bad"]


async def test_gather_early_handles_empty_input() -> None:
    """没有引擎时不应抛异常。"""
    result = await gather_early({}, quorum=1, grace_seconds=0.05, deadline_seconds=1.0)
    assert result.values == {}
    assert result.cancelled == []
    assert result.settled == 0
