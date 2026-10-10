"""Small in-memory cache and per-user limiter for manual blockchain checks."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any


class TTLCache:
    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        import time

        self._clock = clock or time.monotonic
        self._values: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._values.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            self._values.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._values[key] = (self._clock() + max(0, ttl_seconds), value)


class CheckRateLimiter:
    """Allow 10 checks per rolling minute with at least 3 seconds between."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        import time

        self._clock = clock or time.monotonic
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, user_id: str) -> bool:
        now = self._clock()
        events = self._events[str(user_id)]
        while events and now - events[0] >= 60.0:
            events.popleft()
        if events and now - events[-1] < 3.0:
            return False
        if len(events) >= 10:
            return False
        events.append(now)
        return True
