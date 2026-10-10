from __future__ import annotations

import time


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
            await (self._sleep(delay) if self._sleep else asyncio.sleep(delay))
            waited += delay
