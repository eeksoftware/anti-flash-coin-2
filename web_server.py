"""Local HTTP server for the dashboard.

It serves the HTML frontend and JSON views from reporting.dashboard_data.
It does not start the bot, does not write to SQLite, and binds only to the
loopback address passed by the caller after that address is checked here.
Login keeps a session in memory and does not store the password.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from reporting.access import MAX_RANGE
from reporting.dashboard_data import (
    CLASSIFICATIONS,
    RISK_LEVELS,
    DataPaths,
    default_paths,
    feedback_view,
    load_overview,
    report_view,
    system_summary,
    risk_view,
    transfer_rows,
)
from reporting.operations_view import ViewError, log_page, secret_values, settings_payload, users_payload
from reporting.telegram_profiles import attach_profiles, read_photo


logger = logging.getLogger(__name__)

LOCAL_HOST = "127.0.0.1"
FRONTEND = Path(__file__).resolve().parent / "frontend"
DETECTIONS = frozenset({"transfer", "poisoning", "approval"})
NETWORK_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
PAGE_SIZE = 20
SESSION_COOKIE = "aft_session"
SESSION_TTL = 12 * 60 * 60
LOGIN_LIMIT = 8
LOGIN_WINDOW = 5 * 60
HTML_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class LocalDashboardServer(ThreadingHTTPServer):
    # Lets the panel restart after the previous listener has closed.
    # The bind address is still checked before this socket is created.
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], paths: DataPaths) -> None:
        self.paths = paths
        self.sessions: dict[str, float] = {}
        self.login_failures: list[float] = []
        self.auth_lock = threading.Lock()
        super().__init__(server_address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._respond(head_only=False)

    def do_HEAD(self) -> None:
        self._respond(head_only=True)

    def do_POST(self) -> None:
        try:
            route = self._route()
            if route == "/api/login":
                self._login()
                return
            if route == "/api/logout":
                self._logout()
                return
            self._reject_write()
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception:
            logger.warning("Panel isteği tamamlanamadı.")
            self._send_json(500, {"error": "İstek tamamlanamadı."})

    def do_PUT(self) -> None:
        self._reject_write()

    def do_PATCH(self) -> None:
        self._reject_write()

    def do_DELETE(self) -> None:
        self._reject_write()

    def log_message(self, fmt: str, *args) -> None:
        logger.info("panel %s", args[0] if args else fmt)

    def _respond(self, *, head_only: bool) -> None:
        try:
            parsed = urlparse(self.path)
            if len(self.path) > 2048:
                self._send_json(400, {"error": "İstek çok uzun."}, head_only=head_only)
                return
            route = unquote(parsed.path)
            if "\x00" in route:
                self._send_json(400, {"error": "İstek geçersiz."}, head_only=head_only)
                return
            if route != "/" and route.endswith("/"):
                route = route[:-1]
            query = parse_qs(parsed.query, keep_blank_values=False)
            if route == "/api/session":
                required = credentials() is not None
                self._send_json(
                    200,
                    {"required": required, "authenticated": self._authenticated()},
                    head_only=head_only,
                )
                return
            if route == "/api/ticker":
                self._send_json(200, ticker_payload(), head_only=head_only)
                return
            if route.startswith("/api/") and not self._authenticated():
                self._send_json(401, {"error": "Giriş gerekli."}, head_only=head_only)
                return
            if route == "/api/users/photo":
                self._send_user_photo(query, head_only=head_only)
                return
            if route.startswith("/api/"):
                payload, status = self._api(route, query)
                self._send_json(status, payload, head_only=head_only)
                return
            static = resolve_static(route)
            if static is None:
                self._send_json(404, {"error": "Sayfa bulunamadı."}, head_only=head_only)
                return
            body = static.read_bytes()
            content_type = STATIC_TYPES[static.suffix.lower()]
            policy = HTML_POLICY if static.suffix.lower() == ".html" else "default-src 'none'"
            self._send_bytes(200, body, content_type, head_only=head_only, policy=policy)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message}, head_only=head_only)
        except Exception:
            logger.warning("Panel isteği tamamlanamadı.")
            self._send_json(500, {"error": "İstek tamamlanamadı."}, head_only=head_only)

    def _api(self, route: str, query: dict) -> tuple[dict, int]:
        paths = self.server.paths
        if route == "/api/meta":
            return meta_payload(paths), 200
        if route == "/api/overview":
            return overview_payload(paths), 200
        if route == "/api/transfers":
            return transfers_payload(paths, query), 200
        if route == "/api/risk":
            return risk_payload(paths, query), 200
        if route == "/api/feedback":
            return feedback_payload(paths, query), 200
        if route == "/api/statistics":
            return statistics_payload(paths, query), 200
        if route == "/api/logs":
            return _logs_payload(query), 200
        if route == "/api/chats":
            return _chats_payload(query, paths), 200
        if route == "/api/users":
            payload = users_payload(paths, secrets=secret_values())
            from config import settings
            return attach_profiles(payload, token=str(settings.telegram_bot_token or "")), 200
        if route == "/api/settings":
            return settings_payload(), 200
        raise ApiError(404, "Sayfa bulunamadı.")

    def _send_user_photo(self, query: dict, *, head_only: bool) -> None:
        user_id = (query.get("user_id") or [""])[0]
        if not re.fullmatch(r"[0-9]{1,20}", user_id):
            self._send_json(400, {"error": "Kullanıcı kimliği geçersiz."}, head_only=head_only)
            return
        known = {str(user.get("user_id")) for user in users_payload(self.server.paths, secrets=()).get("users") or []}
        if user_id not in known:
            self._send_json(404, {"error": "Kullanıcı yok."}, head_only=head_only)
            return
        from config import settings
        photo = read_photo(user_id, token=str(settings.telegram_bot_token or ""))
        if photo is None:
            self._send_json(404, {"error": "Profil fotoğrafı yok."}, head_only=head_only)
            return
        body, content_type = photo
        self._send_bytes(200, body, content_type, head_only=head_only, policy="default-src 'none'")

    def _route(self) -> str:
        if len(self.path) > 2048 or "\x00" in self.path:
            raise ApiError(400, "İstek geçersiz.")
        route = unquote(urlparse(self.path).path)
        if "\x00" in route:
            raise ApiError(400, "İstek geçersiz.")
        if route != "/" and route.endswith("/"):
            route = route[:-1]
        return route

    def _login(self) -> None:
        expected = credentials()
        if expected is None:
            self._discard_body()
            self._send_json(404, {"error": "Giriş kapalı."})
            return
        if not self._login_allowed():
            self._discard_body()
            self._send_json(429, {"error": "Çok fazla deneme. Biraz bekleyin."})
            return
        payload = self._read_json()
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ApiError(400, "İstek geçersiz.")
        if len(username) > 64 or len(password) > 256:
            self._record_failure()
            self._send_json(401, {"error": "Kullanıcı adı veya şifre hatalı."})
            return
        user_ok = _same(username, expected[0])
        password_ok = _same(password, expected[1])
        if not (user_ok and password_ok):
            self._record_failure()
            self._send_json(401, {"error": "Kullanıcı adı veya şifre hatalı."})
            return
        token = self._open_session()
        self._send_json(200, {"ok": True}, cookie=_session_cookie(token, SESSION_TTL))

    def _logout(self) -> None:
        self._discard_body()
        token = self._cookie()
        if re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token):
            with self.server.auth_lock:
                self.server.sessions.pop(token, None)
        self._send_json(200, {"ok": True}, cookie=_session_cookie("", 0))

    def _authenticated(self) -> bool:
        if credentials() is None:
            return True
        token = self._cookie()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token):
            return False
        now = time.monotonic()
        with self.server.auth_lock:
            expiry = self.server.sessions.get(token)
            if expiry is None or expiry <= now:
                self.server.sessions.pop(token, None)
                return False
            return True

    def _cookie(self) -> str:
        header = self.headers.get("Cookie", "")
        for piece in header.split(";"):
            name, sep, value = piece.strip().partition("=")
            if sep and name == SESSION_COOKIE:
                return value.strip()
        return ""

    def _login_allowed(self) -> bool:
        now = time.monotonic()
        with self.server.auth_lock:
            self.server.login_failures = [
                stamp for stamp in self.server.login_failures if now - stamp < LOGIN_WINDOW
            ]
            return len(self.server.login_failures) < LOGIN_LIMIT

    def _record_failure(self) -> None:
        with self.server.auth_lock:
            self.server.login_failures.append(time.monotonic())

    def _open_session(self) -> str:
        token = secrets.token_urlsafe(32)
        expiry = time.monotonic() + SESSION_TTL
        now = time.monotonic()
        with self.server.auth_lock:
            expired = [key for key, stamp in self.server.sessions.items() if stamp <= now]
            for key in expired:
                del self.server.sessions[key]
            self.server.sessions[token] = expiry
            self.server.login_failures.clear()
        return token

    def _read_json(self) -> dict:
        length = self._body_length(4096)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "İstek geçersiz.") from None
        if not isinstance(payload, dict):
            raise ApiError(400, "İstek geçersiz.")
        return payload

    def _discard_body(self) -> None:
        length = self._body_length(65536)
        if length:
            self.rfile.read(length)

    def _body_length(self, limit: int) -> int:
        raw = self.headers.get("Content-Length", "0")
        if not raw.isdigit():
            raise ApiError(400, "İstek geçersiz.")
        length = int(raw)
        if length > limit:
            self.close_connection = True
            raise ApiError(400, "İstek çok uzun.")
        return length

    def _reject_write(self) -> None:
        self.close_connection = True
        length = self.headers.get("Content-Length", "0")
        if length.isdigit() and 0 < int(length) <= 65536:
            self.rfile.read(int(length))
        self._send_json(405, {"error": "Bu panel salt okunur."}, allow="GET, HEAD")

    def _send_json(
        self,
        status: int,
        payload: dict,
        *,
        head_only: bool = False,
        allow: str | None = None,
        cookie: str | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            head_only=head_only,
            policy="default-src 'none'",
            allow=allow,
            cookie=cookie,
        )

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head_only: bool,
        policy: str,
        allow: str | None = None,
        cookie: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", policy)
        if allow:
            self.send_header("Allow", allow)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


def make_server(host: str, port: int, paths: DataPaths | None = None) -> LocalDashboardServer:
    if host != LOCAL_HOST:
        raise RuntimeError("Panel yalnızca bu makineye bağlanır.")
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        raise RuntimeError("Panel portu geçersiz.")
    return LocalDashboardServer((host, port), paths if paths is not None else default_paths())


def credentials() -> tuple[str, str] | None:
    username = os.environ.get("DASHBOARD_USERNAME", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not username or not password.strip():
        return None
    if len(username) > 64 or len(password) > 256:
        return None
    return username, password


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(given.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _session_cookie(token: str, max_age: int) -> str:
    return (
        f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"
    )


def serve(host: str = LOCAL_HOST, port: int = 1337) -> None:
    from dotenv import load_dotenv

    load_dotenv()
    server = make_server(host, port)
    bound_host, bound_port = server.server_address[:2]
    print(f"Anti-Flash Token paneli hazır: http://{bound_host}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel durdu.", flush=True)
    finally:
        server.server_close()


def resolve_static(url_path: str) -> Path | None:
    if url_path == "/":
        relative = Path("index.html")
    else:
        raw = url_path.lstrip("/")
        relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    root = FRONTEND.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or candidate.suffix.lower() not in STATIC_TYPES or not candidate.is_file():
        return None
    return candidate


def _logs_payload(query: dict) -> dict:
    return _guarded_log(log_page, query, loggers=None)


def _chats_payload(query: dict, paths: DataPaths) -> dict:
    from reporting.chat_store import ChatStore, chat_path
    chat_id = _one(query, "chat", 32)
    if chat_id and not re.fullmatch(r"-?[0-9]{1,20}", chat_id):
        raise ApiError(400, "Chat id geçersiz.")
    # The transcript database lives beside the user-state database.
    store = ChatStore(chat_path(Path(paths.state_path).parent), readonly=True)
    chats = _mask_chat_rows(store.chats())
    chosen = chat_id or (chats[0]["chat_id"] if chats else "")
    messages = _mask_chat_rows(store.messages(chosen)) if chosen else []
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chats": chats,
        "chat_id": chosen,
        "messages": messages,
    }


def _mask_chat_rows(rows: list[dict]) -> list[dict]:
    secrets = [item for item in secret_values() if item and len(item) >= 8]
    masked = []
    for row in rows:
        item = dict(row)
        for key in ("body", "last_text", "sender_name", "title", "username"):
            value = item.get(key)
            if not isinstance(value, str):
                continue
            for secret in secrets:
                value = value.replace(secret, "••••")
            item[key] = value
        masked.append(item)
    return masked


def _guarded_log(reader, query: dict, loggers) -> dict:
    try:
        return reader(
            None,
            file_name=_one(query, "file", 80) or "app.log",
            page=_int_param(query, "page", 1, 1, 10000),
            page_size=_int_param(query, "page_size", 40, 1, 100),
            query=_one(query, "q", 200),
            loggers=loggers,
            secrets=secret_values(),
        )
    except ViewError as exc:
        raise ApiError(400, exc.message) from None


_TICKER_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'_-]{0,48}$")


def ticker_payload() -> dict:
    """Public login-strip labels. Names only; contracts and addresses stay out."""
    coins = _registered_coin_names()
    seen = {name.casefold() for name in coins}
    networks = []
    for name in _registered_network_names():
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        networks.append(name)
    return {"coins": coins, "networks": networks}


def _registered_coin_names() -> list[str]:
    path = Path(__file__).resolve().parent / "registry" / "tokens.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return []
    names = []
    for symbol, token in tokens.items():
        label = symbol if isinstance(symbol, str) else ""
        if isinstance(token, dict):
            name = token.get("name")
            if isinstance(name, str) and name.strip():
                label = name.strip()
        if isinstance(label, str) and _TICKER_LABEL.fullmatch(label):
            names.append(label)
    return names


def _registered_network_names() -> list[str]:
    from networks import NETWORKS

    names = []
    for config in NETWORKS.values():
        label = config.display_name.strip()
        if _TICKER_LABEL.fullmatch(label):
            names.append(label)
    return names


def meta_payload(paths: DataPaths) -> dict:
    return {
        "generated_at": _now(),
        "timezone": "UTC",
        "read_only": True,
        "sources": {
            "user_state": paths.state_path.exists(),
            "operations": paths.operations_path.exists(),
            "wallets": paths.wallet_path.exists(),
        },
    }


def overview_payload(paths: DataPaths, *, now: datetime | None = None) -> dict:
    data = load_overview(paths, now=now)
    transfers = []
    for row in data.recent_transfers:
        transfers.append({
            "time_utc": row.get("block_time") or "",
            "network": row.get("network") or "",
            "tx_hash": row.get("tx_hash") or "",
            "token": row.get("token_symbol") or "",
            "amount": row.get("amount") or "",
            "classification": row.get("classification") or "",
        })
    return {
        "generated_at": _now(),
        "wallet_count": data.wallet_count,
        "transfer_count": data.transfer_count,
        "transfers_24h": data.transfers_24h,
        "transfers_7d": data.transfers_7d,
        "by_classification": _pairs(data.by_classification),
        "by_risk_level": _pairs(data.by_risk_level),
        "recent_transfers": transfers,
        "recent_detections": list(data.recent_detections),
        "system": system_summary(paths.state_path.parent),
        "notes": list(data.notes),
    }


def transfers_payload(paths: DataPaths, query: dict, *, now: datetime | None = None) -> dict:
    start, end = _window(query, now)
    network = _network(query)
    classification = _choice(query, "classification", CLASSIFICATIONS)
    detection = _choice(query, "detection", DETECTIONS)
    risk_level = _choice(query, "risk", RISK_LEVELS)
    needle = _one(query, "q", 200)
    page = _int_param(query, "page", 1, 1, 10000)
    page_size = _int_param(query, "page_size", PAGE_SIZE, 1, 100)
    rows, truncated = transfer_rows(
        paths,
        start=start,
        end=end,
        network=network,
        classification=classification,
        detection=detection,
        risk_level=risk_level,
    )
    matched = _search(rows, needle)
    begin = (page - 1) * page_size
    return {
        "generated_at": _now(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": matched[begin:begin + page_size],
        "page": page,
        "page_size": page_size,
        "total": len(matched),
        "truncated": truncated,
        "notes": [],
    }


def risk_payload(paths: DataPaths, query: dict, *, now: datetime | None = None) -> dict:
    start, end = _window(query, now)
    view = risk_view(paths, start=start, end=end)
    return {
        "generated_at": _now(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "available": view["available"],
        "by_level": _pairs(view["by_level"]),
        "by_day": view["by_day"],
        "poisoning": view["poisoning"],
        "approvals": view["approvals"],
        "signals": view["signals"],
    }


def feedback_payload(paths: DataPaths, query: dict, *, now: datetime | None = None) -> dict:
    start, end = _window(query, now)
    classification = _choice(query, "classification", CLASSIFICATIONS)
    view = feedback_view(paths, start=start, end=end)
    rows = view.get("by_classification") or []
    if classification and view.get("available"):
        rows = [row for row in rows if row.get("classification") == classification]
    return {
        "generated_at": _now(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "available": view["available"],
        "counts": view.get("counts") or {},
        "by_classification": rows,
        "false_alarm": view.get("false_alarm"),
        "labeled": view.get("labeled") or 0,
    }


def statistics_payload(paths: DataPaths, query: dict, *, now: datetime | None = None) -> dict:
    start, end = _window(query, now)
    report = report_view(paths, start=start, end=end)
    risk = risk_view(paths, start=start, end=end)
    feedback = feedback_view(paths, start=start, end=end)
    return {
        "generated_at": _now(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "daily_transfers": report["daily_transfers"],
        "daily_detections": report["daily_detections"],
        "by_classification": _pairs(report["by_classification"]),
        "by_risk_level": _pairs(risk["by_level"]) if risk["available"] else None,
        "feedback": {
            "available": feedback["available"],
            "counts": feedback.get("counts") or {},
            "false_alarm": feedback.get("false_alarm"),
            "labeled": feedback.get("labeled") or 0,
        },
        "weekly": report["weekly"],
        "global_metrics": report["global_metrics"],
        "notes": report["notes"],
    }


def _search(rows: list[dict], needle: str) -> list[dict]:
    folded = needle.casefold().strip()
    if not folded:
        return rows
    fields = (
        "time_utc", "network", "tx_hash", "token", "amount", "classification",
        "detection_kind", "risk_score", "risk_level", "feedback",
    )
    matched = []
    for row in rows:
        haystack = " ".join("" if row.get(field) is None else str(row.get(field)) for field in fields)
        if folded in haystack.casefold():
            matched.append(row)
    return matched


def _pairs(rows) -> list[dict] | None:
    if rows is None:
        return None
    return [{"name": name, "count": count} for name, count in rows]


def _window(query: dict, now: datetime | None) -> tuple[datetime, datetime]:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start_raw = _one(query, "start", 40)
    end_raw = _one(query, "end", 40)
    if not start_raw and not end_raw:
        return moment - timedelta(days=7), moment
    if not start_raw or not end_raw:
        raise ApiError(400, "Başlangıç ve bitiş birlikte verilmeli.")
    try:
        start = _moment(start_raw)
        end = _moment(end_raw)
    except ValueError:
        raise ApiError(400, "Tarih UTC ve YYYY-AA-GG biçiminde olmalı.") from None
    if start >= end:
        raise ApiError(400, "Başlangıç, bitişten önce olmalı. Bitiş anı dahil değildir.")
    if end - start > MAX_RANGE:
        raise ApiError(400, "Tarih aralığı en fazla 366 gün olabilir.")
    return start, end


def _moment(value: str) -> datetime:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        year, month, day = (int(part) for part in value.split("-"))
        return datetime(year, month, day, tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _choice(query: dict, name: str, allowed) -> str | None:
    value = _one(query, name, 32)
    if not value:
        return None
    if value not in allowed:
        raise ApiError(400, "Filtre değeri geçersiz.")
    return value


def _network(query: dict) -> str | None:
    value = _one(query, "network", 32)
    if not value:
        return None
    if NETWORK_NAME.fullmatch(value) is None:
        raise ApiError(400, "Filtre değeri geçersiz.")
    return value


def _int_param(query: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _one(query, name, 8)
    if not raw:
        return default
    if not raw.isdigit():
        raise ApiError(400, "Sayfa numarası geçersiz.")
    value = int(raw)
    if value < minimum or value > maximum:
        raise ApiError(400, "Sayfa numarası geçersiz.")
    return value


def _one(query: dict, name: str, limit: int) -> str:
    values = query.get(name) or []
    if not values:
        return ""
    value = str(values[0]).strip()
    if len(value) > limit:
        raise ApiError(400, "Parametre çok uzun.")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    from dashboard import BIND_HOST, BIND_PORT

    serve(BIND_HOST, BIND_PORT)
