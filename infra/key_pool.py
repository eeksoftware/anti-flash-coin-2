from __future__ import annotations

import asyncio
import sqlite3
import logging
import random
import time
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from infra.rate_limiter import DepartureQueue, TokenBucket

logger = logging.getLogger(__name__)


class Priority(str, Enum):
    INTERACTIVE = "INTERACTIVE"
    BACKGROUND = "BACKGROUND"


CURRENT_PRIORITY = ContextVar("api_priority", default=Priority.INTERACTIVE)


class Outcome(str, Enum):
    OK = "OK"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_KEY = "INVALID_KEY"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNSUPPORTED = "UNSUPPORTED"


class PoolExhausted(RuntimeError):
    pass


class Secret:
    __slots__ = ("_value",)
    def __init__(self, value: str): self._value = value
    def reveal(self) -> str: return self._value
    def __repr__(self) -> str: return "Secret([REDACTED])"
    __str__ = __repr__


@dataclass(frozen=True)
class Lease:
    key_label: str
    secret: Secret


@dataclass
class _Key:
    label: str
    secret: Secret
    bucket: TokenBucket
    gate: DepartureQueue | None = None
    cooldown_until: float = 0.0
    cooldown: float = 2.0
    disabled: bool = False
    exhausted: bool = False
    load: int = 0
    calls: int = 0


