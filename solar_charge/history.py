"""
Charging session history — SQLite-backed.

Lifecycle
---------
1. ``start_session()``   — INSERT incomplete row (ended_at IS NULL).
2. ``sample()``          — accumulate in memory (power / solar).
3. ``end_session()``     — UPDATE row with final statistics.

Active sessions survive restarts: the incomplete row is restored from the
database on startup so that ``started_at`` and ``rfid_tag`` are preserved.
(Power samples can't be recovered, so energy falls back to Alfen meter
delta when the session eventually ends.)

Migration: if a ``history.json`` file is present beside the database on
first startup, its records are imported automatically and the file is
renamed to ``history.json.migrated``.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solar_charge.db import SolarDB

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ChargingSession:
    """One completed EV charging session."""
    session_id: str
    started_at: str
    ended_at: str
    duration_s: float
    energy_kwh: float
    avg_power_w: float
    peak_power_w: float
    rfid_tag: str
    solar_kwh: float
    solar_fraction_pct: float


# ─────────────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────────────

class HistoryStore:
    """
    Session store backed by SQLite.

    Completed sessions persist across restarts.  Active sessions are also
    written to the DB (as incomplete rows) so start time and RFID survive
    a restart.
    """

    def __init__(self, db: "SolarDB", json_path: Path | None = None) -> None:
        """
        Parameters
        ----------
        db:
            Shared :class:`~solar_charge.db.SolarDB` instance.
        json_path:
            Optional path to a legacy ``history.json`` to migrate on first run.
        """
        self._db = db
        self._active: dict | None = None

        if json_path is not None:
            self._migrate_json(json_path)

        self._restore_active()

    # ------------------------------------------------------------------ #
    #  Migration from history.json                                         #
    # ------------------------------------------------------------------ #

    def _migrate_json(self, path: Path) -> None:
        """Import records from legacy history.json if it still exists."""
        if not path.exists():
            return
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                return
            inserted = 0
            for r in records:
                try:
                    self._db.con.execute(
                        """
                        INSERT OR IGNORE INTO sessions
                            (session_id, started_at, ended_at, duration_s,
                             energy_kwh, avg_power_w, peak_power_w,
                             rfid_tag, solar_kwh, solar_fraction_pct)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            r.get("session_id", str(uuid.uuid4())),
                            r.get("started_at", ""),
                            r.get("ended_at", ""),
                            r.get("duration_s", 0.0),
                            r.get("energy_kwh", 0.0),
                            r.get("avg_power_w", 0.0),
                            r.get("peak_power_w", 0.0),
                            r.get("rfid_tag", ""),
                            r.get("solar_kwh", 0.0),
                            r.get("solar_fraction_pct", 0.0),
                        ),
                    )
                    inserted += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("Migration: skipped a record: %s", exc)
            self._db.con.commit()
            log.info("Migrated %d sessions from %s", inserted, path)
            path.rename(path.with_suffix(".json.migrated"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot migrate %s: %s", path, exc)

    # ------------------------------------------------------------------ #
    #  Active-session recovery                                             #
    # ------------------------------------------------------------------ #

    def _restore_active(self) -> None:
        """Restore any incomplete session row left by a previous crash/restart."""
        row = self._db.con.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return
        self._active = {
            "session_id": row["session_id"],
            "started_at": row["started_at"],
            "rfid_tag": row["rfid_tag"] or "",
            "_start_energy_wh": row["start_energy_wh"],
            "_power_samples": [],   # can't recover mid-session samples
            "_solar_kwh_acc": 0.0,
        }
        log.info(
            "History: restored in-progress session %s (started %s)",
            self._active["session_id"][:8],
            self._active["started_at"],
        )

    # ------------------------------------------------------------------ #
    #  Session lifecycle                                                   #
    # ------------------------------------------------------------------ #

    def start_session(self, rfid_tag: str = "") -> None:
        """Record the beginning of a new charging session."""
        if self._active is not None:
            log.warning("History: start_session called while a session is already active — closing it")
            self.end_session(end_energy_wh=None)

        session_id = str(uuid.uuid4())
        started_at = datetime.now().isoformat()
        self._active = {
            "session_id": session_id,
            "started_at": started_at,
            "rfid_tag": rfid_tag,
            "_start_energy_wh": None,
            "_power_samples": [],
            "_solar_kwh_acc": 0.0,
        }
        # Persist incomplete row so the session survives a restart
        self._db.con.execute(
            """
            INSERT INTO sessions (session_id, started_at, rfid_tag)
            VALUES (?, ?, ?)
            """,
            (session_id, started_at, rfid_tag),
        )
        self._db.con.commit()
        log.info(
            "History: session %s started%s",
            session_id[:8],
            f" RFID={rfid_tag}" if rfid_tag else "",
        )

    def set_start_energy(self, energy_wh: float | None) -> None:
        """Store the Alfen lifetime-energy-meter reading at session start."""
        if self._active is None or energy_wh is None:
            return
        self._active["_start_energy_wh"] = energy_wh
        self._db.con.execute(
            "UPDATE sessions SET start_energy_wh = ? WHERE session_id = ?",
            (energy_wh, self._active["session_id"]),
        )
        self._db.con.commit()

    def sample(self, power_w: float, solar_w: float, poll_interval_s: float) -> None:
        """
        Record one power sample during an active session.

        power_w:
            Current EV charging power (W).
        solar_w:
            Current PV generation (W).
        poll_interval_s:
            Controller poll interval in seconds (used to integrate energy).
        """
        if self._active is None:
            return
        self._active["_power_samples"].append(max(0.0, power_w))
        self._active["_solar_kwh_acc"] += solar_w * poll_interval_s / 3_600_000.0

    def end_session(self, end_energy_wh: float | None) -> None:
        """
        Finalise the active session, compute statistics, and persist to DB.

        end_energy_wh:
            Alfen lifetime-energy-meter reading at session end (Wh), or None.
        """
        if self._active is None:
            log.debug("History: end_session called but no active session")
            return

        a = self._active
        now = datetime.now()
        started = datetime.fromisoformat(a["started_at"])
        duration_s = (now - started).total_seconds()

        samples: list[float] = a["_power_samples"]
        avg_power_w = sum(samples) / len(samples) if samples else 0.0
        peak_power_w = max(samples) if samples else 0.0

        start_e: float | None = a["_start_energy_wh"]
        if start_e is not None and end_energy_wh is not None and end_energy_wh >= start_e:
            energy_kwh = (end_energy_wh - start_e) / 1000.0
        else:
            energy_kwh = avg_power_w * duration_s / 3_600_000.0

        solar_kwh: float = a["_solar_kwh_acc"]
        solar_frac = round(solar_kwh / energy_kwh * 100, 1) if energy_kwh > 0 else 0.0

        self._db.con.execute(
            """
            UPDATE sessions SET
                ended_at           = ?,
                duration_s         = ?,
                energy_kwh         = ?,
                avg_power_w        = ?,
                peak_power_w       = ?,
                solar_kwh          = ?,
                solar_fraction_pct = ?
            WHERE session_id = ?
            """,
            (
                now.isoformat(),
                round(duration_s, 1),
                round(energy_kwh, 3),
                round(avg_power_w, 1),
                round(peak_power_w, 1),
                round(solar_kwh, 3),
                solar_frac,
                a["session_id"],
            ),
        )
        self._db.con.commit()
        self._active = None

        log.info(
            "History: session ended — %.2f kWh in %s (solar %.0f%%)",
            energy_kwh,
            _fmt_duration(duration_s),
            solar_frac,
        )

    # ------------------------------------------------------------------ #
    #  Queries                                                             #
    # ------------------------------------------------------------------ #

    def get_sessions(self, limit: int = 200) -> list[dict]:
        """Return completed sessions newest-first, capped at *limit*."""
        rows = self._db.con.execute(
            """
            SELECT session_id, started_at, ended_at, duration_s,
                   energy_kwh, avg_power_w, peak_power_w,
                   rfid_tag, solar_kwh, solar_fraction_pct
            FROM sessions
            WHERE ended_at IS NOT NULL
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    @property
    def active_session(self) -> dict | None:
        """The in-progress session snapshot (without internal accumulators), or None."""
        if self._active is None:
            return None
        return {k: v for k, v in self._active.items() if not k.startswith("_")}

    @property
    def current_session_kwh(self) -> float:
        """
        Estimated kWh delivered in the currently active session.
        Returns 0.0 when no session is active.
        """
        if self._active is None:
            return 0.0
        a = self._active
        samples: list[float] = a.get("_power_samples", [])
        if not samples:
            return 0.0
        started = datetime.fromisoformat(a["started_at"])
        duration_s = (datetime.now() - started).total_seconds()
        avg_w = sum(samples) / len(samples)
        return round(avg_w * duration_s / 3_600_000.0, 3)

    @property
    def total_sessions(self) -> int:
        (n,) = self._db.con.execute(
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NOT NULL"
        ).fetchone()
        return n


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    """Format seconds as Xh Ym Zs."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"
