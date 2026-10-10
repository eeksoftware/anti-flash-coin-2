from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

import pytest

from audit_log import (
    SecretRedactionFilter,
    configure_logging,
    flush_logging,
    log_transaction,
    shutdown_logging,
)
from scripts.log_summary import (
    read_jsonl_files,
    render_csv,
    summarize_records,
)


WALLET = "0x" + "1" * 40
TX_HASH = "0x" + "a" * 64
TOKEN = "0x" + "b" * 40


@pytest.fixture
def isolated_logs(tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging(log_dir, "DEBUG", 90, secrets=["unit-test-secret"], force=True)
    yield log_dir
    flush_logging()
    shutdown_logging()
    configure_logging(force=True)


def transaction_record(**overrides):
    record = {
        "source": "monitor",
        "network": "ethereum",
        "tx_hash": TX_HASH,
        "log_index": 3,
        "block_number": 123,
        "block_time": "2026-10-10T14:03:11Z",
        "direction": "RECEIVE",
        "wallet": WALLET,
        "sender": "0x" + "2" * 40,
        "receiver": WALLET,
        "token_address": TOKEN,
        "token_symbol": "USDT",
        "amount": "1000.00",
        "classification": "CANONICAL",
        "classification_label": "Resmî token",
        "signals": [],
        "notified": True,
        "telegram_message_id": 55,
        "notify_error": None,
        "detection_latency_s": 11.4,
        "processing_ms": 182.0,
        "expected": None,
        "error": None,
    }
    record.update(overrides)
    return record


def read_lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_app_log_uses_utc_and_sanitizes_multiline_messages(isolated_logs):
    logging.getLogger("test_module").info("first line\nsecond line apikey=message-secret")
    flush_logging()

    rows = read_lines(isolated_logs / "app.log")
    assert len(rows) == 1
    assert rows[0].startswith("20") and " UTC | INFO | test_module | " in rows[0]
    assert "first line second line" in rows[0]
    assert "message-secret" not in rows[0]


def test_transaction_rows_are_valid_json_and_include_required_fields(isolated_logs):
    log_transaction(transaction_record())
    flush_logging()

    rows = read_lines(isolated_logs / "transactions.log")
    assert len(rows) == 1
    record = json.loads(rows[0])
    required = {
        "ts", "run_id", "source", "network", "tx_hash", "block_number",
        "block_time", "direction", "wallet", "sender", "receiver",
        "token_address", "token_symbol", "amount", "classification",
        "classification_label", "signals", "notified", "telegram_message_id",
        "notify_error", "detection_latency_s", "processing_ms", "expected", "error",
    }
    assert required <= record.keys()
    assert record["amount"] == "1000.00"
    assert record["ts"].endswith("Z")


def test_secret_filter_masks_api_key_and_telegram_token_in_message_and_traceback():
    redactor = SecretRedactionFilter(secrets=["private-env-value"])
    try:
        raise RuntimeError(
            "request failed https://api.telegram.org/bot123456:AAExampleTokenValue987/sendMessage"
            "?apikey=etherscan-secret private-env-value"
        )
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 10,
            "request %s", ("https://api.etherscan.io/api?apikey=message-secret",),
            sys.exc_info(),
        )
    assert redactor.filter(record)
    rendered = record.getMessage()
    assert "message-secret" not in rendered
    assert "etherscan-secret" not in rendered
    assert "AAExampleTokenValue987" not in rendered
    assert "private-env-value" not in rendered
    assert "[REDACTED]" in rendered


def test_newline_in_token_symbol_cannot_inject_another_json_line(isolated_logs):
    log_transaction(transaction_record(token_symbol="US\nDT"))
    flush_logging()

    rows = read_lines(isolated_logs / "transactions.log")
    assert len(rows) == 1
    assert json.loads(rows[0])["token_symbol"] == "US\nDT"


def test_same_event_is_deduplicated_across_process_logger_restarts(isolated_logs):
    log_transaction(transaction_record())
    flush_logging()
    shutdown_logging()
    configure_logging(isolated_logs, "DEBUG", 90, force=True)
    log_transaction(transaction_record())
    flush_logging()

    assert len(read_lines(isolated_logs / "transactions.log")) == 1


def test_manual_expected_label_enriches_existing_event_without_duplicate(isolated_logs):
    log_transaction(transaction_record())
    log_transaction(transaction_record(source="manual", expected="gercek", note="bilinen gerçek transfer"))
    flush_logging()

    rows = [json.loads(line) for line in read_lines(isolated_logs / "transactions.log")]
    assert len(rows) == 1
    assert rows[0]["source"] == "monitor"
    assert rows[0]["expected"] == "gercek"
    assert rows[0]["note"] == "bilinen gerçek transfer"


