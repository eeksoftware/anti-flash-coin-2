"""Local process health probe based only on persisted heartbeat data."""
import json
import os
import sqlite3
from contextlib import closing
import sys
from datetime import datetime, timezone
from pathlib import Path

from health import RED


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    try:
        from dotenv import load_dotenv
        load_dotenv(project_root / ".env")
    except ImportError:
        pass
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    if not data_dir.is_absolute(): data_dir = project_root / data_dir
    heartbeat = data_dir / "heartbeat"
    try:
        payload = json.loads(heartbeat.read_text(encoding="utf-8"))
        timestamp = datetime.fromisoformat(payload["timestamp"])
        if timestamp.tzinfo is None: timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc)-timestamp.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age >= 120:
            print("healthcheck failed: heartbeat is stale")
            return 1
        if not isinstance(payload.get("health"), dict) or payload.get("status") not in {"🟢", "🟡", RED}:
            print("healthcheck failed: health state is missing or invalid")
            return 1
        if payload.get("status") == RED:
            print("healthcheck failed: bot health is critical")
            return 1
        ops_db = data_dir / "operations.sqlite"
        if ops_db.exists():
            with closing(sqlite3.connect(ops_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    print("healthcheck failed: operations database is invalid")
                    return 1
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"stats_daily", "run_sessions"}.issubset(tables):
                    print("healthcheck failed: operations database schema is missing")
                    return 1
        print("healthcheck ok")
        return 0
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, json.JSONDecodeError):
        print("healthcheck failed: heartbeat is missing or invalid")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
