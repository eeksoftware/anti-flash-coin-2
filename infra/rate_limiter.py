from __future__ import annotations

import time


class DepartureQueue:
    """FIFO gate. Extra callers wait instead of starting more than `per_second` sends.

    Starts are spaced so a closed one-second window never holds another call.
    """

    def __init__(self, per_second: float, clock=time.monotonic, sleeper=None) -> None:
        pace = float(per_second)
        if pace <= 0:
            raise ValueError("per_second must be positive")
        self.per_second = pace
        self.interval = 1.0 / pace + 0.01
        self._clock = clock
        self._sleep = sleeper
        self._next: float | None = None
        self._lock = __import__("asyncio").Lock()

    async def acquire(self) -> float:
        import asyncio
        async with self._lock:
            now = self._clock()
            slot = now if self._next is None or self._next <= now else self._next
            self._next = slot + self.interval
            delay = max(0.0, slot - now)
        if delay > 0:
            await (self._sleep(delay) if self._sleep else asyncio.sleep(delay))
        return delay


class TokenBucket:
    """Async token bucket with an injectable monotonic clock."""

    def __init__(self, rate: float, burst: int, clock=time.monotonic, sleeper=None) -> None:
        self.rate = max(0.01, float(rate))
        self.capacity = max(1, int(burst))
        self._tokens = float(self.capacity)
        self._updated = clock()
        self._clock = clock
        self._sleep = sleeper
        self._lock = __import__("asyncio").Lock()

    @property
    def tokens(self) -> float:
        self._refill(self._clock())
        return self._tokens

    def _refill(self, now: float) -> None:
        self._tokens = min(self.capacity, self._tokens + max(0.0, now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self) -> float:
        import asyncio
        waited = 0.0
        while True:
            async with self._lock:
                now = self._clock()
                self._refill(now)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return waited
                delay = (1 - self._tokens) / self.rate
                if delay <= 0:
                    delay = 1.0 / self.rate
            await (self._sleep(delay) if self._sleep else asyncio.sleep(delay))
            waited += delay
