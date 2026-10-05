#! /usr/bin/env python

from influxdb_client import InfluxDBClient, Point
import pandas as pd
import pytz
from datetime import datetime, time
from influxdb_client.client.write_api import SYNCHRONOUS
import os
from dotenv import load_dotenv

file_path = os.path.realpath(__file__)
parent_dir = os.path.dirname(os.path.dirname(file_path))
load_dotenv(os.path.join(parent_dir, '.env'))

# InfluxDB connection details
token = os.environ['ADMIN_TOKEN']
org = os.environ['ORG']
bucket = "high_rate"
url = os.environ['URL']

client = InfluxDBClient(url=url, token=token, org=org)
query_api = client.query_api()

# ... (client and query_api initialization as above) ...

TARGET_TIMEZONE = "America/New_York"  # Example: Eastern Time
# Get today's date in the target time zone
if hasattr(pytz, 'timezone'): # For pytz
    tz = pytz.timezone(TARGET_TIMEZONE)
    now_in_tz = tz.localize(datetime.now())
else: # For zoneinfo (Python 3.9+)
    tz = ZoneInfo(TARGET_TIMEZONE)
    now_in_tz = datetime.now(tz)

start_of_day = now_in_tz.replace(hour=0, minute=0, second=0, microsecond=0)
end_of_day = now_in_tz.replace(hour=23, minute=59, second=59, microsecond=999999)

# Convert to UTC for InfluxDB query (InfluxDB stores in UTC by default)
start_of_day_utc = start_of_day.astimezone(pytz.utc) if hasattr(pytz, 'utc') else start_of_day.astimezone(ZoneInfo("UTC"))
end_of_day_utc = end_of_day.astimezone(pytz.utc) if hasattr(pytz, 'utc') else end_of_day.astimezone(ZoneInfo("UTC"))

# Format timestamps for Flux query
start_time_flux = start_of_day_utc.replace(tzinfo=None).isoformat(timespec='seconds') + "Z"
end_time_flux = end_of_day_utc.replace(tzinfo=None).isoformat(timespec='seconds') + "Z"

# Construct the Flux query

flux_query_dataframe = f'''
from(bucket: "{bucket}")
  |> range(start: {start_time_flux}, stop: {end_time_flux})
  |> filter(fn: (r) => r["source"] == "combiner-6")
  |> filter(fn: (r) => r["measurement-type"] == "production")
  |> filter(fn: (r) => r["_field"] == "P")
  |> drop(columns: ["_start", "_stop", "_field", "_measurement", "measurement-type", "source"])
  |> group(columns: ["_time"], mode:"by")
  |> sum(column: "_value")
  |> group()
  |> yield(name: "consumption")
'''

dataframe = query_api.query_data_frame(flux_query_dataframe, org=org)
print(dataframe.head())

import numpy as np
vals = dataframe._value.tolist()
print(np.sum(vals) * 5 / 3600 / 1000)