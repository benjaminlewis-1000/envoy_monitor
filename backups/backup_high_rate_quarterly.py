#!/usr/bin/env python3
"""Quarterly cold-storage backup of the high_rate bucket (the raw 5s envoy-logger data).

Why quarterly, not a rolling window: InfluxDB OSS's `influx backup` always
snapshots the bucket's *entire current* contents -- there's no --start/--end
flag -- so backing up the live bucket on a schedule means every run mostly
re-copies what the last run already captured. Instead, this script copies a
single calendar-quarter window (Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec, each
padded +/-2 days for safety) out of high_rate into a scratch bucket via Flux
`to()`, then backs up just that scratch bucket. That gives a near
non-redundant backup per quarter instead of a full-bucket snapshot every run.

The actual `influx backup`/`influx restore` CLI calls have no REST
equivalent, so they run inside the envoy_influx container via `docker exec`
(see docker-compose.yml's `/backups` mount and INFLUX_TOKEN env var, which
lets the CLI authenticate without the token ever appearing in argv). Bucket
management and the windowed copy itself go through the HTTP API directly
from here, same as the rest of this repo's scripts.

Output lands in /mnt/fast_storage/backups/solar/high_rate_quarterly/<label>/
(host path; /backups/high_rate_quarterly/<label> inside the container),
where <label> is e.g. "2025Q4". Already-completed quarters are tracked in
completed_quarters.json alongside them, so reruns are idempotent.

Usage:
  python3 backup_high_rate_quarterly.py              # backup all pending quarters
  python3 backup_high_rate_quarterly.py --verify      # also restore+check the newest backup
  python3 backup_high_rate_quarterly.py --dry-run      # print the plan, do nothing
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import pytz
from dotenv import dotenv_values

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV = dotenv_values(os.path.join(REPO_ROOT, ".env"))

INFLUX_HTTP_URL = "http://localhost:8086"   # host-side HTTP API, not the public URL (proxy-gated)
INFLUX_CONTAINER = "envoy_influx"
# Absolute path, not just "docker" -- cron's minimal default PATH doesn't
# include /snap/bin, where this host's docker binary actually lives.
DOCKER_BIN = "/snap/bin/docker"
CONTAINER_BACKUP_ROOT = "/backups/high_rate_quarterly"
HOST_BACKUP_ROOT = "/mnt/fast_storage/backups/solar/high_rate_quarterly"
MANIFEST_PATH = os.path.join(HOST_BACKUP_ROOT, "completed_quarters.json")

ORG = ENV["ORG"]
TOKEN = ENV["ADMIN_TOKEN"]
TZ = pytz.timezone(ENV["TZ"])

SOURCE_BUCKET = "high_rate"
STAGING_BUCKET = "backup_staging"

PAD_DAYS = 2


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# InfluxDB HTTP API helpers (bucket management + Flux query/write)
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


def get_org_id():
    buckets = api_get(f"/api/v2/buckets?org={ORG}")["buckets"]
    for b in buckets:
        if b["orgID"]:
            return b["orgID"]
    raise RuntimeError("Could not determine org ID from any bucket")


def find_bucket(name):
    for b in api_get(f"/api/v2/buckets?org={ORG}")["buckets"]:
        if b["name"] == name:
            return b
    return None


def reset_staging_bucket(org_id):
    """Delete and recreate STAGING_BUCKET so each quarter starts from empty."""
    existing = find_bucket(STAGING_BUCKET)
    if existing:
        api_delete(f"/api/v2/buckets/{existing['id']}")
    created = api_post_json("/api/v2/buckets", {
        "orgID": org_id,
        "name": STAGING_BUCKET,
        "retentionRules": [],
    })
    return created["id"]


def delete_staging_bucket():
    existing = find_bucket(STAGING_BUCKET)
    if existing:
        api_delete(f"/api/v2/buckets/{existing['id']}")


def get_data_start():
    """Earliest timestamp present in high_rate (used to clamp the first quarter)."""
    out = flux_query(
        f'from(bucket:"{SOURCE_BUCKET}") |> range(start:0) '
        f'|> filter(fn:(r)=>r._measurement=="consumption-line0" and r._field=="P") '
        f'|> first() |> keep(columns:["_time"])'
    )
    lines = [l for l in out.strip().splitlines() if l and not l.startswith(",result")]
    if not lines:
        raise RuntimeError("No data found in high_rate to determine start date")
    ts = lines[0].split(",")[-1]
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)


def copy_window_to_staging(start_utc, end_utc):
    """Copy [start_utc, end_utc) from SOURCE_BUCKET into STAGING_BUCKET, one day at a time."""
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
# influx backup/restore CLI, run inside the container
# ---------------------------------------------------------------------------

def docker_exec(*args):
    cmd = [DOCKER_BIN, "exec", INFLUX_CONTAINER] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker exec failed: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def run_backup(label):
    # influx backup refuses to write into a directory that already exists
    # (e.g. left over from a prior crashed/interrupted run).
    host_path = f"{HOST_BACKUP_ROOT}/{label}"
    if os.path.exists(host_path):
        shutil.rmtree(host_path)

    container_path = f"{CONTAINER_BACKUP_ROOT}/{label}"
    docker_exec("influx", "backup", container_path,
                "--bucket", STAGING_BUCKET, "--org", ORG, "--host", INFLUX_HTTP_URL)
    log(f"  influx backup written to {host_path}")


def run_restore_check(label):
    """Restore the backup into a scratch bucket and compare point counts
    against what's currently in staging (same data, since we haven't wiped
    staging yet at the point this is called from main())."""
    container_path = f"{CONTAINER_BACKUP_ROOT}/{label}"
    verify_bucket = "verify_restore_tmp"
    # Clean up any stale verify bucket from a prior failed run.
    existing = find_bucket(verify_bucket)
    if existing:
        api_delete(f"/api/v2/buckets/{existing['id']}")

    docker_exec("influx", "restore", container_path,
                "--bucket", STAGING_BUCKET, "--new-bucket", verify_bucket,
                "--org", ORG, "--host", INFLUX_HTTP_URL)

    def count_points(bucket):
        """Sum the _value column of a count() CSV result. Flux's CSV output
        can have multiple annotation/header blocks (one per distinct table
        schema), and tag columns trail _value, so the column index must be
        looked up from each block's own header rather than assumed fixed."""
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

    staged_count = count_points(STAGING_BUCKET)
    restored_count = count_points(verify_bucket)

    existing = find_bucket(verify_bucket)
    if existing:
        api_delete(f"/api/v2/buckets/{existing['id']}")

    if staged_count != restored_count:
        raise RuntimeError(
            f"Restore check FAILED for {label}: staged={staged_count} restored={restored_count}"
        )
    log(f"  restore check OK for {label}: {restored_count} points match")


