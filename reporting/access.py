"""Salt-okunur rapor sorguları ve CSV üretimi.

Transfer satırları report_transfers tablosundan okunur. Risk, approval ve
geri bildirim mevcut detections ve transfer_feedback tablolarına bağlanır.
expected alanı SQLite'ta tutulmaz; log dosyası taranmaz. Günlük sayaçlar
operations.sqlite içindeki stats_daily tablosundan okunur ve kişisel rapora
karıştırılmaz.

Tarih aralığı yarı açıktır: başlangıç dahil, bitiş hariç. Anlar UTC'dir.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


logger = logging.getLogger(__name__)

CSV_COLUMNS = (
    "time_utc",
    "network",
    "tx_hash",
    "direction",
    "token_name",
    "token_symbol",
    "amount",
    "classification",
    "detection_kind",
    "risk_score",
    "risk_level",
    "feedback",
    "notified",
)
CLASSIFICATIONS = frozenset({"CANONICAL", "VARIANT", "KNOWN_FRAUD", "UNKNOWN", "ERROR"})
DETECTION_FILTERS = frozenset({"transfer", "approval", "poisoning", "all"})
RANGE_PRESETS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
MAX_RANGE = timedelta(days=366)
MAX_EXPORT_ROWS = 20_000
MAX_EXPORT_BYTES = 8_000_000
BATCH_SIZE = 500
_NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?$")
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class ReportError(Exception):
    """User-facing reporting error. The message must not contain SQL or paths."""


class InvalidRange(ReportError):
    pass


class UnauthorizedWallet(ReportError):
    pass


class ExportTooLarge(ReportError):
    pass


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%d} {self.end:%Y-%m-%d}"


@dataclass(frozen=True)
class ExportRequest:
    period: TimeRange
    network: str | None = None
    classification: str | None = None
    detection: str | None = None
    wallet: tuple[str, str] | None = None


@dataclass(frozen=True)
class ReportRow:
    time_utc: str
    network: str
    tx_hash: str
    direction: str
    token_name: str
    token_symbol: str
    amount: str
    classification: str
    detection_kind: str
    risk_score: str
    risk_level: str
    feedback: str
    notified: str

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.time_utc, self.network, self.tx_hash, self.direction,
            self.token_name, self.token_symbol, self.amount, self.classification,
            self.detection_kind, self.risk_score, self.risk_level, self.feedback,
            self.notified,
        )


def parse_export_args(parts: list[str], *, now: datetime | None = None) -> ExportRequest:
    """Parse /export arguments. parts excludes the command itself."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tokens = [item.strip() for item in parts if item.strip()]
    period: TimeRange | None = None
    network = classification = detection = None
    wallet: tuple[str, str] | None = None
    dates: list[datetime] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered in RANGE_PRESETS and period is None and not dates:
            period = TimeRange(moment - RANGE_PRESETS[lowered], moment)
        elif token.upper() in CLASSIFICATIONS:
            classification = token.upper()
        elif lowered in DETECTION_FILTERS:
            detection = lowered
        elif _DATE.fullmatch(token):
            dates.append(_utc_midnight(token))
        else:
            maybe_wallet = _parse_wallet_token(token, network)
            if maybe_wallet is not None:
                network, address = maybe_wallet
                wallet = (network, address)
            elif network is None and _known_network(lowered):
                network = lowered
            else:
                raise InvalidRange("Dışa aktarma parametresi anlaşılamadı.")
        index += 1
    if period is None and len(dates) == 2:
        period = TimeRange(dates[0], dates[1])
    elif period is None and not dates:
        raise InvalidRange("Zaman aralığı gerekli.")
    elif period is None:
        raise InvalidRange("Özel aralık başlangıç ve bitiş tarihi ister.")
    if period.start >= period.end:
        raise InvalidRange("Başlangıç, bitişten önce olmalı. Bitiş günü dahil değildir.")
    if period.end - period.start > MAX_RANGE:
        raise InvalidRange("Tarih aralığı en fazla 366 gün olabilir.")
    return ExportRequest(period, network, classification, detection, wallet)


def csv_cell(value: str, *, numeric: bool = False) -> str:
    """Keep real numbers intact and stop spreadsheet formulas in text cells."""
    text = "" if value is None else str(value)
    if numeric and _NUMERIC.fullmatch(text):
        return text
    if text.startswith(_FORMULA_PREFIX):
        return "'" + text
    return text


def export_filename(period: TimeRange) -> str:
    return f"aft-export-{period.start:%Y%m%d}-{period.end:%Y%m%d}.csv"


