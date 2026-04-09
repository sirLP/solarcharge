"""
Entry point for the SolarCharge daemon.

Loads config.toml, validates settings, then runs the controller loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import tomllib
from pathlib import Path
from typing import Any

import uvicorn

from solar_charge.controller import Controller, ControllerConfig
from solar_charge.battery_guard import BatteryGuardConfig
from solar_charge.db import SolarDB
from solar_charge.history import HistoryStore
from solar_charge.state import AppState
from solar_charge.timeseries import TimeseriesStore
from solar_charge.web import create_app

_MIN_POLL_INTERVAL_S = 10
_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.toml"

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Config loading & validation
# ─────────────────────────────────────────────────────────────────────────────

def _load_config(path: Path) -> tuple[ControllerConfig, str, int]:
    """
    Load and validate config.toml.

    Returns
    -------
    (ControllerConfig, web_host, web_port)
    """
    if not path.exists():
        sys.exit(f"Config file not found: {path}")

    with path.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    senec = raw.get("senec", {})
    alfen = raw.get("alfen", {})
    ctrl  = raw.get("control", {})
    web   = raw.get("web", {})
    guard_raw = raw.get("battery_guard", {})

    poll_interval = int(senec.get("poll_interval_s", _MIN_POLL_INTERVAL_S))
    if poll_interval < _MIN_POLL_INTERVAL_S:
        raise ValueError(
            f"poll_interval_s must be ≥ {_MIN_POLL_INTERVAL_S} seconds "
            f"(got {poll_interval}). "
            "Polling faster risks disrupting SENEC's internal cloud sync."
        )

    max_a = float(alfen.get("max_current_a", 16))
    min_a = float(alfen.get("min_current_a", 6))
    release_a = float(alfen.get("release_current_a", 32))
    start_a = float(ctrl.get("start_threshold_a", 7))
    stop_a  = float(ctrl.get("stop_threshold_a", 5))

    if min_a < 6.0:
        raise ValueError(
            f"min_current_a must be ≥ 6.0 A (IEC 61851 minimum), got {min_a}"
        )
    if stop_a > start_a:
        raise ValueError(
            f"stop_threshold_a ({stop_a}) must be ≤ start_threshold_a ({start_a})"
        )
    if max_a > 32:
        raise ValueError(f"max_current_a={max_a} looks unreasonably high (max 32 A)")
    if release_a > 32:
        raise ValueError(f"release_current_a={release_a} looks unreasonably high (max 32 A)")

    ctrl_config = ControllerConfig(
        senec_host=senec.get("host", ""),
        use_https=bool(senec.get("use_https", True)),
        poll_interval_s=poll_interval,
        alfen_host=alfen.get("host", ""),
        alfen_mode=str(alfen.get("mode", "modbus")).lower(),
        alfen_username=str(alfen.get("username", "admin")),
        alfen_password=str(alfen.get("password", "")),
        alfen_port=int(alfen.get("port", 502)),
        alfen_slave_id=int(alfen.get("slave_id", 1)),
        phases=int(alfen.get("phases", 3)),
        voltage_per_phase=float(alfen.get("voltage_per_phase", 230)),
        max_current_a=max_a,
        min_current_a=min_a,
        release_current_a=release_a,
        start_threshold_a=start_a,
        stop_threshold_a=stop_a,
        ramp_step_a=float(ctrl.get("ramp_step_a", 1)),
        battery_guard=_parse_battery_guard(guard_raw),
    )
    return ctrl_config, web.get("host", "0.0.0.0"), int(web.get("port", 8080))


def _parse_battery_guard(raw: dict) -> BatteryGuardConfig | None:
    """Parse the optional [battery_guard] section."""
    if not raw:
        return None
    cfg = BatteryGuardConfig(
        enabled=bool(raw.get("enabled", False)),
        latitude=float(raw.get("latitude", 52.0)),
        longitude=float(raw.get("longitude", 5.0)),
        night_reserve_pct=float(raw.get("night_reserve_pct", 30.0)),
        daytime_reserve_pct=float(raw.get("daytime_reserve_pct", 10.0)),
        hard_min_pct=float(raw.get("hard_min_pct", 5.0)),
        ramp_hours_before_sunset=float(raw.get("ramp_hours_before_sunset", 3.0)),
        use_seasonal=bool(raw.get("use_seasonal", True)),
        seasonal_winter_extra_pct=float(raw.get("seasonal_winter_extra_pct", 15.0)),
        use_weather_forecast=bool(raw.get("use_weather_forecast", True)),
        weather_cache_minutes=int(raw.get("weather_cache_minutes", 60)),
        weather_overcast_threshold=float(raw.get("weather_overcast_threshold", 70.0)),
        weather_max_sunset_advance_h=float(raw.get("weather_max_sunset_advance_h", 2.0)),
        use_historic_solar=bool(raw.get("use_historic_solar", True)),
        historic_months_lookback=int(raw.get("historic_months_lookback", 3)),
        battery_max_charge_w=float(raw.get("battery_max_charge_w", 2500.0)),
        linear_mode=bool(raw.get("linear_mode", True)),
    )
    if cfg.hard_min_pct >= cfg.daytime_reserve_pct:
        raise ValueError(
            f"[battery_guard] hard_min_pct ({cfg.hard_min_pct}) must be "
            f"< daytime_reserve_pct ({cfg.daytime_reserve_pct})"
        )
    if cfg.daytime_reserve_pct >= cfg.night_reserve_pct:
        raise ValueError(
            f"[battery_guard] daytime_reserve_pct ({cfg.daytime_reserve_pct}) must be "
            f"< night_reserve_pct ({cfg.night_reserve_pct})"
        )
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
#  Async entry point
# ─────────────────────────────────────────────────────────────────────────────

async def _run(
    config: ControllerConfig,
    config_path: Path,
    web_host: str,
    web_port: int,
) -> None:
    app_state = AppState()
    db_dir = config_path.parent / "db"
    db_dir.mkdir(exist_ok=True)
    db_path = db_dir / "solar_charge.db"
    db = SolarDB(db_path)
    # Pass legacy history.json path so it gets migrated into SQLite on first run
    json_path = config_path.parent / "history.json"
    history = HistoryStore(db, json_path=json_path)
    ts_store = TimeseriesStore(db)
    app_state.history = history
    app_state.timeseries = ts_store
    controller = Controller(config, app_state, history=history, timeseries=ts_store)
    fastapi_app = create_app(app_state, config, config_path)

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _shutdown(sig: signal.Signals) -> None:
        log.info("Received %s — initiating graceful shutdown …", sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    uv_config = uvicorn.Config(
        fastapi_app,
        host=web_host,
        port=web_port,
        log_level="warning",   # suppress uvicorn access logs; SolarCharge has its own
    )
    uv_server = uvicorn.Server(uv_config)

    log.info("Web UI available at http://%s:%d", web_host if web_host != "0.0.0.0" else "localhost", web_port)

    ctrl_task = asyncio.create_task(controller.run())
    uv_task   = asyncio.create_task(uv_server.serve())

    # Block until a signal arrives
    await shutdown_event.wait()

    # ── Graceful teardown ──────────────────────────────────────────────
    # 1. Restore wallbox to unlimited and close connections
    await controller.stop()

    # 2. Ask uvicorn to exit cleanly and wait for it to drain naturally.
    #    Do NOT cancel uv_task — that races with uvicorn's lifespan handler
    #    and produces a spurious CancelledError traceback in the logs.
    uv_server.should_exit = True
    try:
        await uv_task
    except Exception:
        pass

    # 3. Cancel the controller task (it may still be sleeping in asyncio.sleep)
    ctrl_task.cancel()
    try:
        await ctrl_task
    except (asyncio.CancelledError, Exception):
        pass

    # 4. Close the database
    db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Allow overriding config path via CLI argument
    config_path = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_CONFIG_PATH
    )

    log.info("SolarCharge starting — config: %s", config_path)

    try:
        config, web_host, web_port = _load_config(config_path)
    except (ValueError, KeyError) as exc:
        sys.exit(f"Configuration error: {exc}")

    if not config.senec_host:
        sys.exit("Configuration error: [senec] host must be set in config.toml")

    try:
        asyncio.run(_run(config, config_path, web_host, web_port))
    except KeyboardInterrupt:
        log.info("Interrupted — exiting.")


if __name__ == "__main__":
    main()