def test_only_noncanonical_classifications_enter_detections_log(isolated_logs):
    log_transaction(transaction_record())
    log_transaction(transaction_record(
        log_index=4,
        classification="UNKNOWN",
        classification_label="Bilinmeyen token",
        token_symbol="ODD",
    ))
    flush_logging()

    transactions = [json.loads(line) for line in read_lines(isolated_logs / "transactions.log")]
    detections = [json.loads(line) for line in read_lines(isolated_logs / "detections.log")]
    assert len(transactions) == 2
    assert [item["classification"] for item in detections] == ["UNKNOWN"]


def test_logging_file_setup_failure_does_not_raise_to_bot(tmp_path):
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("block", encoding="utf-8")
    configure_logging(parent_file / "logs", force=True)
    try:
        assert log_transaction(transaction_record()) is True
    finally:
        configure_logging(force=True)


def test_eval_fixture_appends_expected_and_actual_to_tests_log(isolated_logs, eval_logger):
    assert eval_logger(
        tx_hash=TX_HASH,
        expected="gercek",
        actual="CANONICAL",
        note="bilinen resmî kontrat",
    )
    flush_logging()
    rows = [json.loads(line) for line in read_lines(isolated_logs / "tests.log")]
    assert len(rows) == 1
    assert rows[0]["type"] == "evaluation"
    assert rows[0]["tx_hash"] == TX_HASH
    assert rows[0]["expected"] == "gercek"
    assert rows[0]["actual"] == "CANONICAL"


def test_summary_reads_rotated_files_and_exports_csv(tmp_path):
    rotated = tmp_path / "transactions.log.2026-10-09"
    active = tmp_path / "transactions.log"
    rotated.write_text(
        json.dumps({"ts": "2026-10-09T12:00:00Z", "classification": "CANONICAL"}) + "\n",
        encoding="utf-8",
    )
    active.write_text(
        json.dumps({"ts": "2026-10-10T12:00:00Z", "classification": "UNKNOWN"}) + "\n",
        encoding="utf-8",
    )
    start = datetime(2026, 10, 10, tzinfo=timezone.utc)
    records = read_jsonl_files(tmp_path, "transactions.log", start=start)
    summary = summarize_records(records)

    assert summary["total_transactions"] == 1
    assert summary["unknown_alerts"] == 1
    assert "şüpheli_ve_sahte_tespit" in render_csv(summary)


def test_summary_calculates_counts_and_confusion_matrix():
    transactions = [
        {"ts": "2026-10-10T00:00:00Z", "classification": "KNOWN_FRAUD", "expected": "sahte", "notified": True, "detection_latency_s": 1, "processing_ms": 10},
        {"ts": "2026-10-10T01:00:00Z", "classification": "CANONICAL", "expected": "sahte", "notified": False, "detection_latency_s": 3, "processing_ms": 20},
        {"ts": "2026-10-11T00:00:00Z", "classification": "CANONICAL", "expected": "gercek", "notified": True, "detection_latency_s": 5, "processing_ms": 30},
        {"ts": "2026-10-11T01:00:00Z", "classification": "UNKNOWN", "expected": "gercek", "notified": None, "detection_latency_s": 7, "processing_ms": 40, "notify_error": "send failed"},
    ]
    summary = summarize_records(transactions, [transactions[0], transactions[3]])

    assert summary["total_transactions"] == 4
    assert summary["classification_counts"] == {"CANONICAL": 2, "KNOWN_FRAUD": 1, "UNKNOWN": 1}
    assert summary["detections_total"] == 2
    assert summary["known_suspicious_detections"] == 1
    assert summary["unknown_alerts"] == 1
    assert summary["average_detection_latency_s"] == 4
    assert summary["median_detection_latency_s"] == 4
    assert summary["average_processing_ms"] == 25
    assert summary["daily_transactions"] == {"2026-10-10": 2, "2026-10-11": 2}
    assert summary["notification_success_rate"] == pytest.approx(2 / 3)
    assert summary["error_count"] == 1
    assert summary["confusion_matrix"] == {
        "tp": 1, "fp": 1, "tn": 1, "fn": 1, "samples": 4,
        "accuracy": 0.5, "precision": 0.5, "recall": 0.5,
    }
