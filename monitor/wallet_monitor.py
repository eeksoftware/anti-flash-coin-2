import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from blockchain.base import BlockchainClient, BlockchainClientError
from models.transaction import TokenTransfer
from monitor.transaction_tracker import TransactionTracker
from monitor.wallet_store import WalletStateStore, WalletSubscription
from providers.base import ChainProvider
from infra.key_pool import CURRENT_PRIORITY, Priority
from config import settings
from networks import get_network_config


logger = logging.getLogger(__name__)
# Etherscan tokentx rejects a request once page * offset passes this window.
HISTORY_WINDOW = 10_000


def history_page_size(network: str) -> int:
    """Largest single history page that still reaches the provider window."""
    try:
        family = get_network_config(network).family
    except ValueError:
        family = "evm"
    if family == "tron":
        return 200
    return 1000


def spare_history_pages(
    pool,
    *,
    wallets: int,
    reserve_interval: float,
    approval_interval: float | None = None,
    page_size: int = 1000,
    now: datetime | None = None,
    reset_hour: int | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    """Extra history pages one wallet may take without spending the day's monitoring reserve.

    The first page is always fetched by the caller. Zero means do not go further:
    a key is cooling down after a rate limit, or the remaining daily calls are
    already needed to keep polling until the quota resets.
    """
    if pool is None:
        return 0
    active = [
        key for key in getattr(pool, "_keys", [])
        if not getattr(key, "disabled", False) and not getattr(key, "exhausted", False)
    ]
    if not active:
        return 0
    tick = clock or time.monotonic
    if any(getattr(key, "cooldown_until", 0) > tick() for key in active):
        return 0
    remaining = sum(max(0, int(pool._daily_limit) - int(key.calls)) for key in active)
    current = now or datetime.now(timezone.utc)
    hour = settings.daily_reset_utc_hour if reset_hour is None else reset_hour
    reset = current.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
    if reset <= current:
        reset += timedelta(days=1)
    seconds_left = max(1.0, (reset - current).total_seconds())
    wallet_count = max(1, wallets)
    reserved = wallet_count * seconds_left / max(float(reserve_interval), 1.0)
    if approval_interval:
        reserved += wallet_count * seconds_left / max(float(approval_interval), 1.0)
    per_wallet = int(remaining - reserved) // wallet_count
    if per_wallet < 1:
        return 0
    max_extra = max(0, (HISTORY_WINDOW // max(1, page_size)) - 1)
    return min(max_extra, per_wallet)


class WalletMonitor:
    API_UNAVAILABLE_AFTER = 30.0
    PAGE_SIZE = 100

    def __init__(
        self,
        client: BlockchainClient | ChainProvider,
        tracker: TransactionTracker,
        wallet_address: str,
        poll_interval: float,
        max_poll_interval: float,
        normalizer: Callable[[list[dict[str, Any]], str], list[TokenTransfer]] | None = None,
        on_transfers: Callable[[list[TokenTransfer]], Awaitable[None]] | None = None,
        on_api_status: Callable[[str], Awaitable[None]] | None = None,
        health_state=None,
        on_baseline: Callable[[list[TokenTransfer]], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.provider = client if callable(getattr(client, "get_transaction", None)) else None
        self.tracker = tracker
        self.wallet_address = wallet_address
        self._wallet_version = 0
        self._initialized = False
        self.poll_interval = max(0.1, poll_interval)
        self.max_poll_interval = max(self.poll_interval, max_poll_interval)
        self.normalizer = normalizer
        self.on_transfers = on_transfers
        self.on_api_status = on_api_status
        self.on_baseline = on_baseline
        self.health_state = health_state
        self.shared_wallets = 1

    def set_wallet_address(self, wallet_address: str) -> None:
        if self.provider is not None:
            from networks import get_network_config
            wallet_address = get_network_config(getattr(self.provider, "network", "ethereum")).normalize_address(wallet_address)
        if wallet_address == self.wallet_address:
            return
        self.wallet_address = wallet_address
        self._wallet_version += 1
        self._initialized = False
        self.tracker.reset()

    async def _notify_api_status(self, status: str) -> None:
        if self.on_api_status is None:
            return
        try:
            await self.on_api_status(status)
        except Exception:
            # Notification failure must not stop wallet polling.
            logger.exception("API durumu bildirimi iletilemedi.")

    async def run(self) -> None:
        retry_interval = self.poll_interval
        failure_started_at: float | None = None
        unavailable = False
        logger.info("İlk cüzdan kontrolü başlatılıyor.")
        while True:
            try:
                requested_wallet = self.wallet_address
                wallet_version = self._wallet_version
                transfers: list[TokenTransfer] = []
                page_transfers: list[TokenTransfer] = []
                page = 1
                network = getattr(self.provider, "network", "ethereum")
                fetch_limit = self.PAGE_SIZE
                max_pages: int | None = None
                if not self._initialized:
                    fetch_limit = history_page_size(str(network))
                    pool = getattr(self.provider, "pool", None) or getattr(
                        getattr(self.provider, "client", None), "pool", None,
                    )
                    approval_interval = None
                    try:
                        if get_network_config(str(network)).family == "evm":
                            approval_interval = settings.approval_poll_interval
                    except ValueError:
                        approval_interval = None
                    max_pages = 1 + spare_history_pages(
                        pool,
                        wallets=self.shared_wallets,
                        reserve_interval=settings.monitor_max_interval,
                        approval_interval=approval_interval,
                        page_size=fetch_limit,
                    )
                while True:
                    if (page - 1) * fetch_limit >= HISTORY_WINDOW:
                        break
                    try:
                        if self.provider is not None:
                            get_page = getattr(self.provider, "get_token_transfer_page", None)
                            if callable(get_page):
                                page_transfers = await get_page(requested_wallet, page, fetch_limit)
                            elif page == 1:
                                page_transfers = await self.provider.get_token_transfers(
                                    requested_wallet, fetch_limit
                                )
                            else:
                                break
                        else:
                            if self.normalizer is None:
                                raise ValueError("Ham transfer normalizer yapılandırılmamış.")
                            raw_transfers = await self.client.get_token_transfers(
                                requested_wallet,
                                page=page,
                                offset=fetch_limit,
                            )
                            page_transfers = self.normalizer(raw_transfers, requested_wallet)
                    except (BlockchainClientError, ValueError):
                        if transfers and not self._initialized:
                            logger.warning(
                                "Geçmiş taraması yarıda kesildi; %d olay ile başlanıyor.",
                                len(transfers),
                            )
                            break
                        raise
                    transfers.extend(page_transfers)

                    # Newest page first. Stop on a short page, an already seen
                    # event, the provider window, or a baseline budget limit.
                    if (
                        len(page_transfers) < fetch_limit
                        or any(self.tracker.has_seen(item) for item in page_transfers)
                        or len(transfers) >= HISTORY_WINDOW
                        or (max_pages is not None and page >= max_pages)
                    ):
                        break
                    page += 1

                # A Telegram command may have changed the watched wallet while
                # the provider request was in flight. Discard that stale batch.
                if wallet_version != self._wallet_version:
                    continue

                # Commit event IDs only after all required pages succeeded, so
                # a failed later page cannot silently consume earlier events.
                new_transfers = self.tracker.add_new(transfers)

                if unavailable:
                    logger.info("Blokzincir API bağlantısı yeniden kuruldu.")
                    await self._notify_api_status("API_RECOVERED")
                failure_started_at = None
                unavailable = False
                retry_interval = self.poll_interval

                if not self._initialized:
                    # The first successful response establishes the baseline;
                    # older events returned by tokentx are not new detections.
                    self._initialized = True
                    budget_stopped = (
                        max_pages is not None
                        and page >= max_pages
                        and len(transfers) < HISTORY_WINDOW
                        and len(page_transfers) >= fetch_limit
                    )
                    if len(transfers) >= HISTORY_WINDOW:
                        logger.info(
                            "Cüzdan izleme başladı; %d mevcut transfer olayı alındı. Sağlayıcı penceresi doldu.",
                            len(transfers),
                        )
                    elif budget_stopped:
                        logger.info(
                            "Cüzdan izleme başladı; %d mevcut transfer olayı alındı. Günlük API payı geçmişin devamına yetmiyor.",
                            len(transfers),
                        )
                    else:
                        logger.info(
                            "Cüzdan izleme başladı; %d mevcut transfer olayı bulundu.",
                            len(transfers),
                        )
                    if self.on_baseline is not None:
                        try:
                            await self.on_baseline(transfers)
                        except Exception:
                            logger.exception("Temel geçmiş işlenemedi.")
                elif new_transfers:
                    logger.info(
                        "%d yeni token transfer olayı algılandı.",
                        len(new_transfers),
                    )
                    if self.on_transfers is not None:
                        try:
                            await self.on_transfers(new_transfers)
                        except Exception:
                            logger.exception("Yeni transferler işlenemedi.")
                else:
                    logger.debug("Cüzdan kontrolü başarılı; yeni transfer yok.")

                if self.health_state is not None:
                    self.health_state.record_cycle(self.poll_interval)

                await asyncio.sleep(self.poll_interval)

            except (BlockchainClientError, ValueError) as exc:
                if self.health_state is not None:
                    self.health_state.record_cycle(retry_interval, str(exc))
                now = time.monotonic()
                if failure_started_at is None:
                    failure_started_at = now

                logger.warning("Cüzdan kontrolü başarısız: %s", exc)
                if (
                    not unavailable
                    and now - failure_started_at >= self.API_UNAVAILABLE_AFTER
                ):
                    unavailable = True
                    logger.error(
                        "Blokzincir API'sine en az %.0f saniyedir erişilemiyor.",
                        self.API_UNAVAILABLE_AFTER,
                    )
                    await self._notify_api_status("API_UNAVAILABLE")

                await asyncio.sleep(retry_interval)
                retry_interval = min(
                    retry_interval * 2,
                    self.max_poll_interval,
                )


class WalletMonitorManager:
    """Run one independent poller for each configured user/network/wallet pair."""

    def __init__(
        self,
        client: BlockchainClient | ChainProvider,
        store: WalletStateStore,
        poll_interval: float,
        max_poll_interval: float,
        normalizer: Callable[[list[dict[str, Any]], str], list[TokenTransfer]] | None,
        on_transfers: Callable[[WalletSubscription, list[TokenTransfer]], Awaitable[None]],
        on_api_status: Callable[[WalletSubscription, str], Awaitable[None]],
        health_state=None,
        on_baseline: Callable[[str, str, list[TokenTransfer]], Awaitable[None]] | None = None,
    ) -> None:
        self.on_baseline = on_baseline
        if isinstance(client, dict):
            self.providers = {str(name).lower(): value for name, value in client.items()}
        else:
            network = getattr(client, "network", "ethereum")
            self.providers = {network: client} if callable(getattr(client, "get_transaction", None)) else {}
        self.client = client
        self.provider = client if callable(getattr(client, "get_transaction", None)) else None
        self.store = store
        self.poll_interval = poll_interval
        self.max_poll_interval = max_poll_interval
        self.normalizer = normalizer
        self.on_transfers = on_transfers
        self.on_api_status = on_api_status
        self.health_state = health_state
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._monitors: dict[tuple[str, str], WalletMonitor] = {}
        self._effective_interval = min(settings.monitor_max_interval, max(settings.monitor_min_interval, poll_interval))
        self._last_interval_log = self._effective_interval
        self._last_interval_log_at = 0.0

    async def _run_wallet(self, network: str, wallet_address: str, stagger: float = 0.0) -> None:
        provider = self.providers.get(network)
        if provider is None:
            return
        def current_subscriptions() -> list[WalletSubscription]:
            return [
                item for item in self.store.all_subscriptions()
                if item.network == network and item.wallet_address == wallet_address
            ]

        async def transfers_callback(transfers: list[TokenTransfer]) -> None:
            for subscription in current_subscriptions():
                try:
                    await self.on_transfers(subscription, transfers)
                except Exception:
                    logger.exception("Yeni transferler işlenemedi.")

        async def status_callback(status: str) -> None:
            for subscription in current_subscriptions():
                await self.on_api_status(subscription, status)

        async def baseline_callback(transfers: list[TokenTransfer]) -> None:
            if self.on_baseline is not None:
                await self.on_baseline(network, wallet_address, transfers)

        same_network = sum(
            1 for item in self.store.all_subscriptions() if item.network == network
        )
        monitor = WalletMonitor(
            client=provider,
            tracker=TransactionTracker(),
            wallet_address=wallet_address,
            poll_interval=self._effective_interval,
            max_poll_interval=self.max_poll_interval,
            normalizer=self.normalizer,
            on_transfers=transfers_callback,
            on_api_status=status_callback,
            health_state=self.health_state,
            on_baseline=baseline_callback if self.on_baseline is not None else None,
        )
        monitor.shared_wallets = max(1, same_network)
        self._monitors[(network, wallet_address)] = monitor
        if stagger:
            await asyncio.sleep(stagger)
        CURRENT_PRIORITY.set(Priority.BACKGROUND)
        await monitor.run()

    async def _sync(self) -> None:
        subscriptions = self.store.all_subscriptions()
        desired: dict[tuple[str, str], WalletSubscription] = {}
        for item in subscriptions:
            if item.network in self.providers:
                desired[(item.network, item.wallet_address)] = item
        for key in self._tasks.keys() - desired.keys():
            task = self._tasks.pop(key)
            task.cancel()
            self._monitors.pop(key, None)
        self._adjust_interval(len(desired))
        wallet_count = max(1, len(desired))
        for index, (key, subscription) in enumerate(desired.items()):
            task = self._tasks.get(key)
            if task is None or task.done():
                self._tasks[key] = asyncio.create_task(
                    self._run_wallet(*key, self._effective_interval * index / wallet_count),
                    name=f"wallet-monitor-{subscription.wallet_address}",
                )

    def _adjust_interval(self, wallet_count: int) -> None:
        """Stretch polling only when the remaining shared daily budget requires it."""
        pools = []
        for provider in self.providers.values():
            pool = getattr(provider, "pool", None) or getattr(getattr(provider, "client", None), "pool", None)
            if pool is not None:
                pools.append(pool)
        target = max(settings.monitor_min_interval, self.poll_interval)
        if pools and wallet_count:
            budget = 0
            for pool in pools:
                active = [key for key in pool._keys if not key.disabled]
                budget += max(0, len(active) * pool._daily_limit - sum(key.calls for key in active))
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            reset = now.replace(hour=settings.daily_reset_utc_hour % 24, minute=0, second=0, microsecond=0)
            if reset <= now: reset += timedelta(days=1)
            seconds_left = max(1.0, (reset-now).total_seconds())
            required = wallet_count * seconds_left / max(1, budget)
            target = max(target, required)
        self._effective_interval = min(settings.monitor_max_interval, max(settings.monitor_min_interval, target))
        for pool in pools:
            if getattr(pool, "_metrics", None) is not None:
                pool._metrics.monitor_interval = self._effective_interval
        now = time.monotonic()
        if now - self._last_interval_log_at >= 60:
            logger.info("İzleme aralığı bütçeye göre %.2f saniye olarak ayarlandı.", self._effective_interval)
            self._last_interval_log = self._effective_interval
            self._last_interval_log_at = now
        subscriptions = getattr(self.store, "all_subscriptions", None)
        counts: dict[str, int] = {}
        if callable(subscriptions):
            for item in subscriptions():
                counts[item.network] = counts.get(item.network, 0) + 1
        for (network, _address), monitor in self._monitors.items():
            monitor.shared_wallets = max(1, counts.get(network, 1))
            monitor.poll_interval = self._effective_interval

    async def run(self) -> None:
        logger.info("Cüzdan izleme yöneticisi başlatıldı.")
        try:
            while True:
                await self._sync()
                await asyncio.sleep(1)
        finally:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def get_recent_transfers(
        self, network: str, wallet_address: str, limit: int = 10
    ) -> list[TokenTransfer]:
        provider = self.providers.get(network.lower())
        if provider is not None:
            return (await provider.get_token_transfers(wallet_address, limit))[:limit]
        if self.normalizer is None:
            raise ValueError("Ham transfer normalizer yapılandırılmamış.")
        raw = await self.client.get_token_transfers(
            wallet_address, page=1, offset=limit
        )
        return self.normalizer(raw, wallet_address)[:limit]
