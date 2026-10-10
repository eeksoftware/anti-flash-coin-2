"""Wallet settings and transfer feedback.

One versioned SQLite migration owns both tables. Watched wallets stay in
runtime_state.json; this database only stores preferences and feedback.

Quiet hours use UTC. The project has no per-user timezone. A range whose
start and end are the same does not mute notifications.

The minimum amount is compared with that transfer's own token amount.
Amounts of different tokens are never added together or converted to USD.
Zero disables the filter.

"Yalnızca şüpheli" uses the existing suspicious counter: VARIANT and
KNOWN_FRAUD. CANONICAL and UNKNOWN stay outside that set. The filter does
not change the stored classification.

Feedback values are CORRECT, FALSE_POSITIVE and UNSURE. They do not replace
the analyzer result. expected stays limited to gercek, sahte and supheli:

- CORRECT maps CANONICAL to gercek, KNOWN_FRAUD to sahte, and VARIANT or
  UNKNOWN to supheli.
- FALSE_POSITIVE maps a non-CANONICAL detection to gercek (false alarm).
- UNSURE does not write a definitive expected label.
- A manual expected label is never overwritten.

Schema version 2 (stage 7) adds address_history, detections, allowances
and scan_cursors. detections.event_key matches transfer_feedback.event_key,
so false-positive feedback can later be joined with the stored signals.
Approval raw values are stored as decimal strings; SQLite integers are
too small for uint256.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from audit_log import apply_feedback_expected, audit_event_key


SCHEMA_VERSION = 2
SUSPICIOUS_CLASSIFICATIONS = frozenset({"VARIANT", "KNOWN_FRAUD"})
FEEDBACK_KINDS = frozenset({"CORRECT", "FALSE_POSITIVE", "UNSURE"})
_FEEDBACK_CODES = {"c": "CORRECT", "f": "FALSE_POSITIVE", "u": "UNSURE"}
_CODE_FOR_KIND = {value: key for key, value in _FEEDBACK_CODES.items()}
_ALIAS_LIMIT = 32
_AMOUNT_LIMIT = Decimal("1e36")
_QUIET_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\s*[-–]\s*([01]\d|2[0-3]):([0-5]\d)$")
_BIDI = {
    "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
    "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
}


class SettingsInputError(ValueError):
    """User-facing validation error for a wallet setting."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalletSettings:
    alias: str = ""
    min_amount: Decimal = Decimal("0")
    suspicious_only: bool = False
    quiet_start: str | None = None
    quiet_end: str | None = None


@dataclass(frozen=True)
class FeedbackRecord:
    token: str
    event_key: str
    user_id: str
    network: str
    wallet_address: str
    classification: str
    feedback: str | None
    previous_feedback: str | None
    feedback_at: str | None


