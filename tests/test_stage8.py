"""Stage 8 reporting: read-only queries, CSV export, weekly summary."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from models.transaction import TokenTransfer
from monitor.state_db import UserStateStore, WalletSettings
from monitor.wallet_store import WalletSubscription
from notifications.telegram import TelegramBotController, TelegramError
from notifications.telegram_formatter import format_transfer_alert, format_weekly_summary
from reporting.access import (
    CSV_COLUMNS,
    ExportTooLarge,
    InvalidRange,
    ReportReader,
    UnauthorizedWallet,
    csv_cell,
    export_filename,
    parse_export_args,
    write_csv,
)
from reporting.weekly import WeeklyReporter, build_summary, completed_week, week_bounds
from analyzer.token_analyzer import TokenAnalysis
from health import OperationsStore


WALLET = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
SECRET_USER = "991199"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)  # Wednesday
MONDAY = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


def state(tmp_path):
    return UserStateStore(tmp_path / "user_state.sqlite")


def reader(saved, operations=None):
    return ReportReader(saved.path, None if operations is None else operations.path)


def seed_transfer(saved, *, when, classification="UNKNOWN", wallet=WALLET, amount="1.50", symbol="USDT", name="Tether", event="log:1", tx="0x" + "ab" * 32, direction="RECEIVE", notified=True, user=SECRET_USER):
    key_record = {"network": "ethereum", "tx_hash": tx, "event_key": event, "wallet": wallet}
    from audit_log import audit_event_key
    key = audit_event_key(key_record)
    saved.record_report_transfer(
        event_key=key, network="ethereum", wallet_address=wallet, tx_hash=tx, direction=direction,
        token_symbol=symbol, token_name=name, amount=amount, classification=classification,
        block_time=when.astimezone(timezone.utc).isoformat(), notified=notified,
    )
    return key


class Wallets:
    def __init__(self, items):
        self.items = items

    def for_user(self, user_id):
        return [item for item in self.items if item.user_id == str(user_id)]

    def all_subscriptions(self):
        return list(self.items)


class Noop:
    async def close(self):
        return None


class Notifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.docs = []
        self.sent = []

    async def send_document(self, chat_id, path, filename, caption=""):
        self.docs.append((filename, path.read_text(encoding="utf-8-sig"), caption, path))
        if self.fail:
            raise TelegramError("Dosya gönderilemedi.")

    async def send_formatted(self, message, chat_id):
        if self.fail:
            raise TelegramError("gönderilemedi")
        self.sent.append((str(chat_id), message.text))

    async def send_text(self, text, chat_id=None, **kwargs):
        self.sent.append((str(chat_id), text))


def controller(saved, wallets, notifier):
    return TelegramBotController(
        bot_token="token", monitor=Noop(), notifier=notifier, wallet_store=wallets,
        price_client=Noop(), user_state=saved,
    )


def test_week_bounds_are_monday_utc_and_end_exclusive():
    assert MONDAY.weekday() == 0
    start, end = week_bounds(NOW)
    assert (start, end) == (MONDAY, MONDAY + timedelta(days=7))
    assert week_bounds(end - timedelta(seconds=1))[0] == MONDAY
    assert week_bounds(end)[0] == end
    assert completed_week(end) == (MONDAY, end)
    assert completed_week(MONDAY - timedelta(seconds=1))[1] == MONDAY - timedelta(days=7)


def test_range_filters_and_empty_database(tmp_path):
    saved = state(tmp_path)
    report = reader(saved)
    allowed = [("ethereum", WALLET)]
    assert list(report.iter_rows(user_id=SECRET_USER, allowed=allowed, period=parse_export_args(["7d"], now=NOW).period)) == []
    summary = build_summary(report, SECRET_USER, allowed, *completed_week(NOW))
    assert summary.transfers == 0 and summary.poisoning == 0 and summary.previous_transfers is None
    missing = ReportReader(tmp_path / "yok.sqlite")
    empty = build_summary(missing, SECRET_USER, allowed, *completed_week(NOW))
    assert empty.poisoning is None and empty.approvals is None and empty.risk_levels is None
    assert empty.false_alarm == "N/A"
    saved.close()


def test_classification_network_and_half_open_utc_bounds(tmp_path):
    saved = state(tmp_path)
    seed_transfer(saved, when=MONDAY, classification="UNKNOWN", event="log:1")
    seed_transfer(saved, when=MONDAY + timedelta(days=3), classification="KNOWN_FRAUD", event="log:2", tx="0x" + "cd" * 32)
    seed_transfer(saved, when=MONDAY + timedelta(days=7), classification="VARIANT", event="log:3", tx="0x" + "ef" * 32)
    report = reader(saved)
    period = parse_export_args(["2026-10-05", "2026-10-12"]).period
    rows = list(report.iter_rows(user_id=SECRET_USER, allowed=[("ethereum", WALLET)], period=period))
    assert [row.classification for row in rows] == ["UNKNOWN", "KNOWN_FRAUD"]
    fraud = list(report.iter_rows(user_id=SECRET_USER, allowed=[("ethereum", WALLET)], period=period, classification="KNOWN_FRAUD"))
    assert len(fraud) == 1 and fraud[0].amount == "1.50"
    assert list(report.iter_rows(user_id=SECRET_USER, allowed=[("ethereum", WALLET)], period=period, network="tron")) == []
    with pytest.raises(UnauthorizedWallet):
        list(report.iter_rows(user_id=SECRET_USER, allowed=[("ethereum", WALLET)], period=period, wallet=("ethereum", OTHER)))
    saved.close()


def test_reads_do_not_change_rows_and_connection_is_read_only(tmp_path):
    saved = state(tmp_path)
    seed_transfer(saved, when=NOW, classification="CANONICAL")
    report = reader(saved)
    before = saved._connection.execute("SELECT classification, amount FROM report_transfers").fetchall()
    list(report.iter_rows(user_id=SECRET_USER, allowed=[("ethereum", WALLET)], period=parse_export_args(["30d"], now=NOW).period))
    build_summary(report, SECRET_USER, [("ethereum", WALLET)], *completed_week(NOW + timedelta(days=7)))
    after = saved._connection.execute("SELECT classification, amount FROM report_transfers").fetchall()
    assert before == after
    connection = report._connect(saved.path)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM report_transfers")
    connection.close()
    saved.close()


def test_batches_and_duplicate_event_is_one_row(tmp_path):
    saved = state(tmp_path)
    for index in range(5):
        seed_transfer(saved, when=MONDAY + timedelta(hours=index), event=f"log:{index}", tx="0x" + f"{index:064x}")
    seed_transfer(saved, when=MONDAY, event="log:0", tx="0x" + f"{0:064x}", amount="9")
    report = reader(saved)
    rows = list(report.iter_rows(
        user_id=SECRET_USER, allowed=[("ethereum", WALLET)],
        period=parse_export_args(["7d"], now=MONDAY + timedelta(days=6)).period, batch_size=2,
    ))
    assert len(rows) == 5
    assert rows[0].amount == "1.50"
    saved.close()


def test_csv_utf8_formula_and_secrets(tmp_path):
    saved = state(tmp_path)
    seed_transfer(
        saved, when=MONDAY, classification="UNKNOWN", name="=Şüpheli", symbol="-USDT", amount="-2.50",
    )
    saved.save_detection(
        event_key=__import__("audit_log").audit_event_key({"network": "ethereum", "tx_hash": "0x" + "ab" * 32, "event_key": "log:1", "wallet": WALLET}),
        kind="risk", network="ethereum", wallet_address=WALLET, tx_hash="0x" + "ab" * 32,
        score=20, level="DÜŞÜK", weights_version="stage7-v1",
        signals=[{"name": "address_lookalike", "weight": 30, "verified": False, "evidence": "benzer"}],
    )
    report = reader(saved)
    period = parse_export_args(["2026-10-05", "2026-10-12"]).period
    rows = list(report.iter_rows(user_id=SECRET_USER, allowed=[("ethereum", WALLET)], period=period))
    path, count = write_csv(rows, tmp_path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    finally:
        path.unlink()
    assert count == 1
    assert text.startswith("time_utc,")
    assert "Şüpheli" in text and "'=Şüpheli" in text
    assert "'-USDT" in text and ",-2.50," in text
    assert "address_poisoning" in text and "UNKNOWN" in text
    assert WALLET not in text and SECRET_USER not in text and "api_key" not in text.lower()
    assert export_filename(period) == "aft-export-20261005-20261012.csv"
    assert csv_cell("=1+1") == "'=1+1"
    assert csv_cell("12.5", numeric=True) == "12.5"


def test_csv_limits_cleanup_and_other_users_export(tmp_path):
    saved = state(tmp_path)
    for index in range(3):
        seed_transfer(saved, when=MONDAY + timedelta(hours=index), event=f"log:{index}", tx="0x" + f"{index:064x}")
    report = reader(saved)
    period = parse_export_args(["7d"], now=MONDAY + timedelta(days=2)).period
    rows = report.iter_rows(user_id=SECRET_USER, allowed=[("ethereum", WALLET)], period=period)
    with pytest.raises(ExportTooLarge):
        write_csv(rows, tmp_path, max_rows=2)
    assert list(tmp_path.glob("aft-export-*.csv")) == []
    with pytest.raises(InvalidRange):
        parse_export_args(["2026-10-12", "2026-10-05"])
    with pytest.raises(InvalidRange):
        parse_export_args(["2024-01-01", "2026-01-01"])
    wallets = Wallets([WalletSubscription(SECRET_USER, "ethereum", WALLET)])
    failing = Notifier(fail=True)
    bot = controller(saved, wallets, failing)
    asyncio.run(bot._run_export(SECRET_USER, SECRET_USER, ["7d"]))
    assert list(tmp_path.glob("aft-export-*.csv")) == []
    assert failing.docs and "time_utc" in failing.docs[0][1]
    assert failing.docs[0][3].exists() is False
    denied = Notifier()
    asyncio.run(controller(saved, wallets, denied)._run_export(SECRET_USER, SECRET_USER, ["7d", OTHER]))
    assert denied.docs == []
    assert any("yetkin yok" in text for _, text in denied.sent)
    saved.close()


def test_weekly_summary_labels_unknown_and_restart(tmp_path):
    saved = state(tmp_path)
    saved.save_settings(SECRET_USER, "ethereum", WALLET, WalletSettings(weekly_report=True, suspicious_only=True, alias="Korunan"))
    seed_transfer(saved, when=MONDAY + timedelta(days=1), classification="UNKNOWN", notified=True, event="log:1")
    seed_transfer(saved, when=MONDAY + timedelta(days=2), classification="KNOWN_FRAUD", notified=False, event="log:2", tx="0x" + "22" * 32)
    seed_transfer(saved, when=MONDAY - timedelta(days=3), classification="CANONICAL", event="log:old", tx="0x" + "33" * 32)
    saved.save_detection(
        event_key="approval-week", kind="approval", network="ethereum", wallet_address=WALLET, tx_hash="0x" + "55" * 32,
        score=80, level="KRİTİK", weights_version="stage7-v1", signals=[],
    )
    saved._connection.execute(
        "UPDATE detections SET created_at=? WHERE event_key=?",
        ((MONDAY + timedelta(days=1)).isoformat(), "approval-week"),
    )
    saved._connection.commit()
    for kind in ("CORRECT", "FALSE_POSITIVE"):
        token = saved.register_alert(
            user_id=SECRET_USER, network="ethereum", wallet_address=WALLET, classification="UNKNOWN",
            record={"network": "ethereum", "tx_hash": "0x" + "ab" * 32, "event_key": f"fb-{kind}", "wallet": WALLET},
        )
        saved.set_feedback(token, SECRET_USER, kind)
    report = reader(saved)
    summary = build_summary(report, SECRET_USER, [("ethereum", WALLET)], MONDAY, MONDAY + timedelta(days=7))
    assert summary.transfers == 2
    assert summary.unknown == 1 and summary.suspicious == 1
    assert summary.notifications == 1
    assert summary.approvals == 1
    assert summary.false_alarm == "Yeterli etiketli veri yok"
    assert summary.previous_transfers == 1
    text = format_weekly_summary(summary).text
    assert "2026-10-05 00:00 UTC" in text and "2026-10-12 00:00 UTC" in text
    assert "Yeterli etiketli veri yok" in text and WALLET not in text and SECRET_USER not in text
    assert "Haftalık özet" in text and "━━━━━━━━━━━━━━━━━━━━" in text
    plain = format_transfer_alert(
        TokenTransfer(
            network="ethereum", tx_hash="0x" + "ab" * 32, log_index=1, block_number=1, timestamp=NOW,
            token_address="0x" + "cd" * 20, token_symbol="USDT", token_name="Tether", decimals=6,
            amount=Decimal("1"), sender=OTHER, receiver=WALLET, direction="RECEIVE",
        ),
        TokenAnalysis("CANONICAL", "USDT"),
    )
    assert "Haftalık" not in plain.text and "Risk skoru" not in plain.text

    saved.save_settings("2", "ethereum", OTHER, WalletSettings(weekly_report=True))
    wallets = Wallets([
        WalletSubscription(SECRET_USER, "ethereum", WALLET),
        WalletSubscription("2", "ethereum", OTHER),
    ])
    notifier = Notifier()
    reporter = WeeklyReporter(saved, wallets, notifier, report)
    when = MONDAY + timedelta(days=9)  # the week of Oct 5 has ended
    assert asyncio.run(reporter.run_once(when)) == 2
    assert asyncio.run(reporter.run_once(when)) == 0
    assert len(notifier.sent) == 2
    quiet = UserStateStore(tmp_path / "quiet.sqlite")
    quiet.save_settings(SECRET_USER, "ethereum", WALLET, WalletSettings(weekly_report=True, quiet_start="00:00", quiet_end="23:00"))
    quiet_notifier = Notifier()
    quiet_reporter = WeeklyReporter(quiet, Wallets([WalletSubscription(SECRET_USER, "ethereum", WALLET)]), quiet_notifier, ReportReader(quiet.path))
    noon = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    assert asyncio.run(quiet_reporter.run_once(noon)) == 0
    assert quiet_notifier.sent == []
    saved.close()
    quiet.close()


def test_false_alarm_needs_labels_and_send_failure_stays_local(tmp_path):
    from reporting.weekly import false_alarm_text
    assert false_alarm_text(0, 0) == "N/A"
    assert false_alarm_text(2, 1) == "Yeterli etiketli veri yok"
    assert false_alarm_text(6, 3) == "33.3%"
    saved = state(tmp_path)
    saved.save_settings(SECRET_USER, "ethereum", WALLET, WalletSettings(weekly_report=True, min_amount=Decimal("5")))
    wallets = Wallets([WalletSubscription(SECRET_USER, "ethereum", WALLET)])
    failing = Notifier(fail=True)
    reporter = WeeklyReporter(saved, wallets, failing, reader(saved))

    async def both():
        sent = await reporter.run_once(MONDAY + timedelta(days=8))
        return sent, "izleme"

    sent, marker = asyncio.run(both())
    assert sent == 0 and marker == "izleme" and failing.sent == []
    assert saved.weekly_delivery_status(SECRET_USER, MONDAY.isoformat()) is None
    assert saved.settings(SECRET_USER, "ethereum", WALLET).min_amount == Decimal("5")
    assert saved.settings(SECRET_USER, "ethereum", WALLET).weekly_report is True
    saved.close()


def test_global_metrics_stay_separate_from_personal_summary(tmp_path):
    saved = state(tmp_path)
    operations = OperationsStore(tmp_path / "operations.sqlite")
    operations.increment("check_usage", at=MONDAY + timedelta(hours=1))
    operations.increment("errors", at=MONDAY + timedelta(hours=2))
    operations.increment("transfer_total", at=MONDAY + timedelta(hours=3))
    report = reader(saved, operations)
    totals = report.metric_totals(parse_export_args(["2026-10-05", "2026-10-12"]).period)
    assert totals["check_usage"] == 1 and totals["errors"] == 1 and totals["transfer_total"] == 1
    text = format_weekly_summary(build_summary(report, SECRET_USER, [("ethereum", WALLET)], MONDAY, MONDAY + timedelta(days=7))).text
    assert "check_usage" not in text and "API" not in text
    saved.close()


def test_stage6_toggle_is_independent_and_stage7_detection_is_read(tmp_path):
    saved = state(tmp_path)
    saved.save_settings(SECRET_USER, "ethereum", WALLET, WalletSettings(alias="Ada", suspicious_only=True, weekly_report=True))
    prefs = saved.settings(SECRET_USER, "ethereum", WALLET)
    assert prefs.alias == "Ada" and prefs.suspicious_only and prefs.weekly_report
    saved.save_settings(SECRET_USER, "ethereum", WALLET, WalletSettings(alias="Ada", suspicious_only=True, weekly_report=False))
    assert saved.settings(SECRET_USER, "ethereum", WALLET).suspicious_only is True
    key = seed_transfer(saved, when=MONDAY + timedelta(days=1), classification="UNKNOWN")
    saved.save_detection(
        event_key=key, kind="risk", network="ethereum", wallet_address=WALLET, tx_hash="0x" + "ab" * 32,
        score=55, level="YÜKSEK", weights_version="stage7-v1", signals=[{"name": "variant_token"}],
    )
    rows = list(reader(saved).iter_rows(
        user_id=SECRET_USER, allowed=[("ethereum", WALLET)],
        period=parse_export_args(["2026-10-05", "2026-10-12"]).period,
    ))
    assert rows[0].classification == "UNKNOWN"
    assert rows[0].risk_score == "55" and rows[0].risk_level == "YÜKSEK"
    assert rows[0].detection_kind == "transfer"
    header = write_csv([], tmp_path)[0]
    assert header.read_text(encoding="utf-8-sig").strip() == ",".join(CSV_COLUMNS)
    header.unlink()
    saved.close()
