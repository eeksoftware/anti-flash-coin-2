"""Local transcript of Telegram messages the bot sends and receives.

Older chats cannot be pulled back from Telegram. This file only grows
as new messages pass through the running bot.
"""

from __future__ import annotations

import html
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


_TAG = re.compile(r"<[^>]+>")
_LOCK = threading.Lock()


def plain_text(value: str) -> str:
    text = _TAG.sub("", value or "")
    return html.unescape(text).strip()[:4000]


def chat_path(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from config import settings
        data_dir = Path(settings.data_dir)
        if not data_dir.is_absolute():
            data_dir = Path(__file__).resolve().parents[1] / data_dir
    return Path(data_dir) / "chat_messages.sqlite"


class ChatStore:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        chat_id TEXT NOT NULL,
                        message_id INTEGER NOT NULL,
                        direction TEXT NOT NULL,
                        user_id TEXT NOT NULL DEFAULT '',
                        sender_name TEXT NOT NULL DEFAULT '',
                        username TEXT NOT NULL DEFAULT '',
                        body TEXT NOT NULL,
                        sent_at TEXT NOT NULL,
                        PRIMARY KEY (chat_id, message_id, direction)
                    )
                    """
                )

    def add(
        self,
        *,
        chat_id: str,
        message_id: int,
        direction: str,
        user_id: str = "",
        sender_name: str = "",
        username: str = "",
        body: str,
        sent_at: str | None = None,
    ) -> None:
        if self.readonly or direction not in {"in", "out"} or not str(chat_id) or not isinstance(message_id, int):
            return
        text = plain_text(body)
        if not text:
            return
        moment = sent_at or datetime.now(timezone.utc).isoformat()
        with _LOCK:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO messages (
                        chat_id, message_id, direction, user_id, sender_name, username, body, sent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_id, direction) DO NOTHING
                    """,
                    (str(chat_id), message_id, direction, str(user_id or ""), sender_name[:120], username[:64], text, moment),
                )

    def revise(self, chat_id: str, message_id: int, body: str) -> None:
        text = plain_text(body)
        if self.readonly or not text or not isinstance(message_id, int):
            return
        with _LOCK:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE messages SET body=? WHERE chat_id=? AND message_id=? AND direction='out'",
                    (text, str(chat_id), message_id),
                )

    def chats(self) -> list[dict]:
        rows = self._rows(
            """
            SELECT chat_id, direction, user_id, sender_name, username, body, sent_at
            FROM messages
            ORDER BY sent_at DESC
            LIMIT 2000
            """
        )
        grouped: dict[str, dict] = {}
        for row in rows:
            chat_id = row["chat_id"]
            item = grouped.get(chat_id)
            if item is None:
                item = {
                    "chat_id": chat_id,
                    "title": "",
                    "username": "",
                    "user_id": "",
                    "last_text": row["body"],
                    "last_at": row["sent_at"],
                    "count": 0,
                }
                grouped[chat_id] = item
            item["count"] += 1
            if row["direction"] == "in" and row["sender_name"] and not item["title"]:
                item["title"] = row["sender_name"]
                item["username"] = row["username"]
                item["user_id"] = row["user_id"]
        for item in grouped.values():
            if not item["title"]:
                item["title"] = item["chat_id"]
        return sorted(grouped.values(), key=lambda item: item["last_at"], reverse=True)

    def messages(self, chat_id: str) -> list[dict]:
        if not chat_id:
            return []
        rows = self._rows(
            """
            SELECT chat_id, message_id, direction, user_id, sender_name, username, body, sent_at
            FROM messages
            WHERE chat_id=?
            ORDER BY sent_at ASC, message_id ASC
            LIMIT 400
            """,
            (str(chat_id),),
        )
        return [
            {
                "chat_id": row["chat_id"],
                "message_id": row["message_id"],
                "direction": row["direction"],
                "user_id": row["user_id"],
                "sender_name": row["sender_name"] or ("Bot" if row["direction"] == "out" else ""),
                "username": row["username"],
                "body": row["body"],
                "sent_at": row["sent_at"],
            }
            for row in rows
        ]

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        connection = self._connect()
        if connection is None:
            return []
        try:
            connection.row_factory = sqlite3.Row
            return list(connection.execute(sql, params))
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection | None:
        if self.readonly and not self.path.is_file():
            return None
        if self.readonly:
            connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            connection.execute("PRAGMA query_only=ON")
            return connection
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
