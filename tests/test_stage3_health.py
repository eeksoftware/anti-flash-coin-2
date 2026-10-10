from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import sqlite3

from health import GREEN, YELLOW, RED, HealthState, OperationsStore, health_level, write_heartbeat
from notifications.admin import admin_command_allowed, parse_admin_ids
from notifications.telegram_formatter import format_health_report, format_stats_report
from tools.healthcheck import main as healthcheck_main
import main as app_main
from audit_log import _TransactionRouter


def test_admin_ids_parse_and_unauthorized_log_is_hashed(caplog):
    assert parse_admin_ids(" 12,abc,12,, 34 ") == {"12", "34"}
    assert parse_admin_ids("") == set()
    message = {"from": {"id": 987654321}, "chat": {"id": 987654321, "type": "private"}}
    with caplog.at_level(logging.WARNING):
        assert not admin_command_allowed(message, {"12"})
    assert "987654321" not in caplog.text
    assert "user_hash=" in caplog.text


def test_health_thresholds_and_precedence():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = HealthState(started_at=now, last_cycle_at=now, effective_interval=10)
    assert health_level(state, active_keys=1, now=now, disk_free_ratio=.5) == GREEN
    assert health_level(state, active_keys=1, cooldown_keys=1, now=now, disk_free_ratio=.5) == YELLOW
    assert health_level(state, active_keys=1, now=now+timedelta(seconds=20), disk_free_ratio=.5) == GREEN
    assert health_level(state, active_keys=1, now=now+timedelta(seconds=20.1), disk_free_ratio=.5) == YELLOW
    assert health_level(state, active_keys=1, now=now+timedelta(seconds=300.1), disk_free_ratio=.5) == RED
    assert health_level(state, active_keys=1, now=now, disk_free_ratio=.15) == GREEN
    assert health_level(state, active_keys=1, now=now, disk_free_ratio=.149) == YELLOW
    assert health_level(state, active_keys=1, now=now, disk_free_ratio=.05) == YELLOW
    assert health_level(state, active_keys=1, now=now, disk_free_ratio=.049) == RED
    assert health_level(state, active_keys=0, now=now, disk_free_ratio=.5) == RED


def test_health_report_handles_disk_measurement_error_as_warning(tmp_path, monkeypatch):
    class Key:
        label = "key-1"; disabled = False; exhausted = False; cooldown_until = 0; calls = 0
    class Pool: _keys = [Key()]
    class Client: pool = Pool()
    class Provider: client = Client()
    class Store:
        def all_subscriptions(self): return []
    class Sessions:
        def unexpected_restarts_7d(self): return 0
    def fail(_): raise PermissionError("denied")
    monkeypatch.setattr(app_main.shutil, "disk_usage", fail)
    state = HealthState.now(); state.record_cycle(2)
    report = app_main.build_health_report(state=state, provider=Provider(), store=Store(), operations=Sessions(),
        version="v", commit="c", data_dir=tmp_path, log_dir=tmp_path)
    assert report["icon"] == YELLOW
    assert report["disk_free"] == "N/A"
    assert report["keys"][0]["label"] == "key-1"


def test_operations_store_atomic_counts_rolling_window_and_sessions(tmp_path):
    store = OperationsStore(tmp_path / "ops.sqlite")
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: store.increment("check_usage"), range(80)))
    today, rolling = store.stats()
    assert today[("check_usage", "")] == 80
    assert rolling[("check_usage", "")] == 80
    session, _ = store.start_session("v1")
    next_session, stale = store.start_session("v2")
    assert stale == 1
    assert store.unexpected_restarts_7d() == 1
    store.end_session(next_session)
    store.start_session("v3")
    assert store.unexpected_restarts_7d() == 1


def test_daily_counters_roll_at_utc_midnight_but_last24h_crosses_days(tmp_path):
    store = OperationsStore(tmp_path / "daily.sqlite")
    store.increment("transfer_total", at=datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc))
    store.increment("transfer_total", at=datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc))
    daily, rolling = store.stats(now=datetime(2026, 1, 2, 0, 2, tzinfo=timezone.utc))
    assert daily[("transfer_total", "")] == 1
    assert rolling[("transfer_total", "")] == 2


