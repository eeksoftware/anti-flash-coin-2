"""Read-only operator views for logs, users, and bot settings.

Telegram message bodies are not stored, so chat history cannot be rebuilt.
Secrets are masked before a value leaves this module.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from reporting.dashboard_data import DataPaths


LOG_STEMS = ("app", "transactions", "detections", "tests", "api_usage")
_LOG_FILE = re.compile(r"^(app|transactions|detections|tests|api_usage)\.log(?:\.\d{4}-\d{2}-\d{2})?$")
_APP_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC \| ([A-Z]+) \| ([^|]+?) \| (.*)$")
_TELEGRAM_TOKEN = re.compile(r"(?i)\bbot\d+:[a-z0-9_-]{12,}\b")
_KEY_QUERY = re.compile(r"(?i)(\b(?:apikey|api_key|token)=)[^&#\s\"']+")
_CHAT_LOGGERS = ("notifications.telegram", "notifications.admin")
PAGE_SIZE = 40
MAX_BYTES = 8_000_000
MAX_LINES = 20_000


class ViewError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def log_directory(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    from config import settings
    raw = Path(settings.log_dir)
    if not raw.is_absolute():
        raw = Path(__file__).resolve().parents[1] / raw
    return raw


def list_logs(directory: Path | None = None) -> list[dict]:
    root = log_directory(directory)
    if not root.is_dir():
        return []
    found = []
    for path in root.iterdir():
        if not path.is_file() or _LOG_FILE.fullmatch(path.name) is None:
            continue
        found.append({
            "name": path.name,
            "bytes": path.stat().st_size,
            "active": path.suffix == ".log" or path.name.endswith(".log"),
        })
    found.sort(key=lambda item: (not item["name"].endswith(".log"), item["name"]), reverse=False)
    active = [item for item in found if item["name"].count(".") == 1]
    rotated = sorted((item for item in found if item not in active), key=lambda item: item["name"], reverse=True)
    return active + rotated


def log_page(
    directory: Path | None,
    *,
    file_name: str,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    query: str = "",
    loggers: tuple[str, ...] | None = None,
    secrets: tuple[str, ...] = (),
) -> dict:
    path = _resolve_log(log_directory(directory), file_name)
    if path is None:
        return {
            "generated_at": _now(),
            "file": file_name,
            "entries": [],
            "page": 1,
            "page_size": page_size,
            "total": 0,
            "truncated": False,
            "files": list_logs(log_directory(directory)),
            "note": "Günlük dosyası yok.",
        }
    lines, truncated = _read_lines(path)
    entries = []
    for line in reversed(lines):
        entry = _parse_line(line, secrets)
        if loggers and entry.get("logger") not in loggers:
            continue
        if query and query.casefold() not in _haystack(entry).casefold():
            continue
        entries.append(entry)
    size = min(max(page_size, 1), 100)
    begin = (max(page, 1) - 1) * size
    return {
        "generated_at": _now(),
        "file": path.name,
        "entries": entries[begin:begin + size],
        "page": max(page, 1),
        "page_size": size,
        "total": len(entries),
        "truncated": truncated,
        "files": list_logs(path.parent),
    }


def chat_page(directory: Path | None, *, page: int = 1, page_size: int = PAGE_SIZE, query: str = "", secrets: tuple[str, ...] = ()) -> dict:
    view = log_page(
        directory, file_name="app.log", page=page, page_size=page_size, query=query,
        loggers=_CHAT_LOGGERS, secrets=secrets,
    )
    updates = 0
    for entry in view["entries"]:
        match = re.search(r"(\d+) güncelleme", entry.get("message") or "")
        if match:
            updates += int(match.group(1))
    view["updates_on_page"] = updates
    view["stored_messages"] = False
    view["note"] = "Telegram mesaj metni, gönderen adı ve sohbet dökümü kaydedilmiyor. Burada yalnızca günlükteki bot izi var."
    return view


def users_payload(paths: DataPaths, *, secrets: tuple[str, ...] = ()) -> dict:
    wallets = _wallet_records(paths.wallet_path)
    settings_rows = _wallet_setting_rows(paths.state_path)
    feedback_rows = _sqlite_rows(paths.state_path, """
        SELECT user_id, network, wallet_address, classification, feedback, previous_feedback,
               feedback_at, event_key
        FROM transfer_feedback
        ORDER BY feedback_at DESC
        LIMIT 500
    """)
    grouped: dict[str, dict[tuple[str, str], dict]] = {}
    for user_id, items in wallets["users"].items():
        for item in items:
            grouped.setdefault(user_id, {})[(item["network"], item["wallet_address"])] = _wallet_view(item)
    for row in settings_rows:
        user_id = str(row["user_id"])
        key = (str(row["network"]), str(row["wallet_address"]))
        current = grouped.setdefault(user_id, {}).setdefault(key, _wallet_view({"network": key[0], "wallet_address": key[1]}))
        current.update({
            "alias": _mask(row["alias"], secrets),
            "min_amount": str(row["min_amount"] or "0"),
            "suspicious_only": bool(row["suspicious_only"]),
            "quiet_start": row["quiet_start"] or "",
            "quiet_end": row["quiet_end"] or "",
            "weekly_report": bool(row["weekly_report"]),
            "fee_alerts": bool(row["fee_alerts"]),
            "settings_saved": True,
        })
    feedback_by_user: dict[str, list[dict]] = {}
    for row in feedback_rows:
        feedback_by_user.setdefault(str(row["user_id"]), []).append({
            "time_utc": row["feedback_at"] or "",
            "network": row["network"],
            "wallet_address": row["wallet_address"],
            "classification": row["classification"],
            "feedback": row["feedback"] or "",
            "previous_feedback": row["previous_feedback"] or "",
            "event_key": row["event_key"],
        })
    users = []
    for user_id in sorted(grouped):
        pairs = list(grouped[user_id])
        wallet_rows = [grouped[user_id][pair] for pair in pairs]
        transfers = _rows_for_wallets(paths.state_path, "report_transfers", pairs, "block_time", """
            SELECT block_time, network, tx_hash, token_symbol, amount, classification, direction
            FROM report_transfers
            WHERE {clause}
            ORDER BY block_time DESC
            LIMIT 40
        """)
        history = _rows_for_wallets(paths.state_path, "address_history", pairs, "last_seen", """
            SELECT network, wallet_address, counterparty, first_seen, last_seen, transfer_count
            FROM address_history
            WHERE {clause}
            ORDER BY last_seen DESC
            LIMIT 30
        """)
        users.append({
            "user_id": user_id,
            "wallets": [_mask_row(row, secrets) for row in wallet_rows],
            "feedback": [_mask_row(row, secrets) for row in feedback_by_user.get(user_id, [])],
            "transfers": [_mask_row(row, secrets) for row in transfers],
            "counterparties": [_mask_row(row, secrets) for row in history],
        })
    return {
        "generated_at": _now(),
        "users": users,
        "legacy": [_mask_row(_wallet_view(item), secrets) for item in wallets["legacy"]],
        "notes": ["Geri bildirim düğmesinin gizli anahtarı gösterilmez."] if feedback_rows or settings_rows or users else ["Kullanıcı kaydı bulunamadı."],
    }


def settings_payload(current=None, *, secrets: tuple[str, ...] = ()) -> dict:
    if current is None:
        from config import settings as current
        secrets = secret_values(current)
    token = str(getattr(current, "telegram_bot_token", "") or "")
    chat = str(getattr(current, "telegram_chat_id", "") or "")
    activity = str(getattr(current, "activity_log_chat_id", "") or "")
    admins = tuple(getattr(current, "admin_ids", ()) or ())
    keys = getattr(current, "etherscan_api_keys", ()) or ()
    tron_keys = getattr(current, "trongrid_api_keys", ()) or ()
    groups = [
        {"title": "Telegram", "rows": [
            {"label": "Bot token", "value": "tanımlı" if token else "yok"},
            {"label": "Varsayılan sohbet", "value": chat or "yok"},
            {"label": "Aktivite sohbeti", "value": activity or "yok"},
            {"label": "Yöneticiler", "value": ", ".join(admins) if admins else "yok"},
        ]},
        {"title": "Ağ ve izleme", "rows": [
            {"label": "Açık ağlar", "value": ", ".join(getattr(current, "enabled_networks", ()) or ()) or "yok"},
            {"label": "Varsayılan ağ", "value": str(getattr(current, "network", "") or "yok")},
            {"label": "Yoklama aralığı", "value": f"{getattr(current, 'poll_interval', '')} sn"},
            {"label": "En uzun yoklama", "value": f"{getattr(current, 'max_poll_interval', '')} sn"},
            {"label": "Approval yoklama", "value": f"{getattr(current, 'approval_poll_interval', '')} sn"},
            {"label": "İzleme alt sınır", "value": f"{getattr(current, 'monitor_min_interval', '')} sn"},
            {"label": "İzleme üst sınır", "value": f"{getattr(current, 'monitor_max_interval', '')} sn"},
        ]},
        {"title": "API kotası", "rows": [
            {"label": "Etherscan anahtar sayısı", "value": str(len(keys))},
            {"label": "Etherscan hız", "value": f"{getattr(current, 'etherscan_rate_per_key', '')} / sn"},
            {"label": "Etherscan günlük sınır", "value": str(getattr(current, "etherscan_daily_limit", ""))},
            {"label": "TronGrid anahtar sayısı", "value": str(len(tron_keys))},
            {"label": "TronGrid adres", "value": str(getattr(current, "trongrid_base_url", "") or "yok")},
            {"label": "TronGrid hız", "value": f"{getattr(current, 'trongrid_rate_per_key', '')} / sn"},
            {"label": "Yalnızca onaylı TRON", "value": "açık" if getattr(current, "tron_only_confirmed", False) else "kapalı"},
            {"label": "Önbellek üst sınırı", "value": str(getattr(current, "cache_max_entries", ""))},
        ]},
        {"title": "Günlük", "rows": [
            {"label": "Seviye", "value": str(getattr(current, "log_level", ""))},
            {"label": "Saklama", "value": f"{getattr(current, 'log_retention_days', '')} gün"},
            {"label": "Klasör", "value": str(getattr(current, "log_dir", ""))},
            {"label": "Heartbeat", "value": "tanımlı" if getattr(current, "heartbeat_url", "") else "yok"},
        ]},
    ]
    masked = []
    for group in groups:
        masked.append({
            "title": group["title"],
            "rows": [{"label": row["label"], "value": _mask(row["value"], secrets)} for row in group["rows"]],
        })
    return {
        "generated_at": _now(),
        "groups": masked,
        "notes": ["API anahtarı, bot tokenı ve heartbeat adresi gösterilmez."],
    }


def secret_values(current=None) -> tuple[str, ...]:
    if current is None:
        from config import settings as current
    found = []
    token = str(getattr(current, "telegram_bot_token", "") or "")
    if len(token) >= 8:
        found.append(token)
    for item in list(getattr(current, "etherscan_api_keys", ()) or ()) + list(getattr(current, "trongrid_api_keys", ()) or ()):
        value = getattr(item, "value", item)
        text = str(value or "")
        if len(text) >= 8:
            found.append(text)
    return tuple(found)


def _wallet_records(path: Path) -> dict:
    empty = {"users": {}, "legacy": []}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    legacy = []
    if isinstance(data.get("wallet_address"), str) and data.get("wallet_address"):
        legacy.append({"network": str(data.get("network", "ethereum")), "wallet_address": data["wallet_address"]})
    raw_legacy = data.get("legacy")
    if isinstance(raw_legacy, list):
        legacy.extend(item for item in raw_legacy if isinstance(item, dict))
    users = {}
    raw_users = data.get("users")
    if isinstance(raw_users, dict):
        for user_id, items in raw_users.items():
            if isinstance(items, list):
                users[str(user_id)] = [item for item in items if isinstance(item, dict)]
    return {"users": users, "legacy": legacy}


def _wallet_view(item: dict) -> dict:
    return {
        "network": str(item.get("network") or ""),
        "wallet_address": str(item.get("wallet_address") or ""),
        "alias": "",
        "min_amount": "0",
        "suspicious_only": False,
        "quiet_start": "",
        "quiet_end": "",
        "weekly_report": False,
        "fee_alerts": False,
        "settings_saved": False,
    }


def _rows_for_wallets(path: Path, table: str, wallets: list[tuple[str, str]], _order: str, sql: str) -> list[dict]:
    if not wallets:
        return []
    clause = " OR ".join("(network=? AND wallet_address=?)" for _ in wallets)
    params: list[str] = []
    for network, address in wallets:
        params.extend((network, address))
    return _sqlite_rows(path, sql.format(clause=clause), params, required=table)


def _wallet_setting_rows(path: Path) -> list[dict]:
    """Read wallet settings even before the fee column exists."""
    info = _sqlite_rows(path, "PRAGMA table_info(wallet_settings)", required="wallet_settings")
    names = {str(row.get("name") or "") for row in info}
    if "user_id" not in names:
        return []
    weekly = "weekly_report" if "weekly_report" in names else "0 AS weekly_report"
    fee = "fee_alerts" if "fee_alerts" in names else "0 AS fee_alerts"
    return _sqlite_rows(path, f"""
        SELECT user_id, network, wallet_address, alias, min_amount, suspicious_only,
               quiet_start, quiet_end, {weekly}, {fee}
        FROM wallet_settings
    """)


def _sqlite_rows(path: Path, sql: str, params: list | tuple = (), required: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = required or _first_table(sql)
        if table and table not in names:
            return []
        cursor = connection.execute(sql, tuple(params))
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _first_table(sql: str) -> str:
    match = re.search(r"\bFROM\s+([a-z_]+)", sql, re.I)
    return match.group(1) if match else ""


def _resolve_log(root: Path, file_name: str) -> Path | None:
    if _LOG_FILE.fullmatch(file_name) is None:
        raise ViewError("Günlük dosyası geçersiz.")
    root_resolved = root.resolve()
    candidate = (root_resolved / file_name).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ViewError("Günlük dosyası geçersiz.")
    if not candidate.is_file():
        return None
    return candidate


def _read_lines(path: Path) -> tuple[list[str], bool]:
    size = path.stat().st_size
    truncated = size > MAX_BYTES
    try:
        with path.open("rb") as handle:
            if truncated:
                handle.seek(size - MAX_BYTES)
                handle.readline()
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise ViewError("Günlük okunamadı.") from exc
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        return lines[-MAX_LINES:], True
    return lines, truncated


def _parse_line(line: str, secrets: tuple[str, ...]) -> dict:
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            fields = {str(key): _present(value, secrets) for key, value in data.items()}
            return {
                "time": str(data.get("ts") or data.get("time") or ""),
                "level": str(data.get("outcome") or data.get("level") or data.get("classification") or "KAYIT"),
                "logger": str(data.get("source") or data.get("type") or data.get("provider") or ""),
                "message": _summary(data, secrets),
                "fields": fields,
            }
    match = _APP_LINE.match(stripped)
    if match:
        return {
            "time": match.group(1) + " UTC",
            "level": match.group(2),
            "logger": match.group(3).strip(),
            "message": _mask(match.group(4).strip(), secrets),
            "fields": {},
        }
    return {"time": "", "level": "KAYIT", "logger": "", "message": _mask(stripped, secrets), "fields": {}}


def _summary(data: dict, secrets: tuple[str, ...]) -> str:
    if data.get("test_nodeid"):
        return _mask(f"{data.get('test_nodeid')} · {data.get('outcome')}", secrets)
    if data.get("provider") and "window" in data:
        return _mask(f"{data.get('provider')} kullanım penceresi", secrets)
    parts = [str(data.get("classification_label") or data.get("classification") or "")]
    if data.get("token_symbol"):
        parts.append(str(data["token_symbol"]))
    if data.get("network"):
        parts.append(str(data["network"]))
    text = " · ".join(part for part in parts if part)
    return _mask(text or "Kayıt", secrets)


def _present(value, secrets: tuple[str, ...]) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (dict, list)):
        return _mask(json.dumps(value, ensure_ascii=False), secrets)
    return _mask(value, secrets)


def _haystack(entry: dict) -> str:
    fields = " ".join(f"{key} {value}" for key, value in (entry.get("fields") or {}).items())
    return " ".join((entry.get("time") or "", entry.get("level") or "", entry.get("logger") or "", entry.get("message") or "", fields))


def _mask_row(row: dict, secrets: tuple[str, ...]) -> dict:
    return {key: _mask(value, secrets) if isinstance(value, str) else value for key, value in row.items()}


def _mask(value, secrets: tuple[str, ...]) -> str:
    text = "" if value is None else str(value)
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in text:
            text = text.replace(secret, "••••")
    text = _TELEGRAM_TOKEN.sub("bot••••", text)
    return _KEY_QUERY.sub(r"\1••••", text)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
