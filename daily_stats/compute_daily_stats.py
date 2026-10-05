#!/usr/bin/env python3
"""Compute per-day summary stats from high_rate's raw 5s telemetry and write
them to a new, tiny `daily_stats` bucket.

Why this exists: low_rate already has one Wh total per inverter/line per day
(computed by envoy-logger itself), but that's a sum -- it throws away peak
power, timing, and shape. Once high_rate's raw samples eventually age out
(see the live-retention discussion), anything not captured here is gone for
good. This script captures the things worth keeping before that happens:

  inverter_daily_stats (tags: serial, array, row, col, source)
    wh              - daily energy, computed independently of low_rate
    max_w           - peak output that day
    max_w_time      - unix epoch seconds when the peak happened
    sample_count    - how many samples reported (coverage; a dropping-out
                      panel shows up here before it shows up anywhere else)
    near_peak_seconds - time spent within 2% of that day's own peak (a
                      clipping proxy)
    relative_perf   - wh / median(wh of inverters sharing its array tag,
                      that day). Comparing within the same array cancels out
                      both weather and orientation (sun angle), so a panel
                      sitting persistently below ~1.0 relative to its
                      arraymates is a real, local problem -- not a west vs.
                      south confound.

  line_daily_stats (tags: line-idx, measurement-type, source)
    wh, max_w, max_w_time, avg_w
    ramp_stddev     - stddev of 5s-to-5s P deltas (catches transient/
                      flickering shade; does NOT catch steady partial shade,
                      which shows up as a smooth lower plateau instead)
    import_seconds / export_seconds (net line only) - net>0 means importing
                      from the grid, net<=0 means exporting (confirmed
                      empirically: at night, with production=0, net equals
                      consumption exactly).

Deliberately not stored: any trend/rolling-window derivative of
relative_perf. That's cheap to compute at query time in Grafana and the
window length is then just a dashboard knob, not a backfill.

Points are stamped at local midnight of the day they summarize, so re-running
a day is naturally idempotent -- InfluxDB overwrites by exact tag-set +
timestamp, no manifest file needed (unlike the quarterly backup script,
which copies raw data and genuinely can't just "overwrite").

Usage:
  python3 compute_daily_stats.py                  # yesterday (local date)
  python3 compute_daily_stats.py --date 2026-03-14 # one specific day
  python3 compute_daily_stats.py --backfill        # every day since data began
"""

import argparse
import os
import warnings
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytz
from dotenv import dotenv_values
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.warnings import MissingPivotFunction
from influxdb_client.client.write_api import SYNCHRONOUS

# We intentionally query one field ("P") in long form for pandas groupby,
# rather than pivoting -- this warning doesn't apply to how we use the result.
warnings.simplefilter("ignore", MissingPivotFunction)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
ENV = dotenv_values(os.path.join(REPO_ROOT, ".env"))

INFLUX_HTTP_URL = "http://localhost:8086"
SOURCE_BUCKET = "high_rate"
STATS_BUCKET = "daily_stats"
ORG = ENV["ORG"]
TOKEN = ENV["ADMIN_TOKEN"]
TZ = pytz.timezone(ENV["TZ"])

NEAR_PEAK_FRACTION = 0.98

client = InfluxDBClient(url=INFLUX_HTTP_URL, token=TOKEN, org=ORG)
query_api = client.query_api()
write_api = client.write_api(write_options=SYNCHRONOUS)
buckets_api = client.buckets_api()


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def ensure_stats_bucket():
    if buckets_api.find_bucket_by_name(STATS_BUCKET):
        return
    org = client.organizations_api().find_organizations(org=ORG)[0]
    buckets_api.create_bucket(bucket_name=STATS_BUCKET, org_id=org.id, retention_rules=[])
    log(f"Created bucket {STATS_BUCKET}")


