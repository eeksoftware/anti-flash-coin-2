"""Read-only figures for the local dashboard.

Queries use the Stage 6–8 tables already on disk. Missing tables stay
missing: counts are not invented. Wallet addresses and user ids are never
returned. Connections are query-only so the bot can keep writing.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reporting.access import detection_kind, signal_names
from reporting.weekly import false_alarm_text


logger = logging.getLogger(__name__)

ROW_LIMIT = 500
SCAN_LIMIT = 5000
CLASSIFICATIONS = ("CANONICAL", "VARIANT", "KNOWN_FRAUD", "UNKNOWN", "ERROR")
RISK_LEVELS = ("DÜŞÜK", "ORTA", "YÜKSEK", "KRİTİK")
GLOBAL_METRICS = (
    "transfer_total",
    "notifications_sent",
    "check_usage",
    "errors",
    "suspicious_total",
)


@dataclass(frozen=True)
class DataPaths:
    state_path: Path
    operations_path: Path
    wallet_path: Path


@dataclass(frozen=True)
class Overview:
    wallet_count: int | None
    transfer_count: int | None
    transfers_24h: int | None
    transfers_7d: int | None
    by_classification: tuple[tuple[str, int], ...] | None
    by_risk_level: tuple[tuple[str, int], ...] | None
    recent_transfers: tuple[dict, ...]
    recent_detections: tuple[dict, ...]
    notes: tuple[str, ...]


def default_paths(data_dir: Path | None = None) -> DataPaths:
    if data_dir is None:
        from config import settings
        data_dir = Path(settings.data_dir)
        if not data_dir.is_absolute():
            data_dir = Path(__file__).resolve().parents[1] / data_dir
    root = Path(data_dir)
    return DataPaths(root / "user_state.sqlite", root / "operations.sqlite", root / "runtime_state.json")


def load_overview(paths: DataPaths, *, now: datetime | None = None) -> Overview:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    notes: list[str] = []
    wallets = count_wallets(paths.wallet_path)
    if wallets is None:
        notes.append("İzlenen cüzdan kaydı bulunamadı.")
    connection = _connect(paths.state_path)
    transfers = transfers_24h = transfers_7d = None
    by_class = by_risk = None
    recent_transfers: tuple[dict, ...] = ()
    recent_detections: tuple[dict, ...] = ()
    if connection is None:
        notes.append("Kullanıcı veritabanı bulunamadı.")
    else:
        try:
            tables = _tables(connection)
            if "report_transfers" not in tables:
                notes.append("Transfer tablosu yok. Toplam transfer sayısı gösterilmiyor.")
            else:
                transfers = _scalar(connection, "SELECT COUNT(*) FROM report_transfers")
                transfers_24h = _scalar(
                    connection,
                    "SELECT COUNT(*) FROM report_transfers WHERE block_time>=? AND block_time<?",
                    (_iso(moment - timedelta(hours=24)), _iso(moment)),
                )
                transfers_7d = _scalar(
                    connection,
                    "SELECT COUNT(*) FROM report_transfers WHERE block_time>=? AND block_time<?",
                    (_iso(moment - timedelta(days=7)), _iso(moment)),
                )
                by_class = tuple(_grouped(connection, "SELECT classification, COUNT(*) FROM report_transfers GROUP BY classification"))
                recent_transfers = tuple(_rows(connection, """
                    SELECT block_time, network, tx_hash, token_symbol, amount, classification
                    FROM report_transfers ORDER BY block_time DESC LIMIT 8
                """))
            if "detections" not in tables:
                notes.append("Tespit tablosu yok. Risk dağılımı gösterilmiyor.")
            else:
                by_risk = tuple(_grouped(connection, "SELECT level, COUNT(*) FROM detections WHERE kind='risk' AND level IS NOT NULL GROUP BY level"))
                recent_detections = tuple(
                    _public_detection(row) for row in _rows(connection, """
                        SELECT created_at, network, tx_hash, kind, score, level, signals
                        FROM detections ORDER BY created_at DESC LIMIT 8
                    """)
                )
        except sqlite3.Error:
            logger.warning("Panel özeti okunamadı.")
            notes.append("Özet sorgusu başarısız.")
        finally:
            connection.close()
    return Overview(
        wallets, transfers, transfers_24h, transfers_7d, by_class, by_risk,
        recent_transfers, recent_detections, tuple(notes),
    )


def system_summary(data_dir: Path) -> dict:
    """Verifiable host figures for the overview card. No keys, tokens, or addresses."""
    root = data_dir if data_dir.is_absolute() else Path(__file__).resolve().parents[1] / data_dir
    eth_count, tron_count, eth_limit, tron_limit, log_dir = _summary_settings()
    total = eth_count + tron_count
    exhausted = _exhausted_keys(root / "api_usage.sqlite", eth_limit, tron_limit, eth_count, tron_count)
    active = max(0, total - exhausted)
    last_api, last_telegram = _heartbeat_times(root / "heartbeat")
    return {
        "api_keys_active": active,
        "api_keys_total": total,
        "api_keys_ready": total > 0 and active == total,
        "last_api": last_api,
        "last_telegram": last_telegram,
        "log_size": _directory_size(log_dir),
        "disk_free": _disk_free(log_dir if log_dir.exists() else root),
    }


def transfer_rows(
    paths: DataPaths,
    *,
    start: datetime,
    end: datetime,
    network: str | None = None,
    classification: str | None = None,
    detection: str | None = None,
    risk_level: str | None = None,
) -> tuple[list[dict], bool]:
    """Return transfer rows for a half-open UTC window. The flag means the scan cap was hit."""
    connection = _connect(paths.state_path)
    if connection is None:
        return [], False
    try:
        tables = _tables(connection)
        if detection == "approval":
            if "detections" not in tables:
                return [], False
            return _approval_rows(connection, start, end, network, risk_level), False
        if "report_transfers" not in tables:
            return [], False
        feedback = _feedback_labels(connection) if "transfer_feedback" in tables else {}
        risk_sql = "AND d.level=?" if risk_level else ""
        params: list = [_iso(start), _iso(end)]
        network_sql = ""
        if network:
            network_sql = "AND t.network=?"
            params.append(network)
        class_sql = ""
        if classification:
            class_sql = "AND t.classification=?"
            params.append(classification)
        if risk_level:
            params.append(risk_level)
        params.append(SCAN_LIMIT)
        cursor = connection.execute(
            f"""
            SELECT t.block_time, t.network, t.tx_hash, t.token_symbol, t.token_name,
                   t.amount, t.classification, t.event_key, d.score, d.level, d.signals
            FROM report_transfers t
            LEFT JOIN detections d ON d.event_key=t.event_key AND d.kind='risk'
            WHERE t.block_time>=? AND t.block_time<? {network_sql} {class_sql} {risk_sql}
            ORDER BY t.block_time DESC
            LIMIT ?
            """,
            params,
        )
        rows = []
        scanned = 0
        truncated = False
        for item in cursor:
            scanned += 1
            kind = detection_kind("risk" if item["signals"] else "transfer", item["signals"])
            if detection == "poisoning" and kind != "address_poisoning":
                continue
            if detection == "transfer" and kind != "transfer":
                continue
            if detection not in {None, "", "all", "poisoning", "transfer"} and kind != detection:
                continue
            rows.append({
                "time_utc": item["block_time"],
                "network": item["network"],
                "tx_hash": item["tx_hash"],
                "token": _token(item["token_symbol"], item["token_name"]),
                "amount": item["amount"],
                "classification": item["classification"],
                "detection_kind": kind,
                "risk_score": item["score"],
                "risk_level": item["level"] or "",
                "feedback": feedback.get(item["event_key"], ""),
            })
            if len(rows) >= ROW_LIMIT:
                truncated = True
                break
        if scanned >= SCAN_LIMIT and not truncated:
            truncated = True
        return rows, truncated
    except sqlite3.Error:
        logger.warning("Transfer listesi okunamadı.")
        return [], False
    finally:
        connection.close()


def risk_view(paths: DataPaths, *, start: datetime, end: datetime) -> dict:
    connection = _connect(paths.state_path)
    empty = {
        "available": False,
        "by_level": [],
        "by_day": [],
        "poisoning": [],
        "approvals": [],
        "signals": [],
    }
    if connection is None:
        return empty
    try:
        if "detections" not in _tables(connection):
            return empty
        params = (_iso(start), _iso(end))
        by_level = _grouped(connection, """
            SELECT level, COUNT(*) FROM detections
            WHERE kind='risk' AND level IS NOT NULL AND created_at>=? AND created_at<?
            GROUP BY level
        """, params)
        by_day = [
            {"day": day, "level": level, "count": count}
            for day, level, count in connection.execute(
                """
                SELECT substr(created_at, 1, 10), level, COUNT(*)
                FROM detections
                WHERE kind='risk' AND level IS NOT NULL AND created_at>=? AND created_at<?
                GROUP BY substr(created_at, 1, 10), level
                ORDER BY 1
                """,
                params,
            )
        ]
        poisoning = []
        approvals = []
        signals = []
        cursor = connection.execute(
            """
            SELECT created_at, network, tx_hash, kind, score, level, signals
            FROM detections
            WHERE created_at>=? AND created_at<?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*params, SCAN_LIMIT),
        )
        for row in cursor:
            names = signal_names(row["signals"])
            kind = detection_kind(row["kind"], row["signals"])
            parsed = _parse_signals(row["signals"])
            if kind == "address_poisoning" and len(poisoning) < ROW_LIMIT:
                poisoning.append(_public_detection(row) | {"signals": ", ".join(sorted(names))})
            if row["kind"] == "approval" and len(approvals) < ROW_LIMIT:
                approvals.append(_public_detection(row) | {"signals": ", ".join(name for name, *_rest in parsed)})
            for name, weight, verified, evidence in parsed:
                if len(signals) >= ROW_LIMIT:
                    break
                signals.append({
                    "time_utc": row["created_at"],
                    "network": row["network"],
                    "tx_hash": row["tx_hash"],
                    "detection_kind": kind,
                    "signal": name,
                    "weight": weight,
                    "verified": verified,
                    "evidence": evidence,
                })
        return {
            "available": True,
            "by_level": by_level,
            "by_day": by_day,
            "poisoning": poisoning,
            "approvals": approvals,
            "signals": signals,
        }
    except sqlite3.Error:
        logger.warning("Risk görünümü okunamadı.")
        return empty
    finally:
        connection.close()


