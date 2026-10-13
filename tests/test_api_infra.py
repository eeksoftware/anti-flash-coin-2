import asyncio
import logging

from infra.cache import AsyncTTLCache
from monitor.wallet_monitor import WalletMonitorManager
from infra.key_pool import Outcome, PoolExhausted, RateLimitedPool
from infra.rate_limiter import TokenBucket


def test_secret_is_masked_and_key_disabled_on_invalid():
    pool = RateLimitedPool(["secret-value"], {"rate": 100, "burst": 2})
    lease = asyncio.run(pool.acquire())
    assert "secret-value" not in repr(lease)
    pool.report(lease, Outcome.INVALID_KEY)
    try:
        asyncio.run(pool.acquire(timeout=0.01))
    except PoolExhausted:
        pass
    else:
        raise AssertionError("disabled key should not be leased")


def test_token_bucket_waits_and_secret_wrapper_is_not_needed_for_metering():
    now = [0.0]
    async def sleep(delay): now[0] += delay
    async def run():
        bucket = TokenBucket(2, 1, clock=lambda: now[0], sleeper=sleep)
        await bucket.acquire()
        waited = await bucket.acquire()
        return waited
    assert asyncio.run(run()) >= 0.5


def test_cache_ttl_lru_and_pending_skip():
    now = [0.0]
    cache = AsyncTTLCache(1, clock=lambda: now[0])
    cache.set("a", 1, 5)
    assert cache.get("a") == 1
    cache.set("b", 2, 5)
    assert cache.get("a") is None
    cache.set("pending", 3, 10, pending=True)
    assert cache.get("pending") is None
    now[0] = 6
    assert cache.get("b") is None


def test_calls_past_five_per_second_wait_in_line():
    now = [0.0]

    async def sleep(delay):
        now[0] += delay

    pool = RateLimitedPool(
        ["only"],
        {"rate": 100, "burst": 10, "max_per_second": 5},
        clock=lambda: now[0],
        sleeper=sleep,
        jitter=lambda: 0,
    )

    async def run():
        times = []
        for _ in range(8):
            lease = await pool.acquire()
            times.append(now[0])
            pool.report(lease, Outcome.OK)
        return times

    times = asyncio.run(run())
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert min(gaps) >= 0.2
    assert times[5] - times[0] >= 1
    for index, moment in enumerate(times):
        inside = [item for item in times if moment <= item <= moment + 1]
        assert len(inside) <= 5, index


def test_rate_limited_key_enters_cooldown():
    now = [0.0]
    async def sleep(delay): now[0] += delay
    pool = RateLimitedPool(["one", "two"], {"rate": 100, "burst": 2}, clock=lambda: now[0], sleeper=sleep, jitter=lambda: 0)
    async def run():
        lease = await pool.acquire()
        pool.report(lease, Outcome.RATE_LIMITED)
        next_lease = await pool.acquire(timeout=0.1)
        return lease.key_label, next_lease.key_label
    first, second = asyncio.run(run())
    assert first != second


def test_monitor_interval_is_logged_once_per_minute(caplog):
    manager = WalletMonitorManager(
        client={},
        store=object(),
        poll_interval=1,
        max_poll_interval=30,
        normalizer=None,
        on_transfers=lambda *_args: None,
        on_api_status=lambda *_args: None,
    )
    with caplog.at_level(logging.INFO):
        manager._adjust_interval(1)
        manager._adjust_interval(1)
        assert caplog.text.count("İzleme aralığı") == 1
        manager._last_interval_log_at -= 60
        manager._adjust_interval(1)
    assert caplog.text.count("İzleme aralığı") == 2