def test_increment_many_updates_related_metrics_atomically(tmp_path):
    store = OperationsStore(tmp_path / "many.sqlite")
    store.increment_many([("transfer_total", "", 1), ("classification:VARIANT", "", 1), ("suspicious_total", "", 1)])
    daily, _ = store.stats()
    assert daily[("transfer_total", "")] == 1
    assert daily[("classification:VARIANT", "")] == 1
    assert daily[("suspicious_total", "")] == 1


def test_increment_many_once_deduplicates_event_across_retries(tmp_path):
    store = OperationsStore(tmp_path / "once.sqlite")
    metrics = [("transfer_total", "", 1), ("classification:UNKNOWN", "", 1)]
    assert store.increment_many_once("fingerprint", metrics)
    assert not store.increment_many_once("fingerprint", metrics)
    today, _ = store.stats()
    assert today[("transfer_total", "")] == 1


def test_audit_sqlite_migrates_without_overwriting_or_removing_old_file(tmp_path):
    old_logs = tmp_path / "old_logs"; data = tmp_path / "data"
    old_logs.mkdir(); data.mkdir()
    legacy_db = old_logs / "seen_transactions.sqlite3"
    with sqlite3.connect(legacy_db) as db:
        db.execute("CREATE TABLE seen(event_key TEXT PRIMARY KEY, first_seen TEXT NOT NULL)")
        db.execute("INSERT INTO seen VALUES('event','2026-01-01T00:00:00+00:00')")
    router = _TransactionRouter(old_logs, 1, data)
    assert legacy_db.exists()
    with sqlite3.connect(data / "seen_transactions.sqlite3") as db:
        assert db.execute("SELECT event_key FROM seen").fetchone()[0] == "event"
    router.close()


def test_heartbeat_is_atomic_json(tmp_path):
    state = HealthState.now()
    state.record_cycle(2.0)
    write_heartbeat(tmp_path, state, GREEN)
    payload = json.loads((tmp_path / "heartbeat").read_text())
    assert payload["status"] == GREEN
    assert payload["health"]["cycle_count"] == 1
    assert list(tmp_path.iterdir()) == [tmp_path / "heartbeat"]


def test_healthcheck_fresh_stale_critical_and_corrupt_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = HealthState.now()
    write_heartbeat(tmp_path, state, GREEN)
    assert healthcheck_main() == 0
    payload = json.loads((tmp_path / "heartbeat").read_text())
    payload["status"] = RED
    (tmp_path / "heartbeat").write_text(json.dumps(payload))
    assert healthcheck_main() == 1
    payload["status"] = GREEN
    payload["timestamp"] = (datetime.now(timezone.utc)-timedelta(seconds=121)).isoformat()
    (tmp_path / "heartbeat").write_text(json.dumps(payload))
    assert healthcheck_main() == 1
    (tmp_path / "heartbeat").write_text(json.dumps({"status": GREEN, "timestamp": datetime.now(timezone.utc).isoformat()}))
    (tmp_path / "operations.sqlite").write_text("broken")
    assert healthcheck_main() == 1


def test_admin_report_formatters_escape_and_do_not_add_sensitive_fields():
    health = format_health_report({"icon": GREEN, "label": "Normal", "version": "v1", "commit": "abc",
        "last_error": "<private>", "keys": [{"label": "key-1", "state": "ACTIVE", "usage": "2/10"}]})
    assert "&lt;private&gt;" in health.text
    assert "key-1" in health.text
    assert "api_secret" not in health.text
    stats = format_stats_report({"today": {"transfer_total": 2, "classes": [{"name": "UNKNOWN", "count": 2, "percent": "100"}]},
        "last24h": {}, "api": {"keys": [{"label": "key-1", "calls": 2, "limit": 10, "percent": "20"}]}})
    assert "Son 24 saat" in stats.text and "key-1" in stats.text and "N/A" in stats.text
