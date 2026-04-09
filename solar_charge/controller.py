"""
Solar-only EV charging controller.

Logic
-----
Every poll cycle:

1.  Check for operator override (set via the web UI).
2.  Fetch live power data from SENEC.
3.  Compute available solar surplus:

        surplus_W = solar_power_w - (house_power_w - wallbox_power_w)

    SENEC's GUI_HOUSE_POW includes all loads on the home circuit, including
    the EV wallbox.  Without the correction the computed surplus would drop
    as soon as charging starts (the wallbox power would be double-counted:
    once as house load and once as the power we are deliberately directing
    to the car), causing the controller to reduce the setpoint and creating
    a self-defeating feedback loop.  Subtracting the current wallbox power
    gives the *true* household load so the surplus represents only solar
    energy not consumed by the house (excluding EV charging).
    The SENEC battery naturally absorbs/releases whatever is left — we do
    not need to account for it.  Grid import/export is also ignored: SENEC
    will honour whatever current we ask the wallbox to draw.

4.  Apply Battery Guard surplus factor (0–1) if enabled:

        guarded_surplus_W = surplus_W × surplus_factor

5.  Divide by (phases × voltage) to get target amps per phase.
5.  Apply hysteresis:
      - Start charging only when target >= start_threshold_a
      - Stop  charging only when target <  stop_threshold_a
6.  Clamp to [min_current_a, max_current_a] when charging, 0 when stopped.
7.  Apply ramp limit (+/-ramp_step_a per cycle) to smooth solar fluctuations.
8.  Write setpoint to Alfen wallbox (also serves as the required heartbeat).
9.  Publish the cycle snapshot to AppState for the web UI.

If no Alfen host is configured, steps 3-8 still run (and are logged) but
the Modbus write in step 8 is skipped -- "calculation-only mode".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from solar_charge.alfen import AlfenClient, AlfenState, ChargeStatus
from solar_charge.alfen_http import AlfenHTTPClient
from solar_charge.battery_guard import BatteryGuard, BatteryGuardConfig, GuardStatus
from solar_charge.history import HistoryStore
from solar_charge.senec import SenecClient, SenecState
from solar_charge.state import AppState, DiagnosticsEntry
from solar_charge.timeseries import TimeseriesStore

log = logging.getLogger(__name__)


@dataclass
class ControllerConfig:
    """Validated configuration for the controller loop."""
    # SENEC
    senec_host: str
    use_https: bool
    poll_interval_s: int

    # Alfen (optional)
    alfen_host: str          # empty string -> calculation-only mode
    alfen_mode: str          # "http" or "modbus"
    alfen_username: str      # HTTP mode: login username
    alfen_password: str      # HTTP mode: login password
    alfen_port: int
    alfen_slave_id: int
    phases: int
    voltage_per_phase: float
    max_current_a: float
    min_current_a: float
    release_current_a: float    # current written on shutdown to return wallbox to standalone

    # Control
    start_threshold_a: float
    stop_threshold_a: float
    ramp_step_a: float

    # Battery guard (optional)
    battery_guard: BatteryGuardConfig | None = None

    # RFID card guard (optional)
    rfid_enabled: bool = False
    rfid_allowlist: list[str] = field(default_factory=list)  # uppercase UIDs
    rfid_cards: list[dict] = field(default_factory=list)     # [{uid, name}] for web API


@dataclass
class ControllerState:
    """Mutable state carried between poll cycles (internal to controller)."""
    current_setpoint_a: float = 0.0
    charging_active: bool = False
    session_start_wh: float = 0.0   # Wallbox lifetime-energy reading when the current session began


class Controller:
    """
    Main control-loop class.

    Parameters
    ----------
    config:
        Validated :class:`ControllerConfig` instance.
    app_state:
        Shared :class:`~solar_charge.state.AppState` written after every
        poll cycle and read by the web UI.
    """

    def __init__(self, config: ControllerConfig, app_state: AppState, history: HistoryStore | None = None, timeseries: TimeseriesStore | None = None) -> None:
        self._cfg = config
        self._internal = ControllerState()
        self._app_state = app_state
        self._history = history
        self._timeseries = timeseries
        self._prev_wallbox_charging: bool = False  # wallbox ChargeStatus.CHARGING last cycle
        self._rfid_session_validated: bool = False  # True once RFID cleared for current session
        self._calc_only = not bool(config.alfen_host)

        self._senec = SenecClient(
            host=config.senec_host,
            use_https=config.use_https,
        )
        if self._calc_only:
            self._alfen: AlfenClient | AlfenHTTPClient | None = None
        elif config.alfen_mode == "http":
            self._alfen = AlfenHTTPClient(
                host=config.alfen_host,
                username=config.alfen_username,
                password=config.alfen_password,
            )
            log.info("Alfen backend: local HTTPS API (MyEve mode)")
        else:
            self._alfen = AlfenClient(
                host=config.alfen_host,
                port=config.alfen_port,
                slave_id=config.alfen_slave_id,
            )
            log.info("Alfen backend: Modbus TCP")

        # Expose the client on the shared state so web routes can call it
        app_state.alfen_client = self._alfen

        # Battery guard (optional)
        if config.battery_guard and config.battery_guard.enabled:
            self._guard: BatteryGuard | None = BatteryGuard(
                config.battery_guard, timeseries
            )
            # Pre-populate guard_status so the UI panel shows immediately,
            # even before the first full poll cycle completes.
            app_state.guard_status = GuardStatus(
                enabled=True,
                surplus_factor=1.0,
                reason="Initialising…",
            )
            log.info(
                "Battery guard enabled: night_reserve=%.0f%%  daytime_reserve=%.0f%%  "
                "ramp_start=%.1fh before sunset  lat=%.3f lon=%.3f",
                config.battery_guard.night_reserve_pct,
                config.battery_guard.daytime_reserve_pct,
                config.battery_guard.ramp_hours_before_sunset,
                config.battery_guard.latitude,
                config.battery_guard.longitude,
            )
        else:
            self._guard = None
            app_state.guard_status = GuardStatus(enabled=False)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Start the control loop.  Runs indefinitely."""
        self._app_state.calc_only = self._calc_only

        if self._calc_only:
            log.warning(
                "CALCULATION-ONLY MODE -- "
                "Alfen host not configured, wallbox will NOT be controlled. "
                "Set [alfen] host in config.toml to enable wallbox control."
            )
        else:
            log.info(
                "Starting controller: SENEC=%s  Alfen=%s  poll=%ds",
                self._cfg.senec_host,
                self._cfg.alfen_host,
                self._cfg.poll_interval_s,
            )

        async with self._senec:
            while True:
                try:
                    await self._poll_cycle()
                except Exception as exc:  # noqa: BLE001
                    log.error("Poll cycle error: %s", exc)

                await asyncio.sleep(self._cfg.poll_interval_s)

    async def stop(self) -> None:
        """Gracefully stop: restore wallbox to release current and close connections."""
        release_a = self._cfg.release_current_a
        log.info("Shutting down -- releasing wallbox to %.0f A (standalone)", release_a)
        if self._alfen and not self._calc_only:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._alfen.set_current, release_a
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not release wallbox current on shutdown: %s", exc)
            self._alfen.close()
        await self._senec.close()

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _update_session(self, was_charging: bool, is_charging: bool) -> None:
        """Detect charging start/stop transitions and maintain session history."""

        # ── RFID guard (independent of session history) ────────────────
        # Re-attempt on every cycle until validated so that timing issues
        # (e.g. Alfen hasn't written txstart2 yet) are automatically retried.
        if is_charging and not self._rfid_session_validated and self._cfg.rfid_enabled:
            rfid_for_guard = ""
            if self._alfen and not self._calc_only:
                try:
                    rfid_for_guard = await asyncio.get_event_loop().run_in_executor(
                        None, self._alfen.read_rfid_tag
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("RFID guard: cannot read tag: %s", exc)

            if self._cfg.rfid_allowlist:
                rfid_upper = rfid_for_guard.upper() if rfid_for_guard else ""
                if rfid_upper not in self._cfg.rfid_allowlist:
                    log.warning(
                        "RFID guard: tag %r not in allowlist — stopping charge",
                        rfid_for_guard or "<none>",
                    )
                    known_name = next(
                        (c["name"] for c in self._cfg.rfid_cards if c["uid"] == rfid_upper),
                        None,
                    )
                    self._app_state.rfid_blocked_log.appendleft({
                        "ts":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "uid":  rfid_for_guard or "<none>",
                        "name": known_name or "",
                    })
                    await self._write_alfen(0.0)
                    return  # do not start/continue session
                else:
                    # Tag is valid — mark session as validated so we don't recheck
                    log.info(
                        "RFID guard: tag %r authorised (%s)",
                        rfid_for_guard,
                        next((c["name"] for c in self._cfg.rfid_cards if c["uid"] == rfid_upper), ""),
                    )
                    self._rfid_session_validated = True
            else:
                # No cards configured — guard enabled but empty list; block everything
                log.warning(
                    "RFID guard: enabled but allowlist is empty — stopping charge"
                )
                await self._write_alfen(0.0)
                return

        h = self._history
        if h is None or self._calc_only:
            return

        app = self._app_state

        if not was_charging and is_charging:
            # ── Session just started ───────────────────────────────────
            rfid = ""
            energy_wh: float | None = None
            if self._alfen and not self._calc_only:
                loop = asyncio.get_event_loop()
                try:
                    energy_wh = await loop.run_in_executor(None, self._alfen.read_total_energy_wh)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Session start: cannot read energy meter: %s", exc)
                try:
                    rfid = await loop.run_in_executor(None, self._alfen.read_rfid_tag)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Session start: cannot read RFID tag: %s", exc)

            h.start_session(rfid)
            h.set_start_energy(energy_wh)

        elif was_charging and not is_charging:
            # ── Session just ended ─────────────────────────────────────
            self._rfid_session_validated = False  # reset for next session
            energy_wh = None
            if self._alfen and not self._calc_only:
                try:
                    energy_wh = await asyncio.get_event_loop().run_in_executor(
                        None, self._alfen.read_total_energy_wh
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("Session end: cannot read energy meter: %s", exc)
            await asyncio.get_event_loop().run_in_executor(None, h.end_session, energy_wh)

        elif is_charging:
            # ── Mid-session sample ─────────────────────────────────────
            senec = app.senec_state
            wallbox = app.alfen_state
            power_w = wallbox.active_power_w if wallbox else 0.0
            solar_w = senec.solar_power_w if senec else 0.0
            h.sample(power_w, solar_w, self._cfg.poll_interval_s)

    async def _write_alfen(self, amps: float) -> None:
        """Write current setpoint to Alfen (no-op in calc-only mode)."""
        if self._alfen and not self._calc_only:
            await asyncio.get_event_loop().run_in_executor(
                None, self._alfen.set_current, amps
            )

    def _snapshot_diagnostics(self, alfen_reads_updated: bool = False) -> None:
        """
        Copy latest raw client data into app.diagnostics.

        Called without the app.lock — diagnostics are display-only so
        a transient race is acceptable.
        """
        diag = self._app_state.diagnostics
        diag.senec_url = self._senec._url
        diag.senec_request_json = dict(self._senec.last_raw_request)
        diag.senec_response_raw = dict(self._senec.last_raw_response)
        diag.senec_timestamp = datetime.now()
        if self._alfen and not self._calc_only:
            if self._cfg.alfen_mode == "http":
                diag.alfen_host = f"https://{self._cfg.alfen_host}"
            else:
                diag.alfen_host = f"{self._cfg.alfen_host}:{self._cfg.alfen_port}"
            if alfen_reads_updated:
                diag.alfen_reads = list(self._alfen.last_raw_reads)
                diag.alfen_writes = list(self._alfen.last_raw_writes)
                diag.alfen_timestamp = datetime.now()

    async def _poll_cycle(self) -> None:
        """Wrap _do_poll_cycle with session-history tracking."""
        prev_wb_charging = self._prev_wallbox_charging
        try:
            await self._do_poll_cycle()
        finally:
            # Derive session open/close from the wallbox's own ChargeStatus so
            # state is always consistent with hardware, even after a restart.
            # Fall back to internal flag in calc-only mode (no wallbox present).
            alfen = self._app_state.alfen_state
            cur_wb_charging = (
                alfen is not None and alfen.status == ChargeStatus.CHARGING
            ) if not self._calc_only else self._internal.charging_active
            await self._update_session(prev_wb_charging, cur_wb_charging)
            self._prev_wallbox_charging = cur_wb_charging
            # Record timeseries sample from app_state (works regardless of which
            # branch _do_poll_cycle took, including all early-return paths).
            if self._timeseries is not None:
                app = self._app_state
                senec = app.senec_state
                if senec is not None:
                    ev_w = app.alfen_state.active_power_w if app.alfen_state else 0.0
                    self._timeseries.add(
                        solar_w=senec.solar_power_w,
                        # Store true household consumption (excluding EV charging)
                        house_w=max(0.0, senec.house_power_w - ev_w),
                        grid_w=senec.grid_power_w,
                        battery_w=senec.battery_power_w,
                        battery_soc_pct=senec.battery_soc_pct,
                        surplus_w=app.surplus_w,
                        setpoint_a=app.setpoint_a,
                        charging=app.charging_active,
                        ev_w=ev_w,
                    )

    async def _do_poll_cycle(self) -> None:
        cfg = self._cfg
        internal = self._internal
        app = self._app_state

        # -- 1. Check operator override ------------------------------------
        if app.override.is_active:
            senec = await self._senec.fetch()
            self._snapshot_diagnostics()
            override_a = app.override.current_a

            # Clamp to [0, max]; allow 0 to stop charging via override
            clamped = max(0.0, min(cfg.max_current_a, override_a))
            log.info("[OVERRIDE] Forcing %.1f A", clamped)
            await self._write_alfen(clamped)
            new_setpoint = clamped
            active = clamped > 0

            internal.current_setpoint_a = new_setpoint
            internal.charging_active = active
            async with app.lock:
                app.senec_state = senec
                app.setpoint_a = new_setpoint
                app.charging_active = active
                app.last_updated = datetime.now()
            return

        # -- 2. Read SENEC -------------------------------------------------
        senec = await self._senec.fetch()
        self._snapshot_diagnostics()

        # -- 3. Read Alfen (if configured) ---------------------------------
        wallbox_power_w = 0.0
        alfen_snap: AlfenState | None = None

        if self._alfen and not self._calc_only:
            try:
                alfen_snap = await asyncio.get_event_loop().run_in_executor(
                    None, self._alfen.read_state
                )
                wallbox_power_w = alfen_snap.active_power_w
                self._snapshot_diagnostics(alfen_reads_updated=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Alfen read failed — running on SENEC data only this cycle: %s", exc)
                # Fall through with wallbox_power_w=0 / alfen_snap=None so
                # SENEC data still reaches app_state and the timeseries store.
                async with app.lock:
                    app.senec_state = senec
                    app.surplus_w = senec.solar_power_w - senec.house_power_w
                    app.last_updated = datetime.now()
                return  # _poll_cycle finally block records timeseries from app_state

            # Car not connected -- ensure 0 A heartbeat and bail early.
            if alfen_snap.status in (ChargeStatus.NO_VEHICLE, ChargeStatus.DEACTIVATED):
                if internal.current_setpoint_a != 0.0:
                    log.info("Car disconnected -- setting current to 0 A")
                    internal.current_setpoint_a = 0.0
                    internal.charging_active = False
                # Always write 0 A every cycle so the Alfen watchdog never
                # times out and shows "waiting for load management".
                await self._write_alfen(0.0)
                # Still compute and display the potential surplus even when no
                # car is connected, so the UI shows what *could* be charged.
                potential_surplus_w = senec.solar_power_w - senec.house_power_w
                potential_target_a  = potential_surplus_w / (cfg.phases * cfg.voltage_per_phase)
                # Run guard evaluate so required_soc / factor stay current
                if self._guard is not None:
                    loop = asyncio.get_event_loop()
                    _, guard_status = await loop.run_in_executor(
                        None, self._guard.evaluate,
                        senec.battery_soc_pct, potential_surplus_w, senec.battery_power_w,
                    )
                    async with app.lock:
                        app.guard_status = guard_status
                async with app.lock:
                    app.senec_state = senec
                    app.alfen_state = alfen_snap
                    app.surplus_w = potential_surplus_w
                    app.target_a = potential_target_a
                    app.setpoint_a = 0.0
                    app.charging_active = False
                    app.last_updated = datetime.now()
                return

            if alfen_snap.status == ChargeStatus.FAULT:
                log.warning("Alfen fault state: %s -- pausing control", alfen_snap.status_str)
                async with app.lock:
                    app.alfen_state = alfen_snap
                    app.last_updated = datetime.now()
                return

        # -- 4. Compute surplus (solar minus true house load) ---------------
        # senec.house_power_w includes the wallbox draw (EV charging is part
        # of the home circuit measured by SENEC).  We subtract wallbox_power_w
        # so the surplus is based only on non-EV household consumption.  This
        # prevents the self-defeating feedback loop where starting to charge
        # immediately shrinks the perceived surplus and reduces the setpoint.
        surplus_w = senec.solar_power_w - (senec.house_power_w - wallbox_power_w)

        # -- 4b. Apply battery guard (if enabled) -------------------------
        guarded_surplus_w = surplus_w
        if self._guard is not None:
            loop = asyncio.get_event_loop()
            guarded_surplus_w, guard_status = await loop.run_in_executor(
                None,
                self._guard.evaluate,
                senec.battery_soc_pct,
                surplus_w,
                senec.battery_power_w,
            )
            async with app.lock:
                app.guard_status = guard_status
        else:
            guard_status = None

        target_a = guarded_surplus_w / (cfg.phases * cfg.voltage_per_phase)

        # -- 5. Hysteresis -------------------------------------------------
        session_just_started = False
        if not internal.charging_active:
            if target_a >= cfg.start_threshold_a:
                log.info(
                    "Solar surplus %.0f W -> %.2f A/phase -- START charging",
                    guarded_surplus_w, target_a,
                )
                internal.charging_active = True
                session_just_started = True
                internal.session_start_wh = alfen_snap.meter_wh if alfen_snap is not None else 0.0
            else:
                log.debug(
                    "Surplus %.0f W (%.2f A) below start threshold %.1f A -- idle",
                    guarded_surplus_w, target_a, cfg.start_threshold_a,
                )
                await self._write_alfen(0.0)
                async with app.lock:
                    app.senec_state = senec
                    app.alfen_state = alfen_snap
                    app.surplus_w = guarded_surplus_w
                    app.target_a = target_a
                    app.setpoint_a = 0.0
                    app.charging_active = False
                    app.last_updated = datetime.now()
                return
        else:
            if target_a < cfg.stop_threshold_a:
                log.info(
                    "Solar surplus %.0f W -> %.2f A/phase -- STOP charging",
                    guarded_surplus_w, target_a,
                )
                internal.charging_active = False
                internal.current_setpoint_a = 0.0
                await self._write_alfen(0.0)
                async with app.lock:
                    app.senec_state = senec
                    app.alfen_state = alfen_snap
                    app.surplus_w = guarded_surplus_w
                    app.target_a = target_a
                    app.setpoint_a = 0.0
                    app.charging_active = False
                    app.session_kwh = 0.0
                    app.last_updated = datetime.now()
                return

        # -- 6. Clamp ------------------------------------------------------
        desired_a = max(cfg.min_current_a, min(cfg.max_current_a, target_a))

        # -- 7. Ramp limit -------------------------------------------------
        # On the first cycle of a new session jump straight to the target so
        # the EV gets full power immediately rather than crawling up 1 A/step.
        if session_just_started:
            new_setpoint_a = desired_a
            delta = new_setpoint_a - internal.current_setpoint_a
        else:
            delta = desired_a - internal.current_setpoint_a
            if abs(delta) > cfg.ramp_step_a:
                delta = cfg.ramp_step_a if delta > 0 else -cfg.ramp_step_a
            new_setpoint_a = internal.current_setpoint_a + delta
        changed = abs(delta) > 0.05
        internal.current_setpoint_a = new_setpoint_a

        # -- 8. Write / heartbeat ------------------------------------------
        if self._calc_only:
            log.info(
                "[CALC-ONLY] solar=%.0fW  grid=%.0fW  battery=%.0fW  "
                "surplus=%.0fW  guarded=%.0fW  target=%.2fA  setpoint->%.2fA",
                senec.solar_power_w, senec.grid_power_w, senec.battery_power_w,
                surplus_w, guarded_surplus_w, target_a, new_setpoint_a,
            )
        else:
            if changed:
                log.info(
                    "Adjusting charge current: %.1f A -> %.1f A  (surplus %.0f W)",
                    new_setpoint_a - delta, new_setpoint_a, guarded_surplus_w,
                )
            else:
                log.debug("Heartbeat: %.1f A  surplus %.0f W", new_setpoint_a, guarded_surplus_w)
            await self._write_alfen(new_setpoint_a)

        # -- 9. Publish to AppState ----------------------------------------
        async with app.lock:
            app.senec_state = senec
            app.alfen_state = alfen_snap
            app.surplus_w = guarded_surplus_w
            app.target_a = target_a
            app.setpoint_a = new_setpoint_a
            app.charging_active = internal.charging_active
            if alfen_snap is not None and internal.session_start_wh > 0:
                app.session_kwh = max(0.0, (alfen_snap.meter_wh - internal.session_start_wh) / 1000.0)
            app.last_updated = datetime.now()

        # (timeseries sample is recorded in _poll_cycle after this method returns)