def feedback_view(paths: DataPaths, *, start: datetime, end: datetime) -> dict:
    unavailable = {"available": False, "counts": {}, "by_classification": [], "false_alarm": None, "labeled": 0}
    connection = _connect(paths.state_path)
    if connection is None:
        return unavailable
    try:
        if "transfer_feedback" not in _tables(connection):
            return unavailable
        params = (_iso(start), _iso(end))
        counts = dict(_grouped(connection, """
            SELECT feedback, COUNT(*) FROM transfer_feedback
            WHERE feedback IS NOT NULL AND feedback_at>=? AND feedback_at<?
            GROUP BY feedback
        """, params))
        by_classification = [
            {"classification": classification, "feedback": feedback, "count": count}
            for classification, feedback, count in connection.execute(
                """
                SELECT classification, feedback, COUNT(*)
                FROM transfer_feedback
                WHERE feedback IS NOT NULL AND feedback_at>=? AND feedback_at<?
                GROUP BY classification, feedback
                ORDER BY classification, feedback
                """,
                params,
            )
        ]
        correct = int(counts.get("CORRECT", 0))
        false_positive = int(counts.get("FALSE_POSITIVE", 0))
        return {
            "available": True,
            "counts": {name: int(counts.get(name, 0)) for name in ("CORRECT", "FALSE_POSITIVE", "UNSURE")},
            "by_classification": by_classification,
            "false_alarm": false_alarm_text(correct, false_positive),
            "labeled": correct + false_positive,
        }
    except sqlite3.Error:
        logger.warning("Geri bildirim görünümü okunamadı.")
        return unavailable
    finally:
        connection.close()


