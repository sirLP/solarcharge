"""
SENEC & charging time-series store — SQLite-backed.

Every poll cycle the controller calls :meth:`TimeseriesStore.add`, which
inserts one row into the ``timeseries`` table of ``solar_charge.db``.

Data is retained for ``RETAIN_DAYS`` days (default 30); rows older than that
are pruned on startup and once a day while the daemon is running.

Queryable via ``/api/timeseries``; thin-out is done in SQL using the
``ROW_NUMBER()`` window function (SQLite ≥ 3.25, available since Python 3.6).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solar_charge.db import SolarDB

log = logging.getLogger(__name__)

RETAIN_DAYS = 365

# All numeric fields that can be plotted (order matters – used in the UI)
PLOTTABLE_FIELDS: list[tuple[str, str, str]] = [
    # (field_name,        label,                 unit)
    ("solar_w",          "Solar PV",            "W"),
    ("house_w",          "House load",          "W"),
    ("grid_w",           "Grid (+ import)",     "W"),
    ("battery_w",        "Battery (+ charge)",  "W"),
    ("battery_soc_pct",  "Battery SoC",         "%"),
    ("surplus_w",        "EV surplus",          "W"),
    ("setpoint_a",       "Charge setpoint",     "A"),
    ("ev_w",             "EV charging",         "W"),
]

_FIELD_NAMES = [f for f, _, _ in PLOTTABLE_FIELDS]


class TimeseriesStore:
    """
    SQLite-backed time-series store.

    All data survives process restarts up to RETAIN_DAYS days.
    """

    def __init__(self, db: "SolarDB") -> None:
        self._db = db
        self._trim()

    # ------------------------------------------------------------------ #
    #  Write                                                               #
    # ------------------------------------------------------------------ #

    def add(
        self,
        *,
        solar_w: float,
        house_w: float,
        grid_w: float,
        battery_w: float,
        battery_soc_pct: float,
        surplus_w: float,
        setpoint_a: float,
        charging: bool,
        ev_w: float = 0.0,
    ) -> None:
        ts = datetime.now(tz=timezone.utc).isoformat()
        self._db.con.execute(
            """
            INSERT INTO timeseries
                (ts, solar_w, house_w, grid_w, battery_w,
                 battery_soc_pct, surplus_w, setpoint_a, charging, ev_w)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ts,
                round(solar_w, 1),
                round(house_w, 1),
                round(grid_w, 1),
                round(battery_w, 1),
                round(battery_soc_pct, 1),
                round(surplus_w, 1),
                round(setpoint_a, 2),
                1 if charging else 0,
                round(ev_w, 1),
            ),
        )
        self._db.con.commit()

    def _trim(self) -> None:
        """Delete rows older than RETAIN_DAYS."""
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=RETAIN_DAYS)).isoformat()
        cur = self._db.con.execute(
            "DELETE FROM timeseries WHERE ts < ?", (cutoff,)
        )
        self._db.con.commit()
        if cur.rowcount:
            log.info("Timeseries: pruned %d rows older than %d days", cur.rowcount, RETAIN_DAYS)

    # ------------------------------------------------------------------ #
    #  Query                                                               #
    # ------------------------------------------------------------------ #

    def query(
        self,
        *,
        since: datetime | None = None,
        fields: list[str] | None = None,
        max_points: int = 2000,
    ) -> dict:
        """
        Return a dict suitable for JSON serialisation.

        Parameters
        ----------
        since:
            Return only samples newer than this UTC datetime.
        fields:
            Subset of PLOTTABLE_FIELDS names to include; all when *None*.
        max_points:
            Cap on returned points; evenly-spaced thin-out via SQL ROW_NUMBER.

        Returns
        -------
        {"timestamps": [...], "fields": {"solar_w": [...], ...}}
        """
        valid = set(_FIELD_NAMES)
        want = [f for f in (fields or _FIELD_NAMES) if f in valid]

        since_ts = since.isoformat() if since else "1970-01-01T00:00:00"

        # Count matching rows
        (n,) = self._db.con.execute(
            "SELECT COUNT(*) FROM timeseries WHERE ts >= ?", (since_ts,)
        ).fetchone()

        if n == 0:
            return {"timestamps": [], "fields": {f: [] for f in want}}

        if n <= max_points or max_points <= 0:
            rows = self._db.con.execute(
                "SELECT ts," + ",".join(_FIELD_NAMES) + " FROM timeseries WHERE ts >= ? ORDER BY ts",
                (since_ts,),
            ).fetchall()
        else:
            step = max(1, round(n / max_points))
            rows = self._db.con.execute(
                f"""
                SELECT ts,{','.join(_FIELD_NAMES)} FROM (
                    SELECT ts,{','.join(_FIELD_NAMES)},
                           ROW_NUMBER() OVER (ORDER BY id) rn
                    FROM timeseries WHERE ts >= ?
                ) WHERE (rn - 1) % ? = 0
                LIMIT ?
                """,
                (since_ts, step, max_points),
            ).fetchall()

        timestamps = [r["ts"] for r in rows]
        result_fields: dict[str, list] = {
            f: [r[f] for r in rows]
            for f in want
        }
        return {"timestamps": timestamps, "fields": result_fields}

    def query_grouped(
        self,
        *,
        group_by: str,
        since: datetime | None = None,
        fields: list[str] | None = None,
    ) -> dict:
        """
        Return averaged data bucketed by day or week.

        Parameters
        ----------
        group_by:
            ``"day"``  — one bar per calendar day (YYYY-MM-DD).
            ``"week"`` — one bar per ISO week  (YYYY-WNN).
        since:
            Only include rows newer than this UTC datetime.
        fields:
            Subset of plottable field names; all when *None*.

        Returns
        -------
        Same ``{"timestamps": [...], "fields": {...}}`` shape as :meth:`query`.
        """
        if group_by not in ("day", "week"):
            return self.query(since=since, fields=fields)

        valid = set(_FIELD_NAMES)
        want = [f for f in (fields or _FIELD_NAMES) if f in valid]
        since_ts = since.isoformat() if since else "1970-01-01T00:00:00"

        if group_by == "day":
            bucket_expr = "strftime('%Y-%m-%dT00:00:00', ts)"
            group_expr  = "strftime('%Y-%m-%d', ts)"
        else:  # week
            bucket_expr = "strftime('%Y-W%W', ts)"
            group_expr  = "strftime('%Y-W%W', ts)"

        avg_cols = ", ".join(f"AVG({f}) as {f}" for f in _FIELD_NAMES)
        rows = self._db.con.execute(
            f"""
            SELECT {bucket_expr} as ts, {avg_cols}
            FROM timeseries
            WHERE ts >= ?
            GROUP BY {group_expr}
            ORDER BY ts
            """,
            (since_ts,),
        ).fetchall()

        timestamps = [r["ts"] for r in rows]
        result_fields: dict[str, list] = {
            f: [round(r[f], 1) if r[f] is not None else 0.0 for r in rows]
            for f in want
        }
        return {"timestamps": timestamps, "fields": result_fields}

    # ------------------------------------------------------------------ #
    #  Metadata                                                            #
    # ------------------------------------------------------------------ #

    @property
    def count(self) -> int:
        (n,) = self._db.con.execute("SELECT COUNT(*) FROM timeseries").fetchone()
        return n

    @property
    def field_meta(self) -> list[dict]:
        """Metadata list consumed by the chart UI."""
        return [
            {"key": k, "label": lbl, "unit": unit}
            for k, lbl, unit in PLOTTABLE_FIELDS
        ]