def parse_alias(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if len(text) > _ALIAS_LIMIT:
        raise SettingsInputError(f"Takma ad en fazla {_ALIAS_LIMIT} karakter olabilir.")
    for char in text:
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs"} or char in _BIDI:
            raise SettingsInputError("Takma ad kontrol karakteri içeremez.")
    return text


def parse_min_amount(value: str) -> Decimal:
    text = value.strip().replace(",", ".")
    lowered = text.lower()
    if not text or any(marker in lowered for marker in ("nan", "inf", "infinity")):
        raise SettingsInputError("Tutar sıfır veya daha büyük bir sayı olmalı.")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise SettingsInputError("Tutar sıfır veya daha büyük bir sayı olmalı.") from exc
    if not amount.is_finite() or amount < 0 or amount > _AMOUNT_LIMIT:
        raise SettingsInputError("Tutar sıfır veya daha büyük bir sayı olmalı.")
    if amount.as_tuple().exponent < -36:
        raise SettingsInputError("Tutar en fazla 36 ondalık basamak içerebilir.")
    return amount


def parse_quiet_hours(value: str) -> tuple[str, str] | None:
    text = value.strip()
    if text.casefold() in {"kapat", "yok", "kapali", "kapalı"}:
        return None
    match = _QUIET_RE.fullmatch(text)
    if match is None:
        raise SettingsInputError("Saat aralığı 22:00-08:00 biçiminde olmalı. Kapatmak için kapat yaz.")
    start = f"{match.group(1)}:{match.group(2)}"
    end = f"{match.group(3)}:{match.group(4)}"
    return start, end


def quiet_contains(now: datetime, start: str | None, end: str | None) -> bool:
    """Return whether now falls in a UTC quiet range. Equal bounds mute nothing."""
    if not start or not end or start == end:
        return False
    moment = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    current = moment.hour * 60 + moment.minute
    start_minutes = _minutes(start)
    end_minutes = _minutes(end)
    if start_minutes is None or end_minutes is None or start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= current < end_minutes
    return current >= start_minutes or current < end_minutes


def skip_notification(
    classification: str,
    amount: Decimal,
    prefs: WalletSettings,
    now: datetime,
    *,
    extra_suspicious: bool = False,
    apply_min_amount: bool = True,
) -> str | None:
    """Return a skip reason, or None when the existing notification should be sent.

    amount is this transfer's own token quantity. It is not compared with
    another token and it is not converted to USD. extra_suspicious lets a
    stage 7 finding (lookalike address, high risk) pass the suspicious-only
    filter without changing the classification. apply_min_amount is False
    for approval alerts, which carry no transfer amount.
    """
    if prefs.suspicious_only and classification not in SUSPICIOUS_CLASSIFICATIONS and not extra_suspicious:
        return "suspicious_only"
    if apply_min_amount and prefs.min_amount > 0 and amount < prefs.min_amount:
        return "min_amount"
    if quiet_contains(now, prefs.quiet_start, prefs.quiet_end):
        return "quiet_hours"
    return None


def expected_update(kind: str, classification: str) -> tuple[str, str | None]:
    """Map feedback to an expected-field action without treating UNSURE as a label."""
    if kind == "UNSURE":
        return "clear", None
    if kind == "CORRECT":
        if classification == "CANONICAL":
            return "set", "gercek"
        if classification == "KNOWN_FRAUD":
            return "set", "sahte"
        if classification in {"VARIANT", "UNKNOWN"}:
            return "set", "supheli"
        return "clear", None
    if kind == "FALSE_POSITIVE" and classification not in {"", "CANONICAL"}:
        return "set", "gercek"
    return "clear", None


def parse_feedback_callback(data: str) -> tuple[str, str] | None:
    if not isinstance(data, str) or len(data.encode("utf-8")) > 64 or not data.startswith("fb:"):
        return None
    kind_code, separator, token = data[3:].partition(":")
    kind = _FEEDBACK_CODES.get(kind_code)
    if separator != ":" or kind is None or not re.fullmatch(r"[0-9a-f]{16}", token):
        return None
    return kind, token


def feedback_callback(kind: str, token: str) -> str:
    code = _CODE_FOR_KIND[kind]
    data = f"fb:{code}:{token}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Geri bildirim callback verisi çok uzun.")
    return data


