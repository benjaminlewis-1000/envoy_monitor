from datetime import datetime, timedelta, time
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.client.delete_api import DeleteApi
import pandas as pd
from zoneinfo import ZoneInfo
import pytz
import calendar
import os
from dotenv import load_dotenv


file_path = os.path.realpath(__file__)
parent_dir = os.path.dirname(os.path.dirname(file_path))
dotenv = os.path.join(parent_dir, '.env')

load_dotenv(dotenv)

token = os.environ['ADMIN_TOKEN']
org = os.environ['ORG']
bucket = "test"
url = os.environ['URL']  # "http://influx.exploretheworld.tech"
TZ = os.environ['TZ']
et = ZoneInfo(TZ)
utc = ZoneInfo("UTC")

client = InfluxDBClient(url=url, token=token, org=org)
write_api = client.write_api(write_options=SYNCHRONOUS)

# delete_api = DeleteApi(client)
# # Define the time range for deletion (RFC3339 format)
# start_time = "2020-01-01T00:00:00Z"
# stop_time = "2025-10-31T23:59:59Z"

# # Optional: Define a predicate to filter specific points (e.g., by measurement or tag)
# # predicate = '_measurement="sensor_data" AND location="room_a"'
# predicate = None #_measurement="bill_data"' # To delete all points in the specified time range within the bucket
# delete_api.delete(start=start_time, stop=stop_time, predicate=predicate, bucket=bucket, org=org)
# exit()

data = [
[159.46, 9, 2025,   982, 74.04, 85.42],
[185.05, 8,  2025,     1319, 93.67, 91.38],
[223.18, 7,  2025,    1622, 110.79, 112.39],
[142.15, 6,  2025,     971, 74.89,  67.26],
[95.30,  5,  2025,     644, 50.66,  44.64],
[93.32,  4,  2025,     628, 49.80,  43.52],
[85.81,  3,  2025,   584, 45.36,    40.45],
[92.89,  2,  2025,     642, 48.42,  44.47],
[112.50, 1,  2025,     802, 56.92,  55.58],
[98.91,  12,  2024,     700, 50.43, 48.48],
[80.90,  11,  2024,    539, 43.56,  37.34],
[104.58, 10,  2024,     738, 53.45, 51.13],
[159.21, 9,  2024,   1207, 75.59,   83.62],
[111.78, 8,  2024,     810, 55.66,  56.12],
[172.59, 7,  2024,    1319, 81.21,  91.38],
[124.94, 6,  2024,     921, 61.13,  63.81],
[107.35, 5,  2024,     796, 52.20,  55.15],
[85.32,  4,  2024,     606, 43.34,  41.98],
[85.47,  3,  2024,   620, 42.52,    42.95],
[102.30, 12,  2023,     731, 51.64, 50.66],
[74.46,  11,  2023,    534, 37.45,  37.01],
[95.71,  10,  2023,     719, 45.88, 49.83],
[123.30, 9,  2023,   971, 56.01,    67.29],
[120.88, 8,  2023,     1242, 34.81, 86.07],
[79.43,  7,  2023,    781, 43.66,   35.77],
[87.65,  6,  2023,     883, 47.21,  40.44],
[55.29,  5,  2023,     530, 31.02,  24.27],
[60.20,  4,  2023,     585, 33.41,  26.79],
[69.02,  3,  2023,   689, 37.46,    31.56],
[70.79,  2,  2023,    709, 38.32,   32.47],
[69.09,  1,  2023,     690, 37.49,  31.60],
[68.06,  12,  2022,    698, 36.09,  31.97],
[56.40,  11,  2022,    562, 30.66,  25.74],
[59.72,  10,  2022,     600, 32.24, 27.48],
[70.75,  9,  2022,   724, 37.59,    33.16],
[114.30, 8,  2022,  1234, 57.78,    56.52],
[104.66, 7,  2022,     1121, 53.32, 51.34],
[91.39,  6,  2022,    962, 47.33,   44.06],
[72.27,  5,  2022,     734, 38.65,  33.62],
[61.36,  4,  2022,   610, 33.42,    27.94],
[73.88,  3,  2022,   757, 39.21,    34.67],
[73.43,  2,  2022,     757, 38.76,  34.67],
[62.97,  1,  2022,     637, 33.80,  29.17],
[81.44,  12,  2021,    849, 42.56,  38.88],
[67.86,  11,  2021,     689, 36.30, 31.56],
[67.01,  10,  2021,     685, 35.64, 31.37],
[100.12, 9,  2021,   1072, 51.02,   49.10],
[130.80, 8,  2021,     1362, 68.42, 62.38],
[90.17,  7,  2021,    909, 48.54,    41.639]
]

print(data)

for item in data:
    price, month, year, kwh, delivery, supply = item
    last_day_num = calendar.monthrange(year, month)[1]
    date = datetime(year, month, last_day_num, 20, 59, 0)
    tz = pytz.timezone(TZ)

    # Localize the naive datetime object to the specified timezone
    timezone_aware_dt = tz.localize(date)


    print(timezone_aware_dt, price, kwh)


    point = (
        Point("bill_data")
        .tag("bill_source", "historical")
        .tag("time_period", "pre_solar")
        .field("total_bill_cost", price)
        .field("kwh", kwh)
        .field("delivery", delivery)
        .field("supply", supply)
        .time(timezone_aware_dt, WritePrecision.NS)
    )

    write_api.write(bucket=bucket, org=org, record=point)