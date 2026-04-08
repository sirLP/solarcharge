"""
Alfen Eve Single Pro-line Modbus TCP client.

Register map (community-reverse-engineered, confirmed by evcc project):

  344  ActivePower      R    float32  W     Total active charging power
  1201 SocketStatus     R    char[10] —     IEC 61851 status string (e.g. "A1", "C2")
  1210 MaxChargeCurrent R/W  float32  A     Target charge current; 0.0 = disable

All floats are IEEE 754 big-endian 32-bit, occupying 2 consecutive registers.

IMPORTANT: Alfen requires a write to register 1210 at least every ~60 seconds or
it reverts to its hardware default maximum.  The controller loop re-writes the
setpoint on every poll cycle, which satisfies this requirement.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from enum import Enum

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

log = logging.getLogger(__name__)

# Modbus register addresses
_REG_ACTIVE_POWER  = 344   # float32  W   — total active charging power
_REG_TOTAL_ENERGY  = 374   # float64  Wh  — lifetime energy meter (4 registers)
_REG_SOCKET_STATUS = 1201  # char[10]     — IEC 61851 state string (5 registers)
_REG_MAX_CURRENT   = 1210  # float32  A   — target charge current (R/W)
_REG_RFID_TAG      = 1240  # char[10]     — last authorised RFID UID (best-effort)

# Number of registers per float32 (2 × 16-bit registers = 32 bits)
_FLOAT32_REGS = 2
# Number of registers for the 10-char status string (10 chars / 2 chars per reg = 5)
_STATUS_REGS = 5


class ChargeStatus(str, Enum):
    """Simplified charge status derived from IEC 61851 state string."""
    NO_VEHICLE = "no_vehicle"      # A1/A2
    CONNECTED = "connected"        # B1/B2 — connected but not charging
    CHARGING = "charging"          # C2/C3/C4
    DEACTIVATED = "deactivated"    # E0
    FAULT = "fault"                # F*
    UNKNOWN = "unknown"


def _parse_status(raw_regs: list[int]) -> tuple[str, ChargeStatus]:
    """Decode the 10-char IEC status string from 5 Modbus registers."""
    raw_bytes = b"".join(r.to_bytes(2, "big") for r in raw_regs)
    status_str = raw_bytes.rstrip(b"\x00").decode("ascii", errors="ignore").strip()

    if status_str.startswith("A"):
        charge_status = ChargeStatus.NO_VEHICLE
    elif status_str.startswith("B"):
        charge_status = ChargeStatus.CONNECTED
    elif status_str.startswith("C"):
        charge_status = ChargeStatus.CHARGING
    elif status_str == "E0":
        charge_status = ChargeStatus.DEACTIVATED
    elif status_str.startswith("F"):
        charge_status = ChargeStatus.FAULT
    else:
        charge_status = ChargeStatus.UNKNOWN

    return status_str, charge_status


def _pack_float32(value: float) -> list[int]:
    """Pack a float into a list of two 16-bit register values (big-endian)."""
    raw = struct.pack(">f", value)
    return [
        int.from_bytes(raw[0:2], "big"),
        int.from_bytes(raw[2:4], "big"),
    ]


def _unpack_float32(registers: list[int]) -> float:
    """Unpack two 16-bit registers into a big-endian IEEE 754 float32."""
    raw = struct.pack(">HH", registers[0], registers[1])
    return struct.unpack(">f", raw)[0]


@dataclass
class AlfenState:
    """Snapshot of the Alfen wallbox state."""
    status_str: str          # Raw IEC string e.g. "C2"
    status: ChargeStatus     # Simplified enum
    active_power_w: float    # Current charging power (W)
    current_setpoint_a: float  # Current max-current setpoint (A); 0 = disabled


class AlfenClient:
    """
    Synchronous Modbus TCP client for the Alfen Eve wallbox.

    Usage::

        client = AlfenClient(host="192.168.1.50", port=502, slave_id=1)
        client.connect()
        state = client.read_state()
        client.set_current(10.0)
        client.close()

    Call :meth:`set_current` with ``0.0`` to disable charging.
    Call :meth:`set_current` with a value ≥ 6.0 to enable/adjust.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 502,
        slave_id: int = 1,
    ) -> None:
        self._host = host
        self._port = port
        self._slave_id = slave_id
        self._client: ModbusTcpClient | None = None
        # Diagnostics — updated after every read_state / set_current call
        self.last_raw_reads: list[dict] = []
        self.last_raw_writes: list[dict] = []

    # ------------------------------------------------------------------ #
    #  Connection management                                               #
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Open the Modbus TCP connection.  Raises on failure."""
        self._client = ModbusTcpClient(self._host, port=self._port)
        if not self._client.connect():
            raise ConnectionError(
                f"Cannot connect to Alfen wallbox at {self._host}:{self._port}. "
                "Ensure Modbus TCP is enabled in the Alfen web interface."
            )
        log.info("Connected to Alfen wallbox at %s:%d", self._host, self._port)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def _ensure_connected(self) -> ModbusTcpClient:
        if self._client is None or not self._client.connected:
            log.warning("Alfen connection lost — reconnecting …")
            self.connect()
        assert self._client is not None
        return self._client

    # ------------------------------------------------------------------ #
    #  Read operations                                                     #
    # ------------------------------------------------------------------ #

    def read_state(self) -> AlfenState:
        """
        Read socket status, active power, and current setpoint from the wallbox.

        Returns
        -------
        AlfenState
        """
        client = self._ensure_connected()

        try:
            # Socket status (10 chars in 5 registers)
            status_result = client.read_holding_registers(
                address=_REG_SOCKET_STATUS, count=_STATUS_REGS, slave=self._slave_id
            )
            if status_result.isError():
                raise ModbusException(f"Error reading socket status: {status_result}")
            status_str, charge_status = _parse_status(status_result.registers)

            # Active charging power
            power_result = client.read_holding_registers(
                address=_REG_ACTIVE_POWER, count=_FLOAT32_REGS, slave=self._slave_id
            )
            if power_result.isError():
                raise ModbusException(f"Error reading active power: {power_result}")
            active_power_w = _unpack_float32(power_result.registers)

            # Current setpoint
            current_result = client.read_holding_registers(
                address=_REG_MAX_CURRENT, count=_FLOAT32_REGS, slave=self._slave_id
            )
            if current_result.isError():
                raise ModbusException(f"Error reading current setpoint: {current_result}")
            current_setpoint_a = _unpack_float32(current_result.registers)

        except ModbusException as exc:
            log.error("Alfen Modbus read error: %s", exc)
            raise

        # Record raw register traffic for diagnostics
        raw_status = b"".join(r.to_bytes(2, "big") for r in status_result.registers)
        status_ascii = raw_status.hex().upper()
        self.last_raw_reads = [
            {
                "register": _REG_SOCKET_STATUS,
                "label": "SocketStatus",
                "raw_registers": list(status_result.registers),
                "raw_hex": status_ascii,
                "decoded_value": status_str,
            },
            {
                "register": _REG_ACTIVE_POWER,
                "label": "ActivePower",
                "raw_registers": list(power_result.registers),
                "raw_hex": "%04X %04X" % tuple(power_result.registers),
                "decoded_value": round(active_power_w, 2),
                "unit": "W",
            },
            {
                "register": _REG_MAX_CURRENT,
                "label": "MaxChargeCurrent",
                "raw_registers": list(current_result.registers),
                "raw_hex": "%04X %04X" % tuple(current_result.registers),
                "decoded_value": round(current_setpoint_a, 2),
                "unit": "A",
            },
        ]

        state = AlfenState(
            status_str=status_str,
            status=charge_status,
            active_power_w=active_power_w,
            current_setpoint_a=current_setpoint_a,
        )

        log.debug(
            "Alfen: status=%s (%s) power=%.0fW setpoint=%.1fA",
            state.status_str,
            state.status.value,
            state.active_power_w,
            state.current_setpoint_a,
        )
        return state

    # ------------------------------------------------------------------ #
    #  Write operations                                                    #
    # ------------------------------------------------------------------ #

    def read_total_energy_wh(self) -> float:
        """
        Read the wallbox lifetime energy meter.

        Returns
        -------
        float
            Energy in Wh (divide by 1 000 for kWh).
        """
        client = self._ensure_connected()
        result = client.read_holding_registers(
            address=_REG_TOTAL_ENERGY, count=4, slave=self._slave_id
        )
        if result.isError():
            raise ModbusException(f"Error reading energy meter: {result}")
        raw = b"".join(r.to_bytes(2, "big") for r in result.registers)
        value_wh: float = struct.unpack(">d", raw)[0]
        log.debug("Alfen: lifetime energy meter = %.1f Wh", value_wh)
        return value_wh

    def read_rfid_tag(self) -> str:
        """
        Attempt to read the last authorised RFID UID from register 1240.

        This is a best-effort read — not all Alfen firmware versions expose
        RFID data over Modbus TCP.  Returns an empty string on any failure
        or if the register is blank.
        """
        try:
            client = self._ensure_connected()
            result = client.read_holding_registers(
                address=_REG_RFID_TAG, count=5, slave=self._slave_id
            )
            if result.isError():
                return ""
            raw = b"".join(r.to_bytes(2, "big") for r in result.registers)
            tag = raw.rstrip(b"\x00").decode("ascii", errors="ignore").strip()
            log.debug("Alfen: RFID tag = %r", tag)
            return tag
        except Exception:  # noqa: BLE001
            return ""

    def set_current(self, amps: float) -> None:
        """
        Write a new max charge current setpoint to the wallbox.

        Parameters
        ----------
        amps:
            Target current in Amperes per phase.
            Pass ``0.0`` to disable charging.
            Non-zero values should be ≥ 6.0 (IEC 61851 minimum).

        Notes
        -----
        This write also acts as the mandatory heartbeat — the Alfen reverts to
        its hardware default if no write is received for ~60 seconds.
        """
        client = self._ensure_connected()

        registers = _pack_float32(amps)
        try:
            result = client.write_registers(
                address=_REG_MAX_CURRENT,
                values=registers,
                slave=self._slave_id,
            )
            if result.isError():
                raise ModbusException(f"Error writing current setpoint: {result}")
        except ModbusException as exc:
            log.error("Alfen Modbus write error: %s", exc)
            raise

        # Record raw write for diagnostics
        self.last_raw_writes = [
            {
                "register": _REG_MAX_CURRENT,
                "label": "MaxChargeCurrent",
                "raw_registers": registers,
                "raw_hex": "%04X %04X" % tuple(registers),
                "value_written": round(amps, 2),
                "unit": "A",
            }
        ]

        log.debug("Alfen: wrote current setpoint %.1f A", amps)
