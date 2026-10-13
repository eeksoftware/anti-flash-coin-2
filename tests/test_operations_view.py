"""Operator pages stay read-only and do not reveal secrets."""

import json
from pathlib import Path

from monitor.state_db import UserStateStore, WalletSettings
from reporting.dashboard_data import DataPaths
from reporting.operations_view import ViewError, log_page, secret_values, settings_payload, users_payload
from reporting.telegram_profiles import attach_profiles, read_photo
from web_server import make_server, LOCAL_HOST

import threading
from urllib.request import urlopen, Request
from urllib.error import HTTPError


WALLET = "0x" + "cd" * 20


def test_logs_are_newest_first_and_mask_tokens(tmp_path):
    (tmp_path / "app.log").write_text(
        "2026-10-10 12:00:00 UTC | INFO | notifications.telegram | Telegram'dan 2 güncelleme alındı.\n"
        "2026-10-10 12:01:00 UTC | WARNING | notifications.telegram | bot123456:abcdefghijklmnopqrstuvwxyz\n",
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text("gizli", encoding="utf-8")
    page = log_page(tmp_path, file_name="app.log")
    assert page["entries"][0]["message"] == "bot••••"
    assert "abcdefghijklmnopqrstuvwxyz" not in json.dumps(page)
    assert page["total"] == 2
    assert all(item["name"] != "notes.txt" for item in page["files"])
    try:
        log_page(tmp_path, file_name="../.env")
        raised = False
    except ViewError:
        raised = True
    assert raised


def test_users_show_settings_without_feedback_secret(tmp_path):
    saved = UserStateStore(tmp_path / "user_state.sqlite")
    saved.save_settings("9911", "ethereum", WALLET, WalletSettings(alias="Ada", weekly_report=True, suspicious_only=True))
    token = saved.register_alert(
        user_id="9911", network="ethereum", wallet_address=WALLET, classification="UNKNOWN",
        record={"network": "ethereum", "tx_hash": "0x" + "11" * 32, "event_key": "evt-1", "wallet": WALLET},
    )
    saved.set_feedback(token, "9911", "CORRECT")
    (tmp_path / "runtime_state.json").write_text(
        json.dumps({"users": {"9911": [{"network": "ethereum", "wallet_address": WALLET}]}, "legacy": []}),
        encoding="utf-8",
    )
    payload = users_payload(DataPaths(tmp_path / "user_state.sqlite", tmp_path / "operations.sqlite", tmp_path / "runtime_state.json"), secrets=("super-secret-token-value",))
    raw = json.dumps(payload)
    assert token not in raw and "super-secret-token-value" not in raw
    user = payload["users"][0]
    assert user["user_id"] == "9911"
    assert user["wallets"][0]["alias"] == "Ada"
    assert user["wallets"][0]["weekly_report"] is True
    assert user["feedback"][0]["feedback"] == "CORRECT"
    saved.close()


def test_settings_report_presence_not_secret_values():
    class Current:
        telegram_bot_token = "123456789:abcdefghijklmnopqrstuvwxyz"
        telegram_chat_id = "42"
        activity_log_chat_id = ""
        admin_ids = ("7",)
        enabled_networks = ("ethereum",)
        network = "ethereum"
        poll_interval = 2
        max_poll_interval = 30
        approval_poll_interval = 60
        monitor_min_interval = 2
        monitor_max_interval = 30
        etherscan_api_keys = ()
        etherscan_rate_per_key = 4
        etherscan_daily_limit = 100
        trongrid_api_keys = ()
        trongrid_base_url = "https://api.trongrid.io"
        trongrid_rate_per_key = 10
        tron_only_confirmed = True
        cache_max_entries = 10
        log_level = "INFO"
        log_retention_days = 90
        log_dir = "./logs"
        heartbeat_url = ""

    payload = settings_payload(Current(), secrets=secret_values(Current()))
    raw = json.dumps(payload)
    assert "abcdefghijklmnopqrstuvwxyz" not in raw
    telegram = payload["groups"][0]["rows"]
    assert telegram[0] == {"label": "Bot token", "value": "tanımlı"}
    assert telegram[1]["value"] == "42"


def test_profiles_keep_file_ids_and_tokens_off_the_payload():
    secret = "123456:SUPERSECRETTOKEN"

    def call(method, params):
        if method == "getChat":
            return {
                "id": 9911,
                "type": "private",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "username": "ada_l",
                "bio": "merhaba",
                "photo": {"big_file_id": "file-SECRET-1234", "small_file_id": "tiny-secret"},
            }
        return None

    payload = attach_profiles({"users": [{"user_id": "9911"}, {"user_id": "not-a-user"}]}, token=secret, call=call)
    raw = json.dumps(payload)
    assert "file-SECRET" not in raw and secret not in raw
    profile = payload["users"][0]["profile"]
    assert profile["name"] == "Ada Lovelace"
    assert profile["username"] == "ada_l"
    assert profile["has_photo"] is True
    assert payload["users"][1]["profile"]["available"] is False


def test_photo_bytes_reject_non_images_and_escape_paths():
    jpeg = b"\xff\xd8\xff" + b"pixels"

    def call(method, params):
        if method == "getChat":
            return {"type": "private", "first_name": "Ada", "photo": {"big_file_id": "file-12345678"}}
        if method == "getFile":
            return {"file_path": params.get("path") or "photos/a.jpg", "file_size": len(jpeg)}
        return None

    body, kind = read_photo("9911", token="9:SECRETTOKEN", call=call, download=lambda path: jpeg)
    assert kind == "image/jpeg" and b"SECRETTOKEN" not in body

    def bad_call(method, params):
        if method == "getFile":
            return {"file_path": "../.env", "file_size": 10}
        return call(method, params)

    assert read_photo("9911", token="9:SECRETTOKEN", call=bad_call, download=lambda path: b"secret") is None
    assert read_photo("9911", token="9:SECRETTOKEN", call=call, download=lambda path: b"not-an-image") is None


def test_http_settings_and_invalid_log_name(tmp_path):
    server = make_server(LOCAL_HOST, 0, DataPaths(tmp_path / "a.sqlite", tmp_path / "b.sqlite", tmp_path / "c.json"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(server, "/api/settings")
        assert status == 200
        from config import settings
        if settings.telegram_bot_token:
            assert settings.telegram_bot_token not in body
        assert "Bot token" in body
        status, body = _get(server, "/api/logs?file=../.env")
        assert status == 400
        status, body = _get(server, "/api/users")
        assert status == 200 and "users" in body
        status, body = _get(server, "/api/users/photo?user_id=12345")
        assert status == 404
        status, body = _get(server, "/api/users/photo?user_id=../.env")
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()


def _get(server, path):
    url = f"http://{LOCAL_HOST}:{server.server_address[1]}{path}"
    try:
        with urlopen(Request(url), timeout=5) as response:
            return response.status, response.read().decode()
    except HTTPError as exc:
        return exc.code, exc.read().decode()
