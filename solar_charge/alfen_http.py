"""
Alfen Eve Single Pro-line local HTTPS API client.

Uses the same REST API as the official MyEve / Eve Connect mobile app.
The API endpoint is ``https://{host}/api/{action}``.

Authentication is *connection-based* (not cookie-based): a single TCP/TLS
connection can hold one authenticated session.  Only **one session** is
allowed at a time — while SolarCharge is connected, the MyEve app and any
Home-Assistant integration will be locked out.  ``close()`` releases the
session so other clients can log in afterwards.

No external dependencies — stdlib ``http.client`` + ``ssl`` only.

API parameter IDs used
----------------------
  2501_2   Socket-1 status code  (int)       → ChargeStatus
  2221_16  Active power total    (float, W)  → charging wattage
  2129_0   Max charge current    (float, A)  → read + write setpoint
  2221_22  Meter reading         (float, Wh) → lifetime energy

Implementation notes
--------------------
* Three consecutive GETs are issued per poll cycle.  ``http.client`` keeps
  the TCP connection alive between them (HTTP/1.1 keep-alive) so only the
  first request of each login requires a TCP/TLS handshake.
* After a successful POST (set_current), the Alfen firmware *may* close the
  TCP connection.  We close it ourselves and clear ``self._conn`` so that the
  next poll cycle triggers a fresh login — this is intentional.
* On any network exception during a read we retry once after re-logging in.
"""

from __future__ import annotations

import http.client
import json
import logging
import re as _re
import ssl
from datetime import datetime as _datetime
from typing import Union

from solar_charge.alfen import AlfenState, ChargeStatus

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transaction log parsing
# ---------------------------------------------------------------------------
_RE_TXRECORD = _re.compile(
    r'(\d+)_(txstart2|txstop2): id (0x[0-9a-fA-F]+|Unknown), socket (\d+), '
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([\d.]+)kWh (\S+)'
)


def _parse_transactions(raw: str) -> list[dict]:
    """
    Parse the raw ``/api/transactions`` log text into a list of session dicts.

    Each dict contains:
      id, started_at, ended_at, duration_s, energy_kwh,
      start_meter_kwh, stop_meter_kwh, rfid_tag, status.
    Sessions are returned in log order (oldest first).
    """
    events: list[dict] = []
    for m in _RE_TXRECORD.finditer(raw):
        offset, evt_type, _tx_id, _socket, ts_str, energy_kwh, rfid = m.groups()
        rfid_clean = "" if rfid in ("Unknown", "0x0000000000000000") else rfid
        events.append({
            "offset":     int(offset),
            "type":       evt_type,
            "ts_str":     ts_str,
            "energy_kwh": float(energy_kwh),
            "rfid":       rfid_clean,
        })

    events.sort(key=lambda e: e["offset"])

    # Deduplicate — the same record can appear at the boundary of two pages
    seen: set[int] = set()
    unique: list[dict] = []
    for e in events:
        if e["offset"] not in seen:
            seen.add(e["offset"])
            unique.append(e)
    events = unique

    sessions: list[dict] = []
    open_start: dict | None = None

    for evt in events:
        if evt["type"] == "txstart2":
            open_start = evt

        elif evt["type"] == "txstop2":
            if open_start is None:
                continue
            start = open_start
            open_start = None

            duration_s: float | None = None
            try:
                ts_a = _datetime.fromisoformat(start["ts_str"])
                ts_b = _datetime.fromisoformat(evt["ts_str"])
                duration_s = round((ts_b - ts_a).total_seconds())
            except (ValueError, OverflowError):
                pass

            energy_delta = round(evt["energy_kwh"] - start["energy_kwh"], 3)
            sessions.append({
                "id":              len(sessions) + 1,
                "started_at":      start["ts_str"],
                "ended_at":        evt["ts_str"],
                "duration_s":      duration_s,
                "energy_kwh":      max(0.0, energy_delta),
                "start_meter_kwh": start["energy_kwh"],
                "stop_meter_kwh":  evt["energy_kwh"],
                "rfid_tag":        evt["rfid"] or start["rfid"],
                "status":          "completed",
            })

    # Unmatched start = currently in-progress session
    if open_start is not None:
        sessions.append({
            "id":              len(sessions) + 1,
            "started_at":      open_start["ts_str"],
            "ended_at":        None,
            "duration_s":      None,
            "energy_kwh":      None,
            "start_meter_kwh": open_start["energy_kwh"],
            "stop_meter_kwh":  None,
            "rfid_tag":        open_start["rfid"],
            "status":          "in_progress",
        })

    return sessions

