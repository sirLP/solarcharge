# SolarCharge v1.2.0 — Release Notes

**Release date:** 2026-04-09

---

## New Features

### RFID Card Guard
An optional allowlist-based access-control layer that prevents unauthorised EV
charging sessions.

- Enable via a new `[rfid]` section in `config.toml` with one `[[rfid.card]]`
  entry per allowed card, each carrying a `uid` (hex UID as reported by Alfen)
  and a human-readable `name`.
- When enabled, the controller reads the active RFID tag from the Alfen
  transaction log on every poll cycle until a tag is confirmed.  If the tag is
  absent or not in the allowlist, the wallbox is immediately set to 0 A and the
  session is blocked.  The guard retries on every subsequent cycle so a brief
  read delay does not result in a missed check.
- Blocked access attempts are recorded with timestamp, UID and card name, and
  displayed in the new **RFID Guard** panel on the dashboard.
- The guard state and card list are editable live from the web UI — no service
  restart required.  Changes are written back to `config.toml`.
- RFID scan events are written to the application log:

  ```
  RFID scan started  — uid=XXXXXXXXXXXX  name=Card A  result=AUTHORISED
  RFID scan ended    — uid=XXXXXXXXXXXX  name=Card A
  RFID scan started  — uid=YYYYYYYYYYYY  name=<unknown>  result=DENIED (not in allowlist)
  ```

Configure in `config.toml`:

```toml
[rfid]
enabled = true

[[rfid.card]]
uid  = "XXXXXXXXXXXX"   # uppercase hex UID as reported by the wallbox
name = "Card A"

[[rfid.card]]
uid  = "YYYYYYYYYYYY"
name = "Card B"
```

---

## Bug Fixes

### Charging History showed wrong timestamps (UTC instead of local time)
Alfen's transaction log stores session start/end times in UTC.  These were
being displayed without timezone conversion, so users in UTC+2 (CEST) saw
times two hours early.  Timestamps are now converted to the system local
timezone before being stored.

### Charging History session stuck as "In progress" after car disconnects
When SolarCharge stops a session by reducing the setpoint to 0 A, the Alfen
transaction log does not receive a `txstop2` record, leaving the session
marked *In progress* indefinitely.  `GET /api/wallbox-sessions` now
synthesises a session close from the live meter reading when the last
transaction is still `in_progress` and the wallbox reports no vehicle present.

### Session open/close driven by internal flag instead of wallbox state
In edge cases (service restart mid-session, manual current override) the
internal session-tracking flag could diverge from the actual wallbox charging
state.  Session start and end transitions are now driven entirely from the
wallbox `ChargeStatus.CHARGING` value reported on each poll cycle.

---

## Configuration Changes

| Key | Section | Default | Notes |
|---|---|---|---|
| `enabled` | `[rfid]` | `false` | New — enable/disable the RFID guard |
| `[[rfid.card]]` | `[rfid]` | *(none)* | New — one entry per allowed card |

No other config keys changed.

---

## Upgrade Notes

1. **No breaking changes** — existing `config.toml` files are fully compatible.
2. The RFID guard is **disabled by default** (`enabled = false`).  Add a
   `[rfid]` section with `[[rfid.card]]` entries only if you want card-based
   access control.

---

---

# SolarCharge v1.1.0 — Release Notes

**Release date:** 2026-04-09

---

## New Features

### Live session energy from the wallbox meter
The **Charging Status** card now shows how many kWh have been delivered in the
current charging session, read directly from the Alfen wallbox lifetime energy
meter rather than the previous software-based power-integration estimate.

- `AlfenState` carries a new `meter_wh` field populated by both Modbus and HTTP
  clients on every poll cycle (non-fatal read — falls back to 0 if unavailable).
- The controller tracks `session_start_wh` at session start and publishes the
  live delta as `session_kwh` in `AppState`.
- A new **Session** stat (`x.xx kWh`) appears in the Charging Status card on the
  dashboard.

### Battery Guard — Linear Factor / Full-or-Off toggle
A new toggle on the Battery Guard card replaces the old surplus-factor concept.

- **Linear Factor ON** (default): EV surplus scales proportionally from 0× at
  the hard minimum SoC floor to 1× once the required SoC target is met.
- **Linear Factor OFF** — Full-or-Off mode: the EV receives the full solar
  surplus when the battery is sufficiently charged, or nothing at all when
  protection is needed. Maximises peak-solar charging throughput.

The toggle value is persisted to `config.toml` and survives service restarts.

### Graceful shutdown releases wallbox to standalone
On `systemctl stop` / SIGTERM / SIGINT the controller now writes a configurable
`release_current_a` value (default: **32 A**) to the wallbox before closing
the connection. This returns the Alfen to effectively standalone / unlimited
operation rather than leaving it capped at the solar `max_current_a` setpoint.

Configure in `config.toml`:
```toml
[alfen]
release_current_a = 32   # written on shutdown (default: 32 A = hardware max)
```

---

## Improvements

### Dashboard UI
- **kW display**: Solar surplus card now shows available power in kW alongside
  the watt value for quick readability.
- **Session kWh stat**: New stat in the Charging Status card (see above).
- **Charging History** button added to the header (renamed from *Reports*);
  the dedicated Reports page title is also updated to *Charging History*.

### Removed: Pause functionality
The operator pause feature has been removed. Use the existing **Override** to
force a current of 0 A when you want to temporarily halt charging, or simply
stop the service.

### Removed: History modal
The inline History pop-up modal on the dashboard has been removed. All session
history is available on the dedicated **Charging History** page (`/reports`),
which provides a richer view.

---

## Bug Fixes

### Wallbox showing "Warte auf Lastmanagement" when no car is connected
The Alfen watchdog requires a current-setpoint write every ~60 s to confirm the
controller is still active. Previously the 0 A heartbeat was only sent on the
*transition* from connected → disconnected, so the watchdog timed out and
blocked the socket. The 0 A write is now sent on **every poll cycle** while no
vehicle is present.

### Charging not jumping to maximum current at session start
Due to the 1 A/cycle ramp limit, a newly started session took up to 16 cycles
(≈ 2–3 minutes) to reach full charge current. A `session_just_started` flag now
bypasses the ramp on the **first cycle** of each new session, sending the full
target current immediately.

### Battery Guard "How it works" text out of date
The explanatory text in the Battery Guard detail modal now correctly describes
the Linear Factor toggle (On/Off) instead of referring to the old naming.

---

## Configuration Changes

| Key | Section | Default | Notes |
|---|---|---|---|
| `release_current_a` | `[alfen]` | `32` | New — current written on shutdown |
| `linear_mode` | `[battery_guard]` | `true` | New — replaces implicit surplus factor behaviour |

No other config keys changed. Existing `config.toml` files continue to work
without modification.

---

## Upgrade Notes

1. **No breaking changes** — existing `config.toml` files are fully compatible.
2. Optionally add `release_current_a` to `[alfen]` if your wallbox hardware
   maximum is below 32 A.
3. The `linear_mode` setting is written back to `config.toml` automatically the
   first time you toggle it in the UI; the default (`true`) matches the previous
   behaviour.
