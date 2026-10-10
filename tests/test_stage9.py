"""Stage 9 local dashboard data. No browser and no Streamlit server."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dashboard
from monitor.state_db import UserStateStore
from reporting.dashboard_data import (
    DataPaths,
    count_wallets,
    feedback_view,
    load_overview,
    report_view,
    risk_view,
    transfer_rows,
)


WALLET = "0x" + "ab" * 20
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
START = datetime(2026, 10, 5, tzinfo=timezone.utc)
END = datetime(2026, 10, 12, tzinfo=timezone.utc)


def paths(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "user_state.sqlite", tmp_path / "operations.sqlite", tmp_path / "runtime_state.json")


def store(tmp_path: Path) -> UserStateStore:
    return UserStateStore(tmp_path / "user_state.sqlite")


def seed(saved: UserStateStore, *, when: datetime, classification="UNKNOWN", event="log:1", tx=None, signals=None, level=None, score=None):
    tx = tx or ("0x" + "11" * 32)
    saved.record_report_transfer(
        event_key=event, network="ethereum", wallet_address=WALLET, tx_hash=tx,
        direction="RECEIVE", token_symbol="USDT", token_name="Tether", amount="1.50",
        classification=classification, block_time=when.isoformat(), notified=True,
    )
    if signals is not None:
        saved.save_detection(
            event_key=event, kind="risk", network="ethereum", wallet_address=WALLET, tx_hash=tx,
            score=score, level=level, weights_version="stage7-v1", signals=signals,
        )
        saved._connection.execute("UPDATE detections SET created_at=? WHERE event_key=?", (when.isoformat(), event))
        saved._connection.commit()


def test_empty_and_missing_tables_do_not_crash(tmp_path):
    missing = paths(tmp_path / "yok")
    overview = load_overview(missing, now=NOW)
    assert overview.transfer_count is None and overview.wallet_count is None
    assert overview.by_risk_level is None
    assert any("bulunamadı" in note for note in overview.notes)
    rows, truncated = transfer_rows(missing, start=START, end=END)
    assert rows == [] and truncated is False
    assert risk_view(missing, start=START, end=END)["available"] is False
    assert feedback_view(missing, start=START, end=END)["available"] is False
    report = report_view(missing, start=START, end=END)
    assert report["daily_transfers"] == [] and report["global_metrics"] == []
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    bare = DataPaths(empty, tmp_path / "no-ops.sqlite", tmp_path / "no-wallets.json")
    assert load_overview(bare, now=NOW).transfer_count is None
    assert risk_view(bare, start=START, end=END)["poisoning"] == []
    assert risk_view(bare, start=START, end=END)["approvals"] == []


def test_filters_are_half_open_and_match_counts(tmp_path):
    saved = store(tmp_path)
    seed(saved, when=START, classification="UNKNOWN", event="in", tx="0x" + "aa" * 32, signals=[{"name": "unknown_token"}], level="DÜŞÜK", score=5)
    seed(saved, when=START + timedelta(days=2), classification="KNOWN_FRAUD", event="fraud", tx="0x" + "bb" * 32, signals=[{"name": "address_lookalike", "evidence": "benzer"}], level="YÜKSEK", score=60)
    seed(saved, when=END, classification="VARIANT", event="out", tx="0x" + "cc" * 32)
    data = paths(tmp_path)
    rows, _truncated = transfer_rows(data, start=START, end=END)
    assert [row["classification"] for row in rows] == ["KNOWN_FRAUD", "UNKNOWN"]
    fraud, _truncated = transfer_rows(data, start=START, end=END, classification="KNOWN_FRAUD")
    assert [row["tx_hash"] for row in fraud] == ["0x" + "bb" * 32]
    poison, _truncated = transfer_rows(data, start=START, end=END, detection="poisoning")
    assert len(poison) == 1 and poison[0]["detection_kind"] == "address_poisoning"
    high, _truncated = transfer_rows(data, start=START, end=END, risk_level="YÜKSEK")
    assert len(high) == 1 and high[0]["risk_score"] == 60
    other, _truncated = transfer_rows(data, start=START, end=END, network="tron")
    assert other == []
    overview = load_overview(data, now=END)
    assert overview.transfer_count == 3
    assert overview.transfers_24h == 0
    assert overview.transfers_7d == 2
    assert sum(count for _name, count in overview.by_classification) == overview.transfer_count
    saved.close()


def test_feedback_rate_and_risk_sections_use_real_rows(tmp_path):
    saved = store(tmp_path)
    when = START + timedelta(days=1)
    seed(saved, when=when, event="t1")
    for kind in ("CORRECT", "FALSE_POSITIVE", "UNSURE"):
        token = saved.register_alert(
            user_id="9911", network="ethereum", wallet_address=WALLET, classification="UNKNOWN",
            record={"network": "ethereum", "tx_hash": "0x" + "11" * 32, "event_key": f"fb-{kind}", "wallet": WALLET},
        )
        saved.set_feedback(token, "9911", kind)
    saved.save_detection(
        event_key="approval-1", kind="approval", network="ethereum", wallet_address=WALLET,
        tx_hash="0x" + "dd" * 32, score=30, level="ORTA", weights_version="stage7-v1",
        signals=[{"name": "unlimited_allowance", "weight": 30, "verified": True, "evidence": "sınırsız"}],
    )
    saved._connection.execute("UPDATE detections SET created_at=? WHERE event_key='approval-1'", (when.isoformat(),))
    saved._connection.commit()
    data = paths(tmp_path)
    view = feedback_view(data, start=START, end=END)
    assert view["counts"] == {"CORRECT": 1, "FALSE_POSITIVE": 1, "UNSURE": 1}
    assert view["false_alarm"] == "Yeterli etiketli veri yok"
    assert view["labeled"] == 2
    assert all("9911" not in str(row) and WALLET not in str(row) for row in view["by_classification"])
    risk = risk_view(data, start=START, end=END)
    assert risk["available"] is True
    assert len(risk["approvals"]) == 1
    assert risk["poisoning"] == []
    assert any(item["signal"] == "unlimited_allowance" for item in risk["signals"])
    report = report_view(data, start=START, end=END)
    assert sum(item["count"] for item in report["daily_transfers"]) == 1
    assert report["feedback"]["UNSURE"] == 1
    saved.close()


def test_queries_are_read_only_and_hide_wallet(tmp_path):
    saved = store(tmp_path)
    seed(saved, when=START + timedelta(hours=1), classification="CANONICAL")
    data = paths(tmp_path)
    (tmp_path / "runtime_state.json").write_text(
        '{"legacy":[],"users":{"9911":[{"network":"ethereum","wallet_address":"%s"}]}}\n' % WALLET,
        encoding="utf-8",
    )
    before = saved._connection.execute("SELECT COUNT(*) FROM report_transfers").fetchone()[0]
    overview = load_overview(data, now=END)
    rows, _truncated = transfer_rows(data, start=START, end=END)
    assert overview.wallet_count == 1
    assert WALLET not in str(overview.recent_transfers) and "9911" not in str(rows)
    assert saved._connection.execute("SELECT COUNT(*) FROM report_transfers").fetchone()[0] == before
    from reporting.dashboard_data import _connect
    connection = _connect(data.state_path)
    try:
        raised = False
        try:
            connection.execute("DELETE FROM report_transfers")
        except sqlite3.OperationalError:
            raised = True
        assert raised
    finally:
        connection.close()
    assert count_wallets(tmp_path / "missing.json") is None
    saved.close()


def test_dashboard_stays_local_and_does_not_start_the_bot():
    source = Path(dashboard.__file__).read_text(encoding="utf-8")
    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")
    assert dashboard.BIND_HOST == "127.0.0.1"
    assert dashboard.BIND_PORT == 1337
    assert "0.0.0.0" not in source and "0.0.0.0" not in config
    assert 'address = "127.0.0.1"' in config and "port = 1337" in config
    assert "import main" not in source and "main.py" not in source
    assert "streamlit run" not in source
