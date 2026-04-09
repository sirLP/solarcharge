# GitHub Copilot — repository instructions

## ⚠️ Never start or run the application

Do **not** run `solar-charge`, `uvicorn`, `python -m solar_charge`, or any
command that starts the SolarCharge daemon or web server.

The application may be running live on the production server at the same time.
Starting a second instance will cause communication conflicts with the Alfen
wallbox (duplicate Modbus/HTTP sessions) and may disrupt active EV charging.

Permitted terminal use:
- Syntax / import checks: `python -c "import solar_charge.xxx; print('OK')"`
- Tests, linting, formatting
- `git` operations
- Dependency installs
