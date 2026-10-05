# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A home solar-monitoring stack for an Enphase Envoy system. It logs live inverter/meter data into
InfluxDB, visualizes it in Grafana, and separately backfills daily production/consumption totals
and historical utility-bill data via one-off Python scripts. There is no test suite, build step,
or CI — this is a personal ops/data-pipeline repo, not an application with a release process.

## Services (docker-compose.yml)

Three containers, all attached to the external `infranet` / `traefik_proxy` networks (not created
by this repo — they must already exist on the host):

- **influxdb** (`Dockerfile_influx`, based on `influxdb:latest`) — time-series store. Data lives on
  the host at `/mnt/fast_storage/appdata/solar/database`. `create_buckets.sh` (meant to run as an
  influx init script) creates `high_rate` / `low_rate` buckets and read/write + read-only auth tokens.
- **monitor** (`Dockerfile_monitor`) — runs `python -m envoy_logger /enphase_cfg.yml` from the
  third-party [`envoy-logger`](https://github.com/amykyta3/envoy-logger) package (installed via git
  in the image, not vendored). It pulls live data from the local Envoy gateway
  (`enphase_cfg.yml` → `envoy.url`) and writes it straight to InfluxDB per `enphase_cfg.yml` →
  `influxdb`. This is the only piece that talks to the on-site gateway directly.
- **grafana** — dashboards, served at `solar.exploretheworld.tech` behind an auth proxy (Traefik +
  an external auth service at `auth.exploretheworld.tech`). Admin credentials come from the same
  `DB_USER`/`DB_PW` as InfluxDB.

Bring the stack up/down with standard `docker compose up -d` / `docker compose down` from the repo
root. Config changes to `enphase_cfg.yml` take effect on container restart (it's bind-mounted, not
baked into the image).

## Standalone scripts (run outside docker-compose)

These are independent, manually-run Python scripts — not wired into any container or scheduler.
Each loads the repo-root `.env` itself (via `dotenv`, walking up from its own file location), so
run them from anywhere, not just the repo root. None has a requirements file; dependencies
(`influxdb_client`, `pandas`, `pytz`, `requests`, `numpy`, `python-dotenv`) must be installed
manually into whatever Python environment you run them in.

- **`daily_report/main.py`** — the one script that's actually scheduled: the host user crontab runs
  it twice daily (`5 6,13 * * *`, via `~/.anaconda3/envs/server_scripts/bin/python`, not any
  container). On each run
  it finds the last `daily_totals` point already written to the InfluxDB `computed_information`
  bucket, then calls the Enphase cloud API (`daily_report/enphase_api_daily.py`) once per missing
  day up to (not including) today, writing one `daily_totals` point per day with a 30s pause
  between days to be gentle on the API. Requires `ENPHASE_API_KEY`, `ENPHASE_CLIENT_ID`,
  `ENPHASE_CLIENT_SECRET`, `ENPHASE_REDIRECT_URI`, `ENPHASE_EMAIL`, `ENPHASE_PASSWORD` in `.env`
  (alongside `DB_USER`/`DB_PW`/`ORG`/`BUCKET`/`ADMIN_TOKEN`/`URL`/`TZ`).
- **`daily_report/enphase_api_daily.py`** — the `EnphaseAPI` client class used above. Handles the
  Enphase OAuth2 flow (authorization-code grant, refresh) against `api.enphaseenergy.com`, persists
  tokens to `daily_report/enphase_tokens.json`, and derives a day's totals from four separate
  telemetry endpoints (`production_micro`, `production_meter`, `consumption_meter`,
  `energy_import_telemetry`) — there is no single endpoint that returns all of these together.
  First-time auth is interactive (prompts for a pasted authorization code on stdin); after that it
  runs unattended using the saved refresh token.
- **`monthly_bill/historical_bills.py`** — one-off backfill script (hardcoded historical bill data)
  that writes a `bill_data` measurement to the `test` bucket. Already run; not scheduled anywhere.

## Configuration and secrets

`enphase_cfg.yml` and `.env` contain live credentials (Enphase account password, InfluxDB admin
token, DB password) and are consumed directly by the running containers/scripts — treat them as
secrets, not sample config, and never print their contents or commit them anywhere.

`.env` is the single source of truth for InfluxDB/Grafana/Enphase config (`DB_USER`, `DB_PW`,
`ORG`, `BUCKET`, `ADMIN_TOKEN`, `URL`, `TZ`, `ENPHASE_*`) and is loaded both by docker-compose
(`env_file:`) and by the standalone scripts above. `ADMIN_TOKEN` is a full org-admin InfluxDB
token; only the backfill/admin scripts (`daily_report/main.py`, `monthly_bill/historical_bills.py`)
use it. `enphase_cfg.yml` separately configures the `envoy-logger` container (gateway
credentials/URL, per-inverter array/position tags used for Grafana panel labeling, and its own
InfluxDB token) — it is not derived from `.env`. That token is intentionally scoped to read/write
on just `high_rate`/`low_rate` (created via the InfluxDB v2 API, mirroring the unused read/write
auth that `create_buckets.sh` already creates), not the admin token, since `envoy-logger` only ever
needs those two buckets.