def get_data_start_date():
    df = query_api.query_data_frame(
        f'from(bucket:"{SOURCE_BUCKET}") |> range(start:0) '
        f'|> filter(fn:(r)=>r._measurement=="consumption-line0" and r._field=="P") '
        f'|> first() |> keep(columns:["_time"])'
    )
    if isinstance(df, list):
        df = pd.concat(df)
    if df.empty:
        raise RuntimeError("No data found in high_rate")
    ts = pd.Timestamp(df["_time"].iloc[0])
    return ts.tz_convert(TZ).date()


# ---------------------------------------------------------------------------
# Time-weighted duration helper
# ---------------------------------------------------------------------------

def sample_durations_seconds(times_utc):
    """For irregularly-spaced samples (inverters poll ~5min with dedup;
    lines can have occasional gaps), give each sample a "duration" of half
    the gap before it plus half the gap after it, so integrals/near-peak/
    import-export time aren't biased by uneven spacing. Edge samples get
    just their one adjacent half-gap."""
    t = np.sort(times_utc.view("int64").astype("float64") / 1e9)  # seconds
    n = len(t)
    if n == 0:
        return np.array([])
    if n == 1:
        return np.array([0.0])
    gaps = np.diff(t)
    durations = np.zeros(n)
    durations[:-1] += gaps / 2
    durations[1:] += gaps / 2
    return durations


def weighted_wh(values, durations_seconds):
    """Energy in Wh from power samples (W) and their durations (s)."""
    return float(np.sum(values * durations_seconds) / 3600.0)


# ---------------------------------------------------------------------------
# Per-day computation
# ---------------------------------------------------------------------------

def query_day(measurement_filter, keep_columns, day_start_utc, day_end_utc):
    start_s = day_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    stop_s = day_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    flux = (
        f'from(bucket:"{SOURCE_BUCKET}") '
        f'|> range(start:{start_s}, stop:{stop_s}) '
        f'|> filter(fn:(r)=>r._field=="P" and ({measurement_filter})) '
        f'|> keep(columns:{keep_columns})'
    )
    df = query_api.query_data_frame(flux)
    if isinstance(df, list):
        df = pd.concat(df) if df else pd.DataFrame()
    return df


def compute_inverter_stats(df):
    """Returns a dict {serial: {...fields..., tags: {...}}}."""
    if df.empty:
        return {}

    results = {}
    for serial, g in df.groupby("serial"):
        g = g.sort_values("_time")
        times = pd.to_datetime(g["_time"]).values
        values = g["_value"].to_numpy(dtype="float64")
        durations = sample_durations_seconds(times)

        max_idx = int(np.argmax(values))
        max_w = float(values[max_idx])
        max_w_time = int(pd.Timestamp(times[max_idx]).timestamp())

        near_peak_mask = values >= NEAR_PEAK_FRACTION * max_w if max_w > 0 else np.zeros_like(values, dtype=bool)

        results[serial] = {
            "tags": {
                "serial": serial,
                "array": g["array"].iloc[0],
                "row": str(g["row"].iloc[0]),
                "col": str(g["col"].iloc[0]),
                "source": g["source"].iloc[0],
            },
            "wh": weighted_wh(values, durations),
            "max_w": max_w,
            "max_w_time": max_w_time,
            "sample_count": int(len(g)),
            "near_peak_seconds": float(np.sum(durations[near_peak_mask])),
        }

    # relative_perf: wh vs. median wh of inverters sharing the same array tag
    array_whs = {}
    for serial, r in results.items():
        array_whs.setdefault(r["tags"]["array"], []).append(r["wh"])
    array_medians = {array: float(np.median(whs)) for array, whs in array_whs.items()}
    for serial, r in results.items():
        median = array_medians[r["tags"]["array"]]
        r["relative_perf"] = (r["wh"] / median) if median > 0 else None

    return results


