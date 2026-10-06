#!/usr/bin/env python3
"""Daily rolling backup of high_rate's CURRENT (not-yet-complete) quarter.

backup_high_rate_quarterly.py only archives a quarter once it's fully over
(plus a 2-day buffer) -- so for up to ~3 months, the in-progress quarter's
data would otherwise have no backup at all beyond the live bucket itself.
This closes that gap on a daily cadence.

Unlike the earlier version of this script, this does NOT try to be
incremental -- it re-queries "quarter start - 2 days -> now" fresh every
run and overwrites the same destination file. That's deliberately simple:
it avoids keeping any persistent scratch bucket (i.e. no duplicated live
data sitting in InfluxDB for the whole quarter), at the cost of the daily
query cost growing somewhat as the quarter progresses. Given how little
high_rate grows per day, re-querying the whole quarter-to-date is still
cheap enough to do daily.

Mechanism is the same as backup_high_rate_quarterly.py: stream the window
straight to a gzip'd line-protocol file (backups/influx_lp_backup.py), no
scratch bucket, no CLI, no docker exec. One fixed, overwritten file -- no
dated history, no pruning needed; the quarterly script is the real archive.

Usage:
  python3 backup_high_rate_current_quarter.py            # backup + overwrite
  python3 backup_high_rate_current_quarter.py --verify     # also restore+check
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta

import pytz
from dotenv import dotenv_values

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import influx_lp_backup as lp

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV = dotenv_values(os.path.join(REPO_ROOT, ".env"))

HOST_BACKUP_DIR = "/mnt/fast_storage/backups/solar/high_rate_current_quarter"
DEST_PATH = os.path.join(HOST_BACKUP_DIR, "current_quarter.lp.gz")

TZ = pytz.timezone(ENV["TZ"])
PAD_DAYS = 2  # matches backup_high_rate_quarterly.py's end-of-quarter padding


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def current_quarter_label_and_padded_start(now_local_date):
    q = (now_local_date.month - 1) // 3 + 1
    start_month = (q - 1) * 3 + 1
    start = date(now_local_date.year, start_month, 1) - timedelta(days=PAD_DAYS)
    return f"{now_local_date.year}Q{q}", start


def verify_backup(dest_path):
    verify_bucket = "verify_restore_tmp"
    lp.delete_bucket_if_exists(verify_bucket)
    org_id = lp.get_org_id()
    lp.create_bucket(verify_bucket, org_id)

    n = lp.restore_file_to_bucket(dest_path, verify_bucket)
    restored_count = lp.count_points(verify_bucket)
    lp.delete_bucket_if_exists(verify_bucket)
    log(f"  restore check: {n} lines restored, {restored_count} points counted")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    now_utc = datetime.now(pytz.UTC)
    now_local_date = now_utc.astimezone(TZ).date()

    label, padded_start_date = current_quarter_label_and_padded_start(now_local_date)
    start_utc = TZ.localize(datetime.combine(padded_start_date, datetime.min.time())).astimezone(pytz.UTC)

    log(f"{label}: backing up {start_utc.isoformat()} -> {now_utc.isoformat()}")

    os.makedirs(HOST_BACKUP_DIR, exist_ok=True)
    n_lines = lp.backup_window_to_file(start_utc, now_utc, DEST_PATH)
    size = os.path.getsize(DEST_PATH)
    log(f"  wrote {n_lines} lines ({size/1e6:.1f} MB) to {DEST_PATH}")

    if args.verify:
        verify_backup(DEST_PATH)

    log(f"{label}: done.")


if __name__ == "__main__":
    main()
