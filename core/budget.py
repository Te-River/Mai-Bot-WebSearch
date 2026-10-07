"""延迟预算：全局截止、提前返回、并发背压。

这是"高频聊天场景"的核心保障：**宁可少返回一点，也不能让用户干等**。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Generic, TypeVar

__all__ = ["ConcurrencyGate", "EarlyGatherResult", "gather_early"]

T = TypeVar("T")


@dataclass(slots=True)
class EarlyGatherResult(Generic[T]):
    """``gather_early`` 的结果：成功值、失败原因与被取消的键。"""

    values: dict[str, T] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    deadline_hit: bool = False

    @property
    def settled(self) -> int:
        """已完成（成功 + 失败）的任务数。"""
        return len(self.values) + len(self.errors)


async def gather_early(
    awaitables: Mapping[str, Awaitable[T]],
    *,
    quorum: int,
    grace_seconds: float,
    deadline_seconds: float,
) -> EarlyGatherResult[T]:
    """并发执行，并在"够用了"的时候提前返回，同时取消剩余任务。

    三种收尾条件（先到者生效）：

    1. 全部完成；
    2. 成功数量达到 ``quorum`` 后，再等 ``grace_seconds``；
    3. 到达 ``deadline_seconds``。

    宽限窗口的作用：避免"最快的两个引擎恰好是最差的"就丢掉了更优质引擎的结果。
    """
    result: EarlyGatherResult[T] = EarlyGatherResult()
    if not awaitables:
        return result

    loop = asyncio.get_running_loop()
    deadline_at = loop.time() + max(deadline_seconds, 0.0)
    tasks = {key: asyncio.ensure_future(awaitable) for key, awaitable in awaitables.items()}
    key_by_task = {task: key for key, task in tasks.items()}
    pending: set[asyncio.Task[T]] = set(tasks.values())
    quorum_at: float | None = None

    try:
        while pending:
            now = loop.time()
            if now >= deadline_at:
                result.deadline_hit = True
                break

            time_to_deadline = deadline_at - now
            if quorum_at is None:
                wait_for = time_to_deadline
                # 等待被截断时，原因只可能是截止时间
                timeout_means_deadline = True
            else:
                time_to_grace = quorum_at + grace_seconds - now
                if time_to_grace <= 0:
                    break  # 宽限窗口用尽，正常提前返回
                wait_for = min(time_to_deadline, time_to_grace)
                # 谁更短，等待超时就该归因给谁
                timeout_means_deadline = time_to_deadline <= time_to_grace

            done, pending = await asyncio.wait(pending, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                # 不能靠重新读时钟判断（定时器精度会让它偶发判错），
                # 而要按"这次等待是被谁截断的"来归因。
                result.deadline_hit = timeout_means_deadline
                break

            for task in done:
                if task.cancelled():
                    continue
                key = key_by_task[task]
                error = task.exception()
                if error is None:
                    result.values[key] = task.result()
                else:
                    result.errors[key] = f"{type(error).__name__}: {error}"

            if quorum_at is None and len(result.values) >= quorum:
                quorum_at = loop.time()
    finally:
        for task in pending:
            result.cancelled.append(key_by_task[task])
            task.cancel()
        if pending:
            # 必须回收取消结果，否则会出现 "Task was destroyed but it is pending"
            await asyncio.gather(*pending, return_exceptions=True)

    return result


class ConcurrencyGate:
    """非阻塞并发闸门：满了就快速失败（背压），而不是把请求堆在队列里。"""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._inflight = 0

    @property
    def limit(self) -> int:
        """并发上限。"""
        return self._limit

    @property
    def inflight(self) -> int:
        """当前在途数量。"""
        return self._inflight

    def try_acquire(self) -> bool:
        """尝试占位；满了返回 ``False``（调用方应立即给出提示而不是等待）。"""
        if self._inflight >= self._limit:
            return False
        self._inflight += 1
        return True

    def release(self) -> None:
        """释放占位。"""
        if self._inflight > 0:
            self._inflight -= 1
