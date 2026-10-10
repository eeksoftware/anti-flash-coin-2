import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from audit_log import audit_event_key
from models.transaction import TokenTransfer
from monitor.state_db import (
    SettingsInputError,
    UserStateStore,
    WalletSettings,
    expected_update,
    feedback_callback,
    migrate,
    parse_alias,
    parse_feedback_callback,
    parse_min_amount,
    parse_quiet_hours,
    quiet_contains,
    skip_notification,
)
from monitor.wallet_store import WalletSubscription
from notifications.telegram import TelegramBotController
from notifications.telegram_formatter import format_transfer_alert, format_wallet_settings
from notifications.telegram_keyboards import wallet_settings_keyboard
from analyzer.token_analyzer import TokenAnalysis


WALLET = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
NOW = datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc)


def record(classification="KNOWN_FRAUD", wallet=WALLET, expected=None, source=None):
    payload = {
        "network": "ethereum",
        "tx_hash": "0x" + "ab" * 32,
        "log_index": 4,
        "event_key": "log:4",
        "token_address": "0x" + "cd" * 20,
        "wallet": wallet,
        "classification": classification,
        "expected": expected,
    }
    if source:
        payload["expected_source"] = source
    return payload


def store(tmp_path):
    return UserStateStore(tmp_path / "user_state.sqlite")


