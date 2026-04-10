"""
FastAPI web interface for SolarCharge.

Endpoints
---------
GET  /                  Dashboard HTML (auto-refreshes via JS)
GET  /api/status        Live status snapshot (JSON)
GET  /api/config        Readable/editable config values (JSON)
POST /api/config        Update control thresholds (hot-reload, persisted to TOML)
POST /api/override      Force a specific current or resume auto mode
"""

from __future__ import annotations

import asyncio
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import tomli_w
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, field_validator

from solar_charge.alfen import ChargeStatus
from solar_charge.controller import ControllerConfig
from solar_charge.state import AppState


# ─────────────────────────────────────────────────────────────────────────────
#  Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class StatusResponse(BaseModel):
    timestamp: str | None
    calc_only: bool
    # SENEC
    solar_w: float
    grid_w: float
    battery_w: float
    house_w: float
    battery_soc_pct: float
    # Derived
    surplus_w: float
    target_a: float
    setpoint_a: float
    charging_active: bool
    # Alfen
    car_status: str
    car_status_raw: str
    wallbox_power_w: float
    # Override
    override_active: bool
    override_current_a: float | None
    override_until: str | None
    # Current session
    session_kwh: float
    # Battery guard
    guard_enabled: bool
    guard_active: bool
    guard_factor: float
    guard_linear_mode: bool
    guard_required_soc: float
    guard_sunset: str
    guard_sunrise: str
    guard_reason: str
    guard_cloud_pct: float | None
    guard_tomorrow_cloud_pct: float | None
    guard_tomorrow_boost: float
    guard_seasonal_extra: float


class ConfigResponse(BaseModel):
    poll_interval_s: int
    start_threshold_a: float
    stop_threshold_a: float
    ramp_step_a: float
    min_current_a: float
    max_current_a: float
    phases: int
    voltage_per_phase: float


class ConfigUpdateRequest(BaseModel):
    poll_interval_s: int | None = None
    start_threshold_a: float | None = None
    stop_threshold_a: float | None = None
    ramp_step_a: float | None = None

    @field_validator("poll_interval_s")
    @classmethod
    def validate_poll(cls, v: int | None) -> int | None:
        if v is not None and v < 10:
            raise ValueError("poll_interval_s must be >= 10")
        return v


class OverrideRequest(BaseModel):
    action: str                        # "set_current" | "resume"
    current_a: float | None = None     # required when action = "set_current"
    duration_minutes: float | None = None  # None = indefinite


class GuardUpdateRequest(BaseModel):
    linear_mode: bool | None = None    # enable Linear Factor mode


class RfidCard(BaseModel):
    uid: str
    name: str = ""


class RfidUpdateRequest(BaseModel):
    enabled: bool
    cards: list[RfidCard] = []


# ─────────────────────────────────────────────────────────────────────────────
#  App factory
# ─────────────────────────────────────────────────────────────────────────────

