"""限流与熔断。

交互路径上**不做指数退避重试**：连续失败的源直接熔断，把预算让给健康源；
本地令牌桶则避免我们把对方打到封 IP。
两者都接受注入的 ``clock``，便于离线单测（不需要真的等待）。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["CircuitBreaker", "Cooldown", "TokenBucket"]


class TokenBucket:
    """按 key 限流的令牌桶（默认按主机名）。

    桶数量有上限，超出时淘汰最久未使用的 key，避免长时间运行下字典无限增长。
    """

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: float,
        max_keys: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second 必须为正数")
        self._rate = rate_per_second
        self._burst = max(burst, 1.0)
        self._max_keys = max(max_keys, 1)
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def try_consume(self, key: str, amount: float = 1.0) -> bool:
        """尝试消费令牌；成功返回 ``True``。"""
        now = self._clock()
        tokens, last_ts = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + max(0.0, now - last_ts) * self._rate)
        if tokens < amount:
            self._buckets[key] = (tokens, now)
            self._buckets.move_to_end(key)
            return False
        self._buckets[key] = (tokens - amount, now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
        return True

    @property
    def tracked_keys(self) -> int:
        """当前跟踪的 key 数量（用于验证内存有界）。"""
        return len(self._buckets)


@dataclass(slots=True)
class _BreakerState:
    """单个 key 的熔断状态。"""

    failures: int = 0
    opened_at: float = 0.0
    probing: bool = False


@dataclass(slots=True)
class CircuitBreaker:
    """连续失败即熔断，冷却后放一个探测请求（半开）。

    ``record_failure`` 达到阈值即打开；打开期间 ``allow`` 返回 ``False``；
    冷却结束后允许**一个**探测请求，成功则闭合，失败则重新计时。
    """

    failure_threshold: int = 3
    open_seconds: float = 300.0
    max_keys: int = 128
    clock: Callable[[], float] = time.monotonic
    _states: OrderedDict[str, _BreakerState] = field(default_factory=OrderedDict)

    def __post_init__(self) -> None:
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold 必须为正整数")

    def allow(self, key: str) -> bool:
        """当前是否允许发起请求。

        半开状态下**只放行一个探测请求**：否则冷却结束后会瞬间把流量全打回坏源，
        熔断就失去意义了。
        """
        state = self._states.get(key)
        if state is None:
            return True
        if state.probing:
            return False
        if state.failures < self.failure_threshold:
            return True
        if self.clock() - state.opened_at >= self.open_seconds:
            state.probing = True
            return True
        return False

    def retry_after(self, key: str) -> float:
        """距离半开还有多少秒；未熔断返回 0。"""
        state = self._states.get(key)
        if state is None or state.failures < self.failure_threshold:
            return 0.0
        return max(0.0, self.open_seconds - (self.clock() - state.opened_at))

    def record_success(self, key: str) -> None:
        """请求成功：闭合熔断器。"""
        state = self._states.get(key)
        if state is None:
            return
        state.failures = 0
        state.probing = False
        state.opened_at = 0.0

    def record_failure(self, key: str) -> None:
        """请求失败：累计失败数，达到阈值则打开。"""
        state = self._states.setdefault(key, _BreakerState())
        if state.probing:
            state.probing = False
            state.failures = self.failure_threshold
            state.opened_at = self.clock()
        else:
            state.failures += 1
            if state.failures >= self.failure_threshold:
                state.opened_at = self.clock()
        self._states.move_to_end(key)
        while len(self._states) > self.max_keys:
            self._states.popitem(last=False)

    def snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        """返回各 key 的健康状态，供诊断命令展示。"""
        now = self.clock()
        return {
            key: {
                "failures": state.failures,
                "open": state.failures >= self.failure_threshold,
                "cooldown_left": max(0.0, self.open_seconds - (now - state.opened_at))
                if state.failures >= self.failure_threshold
                else 0.0,
            }
            for key, state in self._states.items()
        }


class Cooldown:
    """同一 key（通常是聊天流）的最小调用间隔。"""

    def __init__(
        self,
        *,
        interval_seconds: float,
        max_keys: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = max(0.0, interval_seconds)
        self._max_keys = max(max_keys, 1)
        self._clock = clock
        self._last: OrderedDict[str, float] = OrderedDict()

    def remaining(self, key: str) -> float:
        """距离可以再次调用还有多少秒；0 表示可以。"""
        if self._interval <= 0:
            return 0.0
        last = self._last.get(key)
        if last is None:
            return 0.0
        return max(0.0, self._interval - (self._clock() - last))

    def mark(self, key: str) -> None:
        """记录一次调用。"""
        self._last[key] = self._clock()
        self._last.move_to_end(key)
        while len(self._last) > self._max_keys:
            self._last.popitem(last=False)

    def clear(self) -> None:
        """清空记录。"""
        self._last.clear()
