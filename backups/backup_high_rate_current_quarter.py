#!/usr/bin/env python3
"""Daily rolling backup of high_rate's CURRENT (not-yet-complete) quarter.

backup_high_rate_quarterly.py only archives a quarter once it's fully over
(plus a 2-day buffer) -- so for up to ~3 months, the in-progress quarter's
data has no backup at all beyond the live bucket itself. This script closes
that gap on a daily cadence, to match the other backup jobs on this host.

It deliberately does NOT re-copy the whole quarter-so-far every day (that
would mean the daily copy cost grows across the quarter, from seconds up to
tens of minutes by quarter-end -- cheap on day 1, expensive and quadratic in
total cron time by day 90). Instead it keeps a persistent scratch bucket
(current_quarter_staging) that accumulates across the quarter: each run only
copies what's new since the last run (tracked in state.json), so the daily
copy cost stays roughly constant (~1 day's worth of data) regardless of how
far into the quarter we are. The `influx backup` step backs up that whole
accumulated bucket into a single, fixed, overwritten directory -- there's no
dated history here and no pruning needed, because this is a rolling gap-
filler, not an archive; backup_high_rate_quarterly.py is the real archive,
and once a quarter completes, the next quarter's state.json rollover starts
this bucket fresh.

Usage:
  python3 backup_high_rate_current_quarter.py            # incremental backup
  python3 backup_high_rate_current_quarter.py --verify     # also restore+check
"""

import argparse
import json
import os
import shutil
import subprocess
import urllib.request
from datetime import date, datetime, timedelta

import pytz
from dotenv import dotenv_values

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV = dotenv_values(os.path.join(REPO_ROOT, ".env"))

INFLUX_HTTP_URL = "http://localhost:8086"
INFLUX_CONTAINER = "envoy_influx"
DOCKER_BIN = "/snap/bin/docker"

SOURCE_BUCKET = "high_rate"
STAGING_BUCKET = "current_quarter_staging"

HOST_BACKUP_DIR = "/mnt/fast_storage/backups/solar/high_rate_current_quarter"
CONTAINER_BACKUP_PATH = "/backups/high_rate_current_quarter/current"
STATE_PATH = os.path.join(HOST_BACKUP_DIR, "state.json")

ORG = ENV["ORG"]
TOKEN = ENV["ADMIN_TOKEN"]
TZ = pytz.timezone(ENV["TZ"])


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# InfluxDB HTTP API helpers (same patterns as backup_high_rate_quarterly.py)
# ---------------------------------------------------------------------------

def api_get(path):
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}{path}", headers={"Authorization": f"Token {TOKEN}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def api_post_json(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}{path}", data=data, method="POST",
                                  headers={"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def api_delete(path):
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}{path}", method="DELETE",
                                  headers={"Authorization": f"Token {TOKEN}"})
    urllib.request.urlopen(req).close()


def flux_query(flux):
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}/api/v2/query?org={ORG}",
                                  data=flux.encode(), method="POST",
                                  headers={"Authorization": f"Token {TOKEN}",
                                           "Content-Type": "application/vnd.flux",
                                           "Accept": "application/csv"})
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


def find_bucket(name):
    for b in api_get(f"/api/v2/buckets?org={ORG}")["buckets"]:
        if b["name"] == name:
            return b
    return None


def get_org_id():
    for b in api_get(f"/api/v2/buckets?org={ORG}")["buckets"]:
        if b["orgID"]:
            return b["orgID"]
    raise RuntimeError("Could not determine org ID")


def ensure_staging_bucket(org_id, fresh=False):
    existing = find_bucket(STAGING_BUCKET)
    if existing and fresh:
        api_delete(f"/api/v2/buckets/{existing['id']}")
        existing = None
    if not existing:
        api_post_json("/api/v2/buckets", {"orgID": org_id, "name": STAGING_BUCKET, "retentionRules": []})


def count_points(bucket):
    out = flux_query(f'from(bucket:"{bucket}") |> range(start:0) |> count()')
    total = 0
    value_idx = None
    for line in out.strip().splitlines():
        if not line.strip():
            value_idx = None
            continue
        cols = line.split(",")
        if line.startswith(",result"):
            value_idx = cols.index("_value")
            continue
        if value_idx is None:
            continue
        total += int(float(cols[value_idx]))
    return total