# ---------------------------------------------------------------------------
# Quarter windows
# ---------------------------------------------------------------------------

def quarter_bounds(year, q):
    """Calendar-quarter [start, end) as naive dates, end exclusive (first day of next quarter)."""
    start_month = (q - 1) * 3 + 1
    start = date(year, start_month, 1)
    if q == 4:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, start_month + 3, 1)
    return start, end


def pending_quarters(data_start_utc, now_utc):
    """Yield (label, padded_start_utc, padded_end_utc) for every complete
    calendar quarter from the one containing data_start through the one
    before "now" (the current, incomplete quarter is never backed up)."""
    data_start_local = data_start_utc.astimezone(TZ).date()
    now_local = now_utc.astimezone(TZ).date()

    year, q = data_start_local.year, (data_start_local.month - 1) // 3 + 1
    while True:
        q_start, q_end = quarter_bounds(year, q)
        if q_start >= now_local.replace(day=1) and (year, q) == (now_local.year, (now_local.month - 1) // 3 + 1):
            break  # reached the current, incomplete quarter
        if q_start > now_local:
            break

        padded_start = TZ.localize(datetime.combine(q_start, datetime.min.time())) - timedelta(days=PAD_DAYS)
        padded_end = TZ.localize(datetime.combine(q_end, datetime.min.time())) + timedelta(days=PAD_DAYS)

        padded_start_utc = padded_start.astimezone(pytz.UTC)
        padded_end_utc = min(padded_end.astimezone(pytz.UTC), now_utc)

        label = f"{year}Q{q}"
        yield label, padded_start_utc, padded_end_utc

        if q == 4:
            year, q = year + 1, 1
        else:
            q += 1


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    os.makedirs(HOST_BACKUP_ROOT, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true", help="restore+check each backup just taken")
    args = parser.parse_args()

    org_id = get_org_id()
    data_start_utc = get_data_start()
    now_utc = datetime.now(pytz.UTC)
    manifest = load_manifest()

    plan = list(pending_quarters(data_start_utc, now_utc))

    def needs_work(label):
        entry = manifest.get(label)
        if entry is None:
            return True
        return args.verify and not entry.get("verified")

    todo = [(label, s, e) for label, s, e in plan if needs_work(label)]

    if not todo:
        log("Nothing to do -- all complete quarters already backed up"
            + (" and verified." if args.verify else "."))
        return

    for label, start_utc, end_utc in todo:
        entry = manifest.get(label)
        need_backup = entry is None
        need_verify = args.verify and not (entry or {}).get("verified")

        log(f"Quarter {label}: window {start_utc.isoformat()} -> {end_utc.isoformat()}"
            f" (backup={need_backup}, verify={need_verify})")
        if args.dry_run:
            continue

        # Re-copying is needed whenever we're about to back up (fresh data)
        # or verify (restore-check compares against what's currently staged).
        reset_staging_bucket(org_id)
        copy_window_to_staging(start_utc, end_utc)

        if need_backup:
            run_backup(label)
            manifest[label] = {
                "window_start_utc": start_utc.isoformat(),
                "window_end_utc": end_utc.isoformat(),
                "backed_up_at": datetime.now(pytz.UTC).isoformat(),
                "verified": False,
            }
            save_manifest(manifest)

        if need_verify:
            run_restore_check(label)
            manifest[label]["verified"] = True
            save_manifest(manifest)

        delete_staging_bucket()
        log(f"Quarter {label} done.")

    if args.dry_run:
        log(f"Dry run: would back up {len(todo)} quarter(s): {[l for l, _, _ in todo]}")


if __name__ == "__main__":
    main()
