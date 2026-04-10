# ☀️ SolarCharge

**Solar-only EV charging controller for SENEC home batteries and Alfen Eve wallboxes.**

SolarCharge continuously reads your SENEC inverter/battery and adjusts the Alfen Eve charge current in real time so your EV charges *only* on solar surplus — no grid energy wasted. A built-in web UI gives you live power-flow monitoring, override controls, charging history, and an optional battery guard that protects home-battery reserves as the day progresses.

> **Current version: v1.2.1** — see [RELEASE_NOTES.md](RELEASE_NOTES.md) for what's new.

---

## Features

| | |
|---|---|
| ☀️ **Solar-surplus tracking** | Adjusts charge current every poll cycle to match available solar surplus |
| 🔋 **Battery Guard** | Dynamic SOC protection — required battery level rises toward sunset; Linear Factor or Full-or-Off surplus mode; seasonal + weather-forecast adjustments |
| 📊 **Live dashboard** | Power flow cards, charging status with live session kWh, override controls, battery guard panel |
| 📋 **Charging History** | Per-session kWh log; dedicated Charging History page |
| 📈 **Charts** | Interactive time-series charts of solar, grid, battery and EV power |
| � **RFID Card Guard** | Optional allowlist-based access control — only registered cards can start a session; blocked attempts are logged and visible in the UI |
| �🔌 **Alfen Eve** | Supports both Modbus TCP and local HTTPS API (MyEve / HTTP mode) |
| 🏠 **SENEC** | Reads solar, grid, battery and house power via the local SENEC API |
| 🌐 **LAN access** | Accessible from any device on your home network |

---

## Requirements

- Python 3.11 or newer
- SENEC inverter reachable on the LAN
- Alfen Eve wallbox reachable on the LAN (Modbus TCP or HTTP/MyEve mode)

---

## Installation

```bash
git clone https://github.com/sirLP/SolarCharge
cd SolarCharge

python3 -m venv .venv
.venv/bin/pip install -e .

cp config.toml.example config.toml
# Edit config.toml — fill in your SENEC host, Alfen host, password, etc.
```

> **Note:** `config.toml` is listed in `.gitignore` — your credentials will never be committed. Only the safe `config.toml.example` template is tracked by git.

---

## Configuration

All settings live in `config.toml` (gitignored). Start from the provided template:

```bash
cp config.toml.example config.toml
```

Key settings:

```toml
[web]
host = "0.0.0.0"   # bind to all interfaces — needed for LAN access
port = 8080

[senec]
host = "192.168.x.x"
use_https = true
poll_interval_s = 10

[alfen]
host      = "192.168.x.x"
mode      = "http"          # "http" (MyEve) or "modbus"
username  = "admin"
password  = "your_password"
phases    = 3
voltage_per_phase = 230
max_current_a = 16
min_current_a = 6
release_current_a = 32    # current written on shutdown — returns wallbox to standalone

[rfid]                     # optional — remove section to disable
enabled = true

[[rfid.card]]
uid  = "XXXXXXXXXXXX"   # uppercase hex UID as reported by the Alfen transaction log
name = "Card A"

[[rfid.card]]
uid  = "YYYYYYYYYYYY"
name = "Card B"

[control]
start_threshold_a = 6.0    # surplus (A) required to start a session
stop_threshold_a  = 4.0    # surplus drops below this → stop
ramp_step_a       = 1.0    # max current change per poll cycle

[battery_guard]            # optional — remove section to disable
enabled                    = true
latitude                   = 48.97   # decimal degrees
longitude                  = 12.10
night_reserve_pct          = 30      # % SoC to hold from sunset through night
daytime_reserve_pct        = 10      # minimum during peak solar hours
hard_min_pct               = 5       # absolute floor — EV charging stops below this
linear_mode                = true    # true = proportional factor; false = Full-or-Off
ramp_hours_before_sunset   = 3.0
use_seasonal               = true
seasonal_winter_extra_pct  = 15
use_weather_forecast       = true    # Open-Meteo free API, no key required
weather_overcast_threshold = 70
weather_max_sunset_advance_h = 2.0
use_tomorrow_forecast          = true    # boost night reserve when tomorrow is rainy
tomorrow_overcast_threshold    = 70      # cloud-cover % that triggers a boost
tomorrow_night_reserve_max_pct = 95      # max night reserve when tomorrow is fully overcast
use_historic_solar         = true
historic_months_lookback   = 3
```

