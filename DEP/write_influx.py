from datetime import datetime, timedelta, time
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
import pandas as pd
from zoneinfo import ZoneInfo
import os
from dotenv import load_dotenv

file_path = os.path.realpath(__file__)
parent_dir = os.path.dirname(os.path.dirname(file_path))
load_dotenv(os.path.join(parent_dir, '.env'))


class InfluxWeatherClient:
    def __init__(self, url, token, org, bucket):
        self.url = url
        self.token = token
        self.org = org
        self.bucket = bucket

        self.et = ZoneInfo("America/New_York")
        self.utc = ZoneInfo("UTC")

        self.client = InfluxDBClient(url=url, token=token, org=org)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

    # ---------------------------------------------------
    # Generate timestamp at 11 PM ET N days ago
    # ---------------------------------------------------
    def _ts_11pm_et(self, days_ago: int) -> datetime:
        now_et = datetime.now(self.et)
        ts = (now_et - timedelta(days=days_ago)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        return ts

    # ---------------------------------------------------
    # Write one weather datapoint
    # ---------------------------------------------------
    def write_weather_point(self, days_ago: int):
        dt_et = self._ts_11pm_et(days_ago)
        dt_utc = dt_et.astimezone(self.utc)

        point = (
            Point("weather_data")
            .tag("location", "home")
            .field("temperature", 20 + days_ago)
            .field("humidity", 40 + days_ago)
            .field("local_time_et", dt_et.isoformat())
            .time(dt_utc, WritePrecision.NS)
        )

        self.write_api.write(bucket=self.bucket, org=self.org, record=point)
        print(f"✓ Wrote {dt_et} ET → {dt_utc} UTC")

    # ---------------------------------------------------
    # Write last N days of 11PM ET weather points
    # ---------------------------------------------------
    def write_last_n_days(self, n: int = 5):
        for i in range(1, n + 1):
            self.write_weather_point(i)

    # ---------------------------------------------------
    # Query back into pandas DataFrame
    # ---------------------------------------------------
    def query_dataframe(self, days_back: int = 7) -> pd.DataFrame:
        query = f'''
        from(bucket: "{self.bucket}")
            |> range(start: -{days_back}d)
            |> filter(fn: (r) => r._measurement == "weather_data")
            |> pivot(rowKey:["time"], columnKey:["_field"], valueColumn:"_value")
            |> keep(columns: ["time", "location", "temperature", "humidity", "local_time_et"])
        '''

        tables = self.client.query_api().query_data_frame(query)
        df = pd.concat(tables) if isinstance(tables, list) else tables

        # convert UTC timestamps → ET for convenience
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df["time_et"] = df["time"].dt.tz_convert("America/New_York")

        return df


# ---------------------------------------------------
# Example usage
# ---------------------------------------------------
if __name__ == "__main__":


    # -----------------------------
    #  InfluxDB Config
    # -----------------------------
    token = os.environ['ADMIN_TOKEN']
    org = os.environ['ORG']
    bucket = "test"
    url = os.environ['URL']

    client = InfluxWeatherClient(
        url=url,
        token=token,
        org=org,
        bucket=bucket,
    )

    # write 5 days of 11pm ET data
    client.write_last_n_days(5)

    # read data back
    df = client.query_dataframe()
    print(df)
