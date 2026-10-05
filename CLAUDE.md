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
- **`backups/backup_high_rate_quarterly.py`** — cold-storage backup of `high_rate` (the raw 5s
  telemetry), run daily via cron (`15 3 * * *`, with `flock`) but idempotent against a
  `completed_quarters.json` manifest, so it only actually does work once per calendar quarter.
  Since `influx backup` (OSS CLI, no REST equivalent) always snapshots a bucket's *entire* current
  contents, this copies just one calendar-quarter window (±2 days padding) into a scratch bucket via
  Flux `to()` first, then backs up only that scratch bucket — avoiding a full-bucket snapshot (and
  its overlap/redundancy) every run. Output: `/mnt/fast_storage/backups/solar/high_rate_quarterly/`,
  picked up automatically by the existing `rclone sync /mnt/fast_storage/backups opendrive:...` cron.
  Running daily (not on a fixed post-quarter date) means a missed window (server down over a quarter
  boundary) still gets caught on the next successful day. `--verify` restores each new backup into a
  scratch bucket and compares point counts before deleting it, so "backed up" means verified
  restorable, not just "the command exited 0." Needs the `envoy_influx` container's `/backups` mount
  and `INFLUX_TOKEN` env var (both in `docker-compose.yml`) since `influx backup`/`restore` only run
  inside the container (via `docker exec`, using `/snap/bin/docker` — not just `docker` — because
  cron's minimal `PATH` doesn't include `/snap/bin`).
- **`daily_stats/compute_daily_stats.py`** — computes per-day summary stats from `high_rate` that
  `low_rate`'s own daily Wh-per-panel summary doesn't capture (peak W + when, reporting coverage,
  clipping proxy, line-level volatility, grid import/export time split, and `relative_perf` — a
  panel's daily Wh against the median of others sharing its `array` tag, which cancels out weather
  and orientation so a sustained low ratio means a real, local problem). Writes to a new, tiny
  `daily_stats` bucket. Run daily via cron (`35 0 * * *`) for the previous day; idempotent by
  design (InfluxDB overwrites by exact tag-set + timestamp), so no manifest needed, unlike the
  backup script above.

Both of the above exist because `high_rate`'s raw 5s data won't be kept forever (see the "pruning"
discussion — a live retention policy is still pending as of this writing); anything not captured in
`daily_stats` or the quarterly cold backups before that happens is gone for good once it expires.

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
