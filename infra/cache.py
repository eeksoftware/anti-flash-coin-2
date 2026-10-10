from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class AsyncTTLCache:
    def __init__(self, max_entries: int = 10000, clock=time.monotonic) -> None:
        self.max_entries = max(1, max_entries)
        self._clock = clock
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self.hits = self.misses = 0

    @staticmethod
    def normalize(value):
        if isinstance(value, dict): return tuple(sorted((k, AsyncTTLCache.normalize(v)) for k, v in value.items()))
        if isinstance(value, (list, tuple)): return tuple(AsyncTTLCache.normalize(v) for v in value)
        if isinstance(value, str): return value.lower() if value.startswith("0x") else value
        return value

    def get(self, key):
        item = self._items.get(key)
        if item is None or item[0] <= self._clock():
            self._items.pop(key, None); self.misses += 1; return None
        self._items.move_to_end(key); self.hits += 1; return item[1]

    def set(self, key, value, ttl: float, *, pending=False, not_found=False):
        if pending: return
        if not_found: ttl = min(ttl, 15.0)
        self._items[key] = (self._clock()+max(0, ttl), value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries: self._items.popitem(last=False)

    def clear(self): self._items.clear()