def test_feedback_kinds_link_to_one_transfer_and_do_not_duplicate(tmp_path):
    saved = store(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    payload = record()
    (log_dir / "transactions.log").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    token = saved.register_alert(
        user_id="7", network="ethereum", wallet_address=WALLET,
        classification="KNOWN_FRAUD", record=payload,
    )
    assert saved.set_feedback(token, "7", "CORRECT", log_dir=log_dir) == "updated"
    assert saved.set_feedback(token, "7", "CORRECT", log_dir=log_dir) == "unchanged"
    row = saved.feedback(token)
    assert row.feedback == "CORRECT" and row.previous_feedback is None
    assert row.event_key == audit_event_key(payload)
    updated = saved.set_feedback(token, "8", "FALSE_POSITIVE", log_dir=log_dir)
    assert updated == "unauthorized"
    assert saved.feedback(token).feedback == "CORRECT"
    assert saved.set_feedback(token, "7", "FALSE_POSITIVE", log_dir=log_dir) == "updated"
    changed = saved.feedback(token)
    assert changed.feedback == "FALSE_POSITIVE" and changed.previous_feedback == "CORRECT"
    lines = [json.loads(line) for line in (log_dir / "transactions.log").read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["classification"] == "KNOWN_FRAUD"
    assert lines[0]["expected"] == "gercek"
    assert lines[0]["expected_source"] == "feedback"
    with sqlite3.connect(saved.path) as db:
        assert db.execute("SELECT COUNT(*) FROM transfer_feedback").fetchone()[0] == 1


def test_unsure_is_not_a_definitive_expected_label(tmp_path):
    saved = store(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    payload = record(classification="VARIANT")
    (log_dir / "transactions.log").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    token = saved.register_alert(
        user_id="7", network="ethereum", wallet_address=WALLET,
        classification="VARIANT", record=payload,
    )
    saved.set_feedback(token, "7", "CORRECT", log_dir=log_dir)
    saved.set_feedback(token, "7", "UNSURE", log_dir=log_dir)
    line = json.loads((log_dir / "transactions.log").read_text())
    assert line["expected"] is None
    assert line["expected"] not in {"gercek", "sahte", "supheli"}
    assert expected_update("UNSURE", "KNOWN_FRAUD") == ("clear", None)


def test_manual_expected_is_preserved(tmp_path):
    saved = store(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    payload = record(expected="sahte", source="manual")
    (log_dir / "transactions.log").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    token = saved.register_alert(
        user_id="7", network="ethereum", wallet_address=WALLET,
        classification="KNOWN_FRAUD", record=payload,
    )
    saved.set_feedback(token, "7", "FALSE_POSITIVE", log_dir=log_dir)
    line = json.loads((log_dir / "transactions.log").read_text())
    assert line["expected"] == "sahte" and line["expected_source"] == "manual"


def test_invalid_feedback_callback_is_rejected():
    assert parse_feedback_callback("fb:c:" + "ab" * 8)[0] == "CORRECT"
    assert parse_feedback_callback("fb:nope:" + "ab" * 8) is None
    assert parse_feedback_callback("fb:c:" + "A" * 16) is None
    assert parse_feedback_callback("x" * 70) is None
    for kind in ("CORRECT", "FALSE_POSITIVE", "UNSURE"):
        data = feedback_callback(kind, "ab" * 8)
        assert len(data.encode("utf-8")) <= 64
        assert parse_feedback_callback(data)[0] == kind


def test_wallet_settings_are_independent_and_keep_tron_case(tmp_path):
    saved = store(tmp_path)
    saved.save_settings("7", "ethereum", WALLET, WalletSettings(alias="Bir", min_amount=Decimal("1.5")))
    saved.save_settings("7", "tron", TRON, WalletSettings(alias="Iki", suspicious_only=True))
    saved.save_settings("8", "ethereum", WALLET, WalletSettings(alias="Uc"))
    assert saved.settings("7", "ethereum", WALLET).alias == "Bir"
    assert saved.settings("7", "tron", TRON).alias == "Iki"
    assert saved.settings("7", "tron", TRON).suspicious_only is True
    assert saved.settings("8", "ethereum", WALLET).alias == "Uc"
    with sqlite3.connect(saved.path) as db:
        stored = db.execute(
            "SELECT wallet_address FROM wallet_settings WHERE network='tron'"
        ).fetchone()[0]
    assert stored == TRON


def test_alias_and_amount_validation():
    assert parse_alias("  Ada  ") == "Ada"
    assert parse_alias("   ") == ""
    with pytest.raises(SettingsInputError):
        parse_alias("a" * 33)
    with pytest.raises(SettingsInputError):
        parse_alias("satır\nbitti")
    assert parse_min_amount("0") == Decimal("0")
    assert parse_min_amount("1,25") == Decimal("1.25")
    assert parse_min_amount("0.000000000000000001") == Decimal("0.000000000000000001")
    for bad in ("-1", "nan", "NaN", "Infinity", "-inf", "abc", ""):
        with pytest.raises(SettingsInputError):
            parse_min_amount(bad)


def test_amount_filter_compares_only_this_transfer_amount():
    prefs = WalletSettings(min_amount=Decimal("10"))
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    small = skip_notification("CANONICAL", Decimal("9.999"), prefs, now)
    large = skip_notification("CANONICAL", Decimal("10"), prefs, now)
    other_token = skip_notification("VARIANT", Decimal("1000000"), prefs, now)
    assert small == "min_amount"
    assert large is None and other_token is None
    tiny = WalletSettings(min_amount=Decimal("0.000000000000000002"))
    assert skip_notification("CANONICAL", Decimal("0.000000000000000001"), tiny, now) == "min_amount"
    huge = WalletSettings(min_amount=Decimal("999999999999999999"))
    assert skip_notification("CANONICAL", Decimal("1000000000000000000"), huge, now) is None


def test_suspicious_filter_does_not_reclassify():
    prefs = WalletSettings(suspicious_only=True)
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    classification = "CANONICAL"
    assert skip_notification(classification, Decimal("5"), prefs, now) == "suspicious_only"
    assert classification == "CANONICAL"
    assert skip_notification("VARIANT", Decimal("5"), prefs, now) is None
    assert skip_notification("KNOWN_FRAUD", Decimal("5"), prefs, now) is None
    assert skip_notification("UNKNOWN", Decimal("5"), prefs, now) == "suspicious_only"
    assert skip_notification("CANONICAL", Decimal("5"), WalletSettings(), now) is None


def test_quiet_hours_cover_midnight_and_equal_bounds_do_not_mute():
    assert parse_quiet_hours("22:00-08:00") == ("22:00", "08:00")
    assert parse_quiet_hours("kapat") is None
    with pytest.raises(SettingsInputError):
        parse_quiet_hours("25:00-01:00")
    start, end = "22:00", "08:00"
    assert quiet_contains(datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc), start, end)
    assert quiet_contains(datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc), start, end)
    assert quiet_contains(datetime(2026, 10, 11, 7, 59, tzinfo=timezone.utc), start, end)
    assert not quiet_contains(datetime(2026, 10, 11, 8, 0, tzinfo=timezone.utc), start, end)
    assert not quiet_contains(datetime(2026, 10, 10, 21, 59, tzinfo=timezone.utc), start, end)
    assert quiet_contains(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc), "09:00", "17:00")
    assert not quiet_contains(datetime(2026, 10, 10, 17, 0, tzinfo=timezone.utc), "09:00", "17:00")
    assert not quiet_contains(NOW, "09:00", "09:00")
    assert skip_notification("VARIANT", Decimal("1"), WalletSettings(quiet_start="22:00", quiet_end="08:00"), NOW) == "quiet_hours"


def test_migration_is_idempotent_and_rolls_back(tmp_path):
    path = tmp_path / "user_state.sqlite"
    legacy = tmp_path / "operations.sqlite"
    wallets = tmp_path / "runtime_state.json"
    logs = tmp_path / "transactions.log"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE sample(value TEXT)")
        db.execute("INSERT INTO sample VALUES ('eski')")
    wallets.write_text('{"legacy":[],"users":{}}\n', encoding="utf-8")
    logs.write_text(json.dumps(record(expected="gercek", source="manual")) + "\n", encoding="utf-8")
    saved = store(tmp_path)
    saved.save_settings("7", "ethereum", WALLET, WalletSettings(alias="Duruyor"))
    UserStateStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        assert db.execute("SELECT alias FROM wallet_settings").fetchone()[0] == "Duruyor"
    with sqlite3.connect(legacy) as db:
        assert db.execute("SELECT value FROM sample").fetchone()[0] == "eski"
    assert wallets.read_text(encoding="utf-8").startswith('{"legacy"')
    assert json.loads(logs.read_text())["expected"] == "gercek"

    broken = sqlite3.connect(tmp_path / "broken.sqlite")
    def fail(connection):
        connection.execute("CREATE TABLE partial_settings (id INTEGER)")
        raise RuntimeError("yarım")
    with pytest.raises(RuntimeError):
        migrate(broken, steps=[(1, fail)])
    names = {row[0] for row in broken.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "partial_settings" not in names
    assert "wallet_settings" not in names
    broken.close()
    assert saved.settings("9", "ethereum", OTHER) == WalletSettings()


def test_one_wallet_settings_error_keeps_other_wallets_on_their_own_rules():
    class Store:
        def settings(self, user_id, network, address):
            if address == WALLET:
                raise sqlite3.OperationalError("locked")
            return WalletSettings(min_amount=Decimal("5"), suspicious_only=True)

    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    decisions = []
    for address, classification, amount in (
        (WALLET, "CANONICAL", Decimal("1")),
        (OTHER, "CANONICAL", Decimal("1")),
        (OTHER, "VARIANT", Decimal("9")),
    ):
        try:
            prefs = Store().settings("7", "ethereum", address)
        except Exception:
            prefs = WalletSettings()
        decisions.append(skip_notification(classification, amount, prefs, now))
        assert classification in {"CANONICAL", "VARIANT"}
    assert decisions == [None, "suspicious_only", None]


def test_default_alert_text_stays_and_buttons_are_added_for_feedback():
    transfer = TokenTransfer(
        network="ethereum", tx_hash="0x" + "ab" * 32, log_index=1, block_number=1,
        timestamp=datetime(2026, 10, 10, tzinfo=timezone.utc),
        token_address="0x" + "cd" * 20, token_symbol="USDT", token_name="Tether",
        decimals=6, amount=Decimal("1"), sender=WALLET, receiver=OTHER, direction="RECEIVE",
    )
    plain = format_transfer_alert(transfer, TokenAnalysis("CANONICAL", "USDT"))
    assert "Takma ad" not in plain.text
    assert "fb:" not in json.dumps(plain.reply_markup)
    alert = format_transfer_alert(
        transfer, TokenAnalysis("CANONICAL", "USDT"), alias="<b>Ada</b>", feedback_token="ab" * 8,
    )
    assert "<code>&lt;b&gt;Ada&lt;/b&gt;</code>" in alert.text
    labels = [button["text"] for row in alert.reply_markup["inline_keyboard"] for button in row]
    assert labels[-3:] == ["👍 Doğru tespit", "👎 Yanlış alarm", "❓ Emin değilim"]
    assert all(len(button.get("callback_data", "").encode("utf-8")) <= 64 for row in alert.reply_markup["inline_keyboard"] for button in row)
    settings = format_wallet_settings(TRON, "tron", WalletSettings(alias="<img>"))
    assert TRON in settings.text and "&lt;img&gt;" in settings.text
    assert all(len(item.encode("utf-8")) <= 64 for item in (
        button["callback_data"]
        for row in wallet_settings_keyboard(TRON, "tron")["inline_keyboard"]
        for button in row if "callback_data" in button
    ))


def test_settings_callback_rejects_another_users_wallet():
    class Store:
        def __init__(self):
            self.saved = []
        def settings(self, user_id, network, address):
            return WalletSettings()
        def save_settings(self, *args):
            self.saved.append(args)

    class Notifier:
        def __init__(self):
            self.edited = []
            self.answered = []
        async def edit_formatted(self, chat_id, message_id, message):
            self.edited.append(message)
        async def answer_callback_query(self, callback_id, text=None, show_alert=False):
            self.answered.append(text)
        async def send_formatted(self, message, chat_id):
            return {}

    class Wallets:
        def for_user(self, user_id):
            if user_id == "7":
                return [WalletSubscription("7", "ethereum", WALLET)]
            return []

    notifier = Notifier()
    state = Store()
    controller = TelegramBotController(
        "token", object(), notifier, Wallets(), object(), activity_log_chat_id="", user_state=state,
    )
    asyncio.run(controller._dispatch_callback(
        "w:set:" + WALLET, "8", "8", 3, {"id": "8", "first_name": "Bob"},
    ))
    assert state.saved == []
    assert notifier.edited[0].text.startswith("⚠️ <b>Bu menü eski</b>")


def test_feedback_callback_answers_and_rejects_unauthorized(tmp_path):
    saved = store(tmp_path)
    payload = record()
    token = saved.register_alert(
        user_id="7", network="ethereum", wallet_address=WALLET,
        classification="KNOWN_FRAUD", record=payload,
    )

    class Notifier:
        def __init__(self):
            self.answered = []
        async def answer_callback_query(self, callback_id, text=None, show_alert=False):
            self.answered.append((callback_id, text))

    class Wallets:
        def for_user(self, user_id):
            if user_id == "7":
                return [WalletSubscription("7", "ethereum", WALLET)]
            return []

    notifier = Notifier()
    controller = TelegramBotController(
        "token", object(), notifier, Wallets(), object(), activity_log_chat_id="", user_state=saved,
    )
    foreign = {
        "id": "cb-1",
        "data": feedback_callback("FALSE_POSITIVE", token),
        "from": {"id": 8},
        "message": {"message_id": 1, "chat": {"id": 8, "type": "private"}},
    }
    asyncio.run(controller._handle_callback_query(foreign))
    assert notifier.answered == [("cb-1", "Bu işlem için yetkin yok.")]
    assert saved.feedback(token).feedback is None
    missing = dict(foreign)
    missing["id"] = "cb-2"
    missing["data"] = "fb:c:" + "zz" * 8
    missing["from"] = {"id": 7}
    missing["message"] = {"message_id": 1, "chat": {"id": 7, "type": "private"}}
    asyncio.run(controller._handle_callback_query(missing))
    assert notifier.answered[-1] == ("cb-2", "Bu geri bildirim geçersiz.")