def create_app(
    app_state: AppState,
    config: ControllerConfig,
    config_path: Path,
) -> FastAPI:
    """
    Create and return the FastAPI application.

    Parameters
    ----------
    app_state:  Shared live state from the controller.
    config:     Mutable ControllerConfig instance (updated on POST /api/config).
    config_path: Path to config.toml for persistence.
    """
    app = FastAPI(title="SolarCharge", version="0.1.0", docs_url="/api/docs")

    # ── Helpers ──────────────────────────────────────────────────────────

    def _snapshot() -> StatusResponse:
        s = app_state
        senec = s.senec_state
        alfen = s.alfen_state

        car_status = ChargeStatus.UNKNOWN.value
        car_status_raw = "—"
        wallbox_power_w = 0.0

        if alfen:
            car_status = alfen.status.value
            car_status_raw = alfen.status_str
            wallbox_power_w = alfen.active_power_w

        ov = s.override
        gs = s.guard_status
        return StatusResponse(
            timestamp=s.last_updated.isoformat() if s.last_updated else None,
            calc_only=s.calc_only,
            solar_w=senec.solar_power_w if senec else 0.0,
            grid_w=senec.grid_power_w if senec else 0.0,
            battery_w=senec.battery_power_w if senec else 0.0,
            house_w=max(0.0, (senec.house_power_w if senec else 0.0) - wallbox_power_w),
            battery_soc_pct=senec.battery_soc_pct if senec else 0.0,
            surplus_w=s.surplus_w,
            target_a=s.target_a,
            setpoint_a=s.setpoint_a,
            charging_active=s.charging_active,
            car_status=car_status,
            car_status_raw=car_status_raw,
            wallbox_power_w=wallbox_power_w,
            override_active=ov.is_active,
            override_current_a=ov.current_a if ov.is_active else None,
            override_until=ov.until.isoformat() if (ov.is_active and ov.until) else None,
            session_kwh=app_state.session_kwh,
            guard_enabled=gs.enabled if gs else False,
            guard_active=gs.active if gs else False,
            guard_factor=gs.surplus_factor if gs else 1.0,
            guard_linear_mode=gs.linear_mode if gs else True,
            guard_required_soc=gs.required_soc_pct if gs else 0.0,
            guard_sunset=gs.sunset_local if gs else "",
            guard_sunrise=gs.sunrise_local if gs else "",
            guard_reason=gs.reason if gs else "",
            guard_cloud_pct=gs.weather_cloud_pct if gs else None,
            guard_tomorrow_cloud_pct=gs.tomorrow_cloud_pct if gs else None,
            guard_tomorrow_boost=gs.tomorrow_night_reserve_boost if gs else 0.0,
            guard_seasonal_extra=gs.seasonal_extra_pct if gs else 0.0,
        )

    def _persist_config() -> None:
        """Write the current [control] and [senec] poll config back to TOML."""
        with config_path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
        raw.setdefault("control", {})
        raw["control"]["start_threshold_a"] = config.start_threshold_a
        raw["control"]["stop_threshold_a"] = config.stop_threshold_a
        raw["control"]["ramp_step_a"] = config.ramp_step_a
        raw["senec"]["poll_interval_s"] = config.poll_interval_s
        with config_path.open("wb") as fh:
            tomli_w.dump(raw, fh)

    def _persist_guard_config() -> None:
        """Write the current [battery_guard] runtime settings back to TOML."""
        if config.battery_guard is None:
            return
        with config_path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
        raw.setdefault("battery_guard", {})
        raw["battery_guard"]["linear_mode"] = config.battery_guard.linear_mode
        with config_path.open("wb") as fh:
            tomli_w.dump(raw, fh)

    def _persist_rfid_config() -> None:
        """Write the current [rfid] settings (enabled + cards list) back to TOML."""
        with config_path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
        raw["rfid"] = {
            "enabled": config.rfid_enabled,
            "card": config.rfid_cards,
        }
        with config_path.open("wb") as fh:
            tomli_w.dump(raw, fh)

    # ── Routes ───────────────────────────────────────────────────────────

    @app.get("/api/status", response_model=StatusResponse)
    async def get_status() -> StatusResponse:
        return _snapshot()

    @app.get("/api/config", response_model=ConfigResponse)
    async def get_config() -> ConfigResponse:
        return ConfigResponse(
            poll_interval_s=config.poll_interval_s,
            start_threshold_a=config.start_threshold_a,
            stop_threshold_a=config.stop_threshold_a,
            ramp_step_a=config.ramp_step_a,
            min_current_a=config.min_current_a,
            max_current_a=config.max_current_a,
            phases=config.phases,
            voltage_per_phase=config.voltage_per_phase,
        )

    @app.post("/api/config")
    async def update_config(req: ConfigUpdateRequest) -> dict:
        if req.poll_interval_s is not None:
            config.poll_interval_s = req.poll_interval_s
        if req.start_threshold_a is not None:
            if req.start_threshold_a <= 0:
                raise HTTPException(400, "start_threshold_a must be > 0")
            config.start_threshold_a = req.start_threshold_a
        if req.stop_threshold_a is not None:
            if req.stop_threshold_a > config.start_threshold_a:
                raise HTTPException(400, "stop_threshold_a must be <= start_threshold_a")
            config.stop_threshold_a = req.stop_threshold_a
        if req.ramp_step_a is not None:
            if req.ramp_step_a <= 0:
                raise HTTPException(400, "ramp_step_a must be > 0")
            config.ramp_step_a = req.ramp_step_a
        _persist_config()
        return {"ok": True, "message": "Config updated and persisted"}

    @app.post("/api/override")
    async def set_override(req: OverrideRequest) -> dict:
        ov = app_state.override
        if req.action == "resume":
            ov.clear()
            return {"ok": True, "message": "Override cleared — returning to auto mode"}

        if req.action == "set_current":
            if req.current_a is None:
                raise HTTPException(400, "current_a required for action set_current")
            if req.current_a < 0 or req.current_a > config.max_current_a:
                raise HTTPException(
                    400,
                    f"current_a must be between 0 and {config.max_current_a} A",
                )
            ov.current_a = req.current_a
            ov.until = (
                datetime.now() + timedelta(minutes=req.duration_minutes)
                if req.duration_minutes
                else None
            )
            ov.active = True
            label = "Charging stopped" if req.current_a == 0 else f"Override set to {req.current_a} A"
            return {"ok": True, "message": label}

        raise HTTPException(400, f"Unknown action: {req.action!r}")

    @app.post("/api/guard")
    async def update_guard(req: GuardUpdateRequest) -> dict:
        """Toggle Battery Guard runtime options (persisted to TOML)."""
        if config.battery_guard is None:
            raise HTTPException(404, "Battery Guard is not configured")
        if req.linear_mode is not None:
            config.battery_guard.linear_mode = req.linear_mode
            # Reflect the change immediately in the live guard status
            if app_state.guard_status is not None:
                app_state.guard_status.linear_mode = req.linear_mode
        _persist_guard_config()
        mode = "Linear factor" if config.battery_guard.linear_mode else "Full-or-Off"
        return {"ok": True, "message": f"Battery Guard mode set to: {mode}"}

    @app.get("/api/rfid")
    async def get_rfid() -> dict:
        """Return RFID guard configuration (enabled flag + allowed card list)."""
        return {
            "enabled": config.rfid_enabled,
            "cards":   config.rfid_cards,
        }

    @app.post("/api/rfid")
    async def update_rfid(req: RfidUpdateRequest) -> dict:
        """Update RFID guard settings and persist to TOML."""
        cards = [
            {"uid": c.uid.upper().strip(), "name": c.name.strip()}
            for c in req.cards
            if c.uid.strip()
        ]
        config.rfid_enabled  = req.enabled
        config.rfid_cards    = cards
        config.rfid_allowlist = [c["uid"] for c in cards]
        _persist_rfid_config()
        return {"ok": True, "message": f"RFID guard {'enabled' if req.enabled else 'disabled'} with {len(cards)} card(s)"}

    @app.get("/api/rfid/blocked")
    async def get_rfid_blocked() -> dict:
        """Return the most recent RFID-blocked access attempts (newest first)."""
        return {"blocked": list(app_state.rfid_blocked_log)}

    @app.get("/api/diagnostics")
    async def get_diagnostics() -> dict:
        d = app_state.diagnostics
        return {
            "senec": {
                "url": d.senec_url,
                "timestamp": d.senec_timestamp.isoformat() if d.senec_timestamp else None,
                "request": d.senec_request_json,
                "response_raw": d.senec_response_raw,
            },
            "alfen": {
                "host": d.alfen_host,
                "timestamp": d.alfen_timestamp.isoformat() if d.alfen_timestamp else None,
                "reads": d.alfen_reads,
                "writes": d.alfen_writes,
            },
        }

    @app.get("/api/history")
    async def get_history(limit: int = 200) -> dict:
        h = app_state.history
        if h is None:
            return {"sessions": [], "active": None, "total": 0}
        return {
            "sessions": h.get_sessions(limit=limit),
            "active":   h.active_session,
            "total":    h.total_sessions,
        }

    @app.get("/api/timeseries")
    async def get_timeseries(
        fields: str = "",
        minutes: int = 60,
        max_points: int = 600,
        group_by: str = "none",
    ) -> dict:
        ts = app_state.timeseries
        if ts is None:
            return {"timestamps": [], "fields": {}, "field_meta": []}
        from datetime import timezone as _tz
        from datetime import timedelta as _td
        since = datetime.now(tz=_tz.utc) - _td(minutes=minutes) if minutes > 0 else None
        wanted = [f.strip() for f in fields.split(",") if f.strip()] or None
        if group_by in ("day", "week"):
            data = ts.query_grouped(group_by=group_by, since=since, fields=wanted)
        else:
            data = ts.query(since=since, fields=wanted, max_points=max_points)
        data["field_meta"] = ts.field_meta
        return data

    @app.get("/api/wallbox-sessions")
    async def get_wallbox_sessions() -> dict:
        """Fetch and return parsed charging sessions from the Alfen wallbox."""
        client = app_state.alfen_client
        if client is None:
            return {
                "sessions": [],
                "error": "Wallbox not connected (calculation-only mode or not yet polled)",
            }
        try:
            loop = asyncio.get_event_loop()
            sessions = await loop.run_in_executor(None, client.read_transactions)

            # If the last session is still flagged in-progress but the wallbox
            # reports no vehicle present, Alfen never emitted a txstop2 (e.g.
            # SolarCharge reduced current to 0 and the driver unplugged without
            # swiping the RFID card again).  Synthesise the end-of-session
            # figures from the live meter reading so the UI shows a closed row.
            alfen = app_state.alfen_state
            if (
                sessions
                and sessions[-1]["status"] == "in_progress"
                and alfen is not None
                and alfen.status == ChargeStatus.NO_VEHICLE
            ):
                s = sessions[-1]
                stop_kwh = round(alfen.meter_wh / 1000.0, 3)
                start_kwh: float = s.get("start_meter_kwh") or 0.0
                energy_kwh = round(max(0.0, stop_kwh - start_kwh), 3)
                try:
                    started = datetime.fromisoformat(s["started_at"])
                    duration_s: float | None = round((datetime.now() - started).total_seconds())
                except (ValueError, KeyError):
                    duration_s = None
                s.update(
                    status="completed",
                    ended_at=datetime.now().isoformat(timespec="seconds"),
                    stop_meter_kwh=stop_kwh,
                    energy_kwh=energy_kwh,
                    duration_s=duration_s,
                )

            return {"sessions": sessions, "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"sessions": [], "error": str(exc)}

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_page() -> str:
        return _build_reports_html()

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _build_dashboard_html(config)

    return app


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard HTML
# ─────────────────────────────────────────────────────────────────────────────

def _build_dashboard_html(cfg: ControllerConfig) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<title>SolarCharge</title>
<style>
  :root {{
    --bg: #f0f4f8; --card: #fff; --border: #dde3ea;
    --text: #1a202c; --muted: #718096;
    --green: #38a169; --yellow: #d69e2e; --red: #e53e3e; --blue: #3182ce;
    --solar: #f6ad55; --grid-imp: #fc8181; --grid-exp: #68d391;
    --battery: #76e4f7; --house: #b794f4;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}
  header {{ background: #1a202c; color: #fff; padding: 1rem 1.5rem; display: flex; align-items: center; gap: 1rem; }}
  header h1 {{ font-size: 1.25rem; font-weight: 700; }}
  #mode-badge {{ font-size: .75rem; padding: .2rem .6rem; border-radius: 999px; background: #4a5568; }}
  #mode-badge.calc {{ background: #d69e2e; color: #1a202c; }}
  #mode-badge.live {{ background: #38a169; }}
  #last-updated {{ margin-left: auto; font-size: .75rem; color: #a0aec0; }}
  main {{ max-width: 900px; margin: 1.5rem auto; padding: 0 1rem; display: grid; gap: 1rem; }}
  .section-title {{ font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
    color: var(--muted); margin-bottom: .5rem; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: .75rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: .75rem;
    padding: 1rem; display: flex; flex-direction: column; gap: .25rem; }}
  .card .label {{ font-size: .7rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
  .card .value {{ font-size: 1.6rem; font-weight: 700; line-height: 1; }}
  .card .unit {{ font-size: .8rem; color: var(--muted); }}
  .card .accent {{ width: 3px; height: 2rem; border-radius: 2px; align-self: flex-start; }}
  .accent-solar {{ background: var(--solar); }}
  .accent-grid  {{ background: var(--grid-imp); }}
  .accent-bat   {{ background: var(--battery); }}
  .accent-house {{ background: var(--house); }}
  .accent-surplus {{ background: var(--green); }}
  .status-row {{ background: var(--card); border: 1px solid var(--border); border-radius: .75rem;
    padding: 1rem 1.25rem; display: flex; flex-wrap: wrap; align-items: center; gap: 1rem; }}
  .status-badge {{ padding: .3rem .9rem; border-radius: 999px; font-size: .8rem; font-weight: 600; }}
  .badge-charging {{ background: #c6f6d5; color: #22543d; }}
  .badge-connected {{ background: #fefcbf; color: #744210; }}
  .badge-no-vehicle {{ background: #e2e8f0; color: #4a5568; }}
  .badge-override {{ background: #bee3f8; color: #2a4365; }}
  .badge-fault {{ background: #fed7d7; color: #822727; }}
  .stat {{ display: flex; flex-direction: column; }}
  .stat .s-label {{ font-size: .65rem; color: var(--muted); text-transform: uppercase; }}
  .stat .s-value {{ font-size: 1rem; font-weight: 600; }}
  .panel {{ background: var(--card); border: 1px solid var(--border); border-radius: .75rem; padding: 1.25rem; }}
  .panel h3 {{ font-size: .8rem; font-weight: 700; text-transform: uppercase; letter-spacing: .07em;
    color: var(--muted); margin-bottom: 1rem; }}
  .form-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: .75rem 1rem; }}
  .field {{ display: flex; flex-direction: column; gap: .3rem; }}
  .field label {{ font-size: .75rem; color: var(--muted); }}
  .field input[type=number] {{ border: 1px solid var(--border); border-radius: .4rem;
    padding: .4rem .6rem; font-size: .9rem; width: 100%; }}
  .field input[type=range] {{ width: 100%; accent-color: var(--blue); }}
  .range-row {{ display: flex; gap: .5rem; align-items: center; }}
  .range-row span {{ font-size: .85rem; font-weight: 600; min-width: 3rem; text-align: right; }}
  .btn {{ padding: .45rem 1rem; border: none; border-radius: .4rem; cursor: pointer;
    font-size: .85rem; font-weight: 600; transition: opacity .15s; }}
  .btn:hover {{ opacity: .85; }}
  .btn-primary {{ background: #3182ce; color: #fff; }}
  .btn-warning {{ background: #d69e2e; color: #fff; }}
  .btn-danger  {{ background: #e53e3e; color: #fff; }}
  .btn-ghost   {{ background: #e2e8f0; color: #4a5568; }}
  .btn-row {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .75rem; }}
  .duration-select {{ border: 1px solid var(--border); border-radius: .4rem;
    padding: .4rem .5rem; font-size: .85rem; }}
  #override-status {{ font-size: .8rem; color: var(--muted); margin-top: .5rem; }}
  #config-msg {{ font-size: .8rem; margin-top: .5rem; }}
  .msg-ok  {{ color: var(--green); }}
  .msg-err {{ color: var(--red); }}
  /* ── Chart modal ───────────────────────────────────────────── */
  .chart-controls {{ display:flex; flex-wrap:wrap; gap:.75rem; align-items:flex-start;
    padding:.75rem 1.2rem; border-bottom:1px solid var(--border); background:#f7fafc; }}
  .chart-controls label {{ font-size:.8rem; }}
  .field-grid {{ display:flex; flex-wrap:wrap; gap:.35rem .75rem; }}
  .field-check {{ display:flex; align-items:center; gap:.3rem; font-size:.8rem;
    cursor:pointer; white-space:nowrap; }}
  .field-check input {{ cursor:pointer; }}
  .range-btn {{ padding:.25rem .6rem; border:1px solid var(--border); border-radius:.35rem;
    background:#fff; cursor:pointer; font-size:.8rem; }}
  .range-btn.active {{ background:var(--green); color:#fff; border-color:var(--green); }}
  .chart-wrap {{ flex:1; padding:.75rem 1.2rem 1rem; min-height:300px;
    display:flex; flex-direction:column; }}
  .chart-wrap canvas {{ flex:1; min-height:0; }}
  /* ── Diagnostics modal ───────────────────────────────────────────── */
  .modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.55);
    z-index:100; align-items:center; justify-content:center; }}
  .modal-overlay.open {{ display:flex; }}
  .modal {{ background:var(--card); border-radius:.75rem; width:min(92vw,820px);
    max-height:85vh; display:flex; flex-direction:column; overflow:hidden;
    box-shadow:0 20px 60px rgba(0,0,0,.35); }}
  .modal-header {{ padding:.9rem 1.2rem; border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:.75rem; }}
  .modal-header h2 {{ font-size:.95rem; font-weight:700; flex:1; }}
  .modal-tabs {{ display:flex; gap:.25rem; }}
  .tab-btn {{ padding:.3rem .75rem; border:1px solid var(--border); border-radius:.4rem;
    background:transparent; cursor:pointer; font-size:.8rem; font-weight:600; color:var(--muted); }}
  .tab-btn.active {{ background:#1a202c; color:#fff; border-color:#1a202c; }}
  .modal-close {{ margin-left:.5rem; padding:.3rem .7rem; border:none; border-radius:.4rem;
    cursor:pointer; background:#e2e8f0; font-size:.9rem; font-weight:700; }}
  .modal-body {{ flex:1; overflow:auto; padding:1rem 1.2rem; }}
  .diag-section {{ margin-bottom:1.25rem; }}
  .diag-section h4 {{ font-size:.7rem; font-weight:700; letter-spacing:.07em;
    text-transform:uppercase; color:var(--muted); margin-bottom:.5rem; }}
  pre.diag-pre {{ background:#1a202c; color:#e2e8f0; border-radius:.5rem;
    padding:.85rem 1rem; font-size:.75rem; line-height:1.5; overflow:auto;
    white-space:pre-wrap; word-break:break-all; }}
  table.reg-table {{ width:100%; border-collapse:collapse; font-size:.8rem; }}
  table.reg-table th {{ background:#f7fafc; text-align:left; padding:.35rem .6rem;
    border-bottom:2px solid var(--border); font-size:.7rem; color:var(--muted); }}
  table.reg-table td {{ padding:.35rem .6rem; border-bottom:1px solid var(--border); }}
  table.reg-table tr:last-child td {{ border-bottom:none; }}
  .diag-ts {{ font-size:.7rem; color:var(--muted); margin-bottom:.5rem; }}
  /* ── Battery Guard ───────────────────────────────────────── */
  .guard-bar-track {{ background:#e2e8f0; border-radius:.5rem; height:.55rem; flex:1; overflow:hidden; }}
  .guard-bar-fill  {{ height:100%; border-radius:.5rem; transition:width .4s,background .4s; }}
  .guard-row {{ display:flex; align-items:center; gap:.75rem; margin-top:.5rem; font-size:.8rem; }}
  .guard-soc-label {{ min-width:2.5rem; text-align:right; font-weight:700; font-size:.85rem; }}
  .guard-badge {{ padding:.2rem .6rem; border-radius:999px; font-size:.7rem; font-weight:700; }}
  .guard-badge-ok {{ background:#c6f6d5; color:#22543d; }}
  .guard-badge-warn {{ background:#fefcbf; color:#744210; }}
  .guard-badge-limit {{ background:#fed7d7; color:#742a2a; }}
  .guard-meta {{ font-size:.72rem; color:var(--muted); margin-top:.35rem; line-height:1.5; }}
  /* ── Toggle switch ───────────────────────────────────────── */
  .toggle {{ position:relative; display:inline-block; width:2.2rem; height:1.2rem; flex-shrink:0; }}
  .toggle input {{ opacity:0; width:0; height:0; }}
  .toggle-slider {{ position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0;
    background:#cbd5e0; border-radius:1rem; transition:background .2s; }}
  .toggle-slider:before {{ content:''; position:absolute; height:.85rem; width:.85rem;
    bottom:.18rem; left:.18rem; background:#fff; border-radius:50%; transition:transform .2s; }}
  input:checked + .toggle-slider {{ background:#48bb78; }}
  input:checked + .toggle-slider:before {{ transform:translateX(1rem); }}
  @media (max-width: 500px) {{ .form-grid {{ grid-template-columns: 1fr; }} }}
  /* ── Tooltips ────────────────────────────────────────────────── */
  .tip {{ display:inline-flex; align-items:center; justify-content:center;
    width:1.1em; height:1.1em; border-radius:50%; font-size:.62rem; font-weight:700;
    cursor:default; color:#718096; border:1px solid #cbd5e0; background:#edf2f7;
    position:relative; vertical-align:middle; margin-left:.25rem;
    flex-shrink:0; line-height:1; }}
  .tip::after {{ content:attr(data-tip); position:absolute;
    bottom:calc(100% + 7px); left:50%; transform:translateX(-50%);
    width:220px; background:#1a202c; color:#e2e8f0;
    font-size:.72rem; font-weight:400; line-height:1.45; padding:.5rem .7rem;
    border-radius:.4rem; box-shadow:0 4px 12px rgba(0,0,0,.35);
    opacity:0; pointer-events:none; transition:opacity .15s;
    z-index:600; text-transform:none; letter-spacing:normal; white-space:normal; }}
  .tip:hover::after {{ opacity:1; }}
  .tip-right::after {{ left:auto; right:0; transform:none; }}
  /* modal-body has overflow:auto — flip tips downward to avoid clipping */
  .modal .tip::after {{ bottom:auto; top:calc(100% + 7px); }}
</style>
</head>
<body>
<header>
  <h1>&#9728;&#65039; SolarCharge</h1>
  <span id="mode-badge">—</span>
  <button class="btn btn-ghost" style="margin-left:auto;font-size:.8rem;padding:.3rem .75rem" onclick="openDiag()">&#128270; Diagnostics</button>
  <button class="btn btn-ghost" style="font-size:.8rem;padding:.3rem .75rem" onclick="openChart()">&#128200; Chart</button>
  <a href="/reports" class="btn btn-ghost" style="font-size:.8rem;padding:.3rem .75rem;color:#000;text-decoration:none">&#9889; Charging History</a>
  <span id="last-updated" style="font-size:.75rem;color:#a0aec0">—</span>
</header>
<main>

  <div>
    <div class="section-title">Power Flow</div>
    <div class="cards">
      <div class="card">
        <div class="accent accent-solar"></div>
        <div class="label">Solar <span class="tip" data-tip="Current output from your solar panels in watts.">i</span></div>
        <div class="value" id="solar-w">—</div>
        <div class="unit">W</div>
      </div>
      <div class="card">
        <div class="accent accent-grid"></div>
        <div class="label">Grid <span class="tip" data-tip="Power exchanged with the utility grid. Positive value = importing (buying), negative = exporting (selling back).">i</span></div>
        <div class="value" id="grid-w">—</div>
        <div class="unit" id="grid-dir">W</div>
      </div>
      <div class="card">
        <div class="accent accent-bat"></div>
        <div class="label">Battery <span class="tip" data-tip="Home battery power flow. Positive = battery charging, negative = battery discharging. State-of-charge (SoC %) is shown below the value.">i</span></div>
        <div class="value" id="bat-w">—</div>
        <div class="unit"><span id="bat-dir">W</span> &nbsp;<span id="bat-soc" style="font-size:.75rem;color:var(--muted)"></span></div>
      </div>
      <div class="card">
        <div class="accent accent-house"></div>
        <div class="label">House <span class="tip" data-tip="Household electricity consumption, excluding EV charging power.">i</span></div>
        <div class="value" id="house-w">—</div>
        <div class="unit">W</div>
      </div>
      <div class="card">
        <div class="accent accent-surplus"></div>
        <div class="label">EV Surplus <span class="tip" data-tip="Clean surplus available for EV charging: solar output minus house load minus grid import. The derived target charge current (A) is shown below.">i</span></div>
        <div class="value" id="surplus-w">—</div>
        <div class="unit">W &nbsp;<span id="target-a" style="font-size:.75rem;color:var(--muted)"></span></div>
        <div style="font-size:.75rem;color:var(--green);min-height:1.1em" id="surplus-kwh"></div>
      </div>
    </div>
  </div>

  <div>
    <div class="section-title">Charging Status</div>
    <div class="status-row">
      <span class="status-badge badge-no-vehicle" id="car-badge">No vehicle</span>
      <div class="stat">
        <span class="s-label">Setpoint <span class="tip" data-tip="Charge current (A) currently commanded to the wallbox by the solar controller.">i</span>
          <span id="setpoint-override-pill" class="status-badge badge-override" style="display:none;font-size:.62rem;padding:.1rem .45rem;margin-left:.3rem;vertical-align:middle">Override</span>
        </span>
        <span class="s-value" id="setpoint-a">— A (— kW)</span>
      </div>
      <div class="stat">
        <span class="s-label">Charging Power <span class="tip" data-tip="Actual power being delivered to the EV right now, as measured by the wallbox meter.">i</span></span>
        <span class="s-value" id="wb-power">— W</span>
      </div>
      <div class="stat">
        <span class="s-label">Wallbox State <span class="tip tip-right" data-tip="Raw status reported by the Alfen firmware, e.g. Charging Normal, EV Connected, Available.">i</span></span>
        <span class="s-value" id="wb-state">—</span>
      </div>
      <div class="stat">
        <span class="s-label">Session <span class="tip tip-right" data-tip="Energy delivered to the EV in the current charging session, read from the wallbox meter.">i</span></span>
        <span class="s-value" id="session-kwh">— kWh</span>
      </div>
    </div>
  </div>

  <div id="guard-panel" class="panel" style="display:none">
    <h3>Battery Guard <span class="tip" data-tip="Dynamically limits EV surplus to protect your home battery. Required SOC rises through the afternoon so the battery is sufficiently charged before sunset.">i</span>
      <span id="guard-badge" class="guard-badge guard-badge-ok" style="margin-left:.5rem">OK</span>
      <button class="btn btn-ghost" onclick="openGuard()" style="margin-left:auto;font-size:.75rem;padding:.2rem .6rem">Details &#8594;</button>
    </h3>
    <div class="guard-row">
      <span style="font-size:.75rem;color:var(--muted)">Battery</span>
      <span id="guard-soc-cur" class="guard-soc-label">—%</span>
      <div class="guard-bar-track">
        <div id="guard-bar-fill" class="guard-bar-fill" style="width:0%"></div>
      </div>
      <span style="font-size:.75rem;color:var(--muted)">Required&nbsp;</span>
      <span id="guard-soc-req" style="font-weight:700;font-size:.85rem;min-width:2.5rem">—%</span>
    </div>
    <div class="guard-row" style="margin-top:.4rem">
      <span style="font-size:.75rem;color:var(--muted)">Linear Factor <span class="tip" data-tip="When on: EV surplus scales gradually with battery SoC (0–1 factor). When off: Full-or-Off — EV gets the full surplus or nothing at all.">i</span></span>
      <label class="toggle" style="margin:0">
        <input type="checkbox" id="guard-linear-toggle" onchange="setGuardLinearMode(this.checked)">
        <span class="toggle-slider"></span>
      </label>
      <span id="guard-factor-label" style="font-size:.75rem;color:var(--muted)"></span>
      <span style="margin-left:auto;font-size:.75rem;color:var(--muted)" id="guard-sun">🌅 — &nbsp; 🌇 —</span>
    </div>
    <div class="guard-meta" id="guard-reason"></div>
  </div>

  <div id="rfid-panel" class="panel" style="display:none">
    <h3>RFID Card Guard <span class="tip" data-tip="When enabled, only cars presenting a listed RFID card may charge. Any vehicle with an unknown or unlisted card is stopped immediately.">i</span>
      <span style="margin-left:auto;display:flex;align-items:center;gap:.4rem">
        <span style="font-size:.75rem;color:var(--muted)">Enforce</span>
        <label class="toggle" style="margin:0">
          <input type="checkbox" id="rfid-enabled-toggle" onchange="setRfidEnabled(this.checked)">
          <span class="toggle-slider"></span>
        </label>
      </span>
    </h3>
    <div id="rfid-card-list" style="margin:.35rem 0 .5rem 0"></div>
    <div id="rfid-blocked" style="margin-top:.4rem;display:none">
      <div style="font-size:.75rem;color:var(--muted);font-weight:600;margin-bottom:.25rem">Blocked attempts</div>
      <div id="rfid-blocked-list"></div>
    </div>
    <div style="display:flex;gap:.4rem;margin-top:.4rem;align-items:center">
      <input id="rfid-add-uid" type="text" placeholder="Card UID (hex)" style="flex:1;padding:.28rem .5rem;border:1px solid var(--border);border-radius:.4rem;font-size:.8rem;font-family:monospace">
      <input id="rfid-add-name" type="text" placeholder="Name (optional)" style="flex:1;padding:.28rem .5rem;border:1px solid var(--border);border-radius:.4rem;font-size:.8rem">
      <button class="btn btn-primary" style="font-size:.78rem;padding:.3rem .7rem" onclick="rfidAddCard()">Add</button>
    </div>
    <div id="rfid-status" style="font-size:.75rem;color:var(--muted);margin-top:.3rem;min-height:1rem"></div>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;flex-wrap:wrap;margin-top:0">

    <div class="panel">
      <h3>Override</h3>
      <div class="field">
        <label>Force current (A) <span class="tip" data-tip="Bypass automatic solar control and charge at this fixed current. Set to 0 to stop charging. Useful when you want to charge at a fixed rate regardless of solar surplus.">i</span></label>
        <div class="range-row">
          <input type="range" id="ov-slider"
            min="0" max="{cfg.max_current_a}" step="1"
            value="{int(cfg.max_current_a / 2)}"
            oninput="document.getElementById('ov-val').textContent=this.value">
          <span id="ov-val">{int(cfg.max_current_a / 2)}</span>
        </div>
      </div>
      <div class="field" style="margin-top:.5rem">
        <label>Duration <span class="tip" data-tip="How long the override stays active. After this time the controller reverts to automatic solar-surplus mode. Choose Indefinite to keep it active until you click Resume Auto.">i</span></label>
        <select class="duration-select" id="ov-duration">
          <option value="">Indefinite</option>
          <option value="15">15 min</option>
          <option value="30">30 min</option>
          <option value="60">1 hour</option>
          <option value="120">2 hours</option>
        </select>
      </div>
      <div class="btn-row">
        <button class="btn btn-primary" onclick="doOverride('set_current')">Force Current</button>
        <button class="btn btn-ghost" onclick="doOverride('resume')">Resume Auto</button>
      </div>
      <div id="override-status"></div>
    </div>

    <div class="panel">
      <h3>Control Settings</h3>
      <div class="form-grid">
        <div class="field">
          <label>Start threshold (A) <span class="tip" data-tip="Minimum surplus current required to start a charging session. Prevents short-cycling when solar fluctuates near the EV's minimum current.">i</span></label>
          <input type="number" id="cfg-start" step="0.5" min="6">
        </div>
        <div class="field">
          <label>Stop threshold (A) <span class="tip" data-tip="If available surplus drops below this value, charging stops. Set lower than Start threshold to create hysteresis and reduce on/off toggling.">i</span></label>
          <input type="number" id="cfg-stop" step="0.5" min="0">
        </div>
        <div class="field">
          <label>Ramp step (A/cycle) <span class="tip" data-tip="Maximum change in charge current per poll cycle. Smaller values ramp smoothly; larger values react faster to sudden changes in solar output.">i</span></label>
          <input type="number" id="cfg-ramp" step="0.5" min="0.5">
        </div>
        <div class="field">
          <label>Poll interval (s) <span class="tip" data-tip="How often (in seconds) SENEC and the wallbox are polled, and the setpoint is adjusted. Minimum 10 s — the Alfen firmware needs time to apply each setpoint change.">i</span></label>
          <input type="number" id="cfg-poll" step="5" min="10">
        </div>
      </div>
      <div class="btn-row">
        <button class="btn btn-primary" onclick="saveConfig()">Save</button>
      </div>
      <div id="config-msg"></div>
    </div>

  </div>
</main>

<!-- ── Diagnostics modal ───────────────────────────────────────── -->
<div class="modal-overlay" id="diag-overlay" onclick="if(event.target===this)closeDiag()">
  <div class="modal">
    <div class="modal-header">
      <h2>&#128270; Diagnostics — last poll cycle</h2>
      <div class="modal-tabs">
        <button class="tab-btn active" id="tab-senec" onclick="switchTab('senec')">SENEC</button>
        <button class="tab-btn" id="tab-alfen" onclick="switchTab('alfen')">Alfen</button>
      </div>
      <button class="modal-close" onclick="closeDiag()">&#x2715;</button>
    </div>
    <div class="modal-body">

      <!-- SENEC tab -->
      <div id="diag-senec">
        <div class="diag-section">
          <h4>Endpoint</h4>
          <div class="diag-ts" id="diag-senec-ts">—</div>
          <pre class="diag-pre" id="diag-senec-url">—</pre>
        </div>
        <div class="diag-section">
          <h4>Request body (POST /lala.cgi)</h4>
          <pre class="diag-pre" id="diag-senec-req">—</pre>
        </div>
        <div class="diag-section">
          <h4>Raw response (hex-encoded)</h4>
          <pre class="diag-pre" id="diag-senec-resp">—</pre>
        </div>
      </div>

      <!-- Alfen tab -->
      <div id="diag-alfen" style="display:none">
        <div class="diag-section">
          <h4>Wallbox host</h4>
          <div class="diag-ts" id="diag-alfen-ts">—</div>
          <pre class="diag-pre" id="diag-alfen-host">—</pre>
        </div>
        <div class="diag-section">
          <h4>Property reads</h4>
          <table class="reg-table" id="diag-alfen-reads">
            <thead><tr><th>ID</th><th>Label</th><th>Value</th><th>Decoded</th><th>Unit</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="diag-section">
          <h4>Property writes</h4>
          <table class="reg-table" id="diag-alfen-writes">
            <thead><tr><th>ID</th><th>Label</th><th>Value</th><th>Value Written</th><th>Unit</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

    </div>
  </div>
</div>

<!-- ── Battery Guard modal ──────────────────────────────────── -->
<div class="modal-overlay" id="guard-overlay" onclick="if(event.target===this)closeGuard()">
  <div class="modal" style="width:min(92vw,560px)">
    <div class="modal-header">
      <h2>&#128274; Battery Guard &mdash; details</h2>
      <span id="gm-badge" class="guard-badge guard-badge-ok" style="margin-left:.25rem">OK</span>
      <button class="modal-close" onclick="closeGuard()">&#x2715;</button>
    </div>
    <div class="modal-body">

      <div class="diag-section">
        <h4>State of Charge</h4>
        <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:.6rem">
          <span style="font-size:.8rem;color:var(--muted)">Current <span class="tip" data-tip="Live battery state-of-charge reported by SENEC.">i</span></span>
          <strong id="gm-soc-cur" style="font-size:1.4rem;min-width:3rem">—%</strong>
          <div class="guard-bar-track" style="flex:1;height:.85rem">
            <div id="gm-bar-fill" class="guard-bar-fill" style="width:0%"></div>
          </div>
          <span style="font-size:.8rem;color:var(--muted)">Required <span class="tip" data-tip="Minimum SoC the guard wants the battery to hold right now, based on time of day, sunset proximity, cloud cover, seasonal correction and tomorrow's forecast.">i</span></span>
          <strong id="gm-soc-req" style="font-size:1.4rem;min-width:3rem">—%</strong>
        </div>
        <div style="font-size:.75rem;color:var(--muted)" id="gm-bar-label"></div>
      </div>

      <div class="diag-section">
        <h4>Guard values</h4>
        <table class="reg-table">
          <tbody>
            <tr>
              <td>Mode <span class="tip" data-tip="Linear Factor: surplus is scaled gradually (0–1×) as SoC rises from the hard-min floor to the required target — smooth proportional charging. Full-or-Off: EV receives the full surplus when SoC ≥ required, or nothing at all otherwise — maximises power during peak sun.">i</span></td>
              <td><strong id="gm-mode">Linear factor</strong></td>
              <td style="text-align:right">
                <label class="toggle" style="margin:0">
                  <input type="checkbox" id="gm-linear-toggle" onchange="setGuardLinearMode(this.checked)">
                  <span class="toggle-slider"></span>
                </label>
              </td>
            </tr>
            <tr><td>Surplus factor <span class="tip" data-tip="Multiplier (0–1) applied to the available EV surplus this cycle. 1.0 = unrestricted charging; 0.0 = EV charging blocked to protect the battery.">i</span></td><td><strong id="gm-factor">—</strong></td><td></td></tr>
            <tr><td>Sunrise <span class="tip" data-tip="Today's calculated sunrise time at your configured location. The guard only applies the daytime reserve after this time.">i</span></td><td id="gm-sunrise">—</td><td></td></tr>
            <tr><td>Effective sunset <span class="tip" data-tip="Sunset time after advancing for heavy cloud cover. The afternoon SoC ramp targets this time rather than the astronomical sunset, giving the battery more time to recover on overcast days.">i</span></td><td id="gm-sunset">—</td><td></td></tr>
            <tr><td>Cloud cover <span class="tip" data-tip="Average cloud cover (%) across the remaining daylight hours today, from the Open-Meteo free forecast API. Values above the overcast threshold advance the effective sunset.">i</span></td><td id="gm-cloud">—</td><td></td></tr>
            <tr><td>Tomorrow cloud cover <span class="tip" data-tip="Average cloud cover (%) across all 24 hours of tomorrow. High values trigger a night-reserve boost so the battery enters a solar-poor day as full as possible.">i</span></td><td id="gm-tomorrow-cloud">—</td><td></td></tr>
            <tr><td>Tomorrow night reserve boost <span class="tip" data-tip="Extra % added to tonight's night reserve because tomorrow is forecast to be overcast or rainy. Scales linearly from 0 at the overcast threshold to the configured maximum at 100% cloud cover.">i</span></td><td id="gm-tomorrow-boost">—</td><td></td></tr>
            <tr><td>Seasonal correction <span class="tip" data-tip="Extra % added to the night reserve in winter months. Follows a cosine curve: maximum around January, zero around July, to compensate for shorter days and weaker sun.">i</span></td><td id="gm-seasonal">—</td><td></td></tr>
          </tbody>
        </table>
      </div>

      <div class="diag-section">
        <h4>Reason</h4>
        <pre class="diag-pre" id="gm-reason" style="font-size:.8rem;white-space:pre-wrap">—</pre>
      </div>

      <div class="diag-section">
        <h4>How it works</h4>
        <p style="font-size:.78rem;color:var(--muted);line-height:1.6;margin:0">
          During peak solar hours a low <em>daytime reserve</em> is required. Starting a
          configurable number of hours before (effective) sunset, the required SoC ramps
          linearly up to the <em>night reserve</em> target. Heavy cloud cover advances the
          effective sunset. A winter seasonal correction raises both targets in the darker
          months. When tomorrow&#39;s forecast is overcast or rainy, the night reserve is
          boosted proportionally (up to a configurable maximum) so the battery enters
          the next day as full as possible.<br><br>
          The <strong>Linear Factor</strong> toggle controls how surplus is applied when
          the battery SoC is below the required target.<br>
          <strong>On</strong> &mdash; surplus scales gradually from 0&times; at the hard
          minimum floor to 1&times; once the required SoC is met, giving smooth
          proportional charging.<br>
          <strong>Off</strong> &mdash; Full-or-Off mode: the EV receives the full surplus
          when the required SoC is met, or nothing at all when the battery needs
          protection &mdash; maximising charging power during high-solar periods.
        </p>
      </div>

    </div>
  </div>
</div>

<!-- ── Chart modal ───────────────────────────────────────── -->
<div class="modal-overlay" id="chart-overlay" onclick="if(event.target===this)closeChart()">
  <div class="modal" style="width:min(98vw,1200px);height:min(90vh,700px);display:flex;flex-direction:column">
    <div class="modal-header">
      <h2>&#128200; Power Graph — SENEC &amp; Charging</h2>
      <button class="modal-close" style="margin-left:auto" onclick="closeChart()">&#x2715;</button>
    </div>
    <div class="chart-controls">
      <div>
        <label style="font-weight:700;display:block;margin-bottom:.3rem">Time range</label>
        <div style="display:flex;gap:.3rem" id="range-btns">
          <button class="range-btn active" onclick="setRange('1h')">1h</button>
          <button class="range-btn" onclick="setRange('1d')">1d</button>
          <button class="range-btn" onclick="setRange('1w')">1w</button>
          <button class="range-btn" onclick="setRange('1mo')">1mo</button>
          <button class="range-btn" onclick="setRange('1y')">1y</button>
        </div>
      </div>
      <div style="flex:1">
        <label style="font-weight:700;display:block;margin-bottom:.3rem">Fields</label>
        <div class="field-grid" id="field-checks"></div>
      </div>
      <div style="align-self:flex-end">
        <button class="btn btn-ghost" style="font-size:.8rem" onclick="refreshChart()">&#8635; Refresh</button>
      </div>
    </div>
    <div class="chart-wrap">
      <canvas id="senec-chart"></canvas>
    </div>
  </div>
</div>

<script>
const REFRESH_MS = {cfg.poll_interval_s * 1000};

async function fetchStatus() {{
  try {{
    const r = await fetch('/api/status');
    const d = await r.json();
    applyStatus(d);
  }} catch(e) {{
    document.getElementById('last-updated').textContent = 'Connection error';
  }}
}}

function fmt(w) {{ return Math.abs(w) >= 1000 ? (w/1000).toFixed(1)+'k' : Math.round(w).toString(); }}

const PHASES = {cfg.phases};
const VOLTS  = {cfg.voltage_per_phase};

function applyStatus(d) {{
  // Header
  const mb = document.getElementById('mode-badge');
  if (d.calc_only) {{ mb.textContent = 'Calc-only'; mb.className = 'calc'; }}
  else {{ mb.textContent = 'Live'; mb.className = 'live'; }}
  document.getElementById('last-updated').textContent =
    d.timestamp ? 'Updated ' + new Date(d.timestamp).toLocaleTimeString() : '—';

  // Power cards
  document.getElementById('solar-w').textContent = fmt(d.solar_w);
  document.getElementById('grid-w').textContent = fmt(Math.abs(d.grid_w));
  document.getElementById('grid-dir').textContent = d.grid_w > 50 ? 'W  import' : d.grid_w < -50 ? 'W  export' : 'W  balanced';
  document.getElementById('bat-w').textContent = fmt(Math.abs(d.battery_w));
  document.getElementById('bat-dir').textContent = d.battery_w > 50 ? 'W  charging' : d.battery_w < -50 ? 'W  discharging' : 'W  idle';
  document.getElementById('bat-soc').textContent = d.battery_soc_pct.toFixed(0) + '% SoC';
  document.getElementById('house-w').textContent = fmt(d.house_w);
  document.getElementById('surplus-w').textContent = fmt(d.surplus_w);
  document.getElementById('target-a').textContent = '→ ' + d.target_a.toFixed(1) + ' A';
  const surplusKwh = d.surplus_w > 0 ? (d.surplus_w / 1000) : 0;
  document.getElementById('surplus-kwh').textContent = surplusKwh > 0 ? surplusKwh.toFixed(2) + ' kW available' : '';
  // Charging status
  const setpointKw = (d.setpoint_a * PHASES * VOLTS / 1000);
  document.getElementById('setpoint-a').textContent =
    d.setpoint_a.toFixed(1) + ' A (' + setpointKw.toFixed(1) + ' kW)';
  document.getElementById('wb-power').textContent = fmt(d.wallbox_power_w) + ' W';
  document.getElementById('wb-state').textContent = d.car_status_raw || '—';
  document.getElementById('session-kwh').textContent = d.charging_active && d.session_kwh > 0 ? d.session_kwh.toFixed(2) + ' kWh' : '—';

  const badge = document.getElementById('car-badge');
  let bc = 'badge-no-vehicle', bt = d.car_status;
  if (d.car_status === 'charging') {{ bc = 'badge-charging'; bt = 'Charging'; }}
  else if (d.car_status === 'connected') {{ bc = 'badge-connected'; bt = 'Connected'; }}
  else if (d.car_status === 'fault') {{ bc = 'badge-fault'; bt = 'Fault'; }}
  else if (d.calc_only && d.charging_active) {{ bc = 'badge-charging'; bt = 'Would charge'; }}
  badge.className = 'status-badge ' + bc;
  badge.textContent = bt;

  // Override indicator on the Setpoint label
  const ovPill = document.getElementById('setpoint-override-pill');
  ovPill.style.display = d.override_active ? '' : 'none';

  // Override status line
  const ovs = document.getElementById('override-status');
  if (d.override_active) {{
    ovs.textContent = d.override_current_a === 0
      ? 'Charging stopped'
        + (d.override_until ? ' until ' + new Date(d.override_until).toLocaleTimeString() : '')
      : d.override_current_a.toFixed(1) + ' A forced'
        + (d.override_until ? ' until ' + new Date(d.override_until).toLocaleTimeString() : '');
  }} else {{
    ovs.textContent = 'Auto mode active';
  }}

  // Battery Guard
  const gp = document.getElementById('guard-panel');
  if (d.guard_enabled) {{
    gp.style.display = '';
    const cur = d.battery_soc_pct;
    const req = d.guard_required_soc;
    const factor = d.guard_factor;
    const linear = d.guard_linear_mode;
    document.getElementById('guard-soc-cur').textContent = cur.toFixed(0) + '%';
    document.getElementById('guard-soc-req').textContent = req.toFixed(0) + '%';
    // toggle state (avoid triggering onchange)
    const bt = document.getElementById('guard-linear-toggle');
    if (bt.checked !== linear) bt.checked = linear;
    // factor label next to toggle
    const fl = document.getElementById('guard-factor-label');
    if (!linear) {{
      fl.textContent = factor >= 1 ? 'Full surplus' : 'Blocked';
    }} else {{
      fl.textContent = 'factor ' + factor.toFixed(2) + '\u00d7';
    }}
    // bar shows current SOC relative to required
    const barPct = req > 0 ? Math.min(100, (cur / req) * 100) : 100;
    const fill = document.getElementById('guard-bar-fill');
    fill.style.width = barPct.toFixed(1) + '%';
    fill.style.background = factor >= 1 ? '#68d391' : factor > 0.3 ? '#f6ad55' : '#fc8181';
    // badge
    const badge = document.getElementById('guard-badge');
    if (factor >= 1)      {{ badge.textContent = 'OK';       badge.className = 'guard-badge guard-badge-ok'; }}
    else if (factor > 0)  {{ badge.textContent = 'Limiting'; badge.className = 'guard-badge guard-badge-warn'; }}
    else                  {{ badge.textContent = 'Blocked';  badge.className = 'guard-badge guard-badge-limit'; }}
    // sun times + cloud
    let sunText = '\U0001F305 ' + (d.guard_sunrise || '\u2014') + '  \U0001F307 ' + (d.guard_sunset || '\u2014');
    if (d.guard_cloud_pct !== null) sunText += '  \u2601\ufe0f ' + d.guard_cloud_pct.toFixed(0) + '%';
    if (d.guard_tomorrow_boost > 0.5) sunText += '  \U0001F327\ufe0f tmrw +' + d.guard_tomorrow_boost.toFixed(0) + '%';
    if (d.guard_seasonal_extra > 0) sunText += '  \u2744\ufe0f +' + d.guard_seasonal_extra.toFixed(0) + '%';
    document.getElementById('guard-sun').textContent = sunText;
    document.getElementById('guard-reason').textContent = d.guard_reason || '';
    // also refresh modal if open
    _refreshGuardModal(d);
  }} else {{
    gp.style.display = 'none';
  }}
}}

function _refreshGuardModal(d) {{
  const factor = d.guard_factor;
  const linear = d.guard_linear_mode;
  const cur = d.battery_soc_pct;
  const req = d.guard_required_soc;
  const badge = document.getElementById('gm-badge');
  if (factor >= 1)     {{ badge.textContent = 'OK';       badge.className = 'guard-badge guard-badge-ok'; }}
  else if (factor > 0) {{ badge.textContent = 'Limiting'; badge.className = 'guard-badge guard-badge-warn'; }}
  else                 {{ badge.textContent = 'Blocked';  badge.className = 'guard-badge guard-badge-limit'; }}
  document.getElementById('gm-soc-cur').textContent = cur.toFixed(0) + '%';
  document.getElementById('gm-soc-req').textContent = req.toFixed(0) + '%';
  document.getElementById('gm-factor').innerHTML = factor.toFixed(2) + ' &times;';
  // sync both toggles
  const bt = document.getElementById('guard-linear-toggle');
  const gmt = document.getElementById('gm-linear-toggle');
  if (bt.checked !== linear) bt.checked = linear;
  if (gmt.checked !== linear) gmt.checked = linear;
  document.getElementById('gm-mode').textContent = linear ? 'Linear factor' : 'Full-or-Off';
  const barPct = req > 0 ? Math.min(100, (cur / req) * 100) : 100;
  const fill = document.getElementById('gm-bar-fill');
  fill.style.width = barPct.toFixed(1) + '%';
  fill.style.background = factor >= 1 ? '#68d391' : factor > 0.3 ? '#f6ad55' : '#fc8181';
  const deficit = req - cur;
  document.getElementById('gm-bar-label').textContent =
    deficit > 0 ? deficit.toFixed(0) + '% below required \u2014 guard active' : 'Battery meets required SoC';
  document.getElementById('gm-sunrise').textContent = d.guard_sunrise || '\u2014';
  document.getElementById('gm-sunset').textContent = d.guard_sunset || '\u2014';
  document.getElementById('gm-cloud').textContent =
    d.guard_cloud_pct !== null ? d.guard_cloud_pct.toFixed(0) + '%' : 'n/a (forecast disabled)';
  document.getElementById('gm-tomorrow-cloud').textContent =
    d.guard_tomorrow_cloud_pct !== null ? d.guard_tomorrow_cloud_pct.toFixed(0) + '%' : 'n/a';
  document.getElementById('gm-tomorrow-boost').textContent =
    d.guard_tomorrow_boost > 0.5 ? '+' + d.guard_tomorrow_boost.toFixed(1) + '% (rainy day expected)' : 'none';
  document.getElementById('gm-seasonal').textContent =
    d.guard_seasonal_extra > 0 ? '+' + d.guard_seasonal_extra.toFixed(1) + '%' : 'none (summer)';
  document.getElementById('gm-reason').textContent = d.guard_reason || '\u2014';
}}

function openGuard() {{
  document.getElementById('guard-overlay').classList.add('open');
}}
function closeGuard() {{
  document.getElementById('guard-overlay').classList.remove('open');
}}

async function setGuardLinearMode(enabled) {{
  // Keep both toggles in sync immediately (avoid flicker from the next poll)
  document.getElementById('guard-linear-toggle').checked = enabled;
  document.getElementById('gm-linear-toggle').checked = enabled;
  const r = await fetch('/api/guard', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{linear_mode: enabled}}),
  }});
  if (!r.ok) {{
    console.warn('Failed to update guard mode');
    // Revert on failure
    document.getElementById('guard-linear-toggle').checked = !enabled;
    document.getElementById('gm-linear-toggle').checked = !enabled;
  }}
  fetchStatus();
}}

async function fetchConfig() {{
  const r = await fetch('/api/config');
  const d = await r.json();
  document.getElementById('cfg-start').value = d.start_threshold_a;
  document.getElementById('cfg-stop').value  = d.stop_threshold_a;
  document.getElementById('cfg-ramp').value  = d.ramp_step_a;
  document.getElementById('cfg-poll').value  = d.poll_interval_s;
}}

async function saveConfig() {{
  const body = {{
    start_threshold_a: parseFloat(document.getElementById('cfg-start').value),
    stop_threshold_a:  parseFloat(document.getElementById('cfg-stop').value),
    ramp_step_a:       parseFloat(document.getElementById('cfg-ramp').value),
    poll_interval_s:   parseInt(document.getElementById('cfg-poll').value),
  }};
  const r = await fetch('/api/config', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }});
  const d = await r.json();
  const el = document.getElementById('config-msg');
  el.textContent = d.message || (r.ok ? 'Saved' : 'Error');
  el.className = r.ok ? 'msg-ok' : 'msg-err';
  setTimeout(() => {{ el.textContent = ''; }}, 4000);
}}

async function doOverride(action) {{
  const body = {{ action }};
  if (action === 'set_current') body.current_a = parseFloat(document.getElementById('ov-val').textContent);
  const dur = document.getElementById('ov-duration').value;
  if (dur) body.duration_minutes = parseFloat(dur);
  const r = await fetch('/api/override', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }});
  const d = await r.json();
  document.getElementById('override-status').textContent = d.message || (r.ok ? 'Done' : 'Error');
  fetchStatus();
}}

// ── RFID Card Guard ─────────────────────────────────────
let _rfidData = {{enabled: false, cards: []}};
function renderRfidPanel(d) {{
  _rfidData = d;
  const tog = document.getElementById('rfid-enabled-toggle');
  if (tog && tog.checked !== d.enabled) tog.checked = d.enabled;
  const list = document.getElementById('rfid-card-list');
  if (!list) return;
  if (!d.cards.length) {{
    list.innerHTML = '<span style="font-size:.78rem;color:var(--muted)">No cards configured — all cars will be blocked when guard is enabled.</span>';
  }} else {{
    list.innerHTML = d.cards.map((c, i) =>
      `<div style="display:flex;align-items:center;gap:.5rem;padding:.25rem 0;border-bottom:1px solid var(--border)">
        <span style="font-family:monospace;font-size:.78rem;color:#553c9a;letter-spacing:.04em;flex-shrink:0">${{c.uid}}</span>
        <span style="font-size:.8rem;color:var(--muted);flex:1">${{c.name || ''}}</span>
        <button class="btn btn-ghost" style="font-size:.7rem;padding:.12rem .4rem;color:var(--red)" onclick="rfidRemove(${{i}})">&#x2715;</button>
      </div>`
    ).join('');
  }}
}}
async function fetchRfid() {{
  try {{
    const [cfgR, blkR] = await Promise.all([fetch('/api/rfid'), fetch('/api/rfid/blocked')]);
    const d = await cfgR.json();
    const b = await blkR.json();
    document.getElementById('rfid-panel').style.display = '';
    renderRfidPanel(d);
    renderRfidBlocked(b.blocked || []);
  }} catch (_) {{}}
}}
function renderRfidBlocked(blocked) {{
  const wrap = document.getElementById('rfid-blocked');
  const list = document.getElementById('rfid-blocked-list');
  if (!wrap || !list) return;
  if (!blocked.length) {{ wrap.style.display = 'none'; return; }}
  wrap.style.display = '';
  list.innerHTML = blocked.map(b =>
    `<div style="display:flex;gap:.5rem;align-items:center;padding:.2rem 0;border-bottom:1px solid var(--border);font-size:.75rem">
      <span style="color:var(--muted);flex-shrink:0">${{b.ts}}</span>
      <span style="font-family:monospace;color:var(--red);letter-spacing:.04em;flex-shrink:0">${{b.uid}}</span>
      <span style="color:var(--muted)">${{b.name || ''}}</span>
    </div>`
  ).join('');
}}
async function setRfidEnabled(val) {{
  _rfidData.enabled = val;
  await rfidSave();
}}
async function rfidAddCard() {{
  const raw = document.getElementById('rfid-add-uid').value.trim();
  const uid = raw.toUpperCase().replace(/[^0-9A-F]/gi, '');
  const name = document.getElementById('rfid-add-name').value.trim();
  const st = document.getElementById('rfid-status');
  if (!uid) {{ st.textContent = 'UID is required.'; return; }}
  if (_rfidData.cards.some(c => c.uid === uid)) {{ st.textContent = 'Card already listed.'; return; }}
  _rfidData.cards.push({{uid, name}});
  document.getElementById('rfid-add-uid').value = '';
  document.getElementById('rfid-add-name').value = '';
  await rfidSave();
}}
async function rfidRemove(idx) {{
  _rfidData.cards.splice(idx, 1);
  await rfidSave();
}}
async function rfidSave() {{
  const r = await fetch('/api/rfid', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(_rfidData),
  }});
  const j = await r.json();
  const el = document.getElementById('rfid-status');
  el.textContent = j.ok ? 'Saved.' : (j.detail || 'Error saving');
  setTimeout(() => {{ if (el) el.textContent = ''; }}, 2500);
  if (j.ok) renderRfidPanel(_rfidData);
}}

fetchConfig();
fetchStatus();
fetchRfid();
setInterval(fetchStatus, REFRESH_MS);
setInterval(async () => {{
  try {{
    const r = await fetch('/api/rfid/blocked');
    renderRfidBlocked((await r.json()).blocked || []);
  }} catch (_) {{}}
}}, REFRESH_MS);

// ── Chart modal ────────────────────────────────────────────────
// Colours cycling through a palette
const _PALETTE = [
  '#3182ce','#38a169','#dd6b20','#805ad5','#e53e3e',
  '#00b5d8','#d69e2e','#319795','#97266d',
];

let _chart = null;
let _rangeKey = '1h';
let _chartFields = null;       // null = not yet initialised
let _fieldMeta = [];           // [{{key,label,unit}}]

// Range definitions: minutes=0 means no since-filter
const RANGES = {{
  '1h':  {{ minutes: 60,     groupBy: 'none', type: 'line' }},
  '1d':  {{ minutes: 1440,   groupBy: 'none', type: 'line' }},
  '1w':  {{ minutes: 10080,  groupBy: 'none', type: 'line' }},
  '1mo': {{ minutes: 43200,  groupBy: 'day',  type: 'bar'  }},
  '1y':  {{ minutes: 525600, groupBy: 'week', type: 'bar'  }},
}};

async function _ensureFieldMeta() {{
  if (_fieldMeta.length) return;
  const r = await fetch('/api/timeseries?minutes=1&max_points=1');
  const d = await r.json();
  _fieldMeta = d.field_meta || [];
  const defaults = ['solar_w','surplus_w','setpoint_a'];
  _chartFields = new Set(defaults);

  const container = document.getElementById('field-checks');
  container.innerHTML = '';
  _fieldMeta.forEach(f => {{
    const checked = defaults.includes(f.key) ? 'checked' : '';
    const el = document.createElement('label');
    el.className = 'field-check';
    el.innerHTML = `<input type="checkbox" ${{checked}} value="${{f.key}}"
      onchange="_toggleField('${{f.key}}',this.checked)">
      ${{f.label}} <span style="color:var(--muted);">(${{f.unit}})</span>`;
    container.appendChild(el);
  }});
}}

function _toggleField(key, on) {{
  if (!_chartFields) return;
  on ? _chartFields.add(key) : _chartFields.delete(key);
  refreshChart();
}}

function setRange(key) {{
  _rangeKey = key;
  document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  refreshChart();
}}

async function openChart() {{
  document.getElementById('chart-overlay').classList.add('open');
  await _ensureFieldMeta();
  await refreshChart();
}}

function closeChart() {{
  document.getElementById('chart-overlay').classList.remove('open');
}}

async function refreshChart() {{
  if (!_chartFields || !_chartFields.size) return;
  const r = RANGES[_rangeKey];
  const fields = [..._chartFields].join(',');
  let url = `/api/timeseries?fields=${{encodeURIComponent(fields)}}&max_points=600`
           + `&minutes=${{r.minutes}}&group_by=${{r.groupBy}}`;

  let data;
  try {{
    const resp = await fetch(url);
    data = await resp.json();
  }} catch(e) {{ console.error('Chart fetch failed', e); return; }}

  const metaMap = {{}};
  (_fieldMeta || []).forEach(f => metaMap[f.key] = f);

  // Format x-axis labels based on range type
  const labels = (data.timestamps || []).map(t => {{
    if (r.groupBy === 'day') {{
      // t is 'YYYY-MM-DDT00:00:00' — show 'Apr 7'
      const d = new Date(t);
      return d.toLocaleDateString(undefined, {{month:'short', day:'numeric'}});
    }} else if (r.groupBy === 'week') {{
      // t is 'YYYY-WNN' — show 'W14'
      const parts = t.split('-W');
      return parts.length === 2 ? 'W' + parts[1] : t;
    }} else if (r.minutes <= 120) {{
      const d = new Date(t);
      return d.toLocaleTimeString(undefined, {{hour:'2-digit', minute:'2-digit', second:'2-digit'}});
    }} else if (r.minutes <= 1440) {{
      const d = new Date(t);
      return d.toLocaleTimeString(undefined, {{hour:'2-digit', minute:'2-digit'}});
    }} else {{
      const d = new Date(t);
      return d.toLocaleString(undefined, {{month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}});
    }}
  }});

  const isBar = r.type === 'bar';

  const datasets = Object.entries(data.fields || {{}}).map(([key, values], i) => {{
    const meta = metaMap[key] || {{label: key, unit: ''}};
    const isPct = meta.unit === '%';
    const color = _PALETTE[i % _PALETTE.length];
    const ds = {{
      label: meta.label + (meta.unit ? ' (' + meta.unit + ')' : ''),
      data: values,
      borderColor: color,
      backgroundColor: color + (isBar ? 'cc' : '22'),
      borderWidth: isBar ? 1 : 1.5,
      yAxisID: isPct ? 'y1' : 'y',
    }};
    if (!isBar) {{
      ds.pointRadius = labels.length > 200 ? 0 : 2;
      ds.tension = 0.3;
      ds.fill = false;
    }}
    return ds;
  }});

  const hasPct = datasets.some(d => d.yAxisID === 'y1');

  const ctx = document.getElementById('senec-chart').getContext('2d');
  if (_chart) _chart.destroy();
  _chart = new Chart(ctx, {{
    type: r.type,
    data: {{ labels, datasets }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ position: 'top', labels: {{ boxWidth: 12, font: {{ size: 11 }} }} }},
        tooltip: {{
          callbacks: {{
            title: items => items[0]?.label || '',
          }},
        }},
      }},
      scales: {{
        x: {{
          ticks: {{
            maxTicksLimit: isBar ? 52 : 10,
            maxRotation: isBar ? 45 : 0,
            font: {{ size: 10 }},
          }},
        }},
        y: {{
          position: 'left',
          ticks: {{ font: {{ size: 10 }} }},
          title: {{ display: true, text: 'W / A', font: {{ size: 10 }} }},
          stacked: false,
        }},
        y1: {{
          display: hasPct,
          position: 'right',
          min: 0,
          max: 100,
          ticks: {{ font: {{ size: 10 }}, callback: v => v + '%' }},
          title: {{ display: true, text: '%', font: {{ size: 10 }} }},
          grid: {{ drawOnChartArea: false }},
        }},
      }},
    }},
  }});
}}

function switchTab(tab) {{
  _diagTab = tab;
  document.getElementById('diag-senec').style.display = tab === 'senec' ? '' : 'none';
  document.getElementById('diag-alfen').style.display = tab === 'alfen' ? '' : 'none';
  document.getElementById('tab-senec').className = 'tab-btn' + (tab==='senec'?' active':'');
  document.getElementById('tab-alfen').className = 'tab-btn' + (tab==='alfen'?' active':'');
}}

function _tableRows(data, valueKey) {{
  if (!data || !data.length) return '<tr><td colspan=5 style="color:var(--muted);padding:.5rem">No data yet</td></tr>';
  return data.map(r =>
    `<tr>
      <td><code>${{r.register}}</code></td>
      <td>${{r.label}}</td>
      <td><code>${{r.raw_hex || (r.raw_registers||[]).join(' ')}}</code></td>
      <td><strong>${{r[valueKey] ?? '—'}}</strong></td>
      <td>${{r.unit || '—'}}</td>
    </tr>`
  ).join('');
}}

// ── SENEC hex decoder ────────────────────────────────────────────────────────
// Prefix map: fl_ = IEEE-754 float32, u8/u1/u3/u6 = unsigned int, i1/i3 = signed int
function _decodeSenecVal(val) {{
  if (typeof val !== 'string') return null;
  const m = val.match(/^(fl|u8|u1|u3|u6|i1|i3|st)_([0-9a-fA-F]*)$/);
  if (!m) return null;
  const [, type, hex] = m;
  if (type === 'st') return '"' + hex + '"';
  if (type === 'fl') {{
    if (hex.length !== 8) return null;
    const buf = new ArrayBuffer(4);
    new DataView(buf).setUint32(0, parseInt(hex, 16), false);
    const f = new DataView(buf).getFloat32(0, false);
    return isFinite(f) ? String(parseFloat(f.toPrecision(7))) : String(f);
  }}
  return String(parseInt(hex, 16));   // integer types
}}

function _formatSenecJson(obj, depth) {{
  if (typeof obj !== 'object' || obj === null) return JSON.stringify(obj);
  depth = depth || 0;
  const pad  = '  '.repeat(depth);
  const ipad = '  '.repeat(depth + 1);
  const parts = Object.entries(obj).map(([k, v]) => {{
    const key = JSON.stringify(k);
    if (v && typeof v === 'object') {{
      return ipad + key + ': ' + _formatSenecJson(v, depth + 1);
    }}
    const raw     = JSON.stringify(v);
    const decoded = _decodeSenecVal(v);
    const suffix  = decoded !== null ? '   -> ' + decoded : '';
    return ipad + key + ': ' + raw + suffix;
  }});
  return '{{\\n' + parts.join(',\\n') + '\\n' + pad + '}}';
}}
// ─────────────────────────────────────────────────────────────────────────────

async function openDiag() {{
  document.getElementById('diag-overlay').classList.add('open');
  await refreshDiag();
}}

function closeDiag() {{
  document.getElementById('diag-overlay').classList.remove('open');
}}

async function refreshDiag() {{
  let d;
  try {{
    const r = await fetch('/api/diagnostics');
    d = await r.json();
  }} catch(e) {{
    document.getElementById('diag-senec-url').textContent = 'Error: ' + e;
    return;
  }}

  // SENEC
  const s = d.senec;
  document.getElementById('diag-senec-ts').textContent =
    s.timestamp ? 'Captured at ' + new Date(s.timestamp).toLocaleTimeString() : 'No data yet';
  document.getElementById('diag-senec-url').textContent = s.url || '(no data yet)';
  document.getElementById('diag-senec-req').textContent =
    s.request && Object.keys(s.request).length ? _formatSenecJson(s.request) : '(no data yet)';
  document.getElementById('diag-senec-resp').textContent =
    s.response_raw && Object.keys(s.response_raw).length ? _formatSenecJson(s.response_raw) : '(no data yet)';

  // Alfen
  const a = d.alfen;
  document.getElementById('diag-alfen-ts').textContent =
    a.timestamp ? 'Captured at ' + new Date(a.timestamp).toLocaleTimeString() : 'No data yet (wallbox not connected)';
  document.getElementById('diag-alfen-host').textContent = a.host || '(not configured)';
  document.getElementById('diag-alfen-reads').querySelector('tbody').innerHTML = _tableRows(a.reads, 'decoded_value');
  document.getElementById('diag-alfen-writes').querySelector('tbody').innerHTML = _tableRows(a.writes, 'value_written');
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
#  Reports page HTML
# ─────────────────────────────────────────────────────────────────────────────

def _build_reports_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Charging History \u2013 SolarCharge</title>
<style>
  :root {
    --bg:#f0f4f8; --card:#fff; --border:#dde3ea;
    --text:#1a202c; --muted:#718096;
    --green:#38a169; --red:#e53e3e; --blue:#3182ce;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:system-ui,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }
  header { background:#1a202c; color:#fff; padding:1rem 1.5rem; display:flex; align-items:center; gap:.75rem; }
  header h1 { font-size:1.1rem; font-weight:700; }
  a.back-btn { color:#a0aec0; text-decoration:none; font-size:.8rem;
    border:1px solid #4a5568; padding:.25rem .65rem; border-radius:.35rem; white-space:nowrap; }
  a.back-btn:hover { background:#2d3748; color:#fff; }
  main { max-width:1100px; margin:1.5rem auto; padding:0 1rem; display:grid; gap:1rem; }
  .filter-bar { background:var(--card); border:1px solid var(--border); border-radius:.75rem;
    padding:.9rem 1.25rem; display:flex; flex-wrap:wrap; gap:.75rem; align-items:center; }
  .range-presets { display:flex; gap:.35rem; flex-wrap:wrap; }
  .pill { padding:.28rem .7rem; border:1px solid var(--border); border-radius:999px;
    cursor:pointer; font-size:.8rem; background:#fff; font-weight:500; }
  .pill:hover { border-color:#a0aec0; }
  .pill.active { background:#1a202c; color:#fff; border-color:#1a202c; }
  .sep { color:var(--border); font-size:1.2rem; }
  .custom-range { display:flex; align-items:center; gap:.4rem; font-size:.8rem; }
  input[type=date] { border:1px solid var(--border); border-radius:.35rem; padding:.28rem .5rem; font-size:.8rem; }
  select.rfid-select { border:1px solid var(--border); border-radius:.4rem; padding:.28rem .55rem; font-size:.8rem; }
  .btn { padding:.35rem .9rem; border:none; border-radius:.4rem; cursor:pointer;
    font-size:.8rem; font-weight:600; transition:opacity .15s; }
  .btn:hover { opacity:.85; }
  .btn-ghost { background:#e2e8f0; color:#4a5568; }
  .summary-cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(155px,1fr)); gap:.75rem; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:.75rem; padding:.9rem 1rem; }
  .card .c-label { font-size:.68rem; color:var(--muted); text-transform:uppercase;
    letter-spacing:.06em; margin-bottom:.25rem; }
  .card .c-value { font-size:1.55rem; font-weight:700; line-height:1.1; }
  .card .c-unit  { font-size:.78rem; color:var(--muted); }
  .table-wrap { background:var(--card); border:1px solid var(--border);
    border-radius:.75rem; overflow:hidden; }
  .sessions-table { width:100%; border-collapse:collapse; font-size:.83rem; }
  .sessions-table thead tr { background:#f7fafc; }
  .sessions-table th { padding:.5rem .75rem; text-align:left;
    border-bottom:2px solid var(--border); font-size:.68rem; font-weight:700;
    color:var(--muted); white-space:nowrap; }
  .sessions-table td { padding:.48rem .75rem; border-bottom:1px solid var(--border);
    vertical-align:middle; }
  .sessions-table tr:last-child td { border-bottom:none; }
  .sessions-table tr:hover td { background:#f7fafc; }
  .chip { display:inline-block; padding:.18rem .55rem; border-radius:999px;
    font-size:.7rem; font-weight:600; white-space:nowrap; }
  .chip-ok   { background:#c6f6d5; color:#22543d; }
  .chip-live { background:#bee3f8; color:#2a4365; }
  .rfid-mono { font-family:monospace; font-size:.78rem; color:#553c9a; letter-spacing:.04em; }
  .empty-cell { padding:2.5rem; text-align:center; color:var(--muted); }
  .error-banner { background:#fed7d7; border:1px solid #fc8181; color:#742a2a;
    border-radius:.65rem; padding:.7rem 1rem; font-size:.85rem; }
  .notice { background:#fefcbf; border:1px solid #f6e05e; color:#744210;
    border-radius:.65rem; padding:.6rem 1rem; font-size:.8rem; }
  @media (max-width:540px) { .custom-range label { display:none; } }
  /* ── Tooltips ────────────────────────────────────────────── */
  .tip { display:inline-flex; align-items:center; justify-content:center;
    width:1.1em; height:1.1em; border-radius:50%; font-size:.62rem; font-weight:700;
    cursor:default; color:#718096; border:1px solid #cbd5e0; background:#edf2f7;
    position:relative; vertical-align:middle; margin-left:.25rem;
    flex-shrink:0; line-height:1; }
  .tip::after { content:attr(data-tip); position:absolute;
    bottom:calc(100% + 7px); left:50%; transform:translateX(-50%);
    width:220px; background:#1a202c; color:#e2e8f0;
    font-size:.72rem; font-weight:400; line-height:1.45; padding:.5rem .7rem;
    border-radius:.4rem; box-shadow:0 4px 12px rgba(0,0,0,.35);
    opacity:0; pointer-events:none; transition:opacity .15s;
    z-index:600; text-transform:none; letter-spacing:normal; white-space:normal; }
  .tip:hover::after { opacity:1; }
  .tip-right::after { left:auto; right:0; transform:none; }
  /* table-wrap has overflow:hidden for rounded corners — flip header tips downward
     so they pop into the table body rather than clipping above the container */
  .sessions-table th .tip::after { bottom:auto; top:calc(100% + 7px); }
</style>
</head>
<body>
<header>
  <a href="/" class="back-btn">&#8592; Dashboard</a>
  <h1>&#9889; Charging History</h1>
  <span id="last-fetch" style="margin-left:auto;font-size:.75rem;color:#a0aec0"></span>
  <button class="btn btn-ghost" onclick="fetchSessions()"
    style="margin-left:.5rem;font-size:.8rem;padding:.3rem .75rem">&#8635; Refresh</button>
</header>

<main>
<div id="error-banner" class="error-banner" style="display:none"></div>
<div id="clock-notice" class="notice" style="display:none">
  &#9888;&#65039;&nbsp; Some sessions show dates before 2020 &mdash; the wallbox clock was not
  synchronised when those sessions occurred. Timestamps reflect the wallbox&rsquo;s internal clock.
</div>

<!-- Filter bar -->
<div class="filter-bar">
  <div class="range-presets">
    <button class="pill" id="pill-today" onclick="setRange('today')">Today</button>
    <button class="pill" id="pill-week"  onclick="setRange('week')">Last week</button>
    <button class="pill" id="pill-month" onclick="setRange('month')">Last month</button>
    <button class="pill" id="pill-year"  onclick="setRange('year')">Last year</button>
    <button class="pill active" id="pill-all" onclick="setRange('all')">All time</button>
  </div>
  <span class="sep">|</span>
  <div class="custom-range">
    <label for="date-from" style="font-size:.78rem;color:var(--muted)">From</label>
    <input type="date" id="date-from" onchange="applyCustom()">
    <label for="date-to"   style="font-size:.78rem;color:var(--muted)">To</label>
    <input type="date" id="date-to"   onchange="applyCustom()">
  </div>
  <span class="sep">|</span>
  <select class="rfid-select" id="rfid-filter" onchange="renderSessions()">
    <option value="">All RFID tags</option>
  </select>
</div>

<!-- Summary cards -->
<div class="summary-cards">
  <div class="card">
    <div class="c-label">Sessions <span class="tip" data-tip="Total number of charging sessions in the selected date range.">i</span></div>
    <div class="c-value" id="sum-count">&#8212;</div>
  </div>
  <div class="card">
    <div class="c-label">Total Energy <span class="tip" data-tip="Sum of kWh delivered across all completed sessions in this period.">i</span></div>
    <div class="c-value" id="sum-energy">&#8212;</div>
    <div class="c-unit">kWh</div>
  </div>
  <div class="card">
    <div class="c-label">Avg per Session <span class="tip" data-tip="Average energy per completed session. In-progress sessions are excluded from this calculation.">i</span></div>
    <div class="c-value" id="sum-avg">&#8212;</div>
    <div class="c-unit">kWh</div>
  </div>
  <div class="card">
    <div class="c-label">Total Charge Time <span class="tip tip-right" data-tip="Combined duration of all completed sessions in the selected period, in hours.">i</span></div>
    <div class="c-value" id="sum-duration">&#8212;</div>
    <div class="c-unit">hours</div>
  </div>
</div>

<!-- Sessions table -->
<div class="table-wrap">
  <table class="sessions-table">
    <thead>
      <tr>
        <th>#</th>
        <th>Started <span class="tip" data-tip="Date and time the charging session began, as recorded by the wallbox clock.">i</span></th>
        <th>Ended <span class="tip" data-tip="Date and time the session ended. Blank if the session is still in progress.">i</span></th>
        <th>Duration <span class="tip" data-tip="Total wall-clock duration of the session (end time minus start time).">i</span></th>
        <th>Energy <span class="tip" data-tip="Total energy delivered to the EV during this session, calculated from the wallbox lifetime meter.">i</span></th>
        <th>Meter reading <span class="tip" data-tip="Wallbox lifetime energy meter values at session start and end (kWh). Useful for cross-checking with utility bills.">i</span></th>
        <th>RFID Tag <span class="tip" data-tip="Unique ID of the RFID card or tag used to authorise the session. Blank if no card was presented.">i</span></th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody id="sessions-tbody">
      <tr><td colspan="8" class="empty-cell">Loading&hellip;</td></tr>
    </tbody>
  </table>
</div>

</main>
<script>
'use strict';
let allSessions = [];
let activePreset = 'all';
let customFrom = null;
let customTo   = null;
const PILLS = ['today','week','month','year','all'];

function setRange(preset) {
  activePreset = preset;
  customFrom   = null;
  customTo     = null;
  document.getElementById('date-from').value = '';
  document.getElementById('date-to').value   = '';
  PILLS.forEach(p =>
    document.getElementById('pill-' + p).classList.toggle('active', p === preset)
  );
  renderSessions();
}

function applyCustom() {
  const from = document.getElementById('date-from').value;
  const to   = document.getElementById('date-to').value;
  if (!from && !to) return;
  activePreset = 'custom';
  customFrom   = from || null;
  customTo     = to   || null;
  PILLS.forEach(p => document.getElementById('pill-' + p).classList.remove('active'));
  renderSessions();
}

// Return YYYY-MM-DD string for a local date
function _isoDate(d) {
  return d.getFullYear() + '-'
    + String(d.getMonth() + 1).padStart(2, '0') + '-'
    + String(d.getDate()).padStart(2, '0');
}

// Return {start, end} ISO date strings for the given preset.
// Weeks are Mon–Sun; months and years are complete calendar periods.
function _getDateRange(preset) {
  const now = new Date();
  // Offset so Monday = 0, Tuesday = 1 … Sunday = 6
  const dowMon = (now.getDay() + 6) % 7;

  switch (preset) {
    case 'today': {
      const s = _isoDate(now);
      return { start: s, end: s };
    }
    case 'week': {
      // Last complete Mon–Sun week
      const thisMon = new Date(now);
      thisMon.setHours(0, 0, 0, 0);
      thisMon.setDate(now.getDate() - dowMon);
      const lastMon = new Date(+thisMon - 7 * 86400 * 1000);
      const lastSun = new Date(+thisMon - 1 * 86400 * 1000);
      return { start: _isoDate(lastMon), end: _isoDate(lastSun) };
    }
    case 'month': {
      // Last complete calendar month
      const start = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      const end   = new Date(now.getFullYear(), now.getMonth(), 0);
      return { start: _isoDate(start), end: _isoDate(end) };
    }
    case 'year': {
      // Last complete calendar year
      const y = now.getFullYear() - 1;
      return { start: y + '-01-01', end: y + '-12-31' };
    }
    case 'custom':
      return { start: customFrom, end: customTo };
    default:
      return { start: null, end: null };
  }
}

function _rangeStart() { return _getDateRange(activePreset).start; }
function _rangeEnd()   { return _getDateRange(activePreset).end; }

function fmtDt(ts) {
  if (!ts) return '\u2014';
  return ts.replace('T',' ').slice(0,16);
}

function fmtDuration(s) {
  if (s == null) return '\u2014';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (h > 0) return h + 'h\u202f' + m + 'm';
  if (m > 0) return m + 'm\u202f' + sec + 's';
  return sec + 's';
}

function fmtMeter(start, stop) {
  const a = start != null ? start.toFixed(3) : '?';
  const b = stop  != null ? stop.toFixed(3)  : '\u2026';
  return a + '\u202f\u2192\u202f' + b + '\u202fkWh';
}

function renderSessions() {
  const rs = _rangeStart();
  const re = _rangeEnd();
  const rfidFilter = document.getElementById('rfid-filter').value;

  const visible = allSessions.filter(s => {
    const dt = (s.started_at || '').slice(0,10);
    if (rs && dt < rs) return false;
    if (re && dt > re) return false;
    if (rfidFilter && s.rfid_tag !== rfidFilter) return false;
    return true;
  }).slice().reverse();     // newest first

  const done = visible.filter(s => s.status === 'completed' && s.energy_kwh != null);
  const totalEnergy   = done.reduce((a, s) => a + s.energy_kwh, 0);
  const totalDuration = done.reduce((a, s) => a + (s.duration_s || 0), 0);

  document.getElementById('sum-count').textContent    = visible.length;
  document.getElementById('sum-energy').textContent   = totalEnergy.toFixed(2);
  document.getElementById('sum-avg').textContent      = done.length ? (totalEnergy / done.length).toFixed(2) : '\u2014';
  document.getElementById('sum-duration').textContent = totalDuration ? (totalDuration / 3600).toFixed(1) : '\u2014';

  const tbody = document.getElementById('sessions-tbody');
  if (!visible.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">No sessions found for the selected range.</td></tr>';
    return;
  }

  tbody.innerHTML = visible.map(s => {
    const chip  = s.status === 'completed'
      ? '<span class="chip chip-ok">Completed</span>'
      : '<span class="chip chip-live">In progress</span>';
    const rfid  = s.rfid_tag
      ? '<span class="rfid-mono">' + s.rfid_tag + '</span>'
      : '<span style="color:var(--muted)">\u2014</span>';
    const nrg   = s.energy_kwh != null ? s.energy_kwh.toFixed(3) + '\u202fkWh' : '\u2014';
    return '<tr>'
      + '<td>' + s.id + '</td>'
      + '<td>' + fmtDt(s.started_at) + '</td>'
      + '<td>' + fmtDt(s.ended_at) + '</td>'
      + '<td>' + fmtDuration(s.duration_s) + '</td>'
      + '<td style="font-weight:600">' + nrg + '</td>'
      + '<td style="font-size:.75rem;color:var(--muted)">' + fmtMeter(s.start_meter_kwh, s.stop_meter_kwh) + '</td>'
      + '<td>' + rfid + '</td>'
      + '<td>' + chip + '</td>'
      + '</tr>';
  }).join('');
}

async function fetchSessions() {
  document.getElementById('sessions-tbody').innerHTML =
    '<tr><td colspan="8" class="empty-cell">Loading&hellip;</td></tr>';
  document.getElementById('error-banner').style.display = 'none';

  try {
    const r = await fetch('/api/wallbox-sessions');
    const d = await r.json();

    if (d.error) {
      document.getElementById('error-banner').textContent = 'Wallbox error: ' + d.error;
      document.getElementById('error-banner').style.display = '';
      document.getElementById('sessions-tbody').innerHTML =
        '<tr><td colspan="8" class="empty-cell">' + d.error + '</td></tr>';
      return;
    }

    allSessions = d.sessions || [];
    document.getElementById('last-fetch').textContent =
      'Fetched at ' + new Date().toLocaleTimeString();

    // Populate RFID dropdown from unique tags
    const tags = [...new Set(allSessions.map(s => s.rfid_tag).filter(Boolean))].sort();
    const sel  = document.getElementById('rfid-filter');
    const prev = sel.value;
    sel.innerHTML = '<option value="">All RFID tags (' + allSessions.length + ' total)</option>'
      + tags.map(t => '<option value="' + t + '">' + t + '</option>').join('');
    if (tags.includes(prev)) sel.value = prev;

    // Warn about sessions with un-synced wallbox clock
    const hasBadClock = allSessions.some(s => s.started_at && s.started_at < '2020');
    document.getElementById('clock-notice').style.display = hasBadClock ? '' : 'none';

    renderSessions();

  } catch (e) {
    document.getElementById('error-banner').textContent = 'Could not reach server: ' + e;
    document.getElementById('error-banner').style.display = '';
  }
}

fetchSessions();
</script>
</body>
</html>"""
