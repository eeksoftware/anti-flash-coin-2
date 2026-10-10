"""Weekly UTC summaries.

A week starts on Monday 00:00 UTC and ends at the next Monday 00:00, which
is excluded. The scheduler sends only the most recently completed week, once
per user. Weeks missed while the bot was down are not replayed. Quiet hours
defer that single send; they do not queue older reports.

False-alarm rate uses CORRECT and FALSE_POSITIVE only. UNSURE and missing
feedback are not treated as either outcome. Fewer than MIN_LABELED samples
is reported as insufficient rather than as a rate.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from monitor.state_db import SUSPICIOUS_CLASSIFICATIONS, quiet_contains
from reporting.access import ReportReader, TimeRange, signal_names


logger = logging.getLogger(__name__)

MIN_LABELED = 5
CHECK_INTERVAL = 900
RISK_LEVELS = ("DÜŞÜK", "ORTA", "YÜKSEK", "KRİTİK")


@dataclass(frozen=True)
class WeeklySummary:
    start: datetime
    end: datetime
    transfers: int
    by_network: tuple[tuple[str, int], ...]
    by_classification: tuple[tuple[str, int], ...]
    suspicious: int
    unknown: int
    poisoning: int | None
    approvals: int | None
    risk_levels: tuple[tuple[str, int], ...] | None
    notifications: int
    feedback: dict[str, int]
    labeled: int
    false_alarm: str
    previous_transfers: int | None


def week_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """Return the Monday 00:00 UTC week that contains moment, end exclusive."""
    moment = moment.astimezone(timezone.utc)
    start = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc) - timedelta(days=moment.weekday())
    return start, start + timedelta(days=7)


def completed_week(now: datetime) -> tuple[datetime, datetime]:
    """The latest finished week. Older missed weeks are not returned."""
    current, _ = week_bounds(now)
    return current - timedelta(days=7), current


def false_alarm_text(correct: int, false_positive: int) -> str:
    labeled = correct + false_positive
    if labeled <= 0:
        return "N/A"
    if labeled < MIN_LABELED:
        return "Yeterli etiketli veri yok"
    rate = (Decimal(false_positive) / Decimal(labeled) * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{format(rate, 'f')}%"


def build_summary(reader: ReportReader, user_id: str, wallets: list[tuple[str, str]], start: datetime, end: datetime) -> WeeklySummary:
    period = TimeRange(start, end)
    previous = TimeRange(start - timedelta(days=7), start)
    breakdown = reader.transfer_breakdown(wallets, period)
    transfers = sum(item[2] for item in breakdown)
    notifications = sum(item[3] for item in breakdown)
    networks: dict[str, int] = {}
    classes: dict[str, int] = {}
    for network, classification, total, _notified in breakdown:
        networks[network] = networks.get(network, 0) + total
        classes[classification] = classes.get(classification, 0) + total
    suspicious = sum(count for name, count in classes.items() if name in SUSPICIOUS_CLASSIFICATIONS)
    tables = reader.feature_tables()
    poisoning = approvals = None
    levels: dict[str, int] | None = None
    if "detections" in tables:
        poisoning = 0
        approvals = 0
        levels = {name: 0 for name in RISK_LEVELS}
        seen_poison: set[str] = set()
        for row in reader.detection_rows(wallets, period):
            kind = row["kind"]
            names = signal_names(row["signals"])
            if kind == "approval":
                approvals += 1
            if "address_lookalike" in names and row["event_key"] not in seen_poison:
                seen_poison.add(row["event_key"])
                poisoning += 1
            level = row["level"]
            if kind == "risk" and level in levels:
                levels[level] += 1
    feedback = reader.feedback_counts(user_id, wallets, period)
    correct = feedback.get("CORRECT", 0)
    false_positive = feedback.get("FALSE_POSITIVE", 0)
    previous_transfers = None
    if reader.has_transfers_before(wallets, start):
        previous_transfers = sum(item[2] for item in reader.transfer_breakdown(wallets, previous))
    return WeeklySummary(
        start=start,
        end=end,
        transfers=transfers,
        by_network=tuple(sorted(networks.items())),
        by_classification=tuple(sorted(classes.items())),
        suspicious=suspicious,
        unknown=classes.get("UNKNOWN", 0),
        poisoning=poisoning,
        approvals=approvals,
        risk_levels=None if levels is None else tuple(levels.items()),
        notifications=notifications,
        feedback=feedback,
        labeled=correct + false_positive,
        false_alarm=false_alarm_text(correct, false_positive),
        previous_transfers=previous_transfers,
    )


def change_text(current: int, previous: int | None) -> str:
    if previous is None:
        return "N/A"
    return f"{current - previous:+d}"


class WeeklyReporter:
    """Background loop. A send failure is logged and does not stop later ticks."""

    def __init__(self, state, wallet_store, notifier, reader: ReportReader | None = None, *, interval: float = CHECK_INTERVAL) -> None:
        self.state = state
        self.wallet_store = wallet_store
        self.notifier = notifier
        self.reader = reader or ReportReader(state.path)
        self.interval = interval

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Haftalık özet turu başarısız.")
            await asyncio.sleep(self.interval)

    async def run_once(self, now: datetime | None = None) -> int:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start, end = completed_week(moment)
        if moment < end or moment >= end + timedelta(days=7):
            return 0
        sent = 0
        for user_id, wallets, in_quiet in self._subscribers(moment):
            if in_quiet:
                continue
            week_key = start.isoformat()
            try:
                status = self.state.weekly_delivery_status(user_id, week_key)
            except Exception:
                logger.warning("Haftalık gönderim kaydı okunamadı.")
                continue
            if status == "sent":
                continue
            try:
                summary = build_summary(self.reader, user_id, wallets, start, end)
                from notifications.telegram_formatter import format_weekly_summary
                await self.notifier.send_formatted(format_weekly_summary(summary), chat_id=user_id)
            except Exception:
                logger.warning("Haftalık özet gönderilemedi.")
                continue
            try:
                self.state.mark_weekly_delivery(user_id, week_key, "sent")
            except Exception:
                logger.warning("Haftalık gönderim kaydı yazılamadı.")
            sent += 1
        return sent

    def _subscribers(self, now: datetime) -> list[tuple[str, list[tuple[str, str]], bool]]:
        grouped: dict[str, list[tuple[str, str]]] = {}
        quiet: dict[str, bool] = {}
        try:
            subscriptions = self.wallet_store.all_subscriptions()
        except Exception:
            logger.warning("Haftalık özet için cüzdan listesi okunamadı.")
            return []
        for item in subscriptions:
            if not item.user_id:
                continue
            try:
                prefs = self.state.settings(item.user_id, item.network, item.wallet_address)
            except Exception:
                logger.warning("Haftalık rapor ayarı okunamadı.")
                continue
            if not prefs.weekly_report:
                continue
            user_id = str(item.user_id)
            grouped.setdefault(user_id, []).append((item.network, item.wallet_address))
            if quiet_contains(now, prefs.quiet_start, prefs.quiet_end):
                quiet[user_id] = True
        return [(user_id, wallets, quiet.get(user_id, False)) for user_id, wallets in grouped.items()]
