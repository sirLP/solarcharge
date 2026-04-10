"""
Battery Guard — dynamic battery SOC protection for EV surplus charging.

Prevents over-discharging the home battery to the EV when there is
insufficient time or solar energy to recharge it before evening.

Algorithm overview
------------------
Each poll cycle the guard computes a *required SOC* for the current moment.
The required SOC varies with:

  •  **Daytime / peak solar**   → low requirement (sun is actively charging)
  •  **Afternoon ramp**         → rises linearly toward the night reserve as
                                   sunset approaches (configurable ramp window)
  •  **Post-sunset / pre-dawn** → held at ``night_reserve_pct``

The raw EV surplus from the controller is then multiplied by a *factor* (0–1)
derived from how close the current battery SOC is to the requirement:

  •  SOC ≥ required                 → factor = 1.0  (no restriction)
  •  SOC ≤ ``hard_min_pct``         → factor = 0.0  (EV charging blocked)
  •  hard_min < SOC < required      → linear 0 → 1

Optional enhancements (each independently togglable in config):

  ``use_seasonal``
      Increases ``night_reserve_pct`` in winter months (shorter days, weaker
      sun) by up to ``seasonal_winter_extra_pct``.

  ``use_weather_forecast``
      Calls the free Open-Meteo API for hourly cloud-cover data.  When the
      remaining daylight is heavily overcast the *effective* sunset is
      advanced so the afternoon ramp starts earlier.

  ``use_historic_solar``
      Uses the SolarCharge time-series store to derive average solar output
      for the current calendar month.  The estimated remaining solar energy
      (now → sunset) is compared to the deficit ``night_reserve - current_soc``
      to relax the guard when enough sun is still expected.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solar_charge.timeseries import TimeseriesStore

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BatteryGuardConfig:
    """All settings for the battery guard — sourced from ``[battery_guard]`` in config.toml."""

    enabled: bool = False

    # Geographic coordinates (required for sunset calculation).
    latitude: float = 52.0
    longitude: float = 5.0

    # SOC thresholds (%):
    #   night_reserve  — target SOC from sunset onwards and through the night.
    #   daytime_reserve — minimum SOC allowed during peak solar hours.
    #   hard_min        — absolute floor; EV charging blocked at or below this.
    night_reserve_pct: float = 30.0
    daytime_reserve_pct: float = 10.0
    hard_min_pct: float = 5.0

    # How many hours before sunset to begin the afternoon SOC ramp.
    ramp_hours_before_sunset: float = 3.0

    # Seasonal adjustment: in mid-winter night_reserve is raised by this amount.
    use_seasonal: bool = True
    seasonal_winter_extra_pct: float = 15.0  # added in Dec/Jan; tapers off toward summer

    # Weather forecast: advance effective sunset when heavily overcast.
    use_weather_forecast: bool = True
    weather_cache_minutes: int = 60    # refresh interval
    weather_overcast_threshold: float = 70.0  # % cloud cover considered "overcast"
    weather_max_sunset_advance_h: float = 2.0  # max hours to advance sunset

    # Tomorrow weather: raise night_reserve when tomorrow is a rainy/cloudy day.
    # The guard will target a higher battery SOC overnight so the battery enters
    # tomorrow as full as possible when solar generation will be low all day.
    use_tomorrow_forecast: bool = True
    tomorrow_overcast_threshold: float = 70.0  # % cloud cover that counts as "overcast tomorrow"
    tomorrow_night_reserve_max_pct: float = 95.0  # night reserve when tomorrow is 100% overcast

    # Historic average solar: relax guard when expected remaining solar is sufficient.
    use_historic_solar: bool = True
    historic_months_lookback: int = 3  # months of data to average

    # Battery charge rate cap: when the battery is already absorbing at (or near)
    # this level, any additional solar is considered freely available for the EV
    # regardless of current SOC vs required SOC.
    battery_max_charge_w: float = 2500.0  # W — adjust to your battery's max charge rate

    # Linear Factor mode: when True (default) the guard applies the gradual
    # 0–1 surplus factor.  When False the guard uses Full-or-Off: the EV
    # receives the full surplus when SOC ≥ required, or nothing at all when
    # SOC < required.  Can be toggled live from the web UI without restarting.
    linear_mode: bool = True

    # Internal — not user-settable
    _weather_cache_ts: float = field(default=0.0,   repr=False)
    _weather_cloud_pct: float | None = field(default=None, repr=False)
    _tomorrow_cloud_pct: float | None = field(default=None, repr=False)


# ─────────────────────────────────────────────────────────────────────────────
#  Status snapshot (exposed to the web UI via AppState)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GuardStatus:
    """Live guard snapshot written by the controller, read by the web UI."""
    enabled: bool = False
    active: bool = False             # True when guard is actually reducing surplus
    surplus_factor: float = 1.0      # multiplier applied to EV surplus (0 – 1)
    linear_mode: bool = True          # True → apply linear factor; False → full-or-off
    required_soc_pct: float = 0.0    # required SOC right now
    current_soc_pct: float = 0.0     # actual battery SOC
    sunset_local: str = ""           # e.g. "19:47"
    sunrise_local: str = ""          # e.g. "06:12"
    reason: str = ""                 # human-readable explanation
    weather_cloud_pct: float | None = None  # avg cloud cover remaining today (0–100)
    tomorrow_cloud_pct: float | None = None  # avg cloud cover for tomorrow (0–100)
    tomorrow_night_reserve_boost: float = 0.0  # extra night reserve added due to tomorrow's forecast
    seasonal_extra_pct: float = 0.0  # extra reserve added for season


# ─────────────────────────────────────────────────────────────────────────────
#  Sun position maths (pure Python, no external libraries)
# ─────────────────────────────────────────────────────────────────────────────

def _solar_event_utc(lat: float, lon: float, d: date, *, sunrise: bool) -> datetime:
    """
    Approximate sunrise or sunset time in UTC for the given latitude, longitude
    and date.  Accurate to ±5 minutes for latitudes up to ~65°.

    Uses the standard NOAA/Meeus simplified algorithm.
    """
    N = d.timetuple().tm_yday

    # Solar declination (degrees)
    decl = -23.45 * math.cos(math.radians(360.0 / 365.0 * (N + 10)))

    # Hour-angle at sunrise/sunset (degrees)
    cos_ha = (
        -math.tan(math.radians(lat)) * math.tan(math.radians(decl))
    )
    # Clamp for polar regions
    cos_ha = max(-1.0, min(1.0, cos_ha))
    ha = math.degrees(math.acos(cos_ha))  # half-day in degrees

    # Convert to hours; solar noon in UTC
    solar_noon_utc_h = 12.0 - lon / 15.0
    event_utc_h = solar_noon_utc_h + ((-ha if sunrise else ha) / 15.0)

    # Convert fractional hours to a datetime
    total_minutes = round(event_utc_h * 60)
    h, m = divmod(total_minutes, 60)
    h = h % 24  # handle edge cases near midnight
    return datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
#  Weather forecast (Open-Meteo, free, no API key required)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_cloud_cover(lat: float, lon: float) -> tuple[float | None, float | None]:
    """
    Fetch average cloud cover (%) for:

    * **today** — remaining hours from now until end of today
    * **tomorrow** — all 24 hours of tomorrow

    Uses the Open-Meteo free API (no key required).  Returns
    ``(today_avg, tomorrow_avg)``; either value is ``None`` on error.
    This function is blocking — call it from a thread-pool executor in
    async code.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&hourly=cloud_cover&forecast_days=2&timezone=auto"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SolarCharge/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        log.debug("BatteryGuard: weather fetch failed: %s", exc)
        return None, None

    try:
        times: list[str] = data["hourly"]["time"]
        covers: list[int] = data["hourly"]["cloud_cover"]
        now_str      = datetime.now().strftime("%Y-%m-%dT%H")
        today_str    = datetime.now().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        # Today: hours from now onwards (inclusive)
        today_remaining = [
            c for t, c in zip(times, covers)
            if t >= now_str and t.startswith(today_str)
        ]
        # Tomorrow: all 24 hours
        tomorrow_all = [
            c for t, c in zip(times, covers)
            if t.startswith(tomorrow_str)
        ]

        today_avg    = round(sum(today_remaining) / len(today_remaining), 1) if today_remaining else None
        tomorrow_avg = round(sum(tomorrow_all)    / len(tomorrow_all),    1) if tomorrow_all    else None
        return today_avg, tomorrow_avg
    except (KeyError, TypeError, ZeroDivisionError) as exc:
        log.debug("BatteryGuard: weather parse error: %s", exc)
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
#  Seasonal factor
# ─────────────────────────────────────────────────────────────────────────────

