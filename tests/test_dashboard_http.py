"""HTML dashboard server. Read-only JSON over loopback; no bot and no browser."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import dashboard
from monitor.state_db import UserStateStore
from reporting.dashboard_data import DataPaths
from web_server import LOCAL_HOST, make_server, resolve_static


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


def request(server, path: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None):
    url = f"http://{LOCAL_HOST}:{server.server_address[1]}{path}"
    if body is None and method != "GET":
        body = b"{}"
    outgoing = Request(url, data=body, method=method, headers=headers or {})
    try:
        with urlopen(outgoing, timeout=5) as response:
            payload = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers, payload
    except HTTPError as exc:
        payload = exc.read()
        headers = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, headers, payload


@pytest.fixture
def http_server(tmp_path):
    data = paths(tmp_path)
    server = make_server(LOCAL_HOST, 0, data)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_server_stays_on_loopback_and_frontend_has_no_html_injection():
    source = Path("web_server.py").read_text(encoding="utf-8")
    dashboard_source = Path(dashboard.__file__).read_text(encoding="utf-8")
    assert dashboard.BIND_HOST == LOCAL_HOST == "127.0.0.1"
    assert dashboard.BIND_PORT == 1337
    assert "0.0.0.0" not in source and "0.0.0.0" not in dashboard_source
    assert "import main" not in dashboard_source and "main.py" not in dashboard_source
    with pytest.raises(RuntimeError):
        make_server("localhost", 1337)
    assert resolve_static("/../config.py") is None
    assert resolve_static("/css/../../.env") is None
    assert resolve_static("/css/styles.css") == Path("frontend/css/styles.css").resolve()
    script = Path("frontend/js/app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def test_overview_and_filters_match_database_without_secrets(tmp_path, http_server):
    saved = store(tmp_path)
    seed(saved, when=START, classification="UNKNOWN", event="in", tx="0x" + "aa" * 32, signals=[{"name": "unknown_token"}], level="DÜŞÜK", score=5)
    seed(
        saved, when=START + timedelta(days=2), classification="KNOWN_FRAUD", event="fraud",
        tx="0x" + "bb" * 32, signals=[{"name": "address_lookalike", "evidence": "benzer"}], level="YÜKSEK", score=60,
    )
    seed(saved, when=END, classification="VARIANT", event="out", tx="0x" + "cc" * 32)
    (tmp_path / "runtime_state.json").write_text(
        '{"users":{"9911":[{"network":"ethereum","wallet_address":"%s"}]}}\n' % WALLET,
        encoding="utf-8",
    )
    before = saved._connection.execute("SELECT COUNT(*) FROM report_transfers").fetchone()[0]
    window = "?start=2026-10-05T00:00:00%2B00:00&end=2026-10-12T00:00:00%2B00:00"
    status, headers, raw = request(http_server, "/api/overview")
    overview = json.loads(raw)
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert "access-control-allow-origin" not in headers
    assert overview["transfer_count"] == 3
    assert overview["wallet_count"] == 1
    assert overview["transfers_24h"] is not None
    assert {item["name"] for item in overview["by_classification"]} == {"UNKNOWN", "KNOWN_FRAUD", "VARIANT"}
    assert WALLET not in raw.decode() and "9911" not in raw.decode()
    status, _headers, raw = request(http_server, "/api/transfers" + window + "&classification=KNOWN_FRAUD")
    page = json.loads(raw)
    assert status == 200
    assert [row["tx_hash"] for row in page["rows"]] == ["0x" + "bb" * 32]
    assert page["rows"][0]["risk_score"] == 60
    assert page["rows"][0]["detection_kind"] == "address_poisoning"
    status, _headers, raw = request(http_server, "/api/transfers" + window + "&q=bbbb&page_size=1&page=1")
    searched = json.loads(raw)
    assert searched["total"] == 1 and searched["rows"][0]["classification"] == "KNOWN_FRAUD"
    status, _headers, raw = request(http_server, "/api/transfers" + window + "&network=tron")
    assert json.loads(raw)["rows"] == []
    status, headers, raw = request(http_server, "/api/transfers?classification=DROP%20TABLE")
    assert status == 400 and "error" in json.loads(raw)
    assert "access-control-allow-origin" not in headers
    status, _headers, raw = request(http_server, "/api/transfers", method="POST")
    assert status == 405 and json.loads(raw)["error"]
    assert saved._connection.execute("SELECT COUNT(*) FROM report_transfers").fetchone()[0] == before
    saved.close()


def test_feedback_rate_is_not_invented_and_empty_database_is_safe(tmp_path, http_server):
    saved = store(tmp_path)
    when = START + timedelta(days=1)
    seed(saved, when=when, event="t1")
    for kind in ("CORRECT", "FALSE_POSITIVE", "UNSURE"):
        token = saved.register_alert(
            user_id="9911", network="ethereum", wallet_address=WALLET, classification="UNKNOWN",
            record={"network": "ethereum", "tx_hash": "0x" + "11" * 32, "event_key": f"fb-{kind}", "wallet": WALLET},
        )
        saved.set_feedback(token, "9911", kind)
    window = "?start=2026-10-05&end=2026-10-12"
    status, _headers, raw = request(http_server, "/api/feedback" + window)
    view = json.loads(raw)
    assert status == 200
    assert view["counts"] == {"CORRECT": 1, "FALSE_POSITIVE": 1, "UNSURE": 1}
    assert view["false_alarm"] == "Yeterli etiketli veri yok"
    assert view["labeled"] == 2
    assert "9911" not in raw.decode() and WALLET not in raw.decode()
    status, _headers, raw = request(http_server, "/api/statistics" + window)
    stats = json.loads(raw)
    assert sum(item["count"] for item in stats["daily_transfers"]) == 1
    assert stats["feedback"]["false_alarm"] == "Yeterli etiketli veri yok"
    status, headers, raw = request(http_server, "/")
    assert status == 200 and b"Overview" in raw
    assert "script-src 'self'" in headers["content-security-policy"]
    status, _headers, raw = request(http_server, "/css/%2e%2e/%2e%2e/config.py")
    assert status == 404
    saved.close()


def test_login_stage_assets_are_served_from_this_host_only(http_server):
    status, headers, raw = request(http_server, "/")
    page = raw.decode()
    assert status == 200
    assert "Tokenlarını" in page and "Güvende" in page and "Tut." in page
    assert 'id="gate-stage"' in page and 'id="gate-form"' in page
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "") and "https://" not in page
    status, headers, raw = request(http_server, "/vendor/three.module.min.js")
    assert status == 200 and headers["content-type"].startswith("text/javascript")
    assert headers["cache-control"] == "no-store" and len(raw) > 100_000
    assert Path("frontend/vendor/three.LICENSE").read_text(encoding="utf-8").startswith("The MIT License")
    orca = Path("frontend/js/orca.js").read_text(encoding="utf-8")
    assert "innerHTML" not in orca and "insertAdjacentHTML" not in orca
    assert 'from "../vendor/three.module.min.js"' in orca
    assert "MeshStandardMaterial" not in orca
    assert "buildOrcaCloud" in orca and "sampleSurface" in orca
    assert "THREE.Points" in orca
    assert "const CONFIG" in orca
    assert 'placeholder="admin"' not in page
    assert "gate-chips" not in page
    status, headers, raw = request(http_server, "/css/styles.css")
    css = raw.decode()
    assert status == 200 and "Bricolage Grotesque" in css and "JetBrains Mono" in css
    status, headers, raw = request(http_server, "/fonts/bricolage-grotesque-800-latin.woff2")
    assert status == 200 and headers["content-type"] == "font/woff2" and len(raw) > 1000
    status, headers, raw = request(http_server, "/fonts/jetbrains-mono-400-latin.woff2")
    assert status == 200 and headers["content-type"] == "font/woff2" and len(raw) > 1000


def test_login_is_off_until_both_credentials_exist(http_server):
    status, _headers, raw = request(http_server, "/api/session")
    assert status == 200
    assert json.loads(raw) == {"required": False, "authenticated": True}
    status, _headers, raw = request(http_server, "/api/login", method="POST", body=b"{}")
    assert status == 404


def test_login_gates_api_data_and_other_writes_stay_closed(http_server, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct-horse")
    status, headers, raw = request(http_server, "/")
    assert status == 200 and b"Overview" in raw and "Kullanıcı adı" in raw.decode()
    assert "script-src 'self'" in headers["content-security-policy"]
    status, _headers, raw = request(http_server, "/api/overview")
    assert status == 401 and json.loads(raw)["error"] == "Giriş gerekli."
    status, _headers, raw = request(http_server, "/api/ticker")
    ticker = json.loads(raw)
    assert status == 200
    assert "Tether USD" in ticker["coins"] and "Ethereum" in ticker["networks"]
    assert "0x" not in raw.decode() and "TR7NHqje" not in raw.decode()
    status, _headers, raw = request(http_server, "/api/session")
    assert json.loads(raw) == {"required": True, "authenticated": False}
    wrong = json.dumps({"username": "admin", "password": "nope"}).encode()
    status, _headers, raw = request(http_server, "/api/login", method="POST", body=wrong)
    assert status == 401 and b"nope" not in raw and b"correct-horse" not in raw
    good = json.dumps({"username": "admin", "password": "correct-horse"}).encode()
    status, headers, raw = request(http_server, "/api/login", method="POST", body=good)
    assert status == 200 and json.loads(raw) == {"ok": True}
    cookie = headers["set-cookie"]
    assert cookie.startswith("aft_session=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "secure" not in lowered
    assert b"correct-horse" not in raw and b"aft_session" not in raw
    token = cookie.split(";", 1)[0]
    status, _headers, raw = request(http_server, "/api/overview", headers={"Cookie": token})
    assert status == 200 and "transfer_count" in json.loads(raw)
    status, _headers, raw = request(http_server, "/api/transfers", method="POST", headers={"Cookie": token})
    assert status == 405 and json.loads(raw)["error"] == "Bu panel salt okunur."
    status, headers, raw = request(http_server, "/api/logout", method="POST", headers={"Cookie": token})
    assert status == 200 and "max-age=0" in headers["set-cookie"].lower()
    status, _headers, raw = request(http_server, "/api/overview", headers={"Cookie": token})
    assert status == 401


def test_missing_database_returns_notes_not_zeros(tmp_path):
    server = make_server(LOCAL_HOST, 0, paths(tmp_path / "yok"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, raw = request(server, "/api/overview")
        overview = json.loads(raw)
        assert status == 200
        assert overview["transfer_count"] is None
        assert overview["wallet_count"] is None
        assert any("bulunamadı" in note for note in overview["notes"])
        status, _headers, raw = request(server, "/api/risk?start=2026-10-05&end=2026-10-12")
        assert json.loads(raw)["available"] is False
        empty = tmp_path / "empty.sqlite"
        sqlite3.connect(empty).close()
        bare = make_server(LOCAL_HOST, 0, DataPaths(empty, tmp_path / "no-ops.sqlite", tmp_path / "no-wallets.json"))
        bare_thread = threading.Thread(target=bare.serve_forever, daemon=True)
        bare_thread.start()
        try:
            status, _headers, raw = request(bare, "/api/overview")
            assert json.loads(raw)["transfer_count"] is None
        finally:
            bare.shutdown()
            bare.server_close()
    finally:
        server.shutdown()
        server.server_close()
