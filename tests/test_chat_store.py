"""Chat transcripts keep sender names and stay free of secrets."""

import json

from reporting.chat_store import ChatStore, plain_text
from web_server import LOCAL_HOST, make_server
from reporting.dashboard_data import DataPaths

import threading
from urllib.request import urlopen, Request
from urllib.error import HTTPError


def test_transcript_keeps_sender_and_strips_markup(tmp_path):
    store = ChatStore(tmp_path / "chat_messages.sqlite")
    store.add(
        chat_id="42",
        message_id=1,
        direction="in",
        user_id="42",
        sender_name="Ada Lovelace",
        username="ada",
        body="hello",
        sent_at="2026-10-10T12:00:00+00:00",
    )
    store.add(
        chat_id="42",
        message_id=2,
        direction="out",
        sender_name="Bot",
        body="<b>token</b> 123456:SUPERSECRETTOKEN",
        sent_at="2026-10-10T12:00:02+00:00",
    )
    assert plain_text("<b>Ada</b>") == "Ada"
    chats = store.chats()
    assert chats[0]["title"] == "Ada Lovelace"
    assert chats[0]["username"] == "ada"
    messages = ChatStore(tmp_path / "chat_messages.sqlite", readonly=True).messages("42")
    assert messages[0]["sender_name"] == "Ada Lovelace"
    assert messages[1]["body"] == "token 123456:SUPERSECRETTOKEN"
    assert "<b>" not in messages[1]["body"]


def test_chat_api_rejects_a_bad_id(tmp_path):
    server = make_server(LOCAL_HOST, 0, DataPaths(tmp_path / "a.sqlite", tmp_path / "b.sqlite", tmp_path / "c.json"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(server, "/api/chats")
        assert status == 200
        payload = json.loads(body)
        assert payload["chats"] == [] and payload["messages"] == []
        status, body = _get(server, "/api/chats?chat=../.env")
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
