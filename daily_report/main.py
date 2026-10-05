#! /usr/bin/env python

from dotenv import load_dotenv
import sys
import os
import influxdb_client
from influxdb_client import WritePrecision, InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import pytz
import time
from datetime import datetime, timezone, timedelta, date
from enphase_api_daily import EnphaseAPI


class DailyWriter(object):
    """docstring for connectDB"""
    def __init__(self, bucket_name: str):
        super(DailyWriter, self).__init__()

        self.bucket_name = bucket_name
        self.totals_point_name = "daily_totals"

        file_path = os.path.realpath(__file__)
        parent_dir = os.path.dirname(os.path.dirname(file_path))
        dotenv = os.path.join(parent_dir, '.env')

        load_dotenv(dotenv)

        DB_USER=os.environ['DB_USER']
        self.ORG=os.environ['ORG']
        URL=os.environ['URL']
        ADMIN_TOKEN=os.environ['ADMIN_TOKEN']
        self.TZ=os.environ["TZ"]

        self.client = influxdb_client.InfluxDBClient(
           url=URL,
           token=ADMIN_TOKEN,
           org=self.ORG
        )


        buckets_api = self.client.buckets_api()
        # Find the bucket by name
        bucket = buckets_api.find_bucket_by_name(bucket_name)

        if not bucket:
            raise ValueError(f"Bucket '{bucket_name}' does not exist.")

        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.query_api = self.client.query_api()
        self._enphase_login() # Set up self.enphase_api
 
    def _enphase_login(self):
        #######################
        # Set up Enphase API
        #######################

        ENPHASE_API_KEY = os.environ['ENPHASE_API_KEY']
        ENPHASE_CLIENT_ID = os.environ['ENPHASE_CLIENT_ID']
        ENPHASE_CLIENT_SECRET = os.environ['ENPHASE_CLIENT_SECRET']
        ENPHASE_REDIRECT_URI = os.environ['ENPHASE_REDIRECT_URI']
        ENPHASE_EMAIL = os.environ['ENPHASE_EMAIL']
        ENPHASE_PASSWORD = os.environ['ENPHASE_PASSWORD']

        # Initialize API client (will automatically load saved tokens)
        self.enphase_api = EnphaseAPI(ENPHASE_API_KEY, ENPHASE_CLIENT_ID, ENPHASE_CLIENT_SECRET)
        
        # Check if we need initial authentication
        if not self.enphase_api.access_token:
            print("\n🔐 First time setup required")
            
            # Step 1: Get authorization code
            auth_code = self.enphase_api.get_authorization_code()
            
            if not auth_code:
                raise RuntimeError("❌ Authorization failed")
            
            # Step 2: Get access token
            if not self.enphase_api.get_access_token(auth_code):
                raise RuntimeError("Access token not found")
            
            # Step 3: Get systems (will save system_id)
            systems = self.enphase_api.get_systems()
            
            if not systems:
                raise RuntimeError("❌ No systems found")
            
        # Ensure token is still valid (will refresh if needed)
        if not self.enphase_api.ensure_authenticated():
            raise RuntimeError("\n❌ Authentication failed. Please delete enphase_tokens.json and run again.")
        
        # Use saved or configured system ID
        self.system_id = self.enphase_api.system_id
        
        if not self.system_id:
            raise RuntimeError("❌ No system ID available")

    def get_last_point_date(self):

        flux_query_dataframe = f'''
        from(bucket: "{self.bucket_name}")
          |> range(start: 0)
          |> filter(fn: (r) => r["_measurement"] == "{self.totals_point_name}")
          |> last()
          |> keep(columns: ["_time"]) // Keep only the timestamp column

        '''

        last_data = self.query_api.query(flux_query_dataframe)

        latest_time = None
        for table in last_data:
            for record in table.records:
                latest_time = record["_time"]
                break # Assuming only one last record is expected

        if not latest_time:
            raise ValueError("No data found or no latest point.")

        tz = pytz.timezone(self.TZ)
        time_localized = latest_time.astimezone(tz)

        return time_localized

    def write_daily_consumption(self, data_dict: dict, date: str) -> None:

        datetime.strptime(date, "%Y-%m-%d")
        date_obj = datetime.strptime(f"{date} 23:59:59", "%Y-%m-%d %H:%M:%S")
        tz = pytz.timezone(self.TZ)
        # Localize the naive datetime object to the specified timezone
        date_obj = tz.localize(date_obj)

        assert 'wh_produced_meter' in data_dict.keys()
        assert 'wh_produced_micros' in data_dict.keys()
        assert 'wh_consumed_total' in data_dict.keys()
        assert 'wh_exported' in data_dict.keys()
        assert 'wh_net_consumption_from_grid' in data_dict.keys()
        assert 'wh_grid_import' in data_dict.keys()
        assert 'wh_self_consumed' in data_dict.keys()

        # Set the date

        point = (
            Point(f"{self.totals_point_name}")
            .tag("source", "enphase_api")
            .field("wh_produced_meter", data_dict["wh_produced_meter"])
            .field("wh_produced_micros", data_dict["wh_produced_micros"])
            .field("wh_consumed_total", data_dict["wh_consumed_total"])
            .field("wh_exported", data_dict["wh_exported"])
            .field("wh_net_consumption_from_grid", data_dict["wh_net_consumption_from_grid"])
            .field("wh_grid_import", data_dict["wh_grid_import"])
            .field("wh_self_consumed", data_dict["wh_self_consumed"])
            .time(date_obj, WritePrecision.NS)
        )

        self.write_api.write(bucket=self.bucket_name, org=self.ORG, record=point)

    def populate_enphase_data(self):

        today_date = date.today()

        # Combine today's date with a time of 00:00:00 to get the start of the day
        start_of_today = datetime(today_date.year, today_date.month, today_date.day, 0, 0, 0)
        tz = pytz.timezone(self.TZ)
        # Localize the naive datetime object to the specified timezone
        start_of_today = tz.localize(start_of_today)
        # Get the last date in the bucket for this data
        last_point_date = db_api.get_last_point_date()

        one_day = timedelta(days=1)

        next_date_required = last_point_date + one_day
        print(start_of_today, next_date_required, last_point_date)

        if start_of_today < next_date_required:
            print("Enphase daily data is caught up to today")

        while next_date_required < start_of_today:

            next_day_str = next_date_required.strftime("%Y-%m-%d")
            # print(today_date, next_day_str, start_of_today, last_point_date)

            data_package = self.enphase_api.get_daily_summary(self.system_id, next_day_str)
            self.write_daily_consumption(data_dict = data_package, date = next_day_str)

            next_date_required = next_date_required + one_day
            print("Next point, if not after now, is at ", next_date_required, "wrote for ", next_day_str)
            if next_date_required > start_of_today:
                return
            time.sleep(30)
            
        