# ---------------------------------------------------------------------------
# Alfen API parameter IDs
# ---------------------------------------------------------------------------
_PARAM_STATUS    = "2501_2"    # Socket-1 status code (int)
_PARAM_POWER_W   = "2221_16"   # Active power total socket 1 (float, W)
_PARAM_MAX_A     = "2129_0"    # Max charge current setpoint (float, A) — R/W
_PARAM_ENERGY_WH = "2221_22"   # Meter reading socket 1 (float, stored as Wh)

# ---------------------------------------------------------------------------
# Status-code → ChargeStatus mapping
# ---------------------------------------------------------------------------
# Derived from the leeyuentuen/alfen_wallbox HA integration STATUS_DICT.
_CHARGING_STATUSES: frozenset[int] = frozenset({
    11,  # Charging Normal
    12,  # Charging Simplified
    13,  # Suspended Over-Current (limited but still active session)
    40,  # Charging Non Charging
    41,  # Solar Charging
    43,  # Partial Solar Charging
})

_CONNECTED_STATUSES: frozenset[int] = frozenset({
    5, 6, 7, 8, 9, 10,       # Authorizing → EV Connected → Preparing → Waiting
    14, 15, 16, 17,           # Suspended states / finish-wait
    33, 35, 36, 38, 39, 42,  # Reserved / load-balancing / waiting for power
})

# Human-readable names for diagnostics display
_STATUS_NAMES: dict[int, str] = {
    0: "Unknown", 1: "Off", 2: "Booting", 3: "Check Mains", 4: "Available",
    5: "Authorizing", 6: "Authorized", 7: "Cable Connected", 8: "EV Connected",
    9: "Preparing Charging", 10: "Wait Vehicle Charging", 11: "Charging Normal",
    12: "Charging Simplified", 13: "Suspended Over-Current",
    14: "Suspended HF Switching", 15: "Suspended EV Disconnected",
    16: "Finish Wait Vehicle", 17: "Finish Wait Disconnect",
    18: "Error PE", 19: "Error Power Failure", 20: "Error Contactor Fault",
    21: "Error Charging", 22: "Error Power Failure", 23: "Error Temperature",
    24: "Error Illegal CP", 25: "Error Illegal PP", 26: "Error Too Many Restarts",
    27: "Error", 28: "Error Message", 29: "Error Not Authorised",
    30: "Error Cable Not Supported", 31: "Error S2 Not Opened",
    32: "Error Time-Out", 33: "Reserved", 34: "In Operative",
    35: "Load Balancing Limited", 36: "Load Balancing Forced Off",
    38: "Not Charging", 39: "Solar Charging Wait", 40: "Charging Non Charging",
    41: "Solar Charging", 42: "Waiting For Power", 43: "Partial Solar Charging",
}


