"""
SENEC.HOME V3 local LAN API client.

All data is fetched via a single POST to /lala.cgi.
Values come back as hex-encoded strings (IEEE 754 floats or unsigned ints)
and are decoded before being returned in a typed SenecState dataclass.
"""

from __future__ import annotations

import logging
import ssl
import struct
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Fields we care about — all live inside the ENERGY section.
_REQUEST_BODY = {
    "ENERGY": {
        "GUI_INVERTER_POWER": "",   # Solar PV generated power (W)
        "GUI_HOUSE_POW": "",        # Total house consumption (W)
        "GUI_GRID_POW": "",         # Grid exchange: >0 importing, <0 exporting (W)
        "GUI_BAT_DATA_POWER": "",   # Battery: >0 charging, <0 discharging (W)
        "GUI_BAT_DATA_FUEL_CHARGE": "",  # Battery state of charge (%)
        "STAT_STATE": "",           # System state enum
    }
}


def _decode_value(raw: str) -> float:
    """
    Decode a SENEC hex-encoded value string into a Python float.

    Supported prefixes:
        fl_XXXXXXXX  →  IEEE 754 big-endian float32
        u8_HH        →  unsigned 8-bit int
        u1_XXXX      →  unsigned 16-bit int
    """
    if not isinstance(raw, str):
        return float(raw)

    if raw.startswith("fl_"):
        hex_str = raw[3:]
        return struct.unpack(">f", bytes.fromhex(hex_str))[0]

    if raw.startswith("u8_"):
        return float(int(raw[3:], 16))

    if raw.startswith("u1_"):
        return float(int(raw[3:], 16))

    # Fallback: try plain float
    try:
        return float(raw)
    except ValueError:
        log.warning("Cannot decode SENEC value: %r", raw)
        return 0.0


@dataclass
class SenecState:
    """Snapshot of real-time SENEC power data (all values in Watts or %)."""

    solar_power_w: float        # PV generation (positive = generating)
    house_power_w: float        # House consumption (positive)
    grid_power_w: float         # Grid exchange (positive = importing)
    battery_power_w: float      # Battery (positive = charging, negative = discharging)
    battery_soc_pct: float      # State of charge 0–100 %
    stat_state: int             # Raw SENEC state enum

    @property
    def grid_export_w(self) -> float:
        """Power being exported to the grid (positive when exporting)."""
        return -self.grid_power_w

    @property
    def is_importing(self) -> bool:
        return self.grid_power_w > 0

    @property
    def is_exporting(self) -> bool:
        return self.grid_power_w < 0


class SenecClient:
    """
    Async client for the SENEC local LAN API.

    Usage::

        async with SenecClient(host="192.168.178.237", use_https=True) as client:
            state = await client.fetch()

    Notes
    -----
    *  Firmware ≥ 825 requires HTTPS with a self-signed certificate — we skip
       TLS verification automatically.
    *  Firmware NPU ≥ 2411 requires a cookie-based session obtained by a
       one-time init POST.  We perform that on first connect and re-use the
       session for all subsequent requests.
    *  Poll no faster than every 10 seconds to avoid disrupting the unit's
       internal cloud sync.
    """

    def __init__(self, host: str, *, use_https: bool = True) -> None:
        self._host = host
        self._scheme = "https" if use_https else "http"
        self._url = f"{self._scheme}://{host}/lala.cgi"
        self._session: aiohttp.ClientSession | None = None
        self._ssl_ctx: ssl.SSLContext | Any = False  # skip self-signed cert verify
        # Diagnostics — updated on every successful fetch
        self.last_raw_request: dict = {}
        self.last_raw_response: dict = {}

    # ------------------------------------------------------------------ #
    #  Context-manager helpers                                             #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "SenecClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
            self._session = aiohttp.ClientSession(connector=connector)
            await self._init_cookie()

    async def _init_cookie(self) -> None:
        """
        Perform the one-time session cookie initialisation required by
        NPU firmware ≥ 2411.  Safe to call on older firmware (returns empty
        JSON which we ignore).
        """
        init_payload = {
            "FACTORY": {"SYS_TYPE": "", "COUNTRY": "", "DEVICE_ID": ""}
        }
        try:
            async with self._session.post(  # type: ignore[union-attr]
                self._url, json=init_payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                log.debug("SENEC cookie init: HTTP %s", resp.status)
        except Exception as exc:  # noqa: BLE001
            log.warning("SENEC cookie init failed (may be OK on older firmware): %s", exc)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def fetch(self) -> SenecState:
        """
        Poll the SENEC unit and return a decoded :class:`SenecState`.

        Raises
        ------
        aiohttp.ClientError
            On any network-level failure.
        ValueError
            If the response cannot be parsed.
        """
        await self._ensure_session()

        try:
            async with self._session.post(  # type: ignore[union-attr]
                self._url,
                json=_REQUEST_BODY,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data: dict = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.error("SENEC fetch failed: %s", exc)
            raise

        # Store raw traffic for diagnostics
        self.last_raw_request = _REQUEST_BODY
        self.last_raw_response = data

        energy = data.get("ENERGY", {})

        def _get(field: str) -> float:
            raw = energy.get(field, "fl_00000000")
            if raw == "VARIABLE_NOT_FOUND":
                log.debug("SENEC field not available: %s", field)
                return 0.0
            return _decode_value(raw)

        # GUI_INVERTER_POWER is reported as negative by SENEC; normalise to positive.
        solar_raw = _get("GUI_INVERTER_POWER")
        solar_w = abs(solar_raw)

        state = SenecState(
            solar_power_w=solar_w,
            house_power_w=_get("GUI_HOUSE_POW"),
            grid_power_w=_get("GUI_GRID_POW"),
            battery_power_w=_get("GUI_BAT_DATA_POWER"),
            battery_soc_pct=_get("GUI_BAT_DATA_FUEL_CHARGE"),
            stat_state=int(_get("STAT_STATE")),
        )

        log.debug(
            "SENEC: solar=%.0fW house=%.0fW grid=%.0fW battery=%.0fW soc=%.1f%% state=%d",
            state.solar_power_w,
            state.house_power_w,
            state.grid_power_w,
            state.battery_power_w,
            state.battery_soc_pct,
            state.stat_state,
        )
        return state