def write_csv(rows, directory: Path, *, max_rows: int = MAX_EXPORT_ROWS, max_bytes: int = MAX_EXPORT_BYTES) -> tuple[Path, int]:
    """Write UTF-8 CSV to a private temp file. Removes the file if the write fails."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="aft-export-", suffix=".csv", dir=directory)
    path = Path(name)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            for row in rows:
                count += 1
                if count > max_rows:
                    raise ExportTooLarge("Dışa aktarma çok büyük. Daha kısa bir aralık seç.")
                writer.writerow([
                    csv_cell(row.time_utc),
                    csv_cell(row.network),
                    csv_cell(row.tx_hash),
                    csv_cell(row.direction),
                    csv_cell(row.token_name),
                    csv_cell(row.token_symbol),
                    csv_cell(row.amount, numeric=True),
                    csv_cell(row.classification),
                    csv_cell(row.detection_kind),
                    csv_cell(row.risk_score, numeric=True),
                    csv_cell(row.risk_level),
                    csv_cell(row.feedback),
                    csv_cell(row.notified),
                ])
                handle.flush()
                if handle.tell() > max_bytes:
                    raise ExportTooLarge("Dışa aktarma çok büyük. Daha kısa bir aralık seç.")
        return path, count
    except Exception:
        path.unlink(missing_ok=True)
        raise


class ReportReader:
    def __init__(self, state_path: Path, operations_path: Path | None = None) -> None:
        self.state_path = Path(state_path)
        self.operations_path = Path(operations_path) if operations_path else None

    def iter_rows(
        self,
        *,
        user_id: str,
        allowed: list[tuple[str, str]],
        period: TimeRange,
        network: str | None = None,
        classification: str | None = None,
        detection: str | None = None,
        wallet: tuple[str, str] | None = None,
        batch_size: int = BATCH_SIZE,
    ):
        wallets = _authorized(allowed, wallet, network)
        if not wallets:
            return
        start, end = _iso(period.start), _iso(period.end)
        connection = self._connect(self.state_path)
        if connection is None:
            return
        try:
            tables = _table_names(connection)
            if "report_transfers" in tables:
                yield from self._transfer_rows(
                    connection, user_id, wallets, start, end, classification, detection, batch_size, tables,
                )
            if "detections" in tables and detection != "transfer":
                yield from self._orphan_detection_rows(
                    connection, user_id, wallets, start, end, classification, detection, batch_size, tables,
                )
        except sqlite3.Error:
            logger.warning("Rapor sorgusu başarısız.")
            raise ReportError("Rapor oluşturulamadı. İzleme sürüyor.") from None
        finally:
            connection.close()

    def feedback_counts(self, user_id: str, wallets: list[tuple[str, str]], period: TimeRange) -> dict[str, int]:
        connection = self._connect(self.state_path)
        if connection is None or not wallets:
            return {}
        try:
            if "transfer_feedback" not in _table_names(connection):
                return {}
            clause, params = _wallet_clause(wallets)
            rows = connection.execute(
                f"""
                SELECT feedback, COUNT(*)
                FROM transfer_feedback
                WHERE user_id=? AND feedback IS NOT NULL AND feedback_at>=? AND feedback_at<? AND {clause}
                GROUP BY feedback
                """,
                (str(user_id), _iso(period.start), _iso(period.end), *params),
            )
            return {str(kind): int(count) for kind, count in rows if kind}
        except sqlite3.Error:
            logger.warning("Geri bildirim sayımı başarısız.")
            return {}
        finally:
            connection.close()

    def transfer_breakdown(self, wallets: list[tuple[str, str]], period: TimeRange) -> list[tuple[str, str, int, int]]:
        """Return (network, classification, total, notified) without loading transfer bodies."""
        connection = self._connect(self.state_path)
        if connection is None or not wallets:
            return []
        try:
            if "report_transfers" not in _table_names(connection):
                return []
            clause, params = _wallet_clause(wallets)
            rows = connection.execute(
                f"""
                SELECT network, classification, COUNT(*),
                       SUM(CASE WHEN notified=1 THEN 1 ELSE 0 END)
                FROM report_transfers
                WHERE block_time>=? AND block_time<? AND {clause}
                GROUP BY network, classification
                """,
                (_iso(period.start), _iso(period.end), *params),
            )
            return [(str(network), str(classification), int(total), int(notified or 0)) for network, classification, total, notified in rows]
        except sqlite3.Error:
            logger.warning("Transfer sayımı başarısız.")
            return []
        finally:
            connection.close()

    def has_transfers_before(self, wallets: list[tuple[str, str]], moment: datetime) -> bool:
        connection = self._connect(self.state_path)
        if connection is None or not wallets:
            return False
        try:
            if "report_transfers" not in _table_names(connection):
                return False
            clause, params = _wallet_clause(wallets)
            row = connection.execute(
                f"SELECT 1 FROM report_transfers WHERE block_time<? AND {clause} LIMIT 1",
                (_iso(moment), *params),
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            logger.warning("Transfer geçmişi okunamadı.")
            return False
        finally:
            connection.close()

    def detection_rows(self, wallets: list[tuple[str, str]], period: TimeRange):
        connection = self._connect(self.state_path)
        if connection is None or not wallets:
            return
        try:
            tables = _table_names(connection)
            if "detections" not in tables:
                return
            clause, params = _wallet_clause(wallets)
            cursor = connection.execute(
                f"""
                SELECT kind, level, signals, event_key
                FROM detections
                WHERE created_at>=? AND created_at<? AND {clause}
                """,
                (_iso(period.start), _iso(period.end), *params),
            )
            while True:
                batch = cursor.fetchmany(BATCH_SIZE)
                if not batch:
                    break
                yield from batch
        except sqlite3.Error:
            logger.warning("Tespit sayımı başarısız.")
        finally:
            connection.close()

    def feature_tables(self) -> set[str]:
        connection = self._connect(self.state_path)
        if connection is None:
            return set()
        try:
            return _table_names(connection)
        finally:
            connection.close()

    def metric_totals(self, period: TimeRange) -> dict[str, int]:
        """UTC-day totals from stats_daily. These counts are global, not per user."""
        connection = self._connect(self.operations_path) if self.operations_path else None
        if connection is None:
            return {}
        try:
            if "stats_daily" not in _table_names(connection):
                return {}
            rows = connection.execute(
                """
                SELECT metric, SUM(value)
                FROM stats_daily
                WHERE day>=? AND day<?
                GROUP BY metric
                """,
                (period.start.astimezone(timezone.utc).date().isoformat(), period.end.astimezone(timezone.utc).date().isoformat()),
            )
            return {str(metric): int(total or 0) for metric, total in rows}
        except sqlite3.Error:
            logger.warning("Günlük sayaç okunamadı.")
            return {}
        finally:
            connection.close()

    def _transfer_rows(self, connection, user_id, wallets, start, end, classification, detection, batch_size, tables):
        if detection == "approval":
            return
        clause, params = _wallet_clause(wallets, "t.network", "t.wallet_address")
        filters = ""
        extra: list = []
        if classification:
            filters += " AND t.classification=?"
            extra.append(classification)
        risk_join = "LEFT JOIN detections d ON d.event_key=t.event_key AND d.kind='risk'" if "detections" in tables else ""
        feedback_join = "LEFT JOIN transfer_feedback f ON f.event_key=t.event_key AND f.user_id=?" if "transfer_feedback" in tables else ""
        risk_cols = "d.score, d.level, d.signals" if risk_join else "NULL AS score, NULL AS level, NULL AS signals"
        feedback_col = "f.feedback" if feedback_join else "NULL AS feedback"
        leading = [str(user_id)] if feedback_join else []
        cursor = connection.execute(
            f"""
            SELECT t.block_time, t.network, t.tx_hash, t.direction, t.token_name, t.token_symbol,
                   t.amount, t.classification, t.notified, {risk_cols}, {feedback_col}
            FROM report_transfers t
            {risk_join}
            {feedback_join}
            WHERE t.block_time>=? AND t.block_time<? AND {clause}{filters}
            ORDER BY t.block_time, t.event_key
            """,
            (*leading, start, end, *params, *extra),
        )
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            for row in batch:
                kind = detection_kind("risk" if row["signals"] is not None or row["level"] is not None else "transfer", row["signals"])
                if not _detection_matches(kind, detection):
                    continue
                yield _row_from_transfer(row, kind)

    def _orphan_detection_rows(self, connection, user_id, wallets, start, end, classification, detection, batch_size, tables):
        clause, params = _wallet_clause(wallets, "d.network", "d.wallet_address")
        feedback_join = ""
        feedback_params: list = []
        if "transfer_feedback" in tables:
            feedback_join = "LEFT JOIN transfer_feedback f ON f.event_key=d.event_key AND f.user_id=?"
            feedback_params = [str(user_id)]
        present = "report_transfers" in tables
        exclude = "AND d.event_key NOT IN (SELECT event_key FROM report_transfers)" if present else ""
        cursor = connection.execute(
            f"""
            SELECT d.created_at, d.network, d.tx_hash, d.kind, d.score, d.level, d.signals
                   {", f.feedback, f.classification" if feedback_join else ""}
            FROM detections d
            {feedback_join}
            WHERE d.created_at>=? AND d.created_at<? AND {clause} {exclude}
            ORDER BY d.created_at, d.event_key
            """,
            (*feedback_params, start, end, *params),
        )
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            for row in batch:
                kind = detection_kind(row["kind"], row["signals"])
                if not _detection_matches(kind, detection):
                    continue
                row_classification = row["classification"] if feedback_join and "classification" in row.keys() else ""
                if classification and (row_classification or "") != classification:
                    continue
                feedback = row["feedback"] if feedback_join and row["feedback"] else ""
                yield ReportRow(
                    time_utc=str(row["created_at"] or ""),
                    network=str(row["network"] or ""),
                    tx_hash=str(row["tx_hash"] or ""),
                    direction="",
                    token_name="",
                    token_symbol="",
                    amount="",
                    classification=str(row_classification or ""),
                    detection_kind=kind,
                    risk_score="" if row["score"] is None else str(row["score"]),
                    risk_level=str(row["level"] or ""),
                    feedback=str(feedback or ""),
                    notified="",
                )

    def _connect(self, path: Path | None) -> sqlite3.Connection | None:
        if path is None or not path.exists():
            return None
        try:
            connection = sqlite3.connect(path, timeout=5)
        except sqlite3.Error:
            logger.warning("Rapor veritabanı açılamadı.")
            return None
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


def _detection_matches(kind: str, detection: str | None) -> bool:
    if not detection or detection == "all":
        return True
    if detection == "poisoning":
        return kind == "address_poisoning"
    return kind == detection


def detection_kind(kind: str | None, signals: str | None) -> str:
    names = signal_names(signals)
    if "address_lookalike" in names:
        return "address_poisoning"
    if kind == "approval":
        return "approval"
    return "transfer"


def signal_names(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {item["name"] for item in data if isinstance(item, dict) and isinstance(item.get("name"), str)}


def _row_from_transfer(row: sqlite3.Row, kind: str) -> ReportRow:
    notified = row["notified"]
    if notified is None:
        notified_text = ""
    else:
        notified_text = "gönderildi" if int(notified) == 1 else "gönderilmedi"
    amount = str(row["amount"] or "")
    try:
        if amount:
            amount = format(Decimal(amount), "f")
    except InvalidOperation:
        amount = ""
    return ReportRow(
        time_utc=str(row["block_time"] or ""),
        network=str(row["network"] or ""),
        tx_hash=str(row["tx_hash"] or ""),
        direction=str(row["direction"] or ""),
        token_name=str(row["token_name"] or ""),
        token_symbol=str(row["token_symbol"] or ""),
        amount=amount,
        classification=str(row["classification"] or ""),
        detection_kind=kind,
        risk_score="" if row["score"] is None else str(row["score"]),
        risk_level=str(row["level"] or ""),
        feedback=str(row["feedback"] or ""),
        notified=notified_text,
    )


def _authorized(allowed: list[tuple[str, str]], wallet: tuple[str, str] | None, network: str | None) -> list[tuple[str, str]]:
    allowed_set = {(item[0], item[1]) for item in allowed}
    if wallet is not None and wallet not in allowed_set:
        raise UnauthorizedWallet("Bu cüzdanı dışa aktarma yetkin yok.")
    chosen = [wallet] if wallet is not None else list(allowed_set)
    if network is not None:
        if wallet is not None and wallet[0] != network:
            raise UnauthorizedWallet("Bu cüzdanı dışa aktarma yetkin yok.")
        chosen = [item for item in chosen if item[0] == network]
    return chosen


def _wallet_clause(wallets: list[tuple[str, str]], network_sql: str = "network", wallet_sql: str = "wallet_address") -> tuple[str, list]:
    parts = [f"({network_sql}=? AND {wallet_sql}=?)" for _ in wallets]
    params: list = []
    for network, address in wallets:
        params.extend((network, address))
    return "(" + " OR ".join(parts) + ")", params


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _utc_midnight(text: str) -> datetime:
    match = _DATE.fullmatch(text)
    if match is None:
        raise InvalidRange("Tarih YYYY-AA-GG biçiminde olmalı.")
    year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError as exc:
        raise InvalidRange("Tarih geçersiz.") from exc


def _known_network(name: str) -> bool:
    try:
        from networks import get_network_config
        get_network_config(name)
    except ValueError:
        return False
    return True


def _parse_wallet_token(token: str, network: str | None) -> tuple[str, str] | None:
    from networks import get_network_config
    candidates = [network] if network else ["ethereum", "tron"]
    for name in candidates:
        try:
            config = get_network_config(name)
        except ValueError:
            continue
        if config.is_valid_address(token):
            return config.name, config.normalize_address(token)
    return None