def report_view(paths: DataPaths, *, start: datetime, end: datetime) -> dict:
    state = _connect(paths.state_path)
    daily_transfers: list[dict] = []
    daily_detections: list[dict] = []
    class_counts: list[tuple] = []
    feedback_counts: dict = {}
    weekly: list[dict] = []
    notes: list[str] = []
    if state is None:
        notes.append("Kullanıcı veritabanı bulunamadı.")
    else:
        try:
            tables = _tables(state)
            params = (_iso(start), _iso(end))
            if "report_transfers" in tables:
                daily_transfers = [
                    {"day": day, "count": count}
                    for day, count in state.execute(
                        """
                        SELECT substr(block_time, 1, 10), COUNT(*)
                        FROM report_transfers
                        WHERE block_time>=? AND block_time<?
                        GROUP BY substr(block_time, 1, 10)
                        ORDER BY 1
                        """,
                        params,
                    )
                ]
                class_counts = _grouped(state, """
                    SELECT classification, COUNT(*) FROM report_transfers
                    WHERE block_time>=? AND block_time<?
                    GROUP BY classification
                """, params)
            else:
                notes.append("Transfer tablosu yok.")
            if "detections" in tables:
                daily_detections = [
                    {"day": day, "count": count}
                    for day, count in state.execute(
                        """
                        SELECT substr(created_at, 1, 10), COUNT(*)
                        FROM detections
                        WHERE created_at>=? AND created_at<?
                        GROUP BY substr(created_at, 1, 10)
                        ORDER BY 1
                        """,
                        params,
                    )
                ]
            else:
                notes.append("Tespit tablosu yok.")
            if "transfer_feedback" in tables:
                feedback_counts = dict(_grouped(state, """
                    SELECT feedback, COUNT(*) FROM transfer_feedback
                    WHERE feedback IS NOT NULL AND feedback_at>=? AND feedback_at<?
                    GROUP BY feedback
                """, params))
            else:
                notes.append("Geri bildirim tablosu yok.")
            if "weekly_deliveries" in tables:
                weekly = [
                    {"week_start": week, "status": status, "count": count}
                    for week, status, count in state.execute(
                        """
                        SELECT week_start, status, COUNT(*)
                        FROM weekly_deliveries
                        GROUP BY week_start, status
                        ORDER BY week_start DESC
                        LIMIT 52
                        """
                    )
                ]
            else:
                notes.append("Haftalık gönderim tablosu yok.")
        except sqlite3.Error:
            logger.warning("Rapor görünümü okunamadı.")
            notes.append("Rapor sorgusu başarısız.")
        finally:
            state.close()
    metrics = daily_metrics(paths.operations_path, start, end)
    if not metrics and not paths.operations_path.exists():
        notes.append("stats_daily veritabanı bulunamadı.")
    return {
        "daily_transfers": daily_transfers,
        "daily_detections": daily_detections,
        "by_classification": class_counts,
        "feedback": feedback_counts,
        "weekly": weekly,
        "global_metrics": metrics,
        "notes": notes,
    }


