"""
SQLite persistence layer shared by TimeseriesStore and HistoryStore.

A single database file (``solar_charge.db``) sits next to ``config.toml``
and holds two tables:

* ``timeseries``  — one row per poll cycle, kept for RETAIN_DAYS days.
* ``sessions``    — one row per EV charging session (completed or in-progress).

WAL journal mode is used for better read/write concurrency and faster commits.
PRAGMA synchronous=NORMAL is safe for our use-case (power loss may drop the
last un-checkpointed write, i.e. a few seconds of timeseries data).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS timeseries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    solar_w         REAL    NOT NULL DEFAULT 0,
    house_w         REAL    NOT NULL DEFAULT 0,
    grid_w          REAL    NOT NULL DEFAULT 0,
    battery_w       REAL    NOT NULL DEFAULT 0,
    battery_soc_pct REAL    NOT NULL DEFAULT 0,
    surplus_w       REAL    NOT NULL DEFAULT 0,
    setpoint_a      REAL    NOT NULL DEFAULT 0,
    charging        INTEGER NOT NULL DEFAULT 0,
    ev_w            REAL    NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ts ON timeseries(ts);

CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,                   -- NULL = session still active
    duration_s          REAL,
    energy_kwh          REAL DEFAULT 0.0,
    avg_power_w         REAL DEFAULT 0.0,
    peak_power_w        REAL DEFAULT 0.0,
    rfid_tag            TEXT DEFAULT '',
    solar_kwh           REAL DEFAULT 0.0,
    solar_fraction_pct  REAL DEFAULT 0.0,
    start_energy_wh     REAL                    -- Alfen meter reading at start (internal)
);
"""


class SolarDB:
    """
    Thin sqlite3 wrapper.

    Thread-safety: ``check_same_thread=False`` is safe here because all writes
    come from the single asyncio event-loop thread; the only other accesses are
    read-only FastAPI handler coroutines which run in that same thread.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(_SCHEMA)
        self._migrate()
        self._con.commit()
        log.info("Database opened: %s", path)

    def _migrate(self) -> None:
        """Apply incremental schema migrations for existing databases."""
        existing = {
            row[1]
            for row in self._con.execute("PRAGMA table_info(timeseries)")
        }
        if "ev_w" not in existing:
            self._con.execute(
                "ALTER TABLE timeseries ADD COLUMN ev_w REAL NOT NULL DEFAULT 0"
            )
            log.info("Database migrated: added ev_w column to timeseries")

    @property
    def con(self) -> sqlite3.Connection:
        return self._con

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # noqa: BLE001
            pass