def _decode_status(code: int) -> tuple[str, ChargeStatus]:
    """Map an Alfen status integer to (str_label, ChargeStatus)."""
    label = _STATUS_NAMES.get(code, str(code))
    if code in _CHARGING_STATUSES:
        return label, ChargeStatus.CHARGING
    if code in _CONNECTED_STATUSES:
        return label, ChargeStatus.CONNECTED
    if 1 <= code <= 4 or code == 34:
        return label, ChargeStatus.NO_VEHICLE
    if code >= 18:
        return label, ChargeStatus.FAULT
    return label, ChargeStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class AlfenHTTPClient:
    """
    Synchronous HTTPS REST client for the Alfen Eve wallbox — drop-in
    replacement for :class:`~solar_charge.alfen.AlfenClient` (Modbus).

    Designed to be called from :func:`asyncio.run_in_executor` in the
    controller loop, so all methods are blocking.

    Parameters
    ----------
    host:
        IP address or hostname of the Alfen wallbox (e.g. ``192.168.1.50``).
    username:
        Login username.  Usually ``"admin"`` for local access.
    password:
        Login password (shown in the MyEve / Eve Connect app under Installer
        settings, or provided by the wallbox installer).

    Notes
    -----
    While an authenticated session is held, the MyEve app will be unable to
    connect.  Always call :meth:`close` when shutting down SolarCharge so the
    app can reconnect.
    """

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "",
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._conn: http.client.HTTPSConnection | None = None

        # SSL context — wallbox uses a self-signed certificate
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

        # Diagnostics — same attribute names as AlfenClient (Modbus)
        self.last_raw_reads: list[dict] = []
        self.last_raw_writes: list[dict] = []

    # ------------------------------------------------------------------ #
    #  Connection / authentication                                         #
    # ------------------------------------------------------------------ #

    def _new_conn(self) -> http.client.HTTPSConnection:
        """Create a fresh (unauthenticated) HTTPS connection."""
        return http.client.HTTPSConnection(
            self._host, port=443, context=self._ssl_ctx, timeout=15
        )

    def connect(self) -> None:
        """Open HTTPS connection and log in.  Raises on failure."""
        self._login()

    def _login(self) -> None:
        """
        (Re-)create the TCP/TLS connection and POST to ``/api/login``.

        After success, ``self._conn`` is an authenticated connection ready
        for subsequent API calls.
        """
        # Close any stale connection first
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        conn = self._new_conn()
        payload = {
            "username": self._username,
            "password": self._password,
            "displayname": "SolarCharge",
        }
        body = json.dumps(payload).encode()
        try:
            conn.request(
                "POST",
                "/api/login",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            resp.read()  # drain response body
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            raise ConnectionError(
                f"Alfen HTTP: login request to {self._host} failed: {exc}"
            ) from exc

        if resp.status not in (200, 201, 204):
            try:
                conn.close()
            except Exception:
                pass
            raise ConnectionError(
                f"Alfen HTTP: login returned HTTP {resp.status} "
                f"(check username/password in config.toml)"
            )

        self._conn = conn
        log.info(
            "Alfen HTTP: authenticated to %s as '%s'", self._host, self._username
        )

    def close(self) -> None:
        """
        POST ``/api/logout`` then close the TCP connection.

        Call this on application shutdown so the MyEve app can reconnect.
        """
        if self._conn is None:
            return
        try:
            body = b"{}"
            self._conn.request(
                "POST",
                "/api/logout",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                },
            )
            self._conn.getresponse().read()
            log.info("Alfen HTTP: logged out from %s", self._host)
        except Exception as exc:
            log.warning("Alfen HTTP: logout error (ignored): %s", exc)
        finally:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------------ #
    #  Low-level helpers                                                   #
    # ------------------------------------------------------------------ #

    def _get_prop(self, param_id: str) -> Union[float, int, str, None]:
        """
        GET ``/api/prop?id={param_id}`` and return the property value.

        On network failure, re-logs in once and retries.
        """
        if self._conn is None:
            self._login()

        path = f"/api/prop?id={param_id}"

        for attempt in range(2):
            try:
                assert self._conn is not None
                self._conn.request("GET", path, headers={})
                resp = self._conn.getresponse()
                data = resp.read()
            except Exception as exc:
                if attempt == 0:
                    log.debug(
                        "Alfen HTTP GET %s failed (%s) — re-logging in", path, exc
                    )
                    self._login()
                    continue
                raise ConnectionError(
                    f"Alfen HTTP: GET {path} failed after retry: {exc}"
                ) from exc

            if resp.status == 401:
                if attempt == 0:
                    log.debug("Alfen HTTP: 401 on GET %s — re-logging in", path)
                    self._login()
                    continue
                raise ConnectionError("Alfen HTTP: repeated 401 on GET — bad credentials?")

            if resp.status != 200:
                raise ConnectionError(
                    f"Alfen HTTP: GET {path} returned HTTP {resp.status}"
                )

            try:
                result = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ConnectionError(
                    f"Alfen HTTP: invalid JSON from GET {path}: {exc}"
                ) from exc

            props = result.get("properties", [])
            if props:
                return props[0].get("value")
            return None

        return None  # unreachable but keeps type checker happy

    def _post_prop(self, param_id: str, value: float) -> None:
        """
        POST ``/api/prop`` to write a single parameter value.

        After a successful write the Alfen firmware may close the TCP
        connection; we close our side too so the next poll cycle starts
        fresh with a new login.
        """
        if self._conn is None:
            self._login()

        payload = {param_id: {"id": param_id, "value": str(round(value, 2))}}
        body = json.dumps(payload).encode()

        for attempt in range(2):
            try:
                assert self._conn is not None
                self._conn.request(
                    "POST",
                    "/api/prop",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                    },
                )
                resp = self._conn.getresponse()
                resp.read()  # drain
            except Exception as exc:
                if attempt == 0:
                    log.debug(
                        "Alfen HTTP POST prop failed (%s) — re-logging in", exc
                    )
                    self._login()
                    continue
                raise ConnectionError(
                    f"Alfen HTTP: POST /api/prop failed after retry: {exc}"
                ) from exc

            if resp.status == 401:
                if attempt == 0:
                    self._login()
                    continue
                raise ConnectionError("Alfen HTTP: repeated 401 on POST prop")

            if resp.status not in (200, 201, 204):
                raise ConnectionError(
                    f"Alfen HTTP: POST prop {param_id}={value} "
                    f"returned HTTP {resp.status}"
                )

            break  # success

        # After a write, the wallbox typically closes the connection.
        # Drop it from our side so the next request triggers a fresh login.
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None
        log.debug(
            "Alfen HTTP: wrote %s = %s (connection released)", param_id, value
        )

    # ------------------------------------------------------------------ #
    #  Public interface — mirrors AlfenClient (Modbus)                    #
    # ------------------------------------------------------------------ #

    def read_state(self) -> AlfenState:
        """
        Read socket status, active power, and current setpoint.

        Returns
        -------
        AlfenState
            Same dataclass as the Modbus client returns.
        """
        status_raw   = self._get_prop(_PARAM_STATUS)
        power_raw    = self._get_prop(_PARAM_POWER_W)
        setpoint_raw = self._get_prop(_PARAM_MAX_A)

        status_code = int(status_raw)   if status_raw   is not None else 0
        power_w     = float(power_raw)  if power_raw    is not None else 0.0
        setpoint_a  = float(setpoint_raw) if setpoint_raw is not None else 0.0

        status_label, charge_status = _decode_status(status_code)

        self.last_raw_reads = [
            {
                "register":      _PARAM_STATUS,
                "label":         "StatusCode",
                "raw_hex":       str(status_code),
                "decoded_value": status_label,
            },
            {
                "register":      _PARAM_POWER_W,
                "label":         "ActivePower",
                "raw_hex":       str(round(power_w, 1)),
                "decoded_value": str(round(power_w, 1)),
                "unit":          "W",
            },
            {
                "register":      _PARAM_MAX_A,
                "label":         "MaxCurrent",
                "raw_hex":       str(round(setpoint_a, 2)),
                "decoded_value": str(round(setpoint_a, 2)),
                "unit":          "A",
            },
        ]

        state = AlfenState(
            status_str=status_label,
            status=charge_status,
            active_power_w=power_w,
            current_setpoint_a=setpoint_a,
        )
        log.debug(
            "Alfen HTTP: status=%s (%s)  power=%.0f W  setpoint=%.1f A",
            status_label,
            charge_status.value,
            power_w,
            setpoint_a,
        )
        return state

    def set_current(self, amps: float) -> None:
        """
        Write a new max charge current setpoint to the wallbox.

        Parameters
        ----------
        amps:
            Target amps per phase.  Pass ``0.0`` to disable charging;
            non-zero values should be ≥ 6.0 (IEC 61851 minimum).

        Notes
        -----
        Unlike the Modbus variant, this call does *not* act as a heartbeat —
        the wallbox does not require periodic writes to maintain its setpoint.
        The controller still re-writes every cycle, which is fine.
        """
        self._post_prop(_PARAM_MAX_A, amps)
        self.last_raw_writes = [
            {
                "register":     _PARAM_MAX_A,
                "label":        "MaxCurrent",
                "raw_hex":      str(round(amps, 2)),
                "value_written": str(round(amps, 2)),
                "unit":         "A",
            }
        ]
        log.debug("Alfen HTTP: wrote current setpoint %.1f A", amps)

    def read_total_energy_wh(self) -> float:
        """
        Read the wallbox lifetime energy meter.

        Returns
        -------
        float
            Energy in **Wh** — divide by 1 000 for kWh.
        """
        raw = self._get_prop(_PARAM_ENERGY_WH)
        if raw is None:
            return 0.0
        # API returns Wh as a float (HA integration divides by 1000 for kWh display)
        return float(raw)

    def read_rfid_tag(self) -> str:
        """
        Best-effort read of the last authorised RFID tag.

        The local HTTPS API does not expose RFID tags via the simple prop
        endpoint — reading them requires parsing the raw transaction log.
        Returns an empty string for now (same behaviour as the Modbus client
        on firmware that does not expose register 1240).
        """
        return ""

    def read_transactions(self) -> list[dict]:
        """
        Fetch the wallbox transaction log and return parsed charging sessions.

        The log lives in a sparse circular byte-addressed buffer.
        ``GET /api/transactions?offset=N`` returns a ~3 KB window of records
        starting at byte offset N.  We walk forward page by page, handling
        two complications:

        * **Sparse gaps** – the buffer may have an empty region between the
          current write-head and older data (e.g. offset 3206 is the write
          pointer, but useful sessions continue at offset 4000+).  On an
          empty page we skip ahead in chunks (up to ``_MAX_GAP_JUMPS``
          attempts) before giving up on that gap.

        * **Circular wrap** – when the per-page minimum entry offset drops
          back below the minimum we saw on the very first page, we have
          looped around and must stop to avoid duplicates.

        Returns
        -------
        list[dict]
            Each dict:  id, started_at, ended_at, duration_s, energy_kwh,
            start_meter_kwh, stop_meter_kwh, rfid_tag, status.
            Ordered oldest-first; in-progress session (if any) is last.
        """
        if self._conn is None:
            self._login()

        _RE_OFFSET = _re.compile(r'^(\d+)_', _re.MULTILINE)
        _GAP_JUMP = 500        # bytes to skip when we hit an empty window
        _MAX_GAP_JUMPS = 20    # give up after this many consecutive empty pages

        all_text_parts: list[str] = []
        all_seen_entry_offsets: set[int] = set()
        offset = 0
        first_min_offset: int | None = None   # min entry offset from page 0
        consecutive_empty = 0

        for _page in range(500):  # hard cap
            path = f"/api/transactions?offset={offset}"
            for attempt in range(2):
                try:
                    assert self._conn is not None
                    self._conn.request("GET", path, headers={})
                    resp = self._conn.getresponse()
                    data = resp.read().decode("utf-8", errors="replace")
                except Exception as exc:
                    if attempt == 0:
                        log.debug(
                            "Alfen HTTP GET %s failed (%s) — re-logging in", path, exc
                        )
                        self._login()
                        continue
                    raise ConnectionError(
                        f"Alfen HTTP: GET {path} failed after retry: {exc}"
                    ) from exc

                if resp.status == 401:
                    if attempt == 0:
                        self._login()
                        continue
                    raise ConnectionError("Alfen HTTP: repeated 401 on transactions")

                if resp.status != 200:
                    raise ConnectionError(
                        f"Alfen HTTP: GET {path} returned HTTP {resp.status}"
                    )
                break  # success

            page_entry_offsets = [int(m) for m in _RE_OFFSET.findall(data)]

            if not page_entry_offsets:
                # Empty window — could be a sparse gap; try skipping ahead.
                consecutive_empty += 1
                if consecutive_empty >= _MAX_GAP_JUMPS:
                    log.debug(
                        "Alfen transactions: %d consecutive empty pages at offset=%d — stopping",
                        consecutive_empty,
                        offset,
                    )
                    break
                offset += _GAP_JUMP
                continue

            consecutive_empty = 0
            page_min = min(page_entry_offsets)

            # Wrap detection: if the lowest entry offset on this page is at or
            # below the lowest we ever saw on the first non-empty page, the
            # circular buffer has looped back around; stop to avoid duplicates.
            if first_min_offset is None:
                first_min_offset = page_min
            elif page_min <= first_min_offset:
                log.debug(
                    "Alfen transactions: circular wrap detected at request offset=%d "
                    "(page_min=%d <= first_min=%d) — stopping",
                    offset,
                    page_min,
                    first_min_offset,
                )
                break

            # Only add entries we haven't seen before (dedup across pages).
            new_offsets = set(page_entry_offsets) - all_seen_entry_offsets
            if new_offsets:
                all_text_parts.append(data)
                all_seen_entry_offsets |= new_offsets

            offset = max(page_entry_offsets) + 1

        combined = "\n".join(all_text_parts)
        sessions = _parse_transactions(combined)
        log.info(
            "Alfen HTTP: fetched transaction log (%d pages) — %d session(s)",
            len(all_text_parts),
            len(sessions),
        )
        return sessions