---

## Running

### Manually

```bash
cd SolarCharge
.venv/bin/solar-charge
```

The web UI is available at **http://localhost:8080** — or from any device on your LAN at **http://\<your-machine-ip\>:8080**.

### As a systemd service (auto-start on boot)

1. Edit `solar_charge.service` — replace `YOUR_USERNAME` with your Linux username and change the paths where needed:

   ```ini
   User=lohdal
   WorkingDirectory=/home/lohdal/Documents/GitHub/SolarCharge
   ExecStart=/home/lohdal/Documents/GitHub/SolarCharge/.venv/bin/solar_charge
   ```

2. Install and enable:

   ```bash
   sudo cp solar_charge.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now solar_charge
   ```

3. Check status:

   ```bash
   sudo systemctl status solar-charge
   journalctl -u solar-charge -f
   ```

### LAN access / firewall

If the web UI is not reachable from other devices, open port 8080 in the firewall:

```bash
# firewalld (Fedora / RHEL)
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload

# ufw (Ubuntu / Debian)
sudo ufw allow 8080/tcp
```

---

## Web UI overview

| Section | Description |
|---|---|
| **Power Flow** | Live solar, grid, battery, house, and EV surplus in watts |
| **Charging Status** | Car connection state, setpoint current, wallbox power, live session kWh |
| **Battery Guard** | Required vs actual SoC, Linear Factor toggle, sun times, cloud cover |
| **Override** | Force a fixed current (0 A to stop) or resume auto mode |
| **Control Settings** | Start/stop thresholds, ramp step, poll interval |
| **🔍 Diagnostics** | Raw SENEC request/response and Alfen register reads |
| **📈 Chart** | Time-series chart of all power channels |
| **⚡ Charging History** | Per-session log with kWh totals, duration, solar fraction |
| **🔑 RFID Guard** | Enable/disable guard, manage allowed cards, view blocked-access log |

---

## Battery Guard

The Battery Guard dynamically limits EV charging surplus when the home battery SoC is below a time-of-day target:

- **Daytime reserve** (e.g. 10 %) applies during peak solar hours.
- Starting `ramp_hours_before_sunset` before dusk, the target ramps linearly up to the **night reserve** (e.g. 30 %).
- Heavy cloud cover (via [Open-Meteo](https://open-meteo.com/) free API) advances the effective sunset.
- **Tomorrow forecast**: if tomorrow is predicted to be overcast or rainy, tonight's night reserve is boosted proportionally (up to `tomorrow_night_reserve_max_pct`) so the battery enters a solar-poor day as full as possible.
- A seasonal cosine correction raises requirements in winter (peaks ~January).
- Historic solar data from the local database can relax the guard when plenty of solar is still expected.
- A **Linear Factor / Full-or-Off** toggle controls how surplus is applied when the battery is below target:
  - **Linear Factor ON** (default) — surplus scales from 0× at `hard_min_pct` to 1× once the required SoC is met.
  - **Linear Factor OFF** — Full-or-Off mode: the EV receives the full surplus when the battery is sufficiently charged, or nothing at all when protection is needed.

Click **Details →** on the Battery Guard card to open the detail modal. The toggle value is persisted to `config.toml`.

---

## Project layout

```
SolarCharge/
├── config.toml.example     Safe template — edit and copy to config.toml
├── config.toml             Your local config (gitignored — contains credentials)
├── solar_charge.service    systemd unit file
├── solar_charge/
│   ├── main.py             Entry point, config parsing
│   ├── controller.py       Control loop, hysteresis, override, guard
│   ├── battery_guard.py    Battery Guard algorithm
│   ├── senec.py            SENEC local API client
│   ├── alfen.py            Alfen Modbus TCP client
│   ├── alfen_http.py       Alfen HTTPS/MyEve API client
│   ├── web.py              FastAPI app + dashboard HTML
│   ├── db.py               SQLite wrapper
│   ├── history.py          Session history store
│   ├── timeseries.py       Time-series store (charts, guard)
│   └── state.py            Shared app state dataclass
└── db/                     SQLite database directory (auto-created, gitignored)
    └── solar_charge.db
```

---

## License

MIT