if __name__ == "__main__":
    bucket = "computed_information"
    db_api = DailyWriter(bucket_name = bucket)
    db_api.populate_enphase_data()

    # data_dict = {
    #     'wh_produced_meter': 15077,
    #     'wh_produced_micros': 15113,
    #     'wh_consumed_total': 30661,
    #     'wh_exported': 6707,
    #     'wh_net_consumption_from_grid': 15584,
    #     'wh_grid_import': 22291,
    #     'wh_self_consumed': 8370,
    # }
    # date = '2025-11-12'

    # d1 = db_api.enphase_api.get_daily_summary(db_api.system_id, "2025-11-11")
    # print(d1)


    a0 = 'wh_produced_meter'
    a1 = 'wh_produced_micros'
    a2 = 'wh_consumed_total'
    a3 = 'wh_exported'
    a4 = 'wh_net_consumption_from_grid'
    a5 = 'wh_grid_import'
    a6 = 'wh_self_consumed'
    hist = [
        ["2025-11-06", {a0: 4187,   a1: 4187    , a2: 0      , a3: 1975    , a4: 0,      a5: 0,     a6: 2212}    ],
        ["2025-11-07", {a0: 2544,   a1: 2544    , a2: 0      , a3: 0       , a4: 0,      a5: 0,     a6: 2544}    ],
        ["2025-11-08", {a0: 17677,  a1: 17677   , a2: 0      , a3: 6321    , a4: 0,      a5: 0,     a6: 11356}    ],
        ["2025-11-09", {a0: 2007,   a1: 1828    , a2: 21779  , a3: 1       , a4: 19772,  a5: 19773, a6: 2006}    ],
        ["2025-11-10", {a0: 109,    a1: 96      , a2: 25219  , a3: 2       , a4: 25110,  a5: 25112, a6: 107}    ],
        ["2025-11-11", {a0: 270,    a1: 175     , a2: 23868  , a3: 3       , a4: 23598,  a5: 23601, a6: 267}    ],
        ["2025-11-12", {a0: 15077,  a1: 15113   , a2: 30661  , a3: 6707    , a4: 15584,  a5: 22291, a6: 8370}    ],
    ]
    # 134 exported on 2025-12-09

    # for row in hist:
    #     date, data_dict = row
    #     db_api.write_daily_consumption(data_dict, date)
