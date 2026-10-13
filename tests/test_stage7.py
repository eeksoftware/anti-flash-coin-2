"""Stage 7: address poisoning, approval monitoring, risk score. No network calls."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from analyzer import address_poisoning
from analyzer.approvals import (
    APPROVAL_TOPIC,
    ApprovalEvent,
    SpenderRegistry,
    UnsupportedApprovalNetwork,
    assess,
    parse_approval_log,
)
from analyzer.risk_engine import (
    LEVEL_THRESHOLDS,
    WEIGHTS,
    Signal,
    classification_signals,
    compute_risk,
    level_for,
    make_signal,
)
from analyzer.token_analyzer import TokenAnalysis
from models.transaction import TokenTransfer
from monitor.approval_monitor import ApprovalMonitor
from monitor.state_db import UserStateStore, WalletSettings, skip_notification
from monitor.wallet_monitor import WalletMonitor
from monitor.transaction_tracker import TransactionTracker
from monitor.wallet_store import WalletSubscription
from notifications.telegram_formatter import format_approval_alert, format_transfer_alert
from providers.base import UnsupportedFeature
from providers.ethereum import EthereumProvider
from providers.tron import TronProvider


WALLET = "0x" + "11" * 20
KNOWN = "0x1234abcd" + "00" * 12 + "9876fedc"
# 33/40 characters match (82.5%). Exactly 80% and a 4+4 head/tail overlap do not.
LOOKALIKE = "0x1234abcd" + "00" * 8 + "0" + "ff" * 3 + "f" + "9876fedc"
BORDERLINE = "0x1234abcd" + "00" * 8 + "ff" * 4 + "9876fedc"
SHORT_OVERLAP = "0x1234" + "ff" * 16 + "fedc"
UNRELATED = "0x" + "77" * 20
TOKEN = "0x" + "cd" * 20
SPENDER = "0x" + "ee" * 20
TX = "0x" + "ab" * 32
TRON_WALLET = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_KNOWN = "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def transfer(sender=KNOWN, receiver=WALLET, amount="0.0001", network="ethereum", event_key="log:1"):
    return TokenTransfer(
        network=network, tx_hash=TX, log_index=1, block_number=100, timestamp=NOW,
        token_address=TOKEN, token_symbol="USDT", token_name="Tether USD", decimals=6,
        amount=Decimal(amount), sender=sender, receiver=receiver,
        direction="RECEIVE" if receiver == WALLET else "SEND", event_key=event_key,
    )


# --- 7a address poisoning ---------------------------------------------------

def test_lookalike_detected_with_prefix_suffix_and_dust():
    finding = address_poisoning.evaluate("ethereum", WALLET, LOOKALIKE, Decimal("0.0001"), [KNOWN, UNRELATED], event_key="log:1")
    assert finding is not None
    assert finding.known_address == KNOWN
    assert finding.similarity.prefix >= 8 and finding.similarity.suffix >= 8
    assert finding.similarity.middle_differs
    names = {signal.name for signal in finding.signals}
    assert names == {"address_lookalike", "lookalike_first_contact", "lookalike_dust_amount"}
    assert all(not signal.verified for signal in finding.signals)


def test_similarity_at_or_below_80_percent_is_not_a_finding():
    assert address_poisoning.similarity(LOOKALIKE, KNOWN).ratio > 0.80
    assert address_poisoning.similarity(BORDERLINE, KNOWN).ratio == 0.80
    short = address_poisoning.similarity(SHORT_OVERLAP, KNOWN)
    assert short.prefix == 4 and short.suffix == 4 and short.ratio < 0.80
    assert address_poisoning.evaluate("ethereum", WALLET, BORDERLINE, Decimal("0.0001"), [KNOWN]) is None
    assert address_poisoning.evaluate("ethereum", WALLET, SHORT_OVERLAP, Decimal("0.0001"), [KNOWN]) is None


def test_unrelated_or_identical_addresses_produce_no_finding():
    assert address_poisoning.evaluate("ethereum", WALLET, UNRELATED, Decimal("5"), [KNOWN]) is None
    assert address_poisoning.evaluate("ethereum", WALLET, KNOWN, Decimal("5"), [KNOWN]) is None
    assert address_poisoning.evaluate("ethereum", WALLET, WALLET, Decimal("5"), [KNOWN]) is None
    assert address_poisoning.evaluate("ethereum", WALLET, LOOKALIKE, Decimal("5"), []) is None


def test_evm_comparison_is_case_insensitive_and_tron_case_preserved():
    upper = address_poisoning.evaluate("ethereum", WALLET.upper().replace("0X", "0x"), LOOKALIKE.upper().replace("0X", "0x"), Decimal("1"), [KNOWN])
    assert upper is not None and upper.counterparty == LOOKALIKE.lower()
    tron_same = address_poisoning.evaluate("tron", TRON_WALLET, TRON_KNOWN, Decimal("1"), [TRON_KNOWN])
    assert tron_same is None  # exact known address, no lookalike
    tron_config = address_poisoning.get_network_config("tron")
    assert address_poisoning.compare_key(tron_config, TRON_KNOWN) == TRON_KNOWN  # never lowercased
    assert address_poisoning.compare_key(tron_config, TRON_KNOWN.lower()) is None  # checksum breaks
    measured = address_poisoning.similarity(TRON_KNOWN, TRON_KNOWN[:-4] + "aaaa")
    assert measured.prefix == len(TRON_KNOWN) - 4 and measured.suffix == 0


def test_cross_network_addresses_are_never_compared():
    assert address_poisoning.evaluate("tron", TRON_WALLET, LOOKALIKE, Decimal("1"), [KNOWN]) is None
    assert address_poisoning.evaluate("ethereum", WALLET, LOOKALIKE, Decimal("1"), [TRON_KNOWN]) is None


def test_address_history_is_seeded_from_baseline_not_alerted(tmp_path):
    state = UserStateStore(tmp_path / "state.sqlite")
    seen: list = []

    class Provider:
        network = "ethereum"
        async def get_transaction(self, tx_hash): return None
        async def get_token_transfer_page(self, address, page, limit):
            return [transfer(sender=KNOWN), transfer(receiver=UNRELATED, sender=WALLET, event_key="log:2")]

    async def on_transfers(items): seen.append(("alert", items))
    async def on_baseline(items):
        state.record_counterparties("ethereum", WALLET, [t.sender if t.direction == "RECEIVE" else t.receiver for t in items])

    monitor = WalletMonitor(Provider(), TransactionTracker(), WALLET, 0.01, 0.01, on_transfers=on_transfers, on_baseline=on_baseline)

    async def run():
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    asyncio.run(run())
    assert seen == []
    assert set(state.counterparties("ethereum", WALLET)) == {KNOWN, UNRELATED}
    state.record_counterparties("ethereum", WALLET, [KNOWN])
    with sqlite3.connect(tmp_path / "state.sqlite") as db:
        assert db.execute("SELECT transfer_count FROM address_history WHERE counterparty=?", (KNOWN,)).fetchone()[0] == 2
    state.close()


# --- 7b approvals -----------------------------------------------------------

def raw_log(value_hex, owner=WALLET, spender=SPENDER, log_index="0x3", block="0x64"):
    return {
        "address": TOKEN,
        "topics": [APPROVAL_TOPIC, "0x" + owner[2:].rjust(64, "0"), "0x" + spender[2:].rjust(64, "0")],
        "data": value_hex, "blockNumber": block, "logIndex": log_index, "transactionHash": TX,
    }


def test_parse_approval_log_and_states():
    event = parse_approval_log(raw_log("0x" + "f" * 64))
    assert event is not None and event.owner == WALLET and event.spender == SPENDER
    assert assess(event, None, 6).state == "UNLIMITED"
    new = parse_approval_log(raw_log(hex(10 * 10**6)))
    assert assess(new, None, 6).state == "NEW"
    assert assess(new, 5 * 10**6, 6).state == "INCREASED"
    assert assess(new, 50 * 10**6, 6).state == "DECREASED"
    revoked = parse_approval_log(raw_log("0x0"))
    assert assess(revoked, 10, 6).state == "REVOKED"
    assert assess(revoked, 10, 6).signals == ()
    assert parse_approval_log({"topics": ["0x00"], "data": "0x"}) is None
    assert parse_approval_log(raw_log("0x1"), "tron") is None


def test_large_allowance_is_weak_signal_not_fraud(tmp_path):
    big = parse_approval_log(raw_log(hex(5_000_000 * 10**6)))
    result = assess(big, None, 6, SpenderRegistry(tmp_path))
    names = {signal.name for signal in result.signals}
    assert names == {"new_allowance", "large_allowance"}
    risk = compute_risk(result.signals)
    assert risk.level in {"DÜŞÜK", "ORTA"}
    assert result.spender_label is None


def test_known_fraud_contract_as_spender_is_flagged(tmp_path):
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    (registry_dir / "known_fraud_tokens.json").write_text(
        '{"known_fraud_tokens": [{"network": "ethereum", "contract": "%s", "symbol": "X"}]}' % SPENDER, encoding="utf-8",
    )
    (registry_dir / "known_spenders.json").write_text('{"risky": [{"network": "ethereum", "address": "%s"}]}' % UNRELATED, encoding="utf-8")
    registry = SpenderRegistry(registry_dir)
    assert registry.lookup("ethereum", UNRELATED) is None  # no source -> ignored
    event = parse_approval_log(raw_log(hex(10**6)))
    result = assess(event, None, 6, registry)
    assert "known_fraud_spender" in {signal.name for signal in result.signals}
    assert compute_risk(result.signals).level in {"YÜKSEK", "KRİTİK"}


def test_tron_provider_reports_unsupported():
    assert TronProvider.supports_approvals is False
    with pytest.raises(UnsupportedFeature):
        asyncio.run(TronProvider.get_approval_events(object(), TRON_WALLET, 1, 2))
    event = ApprovalEvent("tron", "ab" * 32, 1, 0, TRON_KNOWN, TRON_WALLET, TRON_KNOWN, 1)
    with pytest.raises(UnsupportedApprovalNetwork):
        assess(event, None, 6)


def test_ethereum_provider_filters_owner_topic():
    class FakeClient:
        def __init__(self): self.calls = []
        async def get_logs(self, **kwargs):
            self.calls.append(kwargs)
            return [raw_log(hex(7)), raw_log(hex(9), owner=UNRELATED, log_index="0x1")]
        async def get_block_number(self): return 123
        async def close(self): pass
    provider = EthereumProvider(FakeClient())
    events = asyncio.run(provider.get_approval_events(WALLET, 10, 20))
    assert [event.value_raw for event in events] == [7]
    assert provider.client.calls[0]["topic0"] == APPROVAL_TOPIC
    assert provider.client.calls[0]["topic1"].endswith(WALLET[2:])
    assert asyncio.run(provider.get_latest_block()) == 123


class FakeStore:
    def __init__(self, subs): self.subs = subs
    def all_subscriptions(self): return self.subs


class FakeApprovalProvider:
    network = "ethereum"
    supports_approvals = True
    def __init__(self): self.latest = 100; self.logs = []; self.requests = []
    async def get_latest_block(self): return self.latest
    async def get_approval_events(self, owner, from_block, to_block):
        self.requests.append((from_block, to_block))
        return [event for event in self.logs if from_block <= event.block_number <= to_block]


def test_approval_monitor_baselines_then_reports_once(tmp_path):
    state = UserStateStore(tmp_path / "state.sqlite")
    provider = FakeApprovalProvider()
    received = []
    async def on_approval(sub, event, assessment, risk): received.append((sub.user_id, assessment.state, risk.level))
    async def decimals(network, token): return 6
    subs = [WalletSubscription("1", "ethereum", WALLET), WalletSubscription("2", "ethereum", WALLET), WalletSubscription("3", "tron", TRON_WALLET)]
    monitor = ApprovalMonitor({"ethereum": provider, "tron": TronProvider.__new__(TronProvider)}, FakeStore(subs), state, on_approval, decimals_lookup=decimals)
    assert monitor.supported_networks() == ["ethereum"]

    assert asyncio.run(monitor.run_once()) == 0  # baseline only
    assert provider.requests == []
    assert state.scan_cursor("ethereum", WALLET, "approval") == 100

    provider.logs = [ApprovalEvent("ethereum", TX, 105, 2, TOKEN, WALLET, SPENDER, 2**256 - 1)]
    provider.latest = 110
    assert asyncio.run(monitor.run_once()) == 1
    assert provider.requests == [(101, 110)]
    assert [item[1] for item in received] == ["UNLIMITED", "UNLIMITED"]
    assert {item[0] for item in received} == {"1", "2"}
    assert state.allowance("ethereum", WALLET, TOKEN, SPENDER) == 2**256 - 1

    provider.latest = 120
    provider.logs.append(ApprovalEvent("ethereum", TX, 105, 2, TOKEN, WALLET, SPENDER, 2**256 - 1))  # duplicate
    assert asyncio.run(monitor.run_once()) == 0
    assert state.scan_cursor("ethereum", WALLET, "approval") == 120
    stored = state.detection(provider.logs[0].event_key, "approval")
    assert stored["level"] == "ORTA" and stored["signals"][0]["name"] == "unlimited_allowance"
    state.close()


def test_approval_monitor_survives_provider_errors(tmp_path):
    state = UserStateStore(tmp_path / "state.sqlite")
    class Broken(FakeApprovalProvider):
        async def get_latest_block(self): raise RuntimeError("down")
    async def on_approval(*args): raise AssertionError("should not be called")
    monitor = ApprovalMonitor({"ethereum": Broken()}, FakeStore([WalletSubscription("1", "ethereum", WALLET)]), state, on_approval)
    assert asyncio.run(monitor.run_once()) == 0
    state.close()


# --- 7c risk score ------------------------------------------------------------

def test_signal_validation():
    with pytest.raises(ValueError): Signal("Bad Name", 10, "x")
    with pytest.raises(ValueError): Signal("ok", 101, "x")
    with pytest.raises(ValueError): Signal("ok", True, "x")
    signal = make_signal("unknown_token", "e")
    assert signal.category == "classification" and signal.weight == WEIGHTS["unknown_token"]


def test_score_bounds_dedupe_and_levels():
    assert compute_risk([]).score == 0 and compute_risk([]).level == "DÜŞÜK"
    many = [make_signal(name, "e") for name in WEIGHTS]
    assert compute_risk(many).score == 100 and compute_risk(many).level == "KRİTİK"
    doubled = [make_signal("known_fraud_token", "a"), make_signal("known_fraud_token", "b")]
    assert compute_risk(doubled).score == WEIGHTS["known_fraud_token"]
    unverified = compute_risk([make_signal("address_lookalike", "e", verified=False)])
    assert unverified.score == WEIGHTS["address_lookalike"] // 2
    assert [level_for(limit) for limit, _ in LEVEL_THRESHOLDS] == ["KRİTİK", "YÜKSEK", "ORTA", "DÜŞÜK"]
    assert level_for(24) == "DÜŞÜK" and level_for(25) == "ORTA" and level_for(49) == "ORTA"
    assert level_for(50) == "YÜKSEK" and level_for(74) == "YÜKSEK" and level_for(75) == "KRİTİK"


def test_unknown_token_alone_is_low_risk_and_missing_data_is_reported():
    unknown = compute_risk(classification_signals(TokenAnalysis("UNKNOWN")))
    assert unknown.level == "DÜŞÜK" and unknown.score == 5
    fraud = compute_risk(classification_signals(TokenAnalysis("KNOWN_FRAUD", "USDT")))
    assert fraud.level == "YÜKSEK"
    assert classification_signals(TokenAnalysis("CANONICAL", "USDT")) == []
    partial = compute_risk([], missing=("classification",))
    assert not partial.complete and partial.missing == ("classification",)


def test_transfer_alert_unchanged_without_risk_and_extended_with_it():
    analysis = TokenAnalysis("UNKNOWN", details="Kayıtlı değil")
    plain = format_transfer_alert(transfer(), analysis).text
    assert "Risk skoru" not in plain and "Adres benzerliği" not in plain
    finding = address_poisoning.evaluate("ethereum", WALLET, LOOKALIKE, Decimal("0.0001"), [KNOWN], event_key="log:1")
    risk = compute_risk([*classification_signals(analysis), *finding.signals])
    rich = format_transfer_alert(transfer(sender=LOOKALIKE), analysis, risk=risk, poisoning=finding).text
    assert f"Risk skoru: <b>{risk.score}/100 · {risk.level}</b>" in rich
    assert "Adres benzerliği (şüphe)" in rich and KNOWN in rich
    assert "(doğrulanmamış)" in rich
    assert "Bilinmeyen token" in rich  # classification text untouched


def test_approval_alert_text_is_informational():
    event = ApprovalEvent("ethereum", TX, 105, 2, TOKEN, WALLET, SPENDER, 2**256 - 1)
    assessment = assess(event, None, 6)
    message = format_approval_alert(event, assessment, compute_risk(assessment.signals), alias="Ana", token_symbol="USDT")
    assert "Sınırsız yetki" in message.text and "sınırsız" in message.text
    assert "yetki iptal etmez" in message.text and "Ana" in message.text
    assert message.reply_markup["inline_keyboard"][0][0]["url"].endswith(TX)
    revoked = assess(ApprovalEvent("ethereum", TX, 106, 1, TOKEN, WALLET, SPENDER, 0), 5, None)
    text = format_approval_alert(ApprovalEvent("ethereum", TX, 106, 1, TOKEN, WALLET, SPENDER, 0), revoked, compute_risk((), missing=("decimals",))).text
    assert "Yetki iptal edildi" in text and "Eksik veri: decimals" in text


def test_stage6_filters_apply_to_stage7_alerts():
    prefs = WalletSettings(suspicious_only=True, min_amount=Decimal("10"), quiet_start="22:00", quiet_end="06:00")
    assert skip_notification("UNKNOWN", Decimal("50"), prefs, NOW) == "suspicious_only"
    assert skip_notification("UNKNOWN", Decimal("50"), prefs, NOW, extra_suspicious=True) is None
    assert skip_notification("UNKNOWN", Decimal("0.0001"), prefs, NOW, extra_suspicious=True) == "min_amount"
    assert skip_notification("", Decimal("0"), prefs, NOW, extra_suspicious=True, apply_min_amount=False) is None
    night = datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc)
    assert skip_notification("", Decimal("0"), prefs, night, extra_suspicious=True, apply_min_amount=False) == "quiet_hours"
    assert skip_notification("KNOWN_FRAUD", Decimal("50"), WalletSettings(), NOW) is None


def test_migration_v2_tables_and_detection_join(tmp_path):
    path = tmp_path / "state.sqlite"
    state = UserStateStore(path)
    record = {"network": "ethereum", "tx_hash": TX, "log_index": 1, "event_key": "log:1", "token_address": TOKEN, "wallet": WALLET}
    token = state.register_alert(user_id="5", network="ethereum", wallet_address=WALLET, classification="UNKNOWN", record=record)
    from audit_log import audit_event_key
    key = audit_event_key(record)
    assert state.save_detection(event_key=key, kind="risk", network="ethereum", wallet_address=WALLET, tx_hash=TX, score=20, level="DÜŞÜK", weights_version="stage7-v1", signals=[{"name": "address_lookalike"}])
    assert not state.save_detection(event_key=key, kind="risk", network="ethereum", wallet_address=WALLET, tx_hash=TX, score=99, level="KRİTİK", weights_version="stage7-v1", signals=[])
    assert state.detection(key, "risk")["score"] == 20
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"address_history", "detections", "allowances", "scan_cursors", "transfer_feedback", "wallet_settings"} <= tables
        assert [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4]
        joined = db.execute(
            "SELECT d.score, f.token FROM detections d JOIN transfer_feedback f ON f.event_key = d.event_key"
        ).fetchone()
    assert joined == (20, token)
    state.save_allowance(network="ethereum", owner=WALLET, token_address=TOKEN, spender=SPENDER, value_raw=2**256 - 1, block_number=5, tx_hash=TX)
    state.save_allowance(network="ethereum", owner=WALLET, token_address=TOKEN, spender=SPENDER, value_raw=1, block_number=3, tx_hash=TX)  # older block ignored
    assert state.allowance("ethereum", WALLET, TOKEN, SPENDER) == 2**256 - 1
    state.close()
