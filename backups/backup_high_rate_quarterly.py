#!/usr/bin/env python3
"""Quarterly cold-storage backup of the high_rate bucket (the raw 5s envoy-logger data).

Why quarterly, not a rolling window: re-querying from scratch on every run
would mean the daily cost grows across the quarter. Instead this covers a
single calendar-quarter window (Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec, each
padded +/-2 days for safety) once, after it's fully elapsed.

Mechanism: query the window day-by-day and stream it straight to a gzip'd
line-protocol file on disk (via backups/influx_lp_backup.py) -- no scratch
bucket, no duplicate live data, no `influx backup`/`restore` CLI, no
`docker exec`. Just HTTP queries out and, for --verify, an HTTP write
(gzip-encoded body) back into a scratch bucket to compare point counts
before deleting it.

Output lands in /mnt/fast_storage/backups/solar/high_rate_quarterly/<label>.lp.gz,
where <label> is e.g. "2025Q4". Already-completed quarters are tracked in
completed_quarters.json alongside them, so reruns are idempotent.

Usage:
  python3 backup_high_rate_quarterly.py              # backup all pending quarters
  python3 backup_high_rate_quarterly.py --verify      # also restore+check each
  python3 backup_high_rate_quarterly.py --dry-run      # print the plan, do nothing
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

import pytz
from dotenv import dotenv_values

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import influx_lp_backup as lp

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV = dotenv_values(os.path.join(REPO_ROOT, ".env"))

HOST_BACKUP_ROOT = "/mnt/fast_storage/backups/solar/high_rate_quarterly"
MANIFEST_PATH = os.path.join(HOST_BACKUP_ROOT, "completed_quarters.json")

TZ = pytz.timezone(ENV["TZ"])
PAD_DAYS = 2


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def get_data_start():
    out = lp.flux_query(
        f'from(bucket:"{lp.SOURCE_BUCKET}") |> range(start:0) '
        f'|> filter(fn:(r)=>r._measurement=="consumption-line0" and r._field=="P") '
        f'|> first() |> keep(columns:["_time"])'
    )
    lines = [l for l in out.strip().splitlines() if l and not l.startswith(",result")]
    if not lines:
        raise RuntimeError("No data found in high_rate to determine start date")
    ts = lines[0].split(",")[-1]
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)


def quarter_bounds(year, q):
    start_month = (q - 1) * 3 + 1
    start = date(year, start_month, 1)
    end = date(year + 1, 1, 1) if q == 4 else date(year, start_month + 3, 1)
    return start, end


def pending_quarters(data_start_utc, now_utc):
    data_start_local = data_start_utc.astimezone(TZ).date()
    now_local = now_utc.astimezone(TZ).date()

    year, q = data_start_local.year, (data_start_local.month - 1) // 3 + 1
    while True:
        q_start, q_end = quarter_bounds(year, q)
        if (year, q) == (now_local.year, (now_local.month - 1) // 3 + 1):
            break
        if q_start > now_local:
            break

        padded_start = TZ.localize(datetime.combine(q_start, datetime.min.time())) - timedelta(days=PAD_DAYS)
        padded_end = TZ.localize(datetime.combine(q_end, datetime.min.time())) + timedelta(days=PAD_DAYS)
        padded_start_utc = padded_start.astimezone(pytz.UTC)
        padded_end_utc = min(padded_end.astimezone(pytz.UTC), now_utc)

        yield f"{year}Q{q}", padded_start_utc, padded_end_utc

        year, q = (year + 1, 1) if q == 4 else (year, q + 1)


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    os.makedirs(HOST_BACKUP_ROOT, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def verify_backup(dest_path):
    verify_bucket = "verify_restore_tmp"
    lp.delete_bucket_if_exists(verify_bucket)
    org_id = lp.get_org_id()
    lp.create_bucket(verify_bucket, org_id)

    n = lp.restore_file_to_bucket(dest_path, verify_bucket)
    restored_count = lp.count_points(verify_bucket)
    lp.delete_bucket_if_exists(verify_bucket)
    log(f"  restore check: {n} lines restored, {restored_count} points counted")
    return restored_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    data_start_utc = get_data_start()
    now_utc = datetime.now(pytz.UTC)
    manifest = load_manifest()

    todo = [(label, s, e) for label, s, e in pending_quarters(data_start_utc, now_utc)
            if label not in manifest]

    if not todo:
        log("Nothing to do -- all complete quarters already backed up.")
        return

    for label, start_utc, end_utc in todo:
        log(f"Quarter {label}: window {start_utc.isoformat()} -> {end_utc.isoformat()}")
        if args.dry_run:
            continue

        os.makedirs(HOST_BACKUP_ROOT, exist_ok=True)
        dest_path = os.path.join(HOST_BACKUP_ROOT, f"{label}.lp.gz")
        n_lines = lp.backup_window_to_file(start_utc, end_utc, dest_path)
        size = os.path.getsize(dest_path)
        log(f"  wrote {n_lines} lines ({size/1e6:.1f} MB) to {dest_path}")

        if args.verify:
            verify_backup(dest_path)

        manifest[label] = {
            "window_start_utc": start_utc.isoformat(),
            "window_end_utc": end_utc.isoformat(),
            "backed_up_at": datetime.now(pytz.UTC).isoformat(),
            "lines": n_lines,
            "verified": bool(args.verify),
        }
        save_manifest(manifest)
        log(f"Quarter {label} done.")

    if args.dry_run:
        log(f"Dry run: would back up {len(todo)} quarter(s): {[l for l, _, _ in todo]}")


if __name__ == "__main__":
    main()
