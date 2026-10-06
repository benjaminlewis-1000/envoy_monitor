#!/usr/bin/env python3
"""Shared mechanism for backing up a time window of high_rate without ever
writing a duplicate copy into another InfluxDB bucket.

Replaces the earlier scratch-bucket + `influx backup`/`restore` CLI approach
(see git history) with: query the window day-by-day (bounded memory) with
Flux's pivot() so each row already carries all its fields together, build
line-protocol text directly, gzip-compress incrementally straight to the
one destination file (no intermediate bucket, no docker exec, no CLI). The
InfluxDB v2 write API accepts a gzip-encoded body directly
(Content-Encoding: gzip), so restore is the mirror image: read the file's
bytes and POST them back, no decompression step needed either.

Used by both backup_high_rate_quarterly.py (one window per completed
quarter) and backup_high_rate_current_quarter.py (one window, re-done daily,
for the in-progress quarter).
"""

import gzip
import json
import os
import urllib.error
import urllib.request
from datetime import timedelta

from dotenv import dotenv_values

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV = dotenv_values(os.path.join(REPO_ROOT, ".env"))

INFLUX_HTTP_URL = "http://localhost:8086"
ORG = ENV["ORG"]
TOKEN = ENV["ADMIN_TOKEN"]

SOURCE_BUCKET = "high_rate"

# Known a priori from envoy_logger's model.py / sampling_loop.py -- every
# field it ever writes is one of these, all floats. Any other pivoted
# column is a tag, not a field.
FIELD_NAMES = {"P", "Q", "S", "I_rms", "V_rms"}
META_COLUMNS = {"", "result", "table", "_start", "_stop", "_time", "_measurement"}

RESTORE_BATCH_LINES = 200_000  # bound each write POST's size


def log(msg):
    from datetime import datetime
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def flux_query(flux):
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}/api/v2/query?org={ORG}",
                                  data=flux.encode(), method="POST",
                                  headers={"Authorization": f"Token {TOKEN}",
                                           "Content-Type": "application/vnd.flux",
                                           "Accept": "application/csv"})
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


def write_line_protocol(bucket, lp_bytes, gzip_encoded=False):
    headers = {"Authorization": f"Token {TOKEN}", "Content-Type": "text/plain; charset=utf-8"}
    if gzip_encoded:
        headers["Content-Encoding"] = "gzip"
    req = urllib.request.Request(
        f"{INFLUX_HTTP_URL}/api/v2/write?org={ORG}&bucket={bucket}&precision=ns",
        data=lp_bytes, method="POST", headers=headers)
    try:
        urllib.request.urlopen(req).close()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"write failed ({e.code}): {e.read().decode()}") from e


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


def find_bucket(name):
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}/api/v2/buckets?org={ORG}",
                                  headers={"Authorization": f"Token {TOKEN}"})
    with urllib.request.urlopen(req) as r:
        for b in json.load(r)["buckets"]:
            if b["name"] == name:
                return b
    return None


def delete_bucket_if_exists(name):
    b = find_bucket(name)
    if b:
        req = urllib.request.Request(f"{INFLUX_HTTP_URL}/api/v2/buckets/{b['id']}", method="DELETE",
                                      headers={"Authorization": f"Token {TOKEN}"})
        urllib.request.urlopen(req).close()


def create_bucket(name, org_id):
    body = json.dumps({"orgID": org_id, "name": name, "retentionRules": []}).encode()
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}/api/v2/buckets", data=body, method="POST",
                                  headers={"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)["id"]


def get_org_id():
    req = urllib.request.Request(f"{INFLUX_HTTP_URL}/api/v2/buckets?org={ORG}",
                                  headers={"Authorization": f"Token {TOKEN}"})
    with urllib.request.urlopen(req) as r:
        for b in json.load(r)["buckets"]:
            if b["orgID"]:
                return b["orgID"]
    raise RuntimeError("Could not determine org ID")


# ---------------------------------------------------------------------------
# Line protocol construction
# ---------------------------------------------------------------------------

def _escape_tag(s):
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def _escape_measurement(s):
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _parse_rfc3339_to_epoch_ns(ts):
    # e.g. "2026-10-05T04:00:05Z" or with fractional seconds
    from datetime import datetime, timezone
    ts = ts.rstrip("Z")
    if "." in ts:
        base, frac = ts.split(".")
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        ns_frac = int((frac + "000000000")[:9])
    else:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        ns_frac = 0
    epoch_s = int(dt.timestamp())
    return epoch_s * 1_000_000_000 + ns_frac


def csv_to_lp_lines(csv_text):
    """Parse a pivoted Flux CSV response (possibly multiple annotation
    blocks, one per distinct series schema) into line-protocol strings."""
    lines_out = []
    header = None
    for line in csv_text.splitlines():
        if not line.strip():
            header = None
            continue
        cols = line.split(",")
        if line.startswith(",result"):
            header = cols
            continue
        if header is None:
            continue

        row = dict(zip(header, cols))
        measurement = row.get("_measurement")
        time_str = row.get("_time")
        if not measurement or not time_str:
            continue

        tags = {}
        fields = {}
        for col, val in row.items():
            if col in META_COLUMNS or val == "":
                continue
            if col in FIELD_NAMES:
                fields[col] = val
            else:
                tags[col] = val

        if not fields:
            continue

        tag_str = ",".join(f"{_escape_tag(k)}={_escape_tag(v)}" for k, v in sorted(tags.items()))
        field_str = ",".join(f"{k}={float(v)}" for k, v in sorted(fields.items()))
        ts_ns = _parse_rfc3339_to_epoch_ns(time_str)

        lp = f"{_escape_measurement(measurement)},{tag_str} {field_str} {ts_ns}" if tag_str \
            else f"{_escape_measurement(measurement)} {field_str} {ts_ns}"
        lines_out.append(lp)

    return lines_out


# ---------------------------------------------------------------------------
# Backup: query window day-by-day, stream line protocol into a gzip file
# ---------------------------------------------------------------------------

def backup_window_to_file(start_utc, end_utc, dest_path, bucket=SOURCE_BUCKET):
    tmp_path = dest_path + ".tmp"
    total_lines = 0
    day = start_utc
    with gzip.GzipFile(tmp_path, "wb") as gz:
        while day < end_utc:
            next_day = min(day + timedelta(days=1), end_utc)
            start_s = day.strftime("%Y-%m-%dT%H:%M:%SZ")
            stop_s = next_day.strftime("%Y-%m-%dT%H:%M:%SZ")
            log(f"  querying {start_s} -> {stop_s}")
            csv_text = flux_query(
                f'from(bucket:"{bucket}") '
                f'|> range(start:{start_s}, stop:{stop_s}) '
                f'|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")'
            )
            lp_lines = csv_to_lp_lines(csv_text)
            if lp_lines:
                gz.write(("\n".join(lp_lines) + "\n").encode())
                total_lines += len(lp_lines)
            day = next_day

    os.replace(tmp_path, dest_path)
    return total_lines


# ---------------------------------------------------------------------------
# Restore: read the file, POST its gzip bytes straight back (batched)
# ---------------------------------------------------------------------------

def restore_file_to_bucket(src_path, bucket):
    with gzip.open(src_path, "rt") as f:
        lines = f.read().splitlines()

    total = 0
    for i in range(0, len(lines), RESTORE_BATCH_LINES):
        batch = lines[i:i + RESTORE_BATCH_LINES]
        buf = ("\n".join(batch) + "\n").encode()
        import io
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb") as gz:
            gz.write(buf)
        write_line_protocol(bucket, gz_buf.getvalue(), gzip_encoded=True)
        total += len(batch)
    return total