def compute_line_stats(df):
    """Returns a dict {(measurement_type, line_idx): {...}}."""
    if df.empty:
        return {}

    results = {}
    for (mtype, line_idx), g in df.groupby(["measurement-type", "line-idx"]):
        g = g.sort_values("_time")
        times = pd.to_datetime(g["_time"]).values
        values = g["_value"].to_numpy(dtype="float64")
        durations = sample_durations_seconds(times)

        max_idx = int(np.argmax(values))
        max_w = float(values[max_idx])
        max_w_time = int(pd.Timestamp(times[max_idx]).timestamp())
        total_seconds = float(np.sum(durations))
        wh = weighted_wh(values, durations)
        avg_w = (wh * 3600.0 / total_seconds) if total_seconds > 0 else None

        deltas = np.diff(values)
        ramp_stddev = float(np.std(deltas)) if len(deltas) > 0 else None

        entry = {
            "tags": {
                "line-idx": str(line_idx),
                "measurement-type": mtype,
                "source": g["source"].iloc[0],
            },
            "wh": wh,
            "max_w": max_w,
            "max_w_time": max_w_time,
            "avg_w": avg_w,
            "ramp_stddev": ramp_stddev,
        }

        if mtype == "net":
            import_mask = values > 0
            entry["import_seconds"] = float(np.sum(durations[import_mask]))
            entry["export_seconds"] = float(np.sum(durations[~import_mask]))

        results[(mtype, line_idx)] = entry

    return results


def write_stats(day_local, inverter_stats, line_stats):
    ts = TZ.localize(datetime.combine(day_local, datetime.min.time()))
    points = []

    for serial, r in inverter_stats.items():
        p = Point("inverter_daily_stats").time(ts, WritePrecision.S)
        for k, v in r["tags"].items():
            p = p.tag(k, v)
        p = p.field("wh", r["wh"]).field("max_w", r["max_w"]).field("max_w_time", r["max_w_time"])
        p = p.field("sample_count", r["sample_count"]).field("near_peak_seconds", r["near_peak_seconds"])
        if r["relative_perf"] is not None:
            p = p.field("relative_perf", r["relative_perf"])
        points.append(p)

    for (mtype, line_idx), r in line_stats.items():
        p = Point("line_daily_stats").time(ts, WritePrecision.S)
        for k, v in r["tags"].items():
            p = p.tag(k, v)
        p = p.field("wh", r["wh"]).field("max_w", r["max_w"]).field("max_w_time", r["max_w_time"])
        if r["avg_w"] is not None:
            p = p.field("avg_w", r["avg_w"])
        if r["ramp_stddev"] is not None:
            p = p.field("ramp_stddev", r["ramp_stddev"])
        if "import_seconds" in r:
            p = p.field("import_seconds", r["import_seconds"]).field("export_seconds", r["export_seconds"])
        points.append(p)

    write_api.write(bucket=STATS_BUCKET, org=ORG, record=points)
    return len(points)


def process_day(day_local):
    day_start_local = TZ.localize(datetime.combine(day_local, datetime.min.time()))
    day_end_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(pytz.UTC)
    day_end_utc = day_end_local.astimezone(pytz.UTC)

    inverter_df = query_day(
        'r["measurement-type"]=="inverter"',
        '["_time", "_value", "serial", "array", "row", "col", "source"]',
        day_start_utc, day_end_utc,
    )
    line_df = query_day(
        'r["measurement-type"]=="consumption" or r["measurement-type"]=="production" or r["measurement-type"]=="net"',
        '["_time", "_value", "measurement-type", "line-idx", "source"]',
        day_start_utc, day_end_utc,
    )

    inverter_stats = compute_inverter_stats(inverter_df)
    line_stats = compute_line_stats(line_df)

    if not inverter_stats and not line_stats:
        log(f"{day_local}: no data found, skipping")
        return

    n = write_stats(day_local, inverter_stats, line_stats)
    log(f"{day_local}: wrote {n} points ({len(inverter_stats)} inverters, {len(line_stats)} lines)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD, local date (default: yesterday)")
    parser.add_argument("--backfill", action="store_true", help="process every day since data began")
    args = parser.parse_args()

    ensure_stats_bucket()

    if args.backfill:
        start = get_data_start_date()
        today = datetime.now(TZ).date()
        day = start
        while day < today:
            process_day(day)
            day += timedelta(days=1)
        return

    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        day = datetime.now(TZ).date() - timedelta(days=1)

    process_day(day)


if __name__ == "__main__":
    main()