def daily_metrics(path: Path, start: datetime, end: datetime) -> list[dict]:
    """Global stats_daily rows. These are not per-wallet counts."""
    connection = _connect(path)
    if connection is None:
        return []
    try:
        if "stats_daily" not in _tables(connection):
            return []
        rows = connection.execute(
            """
            SELECT day, metric, SUM(value)
            FROM stats_daily
            WHERE day>=? AND day<?
            GROUP BY day, metric
            ORDER BY day, metric
            """,
            (start.astimezone(timezone.utc).date().isoformat(), end.astimezone(timezone.utc).date().isoformat()),
        )
        return [
            {"day": day, "metric": metric, "value": int(value or 0)}
            for day, metric, value in rows
            if metric in GLOBAL_METRICS or str(metric).startswith("classification:")
        ]
    except sqlite3.Error:
        logger.warning("Günlük sayaç okunamadı.")
        return []
    finally:
        connection.close()


def count_wallets(path: Path) -> int | None:
    """Count distinct watched wallets. Does not return addresses or rewrite the file."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Cüzdan kaydı okunamadı.")
        return None
    if not isinstance(data, dict):
        return None
    found: set[tuple[str, str]] = set()
    legacy_address = data.get("wallet_address")
    if isinstance(legacy_address, str) and legacy_address:
        found.add((str(data.get("network", "ethereum")), legacy_address))
    legacy = data.get("legacy")
    if isinstance(legacy, list):
        for item in legacy:
            pair = _wallet_pair(item)
            if pair:
                found.add(pair)
    users = data.get("users")
    if isinstance(users, dict):
        for items in users.values():
            if not isinstance(items, list):
                continue
            for item in items:
                pair = _wallet_pair(item)
                if pair:
                    found.add(pair)
    return len(found)


def _approval_rows(connection, start, end, network, risk_level) -> list[dict]:
    params: list = [_iso(start), _iso(end)]
    filters = ""
    if network:
        filters += " AND network=?"
        params.append(network)
    if risk_level:
        filters += " AND level=?"
        params.append(risk_level)
    params.append(ROW_LIMIT)
    rows = connection.execute(
        f"""
        SELECT created_at, network, tx_hash, score, level, signals
        FROM detections
        WHERE kind='approval' AND created_at>=? AND created_at<? {filters}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    )
    return [
        {
            "time_utc": row["created_at"],
            "network": row["network"],
            "tx_hash": row["tx_hash"],
            "token": "",
            "amount": "",
            "classification": "",
            "detection_kind": "approval",
            "risk_score": row["score"],
            "risk_level": row["level"] or "",
            "feedback": "",
        }
        for row in rows
    ]


def _feedback_labels(connection) -> dict[str, str]:
    grouped: dict[str, set[str]] = {}
    for event_key, feedback in connection.execute(
        "SELECT event_key, feedback FROM transfer_feedback WHERE feedback IS NOT NULL"
    ):
        grouped.setdefault(event_key, set()).add(str(feedback))
    return {key: ", ".join(sorted(values)) for key, values in grouped.items()}


