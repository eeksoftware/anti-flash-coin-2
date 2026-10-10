"""Durable, redacted audit logging for the wallet monitor."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import re
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
SAFE_CLASSIFICATION = "CANONICAL"
CLASSIFICATION_LABELS = {
    "CANONICAL": "Resmî token",
    "VARIANT": "Token varyantı",
    "KNOWN_FRAUD": "Bilinen sahte token",
    "UNKNOWN": "Bilinmeyen token",
}
_SENSITIVE_NAME = re.compile(r"TOKEN|API.?KEY|SECRET|PASSWORD|PASSWD|CHAT.?ID|USER.?ID", re.I)
_API_KEY_QUERY = re.compile(r"(?i)(\b(?:apikey|api_key)=)[^&#\s\"']+")
_TELEGRAM_URL = re.compile(r"(?i)(https?://api\.telegram\.org/)bot[^/\s?\"']+")
_TELEGRAM_TOKEN = re.compile(r"(?i)\bbot\d+:[a-z0-9_-]{12,}\b")
_BIDI_CONTROLS = {
    "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
    "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
}

_runtime_lock = threading.RLock()
_metrics_lock = threading.Lock()
_run_id = uuid.uuid4().hex[:8]
_observed_transactions = 0
_error_count = 0
_runtime: _LoggingRuntime | None = None


def _stderr_warning(message: str) -> None:
    try:
        sys.stderr.write(f"Uyarı: {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _clean_controls(value: str, *, keep_newlines: bool = False) -> str:
    if keep_newlines:
        return "".join(
            char for char in value
            if (char in "\n\r\t") or (unicodedata.category(char) not in {"Cc", "Cs"} and char not in _BIDI_CONTROLS)
        )
    clean: list[str] = []
    for char in value:
        if char in "\n\r\t":
            clean.append(" ")
        elif unicodedata.category(char) not in {"Cc", "Cs"} and char not in _BIDI_CONTROLS:
            clean.append(char)
    return " ".join("".join(clean).split())


def _dotenv_secret_values(env_file: Path) -> list[str]:
    values: list[str] = []
    try:
        from dotenv import dotenv_values

        for name, value in dotenv_values(env_file).items():
            if _SENSITIVE_NAME.search(name or "") and isinstance(value, str) and len(value) >= 4:
                values.append(value)
    except Exception:
        # Redaction still uses environment values and common credential patterns.
        pass
    return values


class SecretRedactionFilter(logging.Filter):
    """Replace credentials in messages, formatting arguments, and tracebacks."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        candidates = list(secrets or [])
        candidates.extend(
            value for name, value in os.environ.items()
            if _SENSITIVE_NAME.search(name) and len(value) >= 4
        )
        candidates.extend(_dotenv_secret_values(PROJECT_ROOT / ".env"))
        self._secrets = sorted(set(candidates), key=len, reverse=True)

    def redact(self, value: str) -> str:
        text = value
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = _API_KEY_QUERY.sub(r"\1[REDACTED]", text)
        text = _TELEGRAM_URL.sub(r"\1bot[REDACTED]", text)
        text = _TELEGRAM_TOKEN.sub("bot[REDACTED]", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        global _error_count
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        if record.exc_info:
            try:
                traceback_text = logging.Formatter().formatException(record.exc_info)
            except Exception:
                traceback_text = "[exception traceback unavailable]"
            message = f"{message} | {traceback_text}"
        if record.stack_info:
            message = f"{message} | {record.stack_info}"
        record.msg = _clean_controls(self.redact(message))
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        if record.levelno >= logging.WARNING:
            with _metrics_lock:
                _error_count += 1
        return True


class _LoggerRoute(logging.Filter):
    def __init__(self, names: tuple[str, ...], *, include: bool) -> None:
        super().__init__()
        self.names = names
        self.include = include

    def filter(self, record: logging.LogRecord) -> bool:
        matches = any(record.name == name or record.name.startswith(name + ".") for name in self.names)
        return matches if self.include else not matches


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime


class _ResilientTimedRotatingHandler(logging.handlers.TimedRotatingFileHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.write_failed = False
        super().__init__(*args, **kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        self.write_failed = False
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self.write_failed = True
        _stderr_warning("Log dosyasına yazılamadı; bot çalışmaya devam ediyor.")


class _TransactionRouter(logging.Handler):
    """Deduplicate transfer records in SQLite, then fan them out to JSONL logs."""

    def __init__(self, log_dir: Path, retention_days: int) -> None:
        super().__init__(logging.INFO)
        self.db_path = log_dir / "seen_transactions.sqlite3"
        self.transactions = _make_json_handler(log_dir / "transactions.log", retention_days)
        self.detections = _make_json_handler(log_dir / "detections.log", retention_days)
        self.connection: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self.connection is None:
            connection = sqlite3.connect(self.db_path, timeout=2.0)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS seen (event_key TEXT PRIMARY KEY, first_seen TEXT NOT NULL)"
            )
            connection.commit()
            self.connection = connection
        return self.connection

    @staticmethod
    def _event_key(record: dict[str, Any]) -> str:
        event_part = record.get("event_key")
        if not event_part:
            event_part = f"log:{record.get('log_index')}:{record.get('token_address', '')}"
        key = [
            str(record.get("network", "")).lower(),
            str(record.get("tx_hash", "")).lower(),
            str(event_part).lower(),
            str(record.get("wallet", "")).lower(),
        ]
        return json.dumps(key, ensure_ascii=True, separators=(",", ":"))

    def _merge_manual_evaluation(self, event_key: str, incoming: dict[str, Any]) -> None:
        if not incoming.get("expected"):
            return
        files = [
            (self.transactions.baseFilename, self.transactions),
            (self.detections.baseFilename, self.detections),
        ]
        for base_name, handler in files:
            base = Path(base_name)
            for path in base.parent.glob(base.name + "*"):
                if not path.is_file():
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                changed = False
                for index, line in enumerate(lines):
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(existing, dict) and self._event_key(existing) == event_key:
                        existing["expected"] = incoming["expected"]
                        existing["expected_source"] = "manual"
                        if incoming.get("note"):
                            existing["note"] = incoming["note"]
                        lines[index] = json.dumps(existing, ensure_ascii=False, separators=(",", ":"))
                        changed = True
                if changed:
                    temporary_path: str | None = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="w",
                            encoding="utf-8",
                            dir=base.parent,
                            prefix="audit-update-",
                            delete=False,
                        ) as output:
                            temporary_path = output.name
                            output.write("\n".join(lines) + "\n")
                        if path == base and handler.stream is not None:
                            handler.stream.flush()
                            handler.stream.close()
                            handler.stream = None
                        os.replace(temporary_path, path)
                        if path == base:
                            handler.stream = handler._open()
                    finally:
                        if temporary_path and os.path.exists(temporary_path):
                            os.unlink(temporary_path)

    def emit(self, log_record: logging.LogRecord) -> None:
        try:
            data = json.loads(log_record.getMessage())
            if not isinstance(data, dict):
                return
            connection = self._connect()
            key = self._event_key(data)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO seen(event_key, first_seen) VALUES (?, ?)",
                (key, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
            if cursor.rowcount == 0:
                if data.get("source") == "manual" and data.get("expected"):
                    self._merge_manual_evaluation(key, data)
                return

            line = logging.LogRecord(
                "audit.transactions", logging.INFO, __file__, 0,
                json.dumps(data, ensure_ascii=False, separators=(",", ":")), (), None,
            )
            self.transactions.emit(line)
            if self.transactions.write_failed:
                connection.execute("DELETE FROM seen WHERE event_key = ?", (key,))
                connection.commit()
                return
            if data.get("classification") != SAFE_CLASSIFICATION:
                self.detections.emit(line)
            if data.get("source") == "monitor":
                global _observed_transactions
                with _metrics_lock:
                    _observed_transactions += 1
        except Exception:
            _stderr_warning("Audit kaydı yazılamadı; bot çalışmaya devam ediyor.")

    def close(self) -> None:
        for handler in (self.transactions, self.detections):
            try:
                handler.close()
            except Exception:
                pass
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
        super().close()


def _make_json_handler(path: Path, retention_days: int) -> _ResilientTimedRotatingHandler:
    handler = _ResilientTimedRotatingHandler(
        path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        delay=True,
        utc=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


class _FlushableQueueListener(logging.handlers.QueueListener):
    def _monitor(self) -> None:
        while True:
            record = self.queue.get()
            try:
                if record is self._sentinel:
                    return
                self.handle(record)
            finally:
                self.queue.task_done()


@dataclass
class _LoggingRuntime:
    log_dir: Path
    queue: queue.Queue[logging.LogRecord]
    queue_handler: logging.handlers.QueueHandler
    listener: _FlushableQueueListener
    handlers: list[logging.Handler]


def configure_logging(
    log_dir: str | Path | None = None,
    level: str | int | None = None,
    retention_days: int | None = None,
    *,
    secrets: list[str] | None = None,
    force: bool = False,
) -> str:
    """Start the asynchronous redacted file logger and return this run's ID."""
    global _runtime, _run_id
    with _runtime_lock:
        if _runtime is not None and not force:
            return _run_id
        if _runtime is not None:
            shutdown_logging()

        raw_dir = Path(log_dir or os.getenv("LOG_DIR", "./logs"))
        resolved_dir = raw_dir if raw_dir.is_absolute() else PROJECT_ROOT / raw_dir
        try:
            resolved_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _stderr_warning("Log klasörü oluşturulamadı; dosya logları devre dışı.")
            return _run_id

        retention = retention_days
        if retention is None:
            try:
                retention = int(os.getenv("LOG_RETENTION_DAYS", "90"))
            except ValueError:
                retention = 90
        retention = max(1, retention)
        configured_level = level if level is not None else os.getenv("LOG_LEVEL", "INFO")
        if isinstance(configured_level, int):
            log_level = configured_level
        else:
            level_name = str(configured_level).upper()
            log_level = logging._nameToLevel.get(level_name, logging.INFO)

        _run_id = uuid.uuid4().hex[:8]
        queue_instance: queue.Queue[logging.LogRecord] = queue.Queue()
        queue_handler = logging.handlers.QueueHandler(queue_instance)
        queue_handler.setLevel(logging.DEBUG)
        queue_handler.addFilter(SecretRedactionFilter(secrets))

        handlers: list[logging.Handler] = []
        try:
            app_handler = _make_json_handler(resolved_dir / "app.log", retention)
            app_handler.setFormatter(_UtcFormatter(
                "%(asctime)s UTC | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            app_handler.addFilter(_LoggerRoute(("audit.transactions", "audit.tests"), include=False))
            handlers.append(app_handler)
            transaction_router = _TransactionRouter(resolved_dir, retention)
            transaction_router.addFilter(_LoggerRoute(("audit.transactions",), include=True))
            handlers.append(transaction_router)
            test_handler = _make_json_handler(resolved_dir / "tests.log", retention)
            test_handler.addFilter(_LoggerRoute(("audit.tests",), include=True))
            handlers.append(test_handler)

            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(_UtcFormatter(
                "%(asctime)s UTC | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            console_handler.addFilter(
                _LoggerRoute(("audit.transactions", "audit.tests"), include=False)
            )
            handlers.append(console_handler)
        except OSError:
            _stderr_warning("Log dosyaları açılamadı; bot dosya logları olmadan çalışacak.")
            for handler in handlers:
                handler.close()
            root_logger = logging.getLogger()
            root_logger.handlers.clear()
            root_logger.setLevel(log_level)
            fallback = logging.StreamHandler()
            fallback.addFilter(SecretRedactionFilter(secrets))
            fallback.setFormatter(_UtcFormatter(
                "%(asctime)s UTC | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            root_logger.addHandler(fallback)
            return _run_id

        listener = _FlushableQueueListener(queue_instance, *handlers, respect_handler_level=True)
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.setLevel(log_level)
        for name in ("audit.transactions", "audit.tests"):
            logging.getLogger(name).setLevel(logging.INFO)
        root_logger.addHandler(queue_handler)
        listener.start()
        _runtime = _LoggingRuntime(resolved_dir, queue_instance, queue_handler, listener, handlers)
        return _run_id


def get_run_id() -> str:
    return _run_id


def flush_logging() -> None:
    runtime = _runtime
    if runtime is None:
        return
    try:
        runtime.queue.join()
    except Exception:
        _stderr_warning("Log kuyruğu boşaltılamadı.")


def shutdown_logging() -> None:
    global _runtime
    with _runtime_lock:
        runtime = _runtime
        if runtime is None:
            return
        try:
            runtime.listener.stop()
        except Exception:
            _stderr_warning("Log yazıcısı düzgün kapatılamadı.")
        root_logger = logging.getLogger()
        if runtime.queue_handler in root_logger.handlers:
            root_logger.removeHandler(runtime.queue_handler)
        for handler in runtime.handlers:
            try:
                handler.close()
            except Exception:
                pass
        runtime.queue_handler.close()
        _runtime = None


def take_interval_counts() -> tuple[int, int]:
    global _observed_transactions, _error_count
    with _metrics_lock:
        observed = _observed_transactions
        errors = _error_count
        _observed_transactions = 0
        _error_count = 0
    return observed, errors


def _safe_text(value: Any, limit: int | None = None) -> str:
    text = _clean_controls(str(value), keep_newlines=True)
    if limit is not None:
        text = text[:limit]
    return text


def _timestamp(value: Any = None) -> str:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            moment = datetime.now(timezone.utc)
    else:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _transaction_payload(record: dict[str, Any]) -> dict[str, Any]:
    classification = _safe_text(record.get("classification", "UNKNOWN"), 40)
    signals = record.get("signals")
    if not isinstance(signals, list):
        signals = []
    payload: dict[str, Any] = {
        "ts": _timestamp(record.get("ts")),
        "run_id": _safe_text(record.get("run_id", get_run_id()), 32),
        "source": _safe_text(record.get("source", "monitor"), 16),
        "network": _safe_text(record.get("network", ""), 32),
        "tx_hash": _safe_text(record.get("tx_hash", ""), 160),
        "log_index": record.get("log_index"),
        "event_key": (
            _safe_text(record["event_key"], 256)
            if record.get("event_key") is not None
            else ""
        ),
        "block_number": record.get("block_number"),
        "block_time": _timestamp(record.get("block_time")) if record.get("block_time") else None,
        "direction": _safe_text(record.get("direction", ""), 16),
        "wallet": _safe_text(record.get("wallet", ""), 160),
        "sender": _safe_text(record.get("sender", ""), 160),
        "receiver": _safe_text(record.get("receiver", ""), 160),
        "token_address": _safe_text(record.get("token_address", ""), 160),
        "token_symbol": _safe_text(record.get("token_symbol", ""), 20),
        "amount": _safe_text(record.get("amount", ""), 160),
        "classification": classification,
        "classification_label": _safe_text(
            record.get("classification_label", CLASSIFICATION_LABELS.get(classification, "Bilinmeyen")),
            100,
        ),
        "signals": [_safe_text(value, 200) for value in signals],
        "notified": record.get("notified") if isinstance(record.get("notified"), bool) else None,
        "telegram_message_id": record.get("telegram_message_id"),
        "notify_error": _safe_text(record["notify_error"], 500) if record.get("notify_error") else None,
        "detection_latency_s": record.get("detection_latency_s"),
        "processing_ms": record.get("processing_ms"),
        "expected": _safe_text(record["expected"], 16) if record.get("expected") else None,
        "note": _safe_text(record["note"], 500) if record.get("note") else None,
        "error": _safe_text(record["error"], 500) if record.get("error") else None,
    }
    return payload


def log_transaction(record: dict[str, Any]) -> bool:
    """Queue one JSONL transaction record; disk and SQLite work runs off-loop."""
    try:
        payload = _transaction_payload(record)
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        logging.getLogger("audit.transactions").info(message)
        return True
    except Exception:
        _stderr_warning("Audit kaydı kuyruğa alınamadı; bot çalışmaya devam ediyor.")
        return False


def log_test_event(record: dict[str, Any]) -> bool:
    try:
        payload = {key: _safe_text(value, 2000) if isinstance(value, str) else value for key, value in record.items()}
        payload.setdefault("ts", _timestamp())
        payload.setdefault("run_id", get_run_id())
        logging.getLogger("audit.tests").info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return True
    except Exception:
        _stderr_warning("Test kaydı kuyruğa alınamadı; bot çalışmaya devam ediyor.")
        return False