# ---------------------------------------------------------------------------
# Copy (day-chunked, same reasoning as backup_high_rate_quarterly.py)
# ---------------------------------------------------------------------------

def copy_window(start_utc, end_utc):
    day = start_utc
    while day < end_utc:
        next_day = min(day + timedelta(days=1), end_utc)
        start_s = day.strftime("%Y-%m-%dT%H:%M:%SZ")
        stop_s = next_day.strftime("%Y-%m-%dT%H:%M:%SZ")
        log(f"  copying {start_s} -> {stop_s}")
        flux_query(
            f'from(bucket:"{SOURCE_BUCKET}") '
            f'|> range(start:{start_s}, stop:{stop_s}) '
            f'|> to(bucket:"{STAGING_BUCKET}", org:"{ORG}")'
        )
        day = next_day


# ---------------------------------------------------------------------------
# influx backup/restore CLI (inside the container)
# ---------------------------------------------------------------------------

def docker_exec(*args):
    cmd = [DOCKER_BIN, "exec", INFLUX_CONTAINER] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker exec failed: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def run_backup():
    if os.path.exists(f"{HOST_BACKUP_DIR}/current"):
        shutil.rmtree(f"{HOST_BACKUP_DIR}/current")
    docker_exec("influx", "backup", CONTAINER_BACKUP_PATH,
                "--bucket", STAGING_BUCKET, "--org", ORG, "--host", INFLUX_HTTP_URL)
    log(f"  influx backup written to {HOST_BACKUP_DIR}/current")


def run_restore_check():
    verify_bucket = "verify_restore_tmp"
    existing = find_bucket(verify_bucket)
    if existing:
        api_delete(f"/api/v2/buckets/{existing['id']}")

    docker_exec("influx", "restore", CONTAINER_BACKUP_PATH,
                "--bucket", STAGING_BUCKET, "--new-bucket", verify_bucket,
                "--org", ORG, "--host", INFLUX_HTTP_URL)

    staged_count = count_points(STAGING_BUCKET)
    restored_count = count_points(verify_bucket)

    existing = find_bucket(verify_bucket)
    if existing:
        api_delete(f"/api/v2/buckets/{existing['id']}")

    if staged_count != restored_count:
        raise RuntimeError(f"Restore check FAILED: staged={staged_count} restored={restored_count}")
    log(f"  restore check OK: {restored_count} points match")


# ---------------------------------------------------------------------------
# Quarter bounds + state
# ---------------------------------------------------------------------------

def current_quarter_label_and_start(now_local_date):
    q = (now_local_date.month - 1) // 3 + 1
    start_month = (q - 1) * 3 + 1
    start = date(now_local_date.year, start_month, 1)
    return f"{now_local_date.year}Q{q}", start


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return None


def save_state(state):
    os.makedirs(HOST_BACKUP_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    org_id = get_org_id()
    now_utc = datetime.now(pytz.UTC)
    now_local_date = now_utc.astimezone(TZ).date()

    quarter_label, quarter_start_date = current_quarter_label_and_start(now_local_date)
    quarter_start_utc = TZ.localize(datetime.combine(quarter_start_date, datetime.min.time())).astimezone(pytz.UTC)

    state = load_state()
    if state is None or state.get("quarter_label") != quarter_label:
        log(f"Starting fresh for {quarter_label} (staging bucket reset)")
        ensure_staging_bucket(org_id, fresh=True)
        covered_through = quarter_start_utc
    else:
        ensure_staging_bucket(org_id, fresh=False)
        covered_through = datetime.fromisoformat(state["covered_through_utc"])

    if covered_through >= now_utc:
        log(f"{quarter_label}: nothing new since last run ({covered_through.isoformat()})")
        return

    log(f"{quarter_label}: copying {covered_through.isoformat()} -> {now_utc.isoformat()}")
    copy_window(covered_through, now_utc)

    run_backup()
    if args.verify:
        run_restore_check()

    save_state({
        "quarter_label": quarter_label,
        "covered_through_utc": now_utc.isoformat(),
        "last_backed_up_at": now_utc.isoformat(),
    })
    log(f"{quarter_label}: done.")


if __name__ == "__main__":
    main()
