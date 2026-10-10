"""Process health, operational counters, and durable run sessions."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


GREEN, YELLOW, RED = "🟢", "🟡", "🔴"
WARNING_CYCLE_MULTIPLIER = 2
CRITICAL_STALE_SECONDS = 300
LOW_DISK_WARNING_RATIO = 0.15
LOW_DISK_CRITICAL_RATIO = 0.05


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class HealthState:
    started_at: datetime
    last_cycle_at: datetime | None = None
    cycle_count: int = 0
    last_error: str | None = None
    effective_interval: float | None = None
    last_api_success_at: datetime | None = None
    last_telegram_success_at: datetime | None = None

    def __post_init__(self):
        self._started_monotonic = time.monotonic()

    @classmethod
    def now(cls): return cls(started_at=utc_now())

    def record_cycle(self, effective_interval: float, error: str | None = None):
        self.cycle_count += 1
        self.last_cycle_at = utc_now()
        self.effective_interval = effective_interval
        if error is not None:
            self.last_error = error

    def as_dict(self):
        result = asdict(self)
        for key, value in result.items():
            if isinstance(value, datetime): result[key] = value.isoformat()
        result.pop("_started_monotonic", None)
        result["uptime_seconds"] = max(0, time.monotonic() - self._started_monotonic)
        return result


def health_level(state: HealthState, *, active_keys: int, cooldown_keys: int = 0,
                 exhausted_keys: int = 0, now: datetime | None = None,
                 disk_free_ratio: float | None = None, measurements_ok: bool = True) -> str:
    now = now or utc_now()
    if active_keys == 0:
        return RED
    cycle_time = state.last_cycle_at or state.started_at
    age = max(0.0, (now - cycle_time).total_seconds())
    if age > CRITICAL_STALE_SECONDS:
        return RED
    if disk_free_ratio is not None and disk_free_ratio < LOW_DISK_CRITICAL_RATIO:
        return RED
    if not measurements_ok or cooldown_keys or exhausted_keys:
        return YELLOW
    if disk_free_ratio is not None and disk_free_ratio < LOW_DISK_WARNING_RATIO:
        return YELLOW
    if state.effective_interval:
        if age > state.effective_interval * WARNING_CYCLE_MULTIPLIER:
            return YELLOW
    return GREEN


class OperationsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init(self):
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS stats_daily (day TEXT NOT NULL, metric TEXT NOT NULL, key TEXT NOT NULL DEFAULT '', value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, metric, key))")
            db.execute("CREATE TABLE IF NOT EXISTS stats_events (ts TEXT NOT NULL, metric TEXT NOT NULL, key TEXT NOT NULL DEFAULT '', value INTEGER NOT NULL DEFAULT 1)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_stats_events_ts ON stats_events(ts)")
            db.execute("CREATE TABLE IF NOT EXISTS stats_counted_events (event_id TEXT PRIMARY KEY, ts TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS run_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts TEXT NOT NULL, end_ts TEXT, bot_version TEXT NOT NULL, unexpected_counted INTEGER NOT NULL DEFAULT 0)")
        try: os.chmod(self.path, 0o600)
        except OSError: pass

    def increment(self, metric: str, key: str = "", value: int = 1, *, at: datetime | None = None):
        self.increment_many([(metric, key, value)], at=at)

    def increment_many(self, metrics, *, at: datetime | None = None):
        at = at or utc_now()
        timestamp, day = at.astimezone(timezone.utc).isoformat(), at.astimezone(timezone.utc).date().isoformat()
        try:
            with self._db() as db:
                for metric, key, value in metrics:
                    db.execute("INSERT INTO stats_daily(day,metric,key,value) VALUES(?,?,?,?) ON CONFLICT(day,metric,key) DO UPDATE SET value=value+excluded.value", (day, metric, key, value))
                    db.execute("INSERT INTO stats_events(ts,metric,key,value) VALUES(?,?,?,?)", (timestamp, metric, key, value))
                cutoff = datetime.fromtimestamp(at.astimezone(timezone.utc).timestamp()-8*86400, timezone.utc).isoformat()
                db.execute("DELETE FROM stats_events WHERE ts < ?", (cutoff,))
        except sqlite3.Error:
            logger.exception("Operasyon sayacı SQLite'a yazılamadı.")

    def increment_many_once(self, event_id: str, metrics, *, at: datetime | None = None):
        at = at or utc_now()
        timestamp = at.astimezone(timezone.utc).isoformat()
        day = at.astimezone(timezone.utc).date().isoformat()
        try:
            with self._db() as db:
                cursor = db.execute("INSERT OR IGNORE INTO stats_counted_events(event_id,ts) VALUES(?,?)", (event_id, timestamp))
                if cursor.rowcount == 0:
                    return False
                for metric, key, value in metrics:
                    db.execute("INSERT INTO stats_daily(day,metric,key,value) VALUES(?,?,?,?) ON CONFLICT(day,metric,key) DO UPDATE SET value=value+excluded.value", (day, metric, key, value))
                    db.execute("INSERT INTO stats_events(ts,metric,key,value) VALUES(?,?,?,?)", (timestamp, metric, key, value))
            return True
        except sqlite3.Error:
            logger.exception("Tekil olay sayacı SQLite'a yazılamadı.")
            return False

    def stats(self, *, now: datetime | None = None):
        now = (now or utc_now()).astimezone(timezone.utc)
        today = now.date().isoformat()
        since = datetime.fromtimestamp(now.timestamp()-86400, timezone.utc).isoformat()
        with self._db() as db:
            daily = db.execute("SELECT metric,key,value FROM stats_daily WHERE day=?", (today,)).fetchall()
            rolling = db.execute("SELECT metric,key,SUM(value) FROM stats_events WHERE ts>=? AND ts<=? GROUP BY metric,key", (since, now.isoformat())).fetchall()
        return ({(m,k):v for m,k,v in daily}, {(m,k):v for m,k,v in rolling})

    def start_session(self, version: str):
        now = utc_now().isoformat()
        with self._db() as db:
            stale = db.execute("SELECT id FROM run_sessions WHERE end_ts IS NULL AND unexpected_counted=0").fetchall()
            db.execute("UPDATE run_sessions SET unexpected_counted=1 WHERE end_ts IS NULL AND unexpected_counted=0")
            cursor = db.execute("INSERT INTO run_sessions(start_ts,bot_version) VALUES(?,?)", (now, version))
            return cursor.lastrowid, len(stale)

    def end_session(self, session_id: int):
        with self._db() as db:
            db.execute("UPDATE run_sessions SET end_ts=? WHERE id=? AND end_ts IS NULL", (utc_now().isoformat(), session_id))

    def unexpected_restarts_7d(self, *, now: datetime | None = None) -> int:
        since = (now or utc_now()).astimezone(timezone.utc).timestamp()-7*86400
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM run_sessions WHERE unexpected_counted=1 AND start_ts>=?", (datetime.fromtimestamp(since, timezone.utc).isoformat(),)).fetchone()[0]


def write_atomic_json(path: str | Path, payload: dict):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
        try: os.chmod(path, 0o600)
        except OSError: pass
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def write_heartbeat(data_dir: str | Path, state: HealthState, level: str = "N/A"):
    now = utc_now()
    write_atomic_json(Path(data_dir) / "heartbeat", {"timestamp": now.isoformat(), "status": level, "health": state.as_dict()})