def _seasonal_extra_pct(month: int, winter_extra: float) -> float:
    """
    Return extra SOC reserve to add for the given month.

    Uses a cosine curve: maximum (``winter_extra``) at month 1 (January),
    zero at month 7 (July).
    """
    # Shift so that month 1 = max, month 7 = min
    angle = math.radians((month - 1) * 30)   # 30° per month
    factor = (math.cos(angle) + 1.0) / 2.0   # 0 → 1, 1 at month 1/12, 0 at month 7
    return round(winter_extra * factor, 1)


# ─────────────────────────────────────────────────────────────────────────────
#  Historic solar helper
# ─────────────────────────────────────────────────────────────────────────────

def _expected_remaining_solar_wh(
    ts_store: "TimeseriesStore",
    lat: float, lon: float,
    now: datetime,
    months_lookback: int,
) -> float | None:
    """
    Estimate remaining solar energy (Wh) from *now* until today's sunset,
    based on historic time-series averages for the current calendar month.

    Returns ``None`` if insufficient data is available.
    """
    try:
        from datetime import timedelta as _td

        since = now - _td(days=months_lookback * 30)
        result = ts_store.query(
            since=since,
            fields=["solar_w"],
            max_points=50_000,
        )
        timestamps = result.get("timestamps", [])
        solar_vals = result.get("fields", {}).get("solar_w", [])
        if len(timestamps) < 48:   # need at least 2 days of data
            return None

        # Build average solar_w per hour-of-day from historic data
        hourly_sum: dict[int, float] = {}
        hourly_cnt: dict[int, int] = {}
        for ts_str, val in zip(timestamps, solar_vals):
            if val is None:
                continue
            hr = datetime.fromisoformat(ts_str).hour
            hourly_sum[hr] = hourly_sum.get(hr, 0.0) + float(val)
            hourly_cnt[hr] = hourly_cnt.get(hr, 0) + 1
        if not hourly_sum:
            return None

        hourly_avg = {
            hr: hourly_sum[hr] / hourly_cnt[hr]
            for hr in hourly_sum
        }

        # Integrate from current hour to sunset
        sunset_utc = _solar_event_utc(lat, lon, now.date(), sunrise=False)
        sunset_local_h = (sunset_utc.replace(tzinfo=timezone.utc)
                          .astimezone()).hour

        current_h = now.hour
        total_wh = 0.0
        for h in range(current_h + 1, sunset_local_h + 1):
            total_wh += hourly_avg.get(h % 24, 0.0)

        return round(total_wh, 0)
    except Exception as exc:
        log.debug("BatteryGuard: historic solar estimate failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Main guard class
# ─────────────────────────────────────────────────────────────────────────────

class BatteryGuard:
    """
    Dynamic battery protection guard.

    Instantiate once in :class:`~solar_charge.controller.Controller` and call
    :meth:`evaluate` once per poll cycle.

    Parameters
    ----------
    config:
        ``BatteryGuardConfig`` parsed from ``[battery_guard]`` in config.toml.
    ts_store:
        Optional reference to the :class:`~solar_charge.timeseries.TimeseriesStore`
        for historic solar averages.
    """

    def __init__(
        self,
        config: BatteryGuardConfig,
        ts_store: "TimeseriesStore | None" = None,
    ) -> None:
        self._cfg = config
        self._ts = ts_store

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        battery_soc_pct: float,
        surplus_w: float,
        battery_power_w: float = 0.0,
        now: datetime | None = None,
    ) -> tuple[float, GuardStatus]:
        """
        Compute the guarded surplus and return a :class:`GuardStatus` snapshot.

        Parameters
        ----------
        battery_soc_pct:
            Current battery state-of-charge (0–100 %).
        surplus_w:
            Raw EV surplus in watts (solar − house).
        battery_power_w:
            Current battery charge power in watts (positive = charging).
            Used to detect when the battery has hit its charge-rate cap.
        now:
            Current local datetime (defaults to ``datetime.now()`` if omitted).

        Returns
        -------
        (guarded_surplus_w, GuardStatus)
        """
        cfg = self._cfg
        if now is None:
            now = datetime.now()

        today = now.date()
        lat, lon = cfg.latitude, cfg.longitude

        # ── Sunrise / sunset ────────────────────────────────────────────
        sunrise_utc = _solar_event_utc(lat, lon, today, sunrise=True)
        sunset_utc  = _solar_event_utc(lat, lon, today, sunrise=False)
        # Convert to local for display
        sunrise_local = sunrise_utc.replace(tzinfo=timezone.utc).astimezone()
        sunset_local  = sunset_utc.replace(tzinfo=timezone.utc).astimezone()
        sunrise_str = sunrise_local.strftime("%H:%M")
        sunset_str  = sunset_local.strftime("%H:%M")

        # ── Seasonal adjustment ─────────────────────────────────────────
        seasonal_extra = 0.0
        if cfg.use_seasonal:
            seasonal_extra = _seasonal_extra_pct(today.month, cfg.seasonal_winter_extra_pct)

        night_reserve    = cfg.night_reserve_pct + seasonal_extra
        daytime_reserve  = cfg.daytime_reserve_pct
        hard_min         = cfg.hard_min_pct

        # ── Weather-based effective sunset ──────────────────────────────
        cloud_pct = self._cloud_cover()
        tomorrow_cloud_pct = self._tomorrow_cloud_cover()
        effective_sunset_utc = sunset_utc
        if cfg.use_weather_forecast and cloud_pct is not None:
            overcast_fraction = max(
                0.0,
                (cloud_pct - cfg.weather_overcast_threshold) / (100.0 - cfg.weather_overcast_threshold),
            )
            advance_seconds = overcast_fraction * cfg.weather_max_sunset_advance_h * 3600
            effective_sunset_utc = sunset_utc - timedelta(seconds=advance_seconds)
            if advance_seconds > 60:
                log.debug(
                    "BatteryGuard: cloud cover %.0f%% → advancing effective sunset by %.0f min",
                    cloud_pct, advance_seconds / 60,
                )

        # ── Historic solar relaxation ────────────────────────────────────
        remaining_solar_wh: float | None = None
        if cfg.use_historic_solar and self._ts is not None:
            remaining_solar_wh = _expected_remaining_solar_wh(
                self._ts, lat, lon, now, cfg.historic_months_lookback
            )

        # ── Tomorrow-weather night-reserve boost ───────────────────────────
        # If tomorrow is predicted to be heavily overcast / rainy, solar output
        # will be low all day.  To compensate we raise tonight's night_reserve
        # so the battery enters tomorrow as full as possible.
        tomorrow_boost = 0.0
        if cfg.use_tomorrow_forecast and tomorrow_cloud_pct is not None:
            overcast_frac = max(
                0.0,
                (tomorrow_cloud_pct - cfg.tomorrow_overcast_threshold)
                / (100.0 - cfg.tomorrow_overcast_threshold),
            )
            max_boost = max(0.0, cfg.tomorrow_night_reserve_max_pct - night_reserve)
            tomorrow_boost = round(overcast_frac * max_boost, 1)
            if tomorrow_boost > 0.5:
                night_reserve = min(cfg.tomorrow_night_reserve_max_pct, night_reserve + tomorrow_boost)
                log.debug(
                    "BatteryGuard: tomorrow cloud cover %.0f%% → night_reserve boosted by %.1f%% to %.0f%%",
                    tomorrow_cloud_pct, tomorrow_boost, night_reserve,
                )

        # ── Required SOC at this moment ──────────────────────────────────
        now_utc = now.astimezone(timezone.utc)
        required_soc, reason = self._required_soc(
            now_utc,
            sunrise_utc,
            effective_sunset_utc,
            sunset_utc,
            night_reserve,
            daytime_reserve,
            cfg.ramp_hours_before_sunset,
        )

        # If historic data says there's enough remaining solar to cover the
        # deficit, relax the guard proportionally.
        if remaining_solar_wh is not None and remaining_solar_wh > 0:
            # Rough estimate: assume 10 kWh battery at 100% = 10 000 Wh
            # We don't know exact capacity, so work in relative terms:
            # If remaining solar (W averaged × hours) would raise SOC by
            # more than the deficit, reduce the required SOC by that amount.
            # We use a conservative capacity assumption of 150 Wh per % SOC
            # (representative of a 15 kWh battery).
            wh_per_pct = 150.0
            potential_soc_gain = remaining_solar_wh / wh_per_pct  # in % SOC units
            deficit = max(0.0, required_soc - battery_soc_pct)
            if deficit > 0 and potential_soc_gain > deficit * 1.2:  # 20% margin, only when behind
                relaxation = min(deficit * 0.5, potential_soc_gain)
                required_soc = max(daytime_reserve, required_soc - relaxation)
                reason += f" (relaxed by {relaxation:.1f}% — expected solar {remaining_solar_wh:.0f} Wh)"

        required_soc = round(min(100.0, max(0.0, required_soc)), 1)

        # ── Surplus factor ───────────────────────────────────────────────
        if cfg.linear_mode:
            factor = self._surplus_factor(battery_soc_pct, required_soc, hard_min)
        else:
            # Full-or-Off: either charge at full surplus or not at all.
            # No linear reduction — the EV gets maximum power while
            # solar is high, and stops when the battery needs protection.
            factor = 1.0 if battery_soc_pct >= required_soc else 0.0

        # ── Battery cap overflow ─────────────────────────────────────────
        # If the battery is already charging at (or near) its maximum rate,
        # the SENEC cannot absorb any more power regardless of SOC.  Any
        # additional solar is genuinely "free" for the EV, so we let it
        # pass through unconstrained even when SOC < required.
        cap_overflow_w = 0.0
        if battery_power_w >= cfg.battery_max_charge_w * 0.95:  # 5% tolerance
            # Solar that exceeds battery cap is free for EV regardless of guard
            cap_overflow_w = max(0.0, surplus_w - cfg.battery_max_charge_w)
            if cap_overflow_w > 0:
                log.debug(
                    "BatteryGuard: battery at cap (%.0f W ≥ %.0f W) "
                    "→ %.0f W overflow available for EV",
                    battery_power_w, cfg.battery_max_charge_w, cap_overflow_w,
                )

        # Apply guard factor to the non-overflow portion, then add the
        # overflow back unconstrained.
        guarded_surplus = surplus_w * factor
        if cap_overflow_w > 0:
            # The overflow is already included in surplus_w * factor when
            # factor=1, so only add the extra when factor < 1.
            guarded_surplus = max(guarded_surplus, cap_overflow_w + (surplus_w - cap_overflow_w) * factor)
            if factor < 1.0:
                reason += f" [+{cap_overflow_w:.0f} W battery-cap overflow]"

        active = factor < 1.0
        if active:
            log.info(
                "BatteryGuard: SOC %.0f%% < required %.0f%% → factor=%.2f  "
                "surplus %.0f W → %.0f W  (%s)",
                battery_soc_pct, required_soc, factor,
                surplus_w, guarded_surplus, reason,
            )

        if tomorrow_boost > 0.5:
            reason += f" [tomorrow rain: +{tomorrow_boost:.0f}% night reserve]"

        status = GuardStatus(
            enabled=True,
            active=active,
            surplus_factor=round(factor, 3),
            linear_mode=cfg.linear_mode,
            required_soc_pct=required_soc,
            current_soc_pct=round(battery_soc_pct, 1),
            sunset_local=sunset_str,
            sunrise_local=sunrise_str,
            reason=reason,
            weather_cloud_pct=cloud_pct,
            tomorrow_cloud_pct=tomorrow_cloud_pct,
            tomorrow_night_reserve_boost=tomorrow_boost,
            seasonal_extra_pct=round(seasonal_extra, 1),
        )
        return guarded_surplus, status

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _required_soc(
        now_utc: datetime,
        sunrise_utc: datetime,
        effective_sunset_utc: datetime,
        real_sunset_utc: datetime,
        night_reserve: float,
        daytime_reserve: float,
        ramp_hours: float,
    ) -> tuple[float, str]:
        """Return (required_soc_pct, human_readable_reason)."""
        ramp_start_utc = effective_sunset_utc - timedelta(hours=ramp_hours)

        # Night / pre-dawn: no solar, maintain night reserve
        if now_utc >= real_sunset_utc or now_utc < sunrise_utc:
            return night_reserve, f"nighttime — holding {night_reserve:.0f}% night reserve"

        # After effective sunset (overcast advance) but before real sunset
        if now_utc >= effective_sunset_utc:
            return night_reserve, (
                f"overcast — treating as post-sunset, holding {night_reserve:.0f}%"
            )

        # Afternoon ramp: linearly increase from daytime_reserve to night_reserve
        if now_utc >= ramp_start_utc:
            window = (effective_sunset_utc - ramp_start_utc).total_seconds()
            elapsed = (now_utc - ramp_start_utc).total_seconds()
            t = elapsed / window if window > 0 else 1.0
            required = daytime_reserve + t * (night_reserve - daytime_reserve)
            sunset_local = effective_sunset_utc.replace(tzinfo=timezone.utc).astimezone()
            return required, (
                f"afternoon ramp ({t * 100:.0f}% to sunset {sunset_local.strftime('%H:%M')}) "
                f"→ {required:.0f}% required"
            )

        # Peak solar window: low reserve
        return daytime_reserve, f"peak solar window — minimum {daytime_reserve:.0f}% reserve"

    @staticmethod
    def _surplus_factor(soc: float, required: float, hard_min: float) -> float:
        """
        Return a multiplier 0–1 describing how freely EV charging can proceed.
        """
        if soc >= required:
            return 1.0
        if soc <= hard_min:
            return 0.0
        band = required - hard_min
        if band <= 0:
            return 0.0
        return round((soc - hard_min) / band, 3)

    def _cloud_cover(self) -> float | None:
        """Return cached today cloud cover %, refreshing if stale."""
        cfg = self._cfg
        if not cfg.use_weather_forecast:
            return None
        age_s = time.monotonic() - cfg._weather_cache_ts
        if age_s < cfg.weather_cache_minutes * 60 and cfg._weather_cloud_pct is not None:
            return cfg._weather_cloud_pct
        today_cloud, tomorrow_cloud = _fetch_cloud_cover(cfg.latitude, cfg.longitude)
        cfg._weather_cloud_pct = today_cloud
        cfg._tomorrow_cloud_pct = tomorrow_cloud
        cfg._weather_cache_ts = time.monotonic()
        return today_cloud

    def _tomorrow_cloud_cover(self) -> float | None:
        """Return cached tomorrow cloud cover %.  Piggybacks on the today cache refresh."""
        cfg = self._cfg
        if not cfg.use_weather_forecast:
            return None
        age_s = time.monotonic() - cfg._weather_cache_ts
        if age_s < cfg.weather_cache_minutes * 60 and cfg._tomorrow_cloud_pct is not None:
            return cfg._tomorrow_cloud_pct
        # Trigger a refresh (populates both today and tomorrow)
        self._cloud_cover()
        return cfg._tomorrow_cloud_pct