def _minutes(value: str) -> int | None:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migration_v1(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE wallet_settings (
            user_id TEXT NOT NULL,
            network TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            alias TEXT NOT NULL DEFAULT '',
            min_amount TEXT NOT NULL DEFAULT '0',
            suspicious_only INTEGER NOT NULL DEFAULT 0,
            quiet_start TEXT,
            quiet_end TEXT,
            PRIMARY KEY (user_id, network, wallet_address)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE transfer_feedback (
            token TEXT PRIMARY KEY,
            event_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            network TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            classification TEXT NOT NULL,
            feedback TEXT,
            previous_feedback TEXT,
            feedback_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (event_key, user_id)
        )
        """
    )


def _migration_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE address_history (
            network TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            counterparty TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            transfer_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (network, wallet_address, counterparty)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE detections (
            event_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            network TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            score INTEGER,
            level TEXT,
            weights_version TEXT NOT NULL,
            signals TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (event_key, kind)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE allowances (
            network TEXT NOT NULL,
            owner TEXT NOT NULL,
            token_address TEXT NOT NULL,
            spender TEXT NOT NULL,
            value_raw TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            tx_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (network, owner, token_address, spender)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE scan_cursors (
            network TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            kind TEXT NOT NULL,
            position INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (network, wallet_address, kind)
        )
        """
    )


MIGRATIONS: list[tuple[int, object]] = [(1, _migration_v1), (2, _migration_v2)]


def migrate(connection: sqlite3.Connection, steps: list[tuple[int, object]] | None = None) -> None:
    """Apply pending schema versions. A failed version rolls back completely."""
    planned = steps if steps is not None else MIGRATIONS
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    for version, step in planned:
        if version in applied:
            continue
        connection.execute("BEGIN")
        try:
            step(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _utc_now()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


class UserStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        try:
            migrate(self._connection)
        except Exception:
            self._connection.close()
            raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def settings(self, user_id: str, network: str, wallet_address: str) -> WalletSettings:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT alias, min_amount, suspicious_only, quiet_start, quiet_end
                FROM wallet_settings
                WHERE user_id=? AND network=? AND wallet_address=?
                """,
                (str(user_id), network, wallet_address),
            ).fetchone()
        if row is None:
            return WalletSettings()
        try:
            amount = Decimal(row["min_amount"])
            if not amount.is_finite() or amount < 0:
                amount = Decimal("0")
        except InvalidOperation:
            amount = Decimal("0")
        return WalletSettings(
            alias=row["alias"] or "",
            min_amount=amount,
            suspicious_only=bool(row["suspicious_only"]),
            quiet_start=row["quiet_start"],
            quiet_end=row["quiet_end"],
        )

    def save_settings(
        self,
        user_id: str,
        network: str,
        wallet_address: str,
        prefs: WalletSettings,
    ) -> None:
        amount = format(prefs.min_amount, "f")
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO wallet_settings (
                    user_id, network, wallet_address, alias, min_amount,
                    suspicious_only, quiet_start, quiet_end
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, network, wallet_address) DO UPDATE SET
                    alias=excluded.alias,
                    min_amount=excluded.min_amount,
                    suspicious_only=excluded.suspicious_only,
                    quiet_start=excluded.quiet_start,
                    quiet_end=excluded.quiet_end
                """,
                (
                    str(user_id),
                    network,
                    wallet_address,
                    prefs.alias,
                    amount,
                    1 if prefs.suspicious_only else 0,
                    prefs.quiet_start,
                    prefs.quiet_end,
                ),
            )
            self._connection.commit()

    def register_alert(
        self,
        *,
        user_id: str,
        network: str,
        wallet_address: str,
        classification: str,
        record: dict,
    ) -> str:
        event_key = audit_event_key(record)
        with self._lock:
            existing = self._connection.execute(
                "SELECT token FROM transfer_feedback WHERE event_key=? AND user_id=?",
                (event_key, str(user_id)),
            ).fetchone()
            if existing is not None:
                return existing["token"]
            for _ in range(3):
                token = secrets.token_hex(8)
                try:
                    self._connection.execute(
                        """
                        INSERT INTO transfer_feedback (
                            token, event_key, user_id, network, wallet_address,
                            classification, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (token, event_key, str(user_id), network, wallet_address, classification, _utc_now()),
                    )
                    self._connection.commit()
                    return token
                except sqlite3.IntegrityError:
                    self._connection.rollback()
                    existing = self._connection.execute(
                        "SELECT token FROM transfer_feedback WHERE event_key=? AND user_id=?",
                        (event_key, str(user_id)),
                    ).fetchone()
                    if existing is not None:
                        return existing["token"]
            raise sqlite3.IntegrityError("Geri bildirim kaydı oluşturulamadı.")

    def feedback(self, token: str) -> FeedbackRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM transfer_feedback WHERE token=?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        return FeedbackRecord(
            token=row["token"],
            event_key=row["event_key"],
            user_id=row["user_id"],
            network=row["network"],
            wallet_address=row["wallet_address"],
            classification=row["classification"],
            feedback=row["feedback"],
            previous_feedback=row["previous_feedback"],
            feedback_at=row["feedback_at"],
        )

    def set_feedback(self, token: str, user_id: str, kind: str, *, log_dir: Path | None = None) -> str:
        if kind not in FEEDBACK_KINDS:
            return "invalid"
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM transfer_feedback WHERE token=?",
                (token,),
            ).fetchone()
            if row is None:
                return "missing"
            if row["user_id"] != str(user_id):
                return "unauthorized"
            if row["feedback"] == kind:
                return "unchanged"
            previous = row["feedback"]
            self._connection.execute(
                """
                UPDATE transfer_feedback
                SET feedback=?, previous_feedback=?, feedback_at=?
                WHERE token=?
                """,
                (kind, previous, _utc_now(), token),
            )
            self._connection.commit()
            event_key = row["event_key"]
            classification = row["classification"]
        action, expected = expected_update(kind, classification)
        try:
            apply_feedback_expected(event_key, action=action, expected=expected, log_dir=log_dir)
        except Exception:
            logger.warning("Geri bildirim günlük kaydına yazılamadı.")
        return "updated"

    # --- stage 7: address history -------------------------------------

    def counterparties(self, network: str, wallet_address: str) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT counterparty FROM address_history WHERE network=? AND wallet_address=? ORDER BY first_seen",
                (network, wallet_address),
            ).fetchall()
        return [row["counterparty"] for row in rows]

    def record_counterparties(self, network: str, wallet_address: str, counterparties: list[str] | tuple[str, ...], seen_at: datetime | None = None) -> None:
        stamp = (seen_at or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._connection.executemany(
                """
                INSERT INTO address_history (network, wallet_address, counterparty, first_seen, last_seen, transfer_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(network, wallet_address, counterparty) DO UPDATE SET
                    last_seen=MAX(last_seen, excluded.last_seen),
                    transfer_count=transfer_count + 1
                """,
                [(network, wallet_address, item, stamp, stamp) for item in counterparties if item and item != wallet_address],
            )
            self._connection.commit()

    # --- stage 7: detections -------------------------------------------

    def save_detection(self, *, event_key: str, kind: str, network: str, wallet_address: str, tx_hash: str, score: int | None, level: str | None, weights_version: str, signals: list[dict]) -> bool:
        """Store a detection once per (event_key, kind). Returns False when it already existed."""
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO detections (event_key, kind, network, wallet_address, tx_hash, score, level, weights_version, signals, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_key, kind, network, wallet_address, tx_hash, score, level, weights_version, json.dumps(signals, ensure_ascii=False, separators=(",", ":")), _utc_now()),
                )
                self._connection.commit()
            except sqlite3.IntegrityError:
                self._connection.rollback()
                return False
        return True

    def detection(self, event_key: str, kind: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM detections WHERE event_key=? AND kind=?",
                (event_key, kind),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["signals"] = json.loads(data["signals"])
        except (TypeError, json.JSONDecodeError):
            data["signals"] = []
        return data

    # --- stage 7: allowances and scan cursors ---------------------------

    def allowance(self, network: str, owner: str, token_address: str, spender: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_raw FROM allowances WHERE network=? AND owner=? AND token_address=? AND spender=?",
                (network, owner, token_address, spender),
            ).fetchone()
        if row is None:
            return None
        try:
            return int(row["value_raw"])
        except (TypeError, ValueError):
            return None

    def save_allowance(self, *, network: str, owner: str, token_address: str, spender: str, value_raw: int, block_number: int, tx_hash: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO allowances (network, owner, token_address, spender, value_raw, block_number, tx_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(network, owner, token_address, spender) DO UPDATE SET
                    value_raw=excluded.value_raw,
                    block_number=excluded.block_number,
                    tx_hash=excluded.tx_hash,
                    updated_at=excluded.updated_at
                WHERE excluded.block_number >= allowances.block_number
                """,
                (network, owner, token_address, spender, str(value_raw), block_number, tx_hash, _utc_now()),
            )
            self._connection.commit()

    def scan_cursor(self, network: str, wallet_address: str, kind: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT position FROM scan_cursors WHERE network=? AND wallet_address=? AND kind=?",
                (network, wallet_address, kind),
            ).fetchone()
        return None if row is None else int(row["position"])

    def save_scan_cursor(self, network: str, wallet_address: str, kind: str, position: int) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO scan_cursors (network, wallet_address, kind, position, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(network, wallet_address, kind) DO UPDATE SET
                    position=excluded.position, updated_at=excluded.updated_at
                """,
                (network, wallet_address, kind, int(position), _utc_now()),
            )
            self._connection.commit()