def _public_detection(row) -> dict:
    return {
        "time_utc": row["created_at"],
        "network": row["network"],
        "tx_hash": row["tx_hash"],
        "detection_kind": detection_kind(row["kind"], row["signals"]),
        "risk_score": row["score"],
        "risk_level": row["level"] or "",
    }


def _parse_signals(raw: str | None) -> list[tuple[str, object, object, str]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    parsed = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        parsed.append((item["name"], item.get("weight"), item.get("verified"), str(item.get("evidence") or "")))
    return parsed


def _token(symbol, name) -> str:
    symbol = str(symbol or "")
    name = str(name or "")
    if symbol and name:
        return f"{symbol} · {name}"
    return symbol or name


def _wallet_pair(item) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    network = item.get("network")
    address = item.get("wallet_address")
    if isinstance(network, str) and isinstance(address, str) and address:
        return network, address
    return None


def _grouped(connection, sql: str, params: tuple = ()) -> list[tuple[str, int]]:
    return [(str(name), int(count)) for name, count in connection.execute(sql, params) if name is not None]


def _rows(connection, sql: str) -> list[dict]:
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _scalar(connection, sql: str, params: tuple = ()) -> int:
    row = connection.execute(sql, params).fetchone()
    return int(row[0] or 0)


def _tables(connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _summary_settings() -> tuple[int, int, int, int, Path]:
    root = Path(__file__).resolve().parents[1]
    try:
        from config import settings
        eth_count = len(tuple(settings.etherscan_api_keys or ()))
        tron_count = len(tuple(settings.trongrid_api_keys or ()))
        eth_limit = int(float(settings.etherscan_daily_limit) * float(settings.etherscan_daily_soft_ratio))
        tron_limit = int(float(settings.trongrid_daily_limit) * float(settings.trongrid_daily_soft_ratio))
        log_dir = Path(settings.log_dir)
        if not log_dir.is_absolute():
            log_dir = root / log_dir
    except Exception:
        logger.warning("Sistem özeti ayarları okunamadı.")
        return 0, 0, 0, 0, root / "logs"
    return eth_count, tron_count, eth_limit, tron_limit, log_dir


def _usage_day() -> str:
    import os
    hour = int(os.getenv("DAILY_RESET_UTC_HOUR", "0") or "0") % 24
    now = datetime.now(timezone.utc)
    return (now - timedelta(hours=hour)).date().isoformat()


def _exhausted_keys(path: Path, eth_limit: int, tron_limit: int, eth_count: int, tron_count: int) -> int:
    connection = _connect(path)
    if connection is None or eth_limit <= 0 and tron_limit <= 0:
        return 0
    try:
        rows = connection.execute(
            "SELECT provider, calls FROM api_usage_daily WHERE day=?",
            (_usage_day(),),
        ).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        connection.close()
    eth = tron = 0
    for row in rows:
        provider = str(row["provider"] or "")
        calls = int(row["calls"] or 0)
        if provider == "etherscan" and eth_limit > 0 and calls >= eth_limit:
            eth += 1
        elif provider == "trongrid" and tron_limit > 0 and calls >= tron_limit:
            tron += 1
    return min(eth, eth_count) + min(tron, tron_count)


def _heartbeat_times(path: Path) -> tuple[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "—", "—"
    health = payload.get("health") if isinstance(payload, dict) else None
    if not isinstance(health, dict):
        return "—", "—"
    return _clock(health.get("last_api_success_at")), _clock(health.get("last_telegram_success_at"))


def _clock(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")


def _directory_size(path: Path) -> str:
    total = 0
    try:
        if path.is_dir():
            for item in path.rglob("*"):
                if item.is_file():
                    total += item.stat().st_size
    except OSError:
        return "—"
    megabytes = total / (1024 * 1024)
    if megabytes >= 1:
        return f"{megabytes:.1f} MB"
    return f"{total / 1024:.1f} KB"


def _disk_free(path: Path) -> str:
    try:
        target = path if path.exists() else path.parent
        usage = shutil.disk_usage(target)
    except OSError:
        return "—"
    if usage.total <= 0:
        return "—"
    free_gb = usage.free / (1024 ** 3)
    percent = usage.free / usage.total * 100
    return f"{free_gb:.1f} GB (%{percent:.0f})"


def _connect(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        logger.warning("Panel veritabanı açılamadı.")
        return None
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection
