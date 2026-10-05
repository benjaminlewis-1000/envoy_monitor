#!/usr/bin/env python3
"""Nightly full backup of envoy_monitor's small InfluxDB buckets, with
tiered pruning.

Unlike high_rate (see backup_high_rate_quarterly.py), these buckets are
small enough that a full nightly `influx backup` is cheap -- no need for
the quarter-window-via-staging-bucket trick, so this script is much
simpler: back up the live bucket directly, every night, for each of:

  low_rate              - envoy-logger's own daily Wh summaries
  computed_information  - daily_report/main.py's cloud-API daily totals
  daily_stats           - compute_daily_stats.py's per-panel/line stats

Retention (applied per bucket, same scheme as solar_dashboard's and
video_breaker's backup scripts on this host, just with different
constants): keep every backup from the last DAILY_RETENTION_DAYS days,
then one per ISO week for the next WEEKLY_RETENTION_DAYS, then delete.

Output: /mnt/fast_storage/backups/solar/envoy_small_buckets_daily/
(host path; /backups/envoy_small_buckets_daily inside envoy_influx).

Usage:
  python3 backup_small_buckets_daily.py             # backup today + prune
  python3 backup_small_buckets_daily.py --verify     # also restore+check each
  python3 backup_small_buckets_daily.py --prune-only # just apply retention
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta

from dotenv import dotenv_values

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV = dotenv_values(os.path.join(REPO_ROOT, ".env"))

INFLUX_HTTP_URL = "http://localhost:8086"
INFLUX_CONTAINER = "envoy_influx"
DOCKER_BIN = "/snap/bin/docker"
CONTAINER_BACKUP_ROOT = "/backups/envoy_small_buckets_daily"
HOST_BACKUP_ROOT = "/mnt/fast_storage/backups/solar/envoy_small_buckets_daily"

ORG = ENV["ORG"]
TOKEN = ENV["ADMIN_TOKEN"]

BUCKETS = ["low_rate", "computed_information", "daily_stats"]

DAILY_RETENTION_DAYS = 3
WEEKLY_RETENTION_DAYS = 28  # on top of the daily window, per bucket

FILENAME_RE = re.compile(r"^([a-zA-Z_]+)_(\d{4}-\d{2}-\d{2})$")


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def docker_exec(*args):
    cmd = [DOCKER_BIN, "exec", INFLUX_CONTAINER] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker exec failed: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_bucket(bucket, today_str):
    label = f"{bucket}_{today_str}"
    host_path = f"{HOST_BACKUP_ROOT}/{label}"
    if os.path.exists(host_path):
        shutil.rmtree(host_path)

    container_path = f"{CONTAINER_BACKUP_ROOT}/{label}"
    docker_exec("influx", "backup", container_path,
                "--bucket", bucket, "--org", ORG, "--host", INFLUX_HTTP_URL)
    log(f"  {bucket}: backed up to {host_path}")
    return label


def count_points_http(bucket):
    import urllib.request
    flux = f'from(bucket:"{bucket}") |> range(start:0) |> count()'
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}/api/v2/query?org={ORG}",
                                  data=flux.encode(), method="POST",
                                  headers={"Authorization": f"Token {TOKEN}",
                                           "Content-Type": "application/vnd.flux",
                                           "Accept": "application/csv"})
    with urllib.request.urlopen(req) as r:
        out = r.read().decode()
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


def delete_bucket_if_exists(name):
    import urllib.request
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}/api/v2/buckets?org={ORG}",
                                  headers={"Authorization": f"Token {TOKEN}"})
    with urllib.request.urlopen(req) as r:
        buckets = json.load(r)["buckets"]
    for b in buckets:
        if b["name"] == name:
            urllib.request.urlopen(urllib.request.Request(
                f"{INFLUX_HTTP_URL}/api/v2/buckets/{b['id']}", method="DELETE",
                headers={"Authorization": f"Token {TOKEN}"})).close()


def verify_backup(bucket, label):
    verify_bucket = "verify_restore_tmp"
    delete_bucket_if_exists(verify_bucket)
    container_path = f"{CONTAINER_BACKUP_ROOT}/{label}"
    docker_exec("influx", "restore", container_path,
                "--bucket", bucket, "--new-bucket", verify_bucket,
                "--org", ORG, "--host", INFLUX_HTTP_URL)
    live_count = count_points_http(bucket)
    restored_count = count_points_http(verify_bucket)
    delete_bucket_if_exists(verify_bucket)
    if live_count != restored_count:
        log(f"  WARNING: {bucket} restore check mismatch "
            f"(live={live_count}, restored={restored_count}) -- "
            f"may just be a concurrent write between backup and this check")
    else:
        log(f"  {bucket}: restore check OK ({restored_count} points match)")


# ---------------------------------------------------------------------------
# Pruning (same two-tier scheme as solar_dashboard/video_breaker's backup
# scripts on this host, adapted for our "<bucket>_<date>" naming)
# ---------------------------------------------------------------------------

def parse_backups(backup_dir):
    backups = {}  # bucket -> [(date, dirname), ...]
    if not os.path.isdir(backup_dir):
        return backups
    for name in os.listdir(backup_dir):
        m = FILENAME_RE.match(name)
        if not m:
            continue
        bucket, date_str = m.group(1), m.group(2)
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        backups.setdefault(bucket, []).append((d, name))
    for bucket in backups:
        backups[bucket].sort()
    return backups


def classify(dated_names, today):
    daily_cutoff = today - timedelta(days=DAILY_RETENTION_DAYS)
    weekly_cutoff = today - timedelta(days=DAILY_RETENTION_DAYS + WEEKLY_RETENTION_DAYS)

    keep = set()
    for d, name in dated_names:
        if d > daily_cutoff:
            keep.add(name)

    weekly_seen = set()
    for d, name in dated_names:
        if weekly_cutoff < d <= daily_cutoff:
            key = d.isocalendar()[:2]
            if key not in weekly_seen:
                weekly_seen.add(key)
                keep.add(name)

    all_names = {name for _, name in dated_names}
    return keep, all_names - keep


def prune(dry_run=False):
    today = date.today()
    backups_by_bucket = parse_backups(HOST_BACKUP_ROOT)
    for bucket, dated_names in backups_by_bucket.items():
        keep, delete = classify(dated_names, today)
        if not delete:
            continue
        log(f"  {bucket}: keeping {len(keep)}, deleting {len(delete)}")
        for d, name in dated_names:
            if name not in delete:
                continue
            path = os.path.join(HOST_BACKUP_ROOT, name)
            if dry_run:
                log(f"    would delete: {name}")
                continue
            log(f"    deleting: {name}")
            shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--prune-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    os.makedirs(HOST_BACKUP_ROOT, exist_ok=True)

    if not args.prune_only:
        today_str = date.today().isoformat()
        for bucket in BUCKETS:
            label = backup_bucket(bucket, today_str)
            if args.verify:
                verify_backup(bucket, label)

    log("Pruning old backups...")
    prune(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
