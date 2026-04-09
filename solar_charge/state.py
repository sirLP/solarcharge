"""
Shared application state — written by the controller, read by the web UI.

Both the controller loop and the FastAPI handlers run in the same asyncio
event loop, so a plain asyncio.Lock is sufficient for thread-safe access.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solar_charge.alfen import AlfenState, ChargeStatus
    from solar_charge.battery_guard import GuardStatus
    from solar_charge.history import HistoryStore
    from solar_charge.senec import SenecState
    from solar_charge.timeseries import TimeseriesStore


@dataclass
class Override:
    """An operator-requested charge current that bypasses solar calculation."""
    current_a: float = 0.0           # amps to force (0 = stop charging)
    until: datetime | None = None    # None means «indefinite»
    active: bool = False             # explicitly set by operator

    @property
    def is_active(self) -> bool:
        if not self.active:
            return False
        if self.until is not None and datetime.now() > self.until:
            return False
        return True

    def clear(self) -> None:
        self.current_a = 0.0
        self.until = None
        self.active = False


@dataclass
class DiagnosticsEntry:
    """Raw API traffic from the last poll cycle."""
    # SENEC
    senec_url: str = ""
    senec_request_json: dict = field(default_factory=dict)
    senec_response_raw: dict = field(default_factory=dict)
    senec_timestamp: datetime | None = None
    # Alfen — each entry: {register, label, raw_registers, decoded_value}
    alfen_host: str = ""
    alfen_reads: list[dict] = field(default_factory=list)
    alfen_writes: list[dict] = field(default_factory=list)
    alfen_timestamp: datetime | None = None


@dataclass
class AppState:
    """
    Live snapshot of the most recent controller cycle.

    Updated by :class:`~solar_charge.controller.Controller` every poll cycle.
    Read (without modification) by the FastAPI request handlers.
    """

    # Latest readings
    senec_state: "SenecState | None" = None
    alfen_state: "AlfenState | None" = None
    # The live Alfen client instance (set by controller, used by web routes)
    alfen_client: object = None

    # Derived control values from the last cycle
    surplus_w: float = 0.0
    target_a: float = 0.0
    setpoint_a: float = 0.0
    charging_active: bool = False
    session_kwh: float = 0.0   # Energy delivered in the current session (wallbox meter)

    # Battery guard status (None when guard is disabled)
    guard_status: "GuardStatus | None" = None

    # Modes
    calc_only: bool = True
    last_updated: datetime | None = None

    # Operator override
    override: Override = field(default_factory=Override)

    # Raw diagnostics from the last poll cycle (for the web UI)
    diagnostics: "DiagnosticsEntry" = field(default_factory=lambda: DiagnosticsEntry())

    # Charging session history
    history: "HistoryStore | None" = None

    # Time-series telemetry ring buffer
    timeseries: "TimeseriesStore | None" = None

    # Concurrency guard — use with «async with state.lock»
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
