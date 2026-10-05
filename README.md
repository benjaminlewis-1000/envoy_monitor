# envoy_monitor

Home solar-monitoring stack for an Enphase IQ Gateway (Envoy), built on Docker Compose +
InfluxDB + Grafana.

## What it does

- **Live metering** — a container runs the third-party [`envoy-logger`](https://github.com/amykyta3/envoy-logger)
  package, which polls the on-site Envoy gateway every 5 seconds for per-line
  production/consumption/net power and per-inverter output, and writes it straight into
  InfluxDB (`high_rate` bucket). Once a day it also computes a daily Wh summary per line/inverter
  into the `low_rate` bucket. This runs continuously and automatically — it's a `restart: always`
  container with no scheduler involved.
- **Daily cloud backfill** — a cron job (twice a day, on the host, outside Docker) runs
  `daily_report/main.py`, which calls the Enphase *cloud* API to fill in any missing daily
  production/consumption/export/import totals, writing one `daily_totals` point per missing day
  into InfluxDB.
- **Dashboards** — Grafana reads from the same InfluxDB instance and is served behind a Traefik +
  auth-proxy setup at `solar.exploretheworld.tech`.
- **One-off historical backfill** — `monthly_bill/historical_bills.py` was used once to seed
  InfluxDB with pre-solar utility bill history for comparison; it's not scheduled.

## Architecture

```
Envoy Gateway (local, 192.168.1.150)
        │  polled every 5s
        ▼
enphase_monitor container ──writes──▶ InfluxDB ◀──writes── daily_report/main.py (cron, 2x/day)
 (envoy-logger)                       (Docker)        (pulls from Enphase cloud API)
                                          │
                                          ▼
                                       Grafana (Docker, dashboards)
```

All three Docker services (`influxdb`, `monitor`, `grafana`) are defined in
`docker-compose.yml` and share the external `infranet` / `traefik_proxy` networks, which must
already exist on the host.

## Running it

```sh
docker compose up -d
```

InfluxDB data persists at `/mnt/fast_storage/appdata/solar/database` on the host; Grafana data at
`/mnt/fast_storage/appdata/solar/grafana`. `enphase_cfg.yml` (bind-mounted into the `monitor`
container) and `.env` (used by every service and by the standalone scripts) hold live credentials
and are **not** committed to this repo — see `.gitignore`.

See `CLAUDE.md` for a more detailed file-by-file breakdown.

## Re-creating `.env`

`.env` is gitignored and must be created by hand. It's loaded by docker-compose (for `influxdb`,
`monitor`, `grafana`) and by `daily_report/main.py` / `monthly_bill/historical_bills.py` directly.
Create a file named `.env` in the repo root with these keys:

```sh
# InfluxDB / Grafana
DB_USER=<influxdb + grafana admin username>
DB_PW=<influxdb + grafana admin password>
ORG=<influxdb org name>
BUCKET=<default influxdb bucket name>
ADMIN_TOKEN=<influxdb org-admin API token>
URL=<influxdb URL, e.g. http://influx.exploretheworld.tech>
TZ=America/New_York

# Enphase cloud API (used only by daily_report/)
ENPHASE_API_KEY=<from https://developer-v4.enphase.com/ app registration>
ENPHASE_CLIENT_ID=<from the same app registration>
ENPHASE_CLIENT_SECRET=<from the same app registration>
ENPHASE_REDIRECT_URI=<redirect URI configured on that app>
ENPHASE_EMAIL=<enphaseenergy.com account email>
ENPHASE_PASSWORD=<enphaseenergy.com account password>
```

Notes:
- `ADMIN_TOKEN` is a full org-admin InfluxDB token — needed by the backfill/admin scripts, but
  deliberately *not* what `enphase_cfg.yml` uses for the always-on `monitor` container (see below).
- No spaces around `=`; quote values that contain special shell characters (`&`, `%`, spaces, etc.)
  so they parse correctly everywhere this file gets read.
- First run of `daily_report/main.py` needs interactive Enphase OAuth (it'll print a URL and
  prompt for a pasted authorization code); after that it refreshes tokens automatically and saves
  them to `daily_report/enphase_tokens.json` (also gitignored).

`enphase_cfg.yml` is a separate gitignored file consumed only by the `monitor` container — it
needs its own Enphase login and a scoped (not admin) InfluxDB token with read/write on
`high_rate`/`low_rate` only. Mint that token via the InfluxDB UI/CLI (or the same
`/api/v2/authorizations` call `create_buckets.sh` uses) rather than reusing `ADMIN_TOKEN`.
