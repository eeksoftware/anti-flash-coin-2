"""Slow poller for ERC-20 Approval events of watched EVM wallets.

Runs beside the transfer monitors with its own, longer interval so it does
not compete with transfer polling for API budget: one getLogs request per
wallet per cycle plus one cached block-number request. The first cycle for
a wallet only records the current block as a cursor; historical approvals
are not reported. Networks whose provider lacks approval support are
skipped and logged once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from analyzer.approvals import ApprovalAssessment, ApprovalEvent, SpenderRegistry, assess
from analyzer.risk_engine import RiskScore, compute_risk
from infra.key_pool import CURRENT_PRIORITY, Priority
from monitor.state_db import UserStateStore
from monitor.wallet_store import WalletStateStore, WalletSubscription
from providers.base import UnsupportedFeature


logger = logging.getLogger(__name__)
CURSOR_KIND = "approval"
DETECTION_KIND = "approval"
MAX_BLOCK_SPAN = 5000

ApprovalCallback = Callable[[WalletSubscription, ApprovalEvent, ApprovalAssessment, RiskScore], Awaitable[None]]


class ApprovalMonitor:
    def __init__(
        self,
        providers: dict[str, object],
        store: WalletStateStore,
        state: UserStateStore,
        on_approval: ApprovalCallback,
        *,
        interval: float = 60.0,
        registry: SpenderRegistry | None = None,
        decimals_lookup: Callable[[str, str], Awaitable[int | None]] | None = None,
    ) -> None:
        self.providers = {str(name).lower(): provider for name, provider in providers.items()}
        self.store = store
        self.state = state
        self.on_approval = on_approval
        self.interval = max(5.0, interval)
        self.registry = registry or SpenderRegistry()
        self.decimals_lookup = decimals_lookup
        self._unsupported_logged: set[str] = set()

    def supported_networks(self) -> list[str]:
        return sorted(name for name, provider in self.providers.items() if getattr(provider, "supports_approvals", False))

    async def run(self) -> None:
        CURRENT_PRIORITY.set(Priority.BACKGROUND)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Approval tarama turu başarısız.")
            await asyncio.sleep(self.interval)

    async def run_once(self) -> int:
        """Scan every watched EVM wallet once. Returns the number of reported events."""
        reported = 0
        wallets: dict[tuple[str, str], list[WalletSubscription]] = {}
        for subscription in self.store.all_subscriptions():
            wallets.setdefault((subscription.network, subscription.wallet_address), []).append(subscription)
        latest_cache: dict[str, int] = {}
        for (network, wallet_address), subscriptions in wallets.items():
            provider = self.providers.get(network)
            if provider is None:
                continue
            if not getattr(provider, "supports_approvals", False):
                if network not in self._unsupported_logged:
                    self._unsupported_logged.add(network)
                    logger.info("Approval izleme %s ağında desteklenmiyor; atlanıyor.", network)
                continue
            try:
                if network not in latest_cache:
                    latest_cache[network] = int(await provider.get_latest_block())
                reported += await self._scan_wallet(provider, network, wallet_address, latest_cache[network], subscriptions)
            except UnsupportedFeature:
                self._unsupported_logged.add(network)
                logger.info("Approval izleme %s ağında desteklenmiyor; atlanıyor.", network)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Approval taraması başarısız (%s): %s", network, exc)
        return reported

    async def _scan_wallet(self, provider, network: str, wallet_address: str, latest: int, subscriptions: list[WalletSubscription]) -> int:
        cursor = self.state.scan_cursor(network, wallet_address, CURSOR_KIND)
        if cursor is None:
            self.state.save_scan_cursor(network, wallet_address, CURSOR_KIND, latest)
            logger.info("Approval izleme başladı; blok %d temel alındı.", latest)
            return 0
        if latest <= cursor:
            return 0
        from_block = cursor + 1
        to_block = min(latest, from_block + MAX_BLOCK_SPAN - 1)
        events = await provider.get_approval_events(wallet_address, from_block, to_block)
        reported = 0
        for event in events:
            reported += await self._handle_event(network, wallet_address, event, subscriptions)
        self.state.save_scan_cursor(network, wallet_address, CURSOR_KIND, to_block)
        return reported

    async def _handle_event(self, network: str, wallet_address: str, event: ApprovalEvent, subscriptions: list[WalletSubscription]) -> int:
        previous = self.state.allowance(network, event.owner, event.token_address, event.spender)
        decimals = None
        if self.decimals_lookup is not None:
            try:
                decimals = await self.decimals_lookup(network, event.token_address)
            except Exception:
                decimals = None
        assessment = assess(event, previous, decimals, self.registry)
        risk = compute_risk(assessment.signals, missing=() if decimals is not None else ("decimals",))
        self.state.save_allowance(
            network=network, owner=event.owner, token_address=event.token_address, spender=event.spender,
            value_raw=event.value_raw, block_number=event.block_number, tx_hash=event.tx_hash,
        )
        fresh = self.state.save_detection(
            event_key=event.event_key, kind=DETECTION_KIND, network=network, wallet_address=wallet_address,
            tx_hash=event.tx_hash, score=risk.score, level=risk.level, weights_version=risk.weights_version,
            signals=[{"name": s.name, "weight": s.weight, "verified": s.verified, "evidence": s.evidence} for s in risk.signals],
        )
        if not fresh:
            return 0
        for subscription in subscriptions:
            try:
                await self.on_approval(subscription, event, assessment, risk)
            except Exception:
                logger.exception("Approval bildirimi iletilemedi.")
        return 1
