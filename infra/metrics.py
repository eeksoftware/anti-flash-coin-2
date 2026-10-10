from __future__ import annotations

import time
from collections import defaultdict, deque


class APIMetrics:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._totals = defaultdict(lambda: defaultdict(int))
        self._window = defaultdict(lambda: defaultdict(int))
        self._latencies = defaultdict(lambda: deque(maxlen=1000))
        self._waits = deque(maxlen=1000)
        self.cache_hits = self.cache_misses = 0
        self.monitor_interval = None

    def record(self, label, outcome, latency=0.0):
        self._totals[label]["calls"] += 1
        self._window[label]["calls"] += 1
        if outcome != "OK":
            self._totals[label]["errors"] += 1; self._window[label]["errors"] += 1
        if outcome == "RATE_LIMITED":
            self._totals[label]["rate_limited"] += 1; self._window[label]["rate_limited"] += 1
        if latency: self._latencies[label].append(latency)

    def record_wait(self, seconds): self._waits.append(seconds)

    def snapshot(self):
        latencies = sorted(x for values in self._latencies.values() for x in values)
        return {"keys": {k: dict(v) for k, v in self._totals.items()}, "today": {k: dict(v) for k, v in self._totals.items()},
                "latency_avg": sum(latencies)/len(latencies) if latencies else 0,
                "latency_p95": latencies[min(len(latencies)-1, int(len(latencies)*.95))] if latencies else 0,
                "queue_wait_avg": sum(self._waits)/len(self._waits) if self._waits else 0,
                "queue_wait_max": max(self._waits, default=0), "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses, "cache_hit_ratio": self.cache_hits/max(1,self.cache_hits+self.cache_misses),
                "monitor_interval": self.monitor_interval}

    def take_window(self):
        result = {k: dict(v) for k, v in self._window.items()}; self._window.clear(); return result