class RateLimitedPool:
    def __init__(self, keys, limits, *, clock=time.monotonic, sleeper=asyncio.sleep, jitter: Callable[[], float] | None=None):
        self._clock, self._sleep = clock, sleeper
        self._jitter = jitter or (lambda: random.uniform(0, .25))
        rate = limits.get("rate", 4)
        burst = limits.get("burst", 2)
        label_prefix = str(limits.get("key_label_prefix", "key"))
        ceiling = limits.get("max_per_second")
        self._ceiling = float(ceiling) if ceiling else None
        if self._ceiling is not None:
            if self._ceiling <= 0:
                raise ValueError("max_per_second must be positive")
            rate = self._ceiling
            burst = 1
        label_prefix = str(limits.get("key_label_prefix", "key"))
        self._keys = []
        for i, raw_key in enumerate(keys):
            if not raw_key:
                continue
            gate = DepartureQueue(self._ceiling, clock, sleeper) if self._ceiling is not None else None
            secret = raw_key if isinstance(raw_key, Secret) else Secret(str(raw_key))
            self._keys.append(_Key(
                f"{label_prefix}-{i+1}",
                secret,
                TokenBucket(rate, burst, clock, sleeper),
                gate,
            ))
        self._condition = asyncio.Condition()
        self._waiters = {Priority.INTERACTIVE: 0, Priority.BACKGROUND: 0}
        self._background_wait_started: float | None = None
        self._daily_limit = int(limits.get("daily_limit", 100000) * limits.get("daily_soft_ratio", .95))
        self._metrics = limits.get("metrics")
        self._db_path = limits.get("db_path")
        self._provider = str(limits.get("provider", "etherscan"))
        self._rate_limit_cooldown = float(limits.get("rate_limit_cooldown", 2.0))
        self._logger_label = str(limits.get("logger_label", "Etherscan"))
        self._api_label = str(limits.get("api_label", "Etherscan"))
        self._pending_persist = {}
        self._last_persist = self._clock()
        self._usage_day = self._day()
        if self._db_path:
            self._init_db()

    def _day(self):
        from datetime import datetime, timedelta, timezone
        hour = int(__import__("os").getenv("DAILY_RESET_UTC_HOUR", "0")) % 24
        now = datetime.now(timezone.utc)
        return (now - timedelta(hours=hour)).date().isoformat()

    def _init_db(self):
        try:
            from pathlib import Path
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS api_usage_daily (day TEXT NOT NULL, provider TEXT NOT NULL, key_label TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0, rate_limited INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, provider, key_label))")
                rows = db.execute("SELECT key_label, calls FROM api_usage_daily WHERE day=? AND provider=?", (self._usage_day, self._provider)).fetchall()
            try:
                import os
                os.chmod(self._db_path, 0o600)
            except OSError: pass
            for label, calls in rows:
                key = next((k for k in self._keys if k.label == label), None)
                if key:
                    key.calls = calls
                    key.exhausted = calls >= self._daily_limit
        except Exception:
            logger.warning("API günlük sayaçları okunamadı; bellek içi sayaçlar kullanılacak.")

    async def acquire(self, priority=None, timeout: float | None=None) -> Lease:
        current_day = self._day()
        if current_day != self._usage_day:
            self._usage_day = current_day
            for key in self._keys:
                key.calls = 0; key.exhausted = False
        priority = Priority(priority or CURRENT_PRIORITY.get())
        started = self._clock()
        if priority is Priority.BACKGROUND and self._waiters[priority] == 0:
            self._background_wait_started = started
        self._waiters[priority] += 1
        try:
            while True:
                async with self._condition:
                    available = [k for k in self._keys if not k.disabled and not k.exhausted and k.cooldown_until <= self._clock()]
                    overdue_bg = self._background_wait_started is not None and self._clock() - self._background_wait_started >= 2.0
                    if available and (priority is Priority.INTERACTIVE or not self._waiters[Priority.INTERACTIVE] or overdue_bg):
                        key = max(available, key=lambda k: (k.bucket.tokens, -k.load))
                        key.load += 1
                        lease = Lease(key.label, key.secret)
                        break
                    if not any(not k.disabled and not k.exhausted for k in self._keys):
                        raise PoolExhausted(f"Tüm {self._api_label} API anahtarları kullanılamıyor.")
                    wake = min((k.cooldown_until for k in self._keys if not k.disabled and not k.exhausted), default=self._clock()+1)
                    delay = max(.01, min(1.0, wake-self._clock()))
                if timeout is not None and self._clock()-started+delay > timeout:
                    raise PoolExhausted(f"{self._api_label} API anahtarı için bekleme süresi doldu.")
                await self._sleep(delay)
            waited = self._clock()-started
            if key.gate is not None:
                await key.gate.acquire()
            else:
                await key.bucket.acquire()
            if self._metrics:
                self._metrics.record_wait(self._clock() - started)
            return lease
        finally:
            self._waiters[priority] -= 1
            if priority is Priority.BACKGROUND and not self._waiters[priority]: self._background_wait_started = None

    def report(self, lease: Lease, outcome: Outcome) -> None:
        key = next((k for k in self._keys if k.label == lease.key_label), None)
        if key is None: return
        key.load = max(0, key.load-1)
        key.calls += 1
        outcome = Outcome(outcome)
        if self._metrics: self._metrics.record(lease.key_label, outcome)
        if outcome is Outcome.OK: key.cooldown = max(2.0, self._rate_limit_cooldown)
        elif outcome is Outcome.RATE_LIMITED:
            key.cooldown_until = self._clock()+max(key.cooldown, self._rate_limit_cooldown)+self._jitter()
            key.cooldown = min(60.0, max(key.cooldown, self._rate_limit_cooldown)*2)
        elif outcome is Outcome.INVALID_KEY:
            key.disabled = True
            logger.error("%s API anahtarı devre dışı bırakıldı (%s).", self._logger_label, key.label)
        elif outcome is Outcome.QUOTA_EXHAUSTED: key.exhausted = True
        if key.calls >= self._daily_limit: key.exhausted = True
        pending = self._pending_persist.setdefault(key.label, [0, 0, 0])
        pending[0] += 1; pending[1] += int(outcome is not Outcome.OK); pending[2] += int(outcome is Outcome.RATE_LIMITED)
        if self._db_path and (sum(row[0] for row in self._pending_persist.values()) >= 20 or self._clock()-self._last_persist >= 5):
            self._persist()

    def _persist(self):
        try:
            with sqlite3.connect(self._db_path, timeout=1) as db:
                for label, (calls, errors, limited) in self._pending_persist.items():
                    db.execute("INSERT INTO api_usage_daily(day,provider,key_label,calls,errors,rate_limited) VALUES(?,?,?,?,?,?) ON CONFLICT(day,provider,key_label) DO UPDATE SET calls=calls+excluded.calls, errors=errors+excluded.errors, rate_limited=rate_limited+excluded.rate_limited",
                               (self._usage_day, self._provider, label, calls, errors, limited))
            self._pending_persist.clear(); self._last_persist = self._clock()
        except Exception:
            logger.warning("API günlük sayaçları kaydedilemedi.")

    def flush(self):
        if self._db_path and self._pending_persist:
            self._persist()
