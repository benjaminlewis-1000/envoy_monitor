#!/usr/bin/env python3
"""
Enphase API v4 - Fetch Daily Production and Export Data

This script authenticates with the Enphase API v4 using OAuth 2.0
and fetches daily production and export (grid feed-in) data.

Setup:
1. Register at https://developer-v4.enphase.com/
2. Create an application and get your API Key, Client ID, and Client Secret
3. Update the configuration variables below
4. Install required package: pip install requests
"""

import requests
import json
from datetime import datetime, timedelta
import numpy as np
from typing import Dict, Optional
import base64
import os
from pathlib import Path
from dotenv import load_dotenv

file_path = os.path.realpath(__file__)
this_dir = os.path.dirname(file_path)
parent_dir = os.path.dirname(os.path.dirname(file_path))
dotenv = os.path.join(parent_dir, '.env')
load_dotenv(dotenv)

API_KEY = os.environ['ENPHASE_API_KEY']
CLIENT_ID = os.environ['ENPHASE_CLIENT_ID']
CLIENT_SECRET = os.environ['ENPHASE_CLIENT_SECRET']
REDIRECT_URI = os.environ['ENPHASE_REDIRECT_URI']
ENPHASE_EMAIL = os.environ['ENPHASE_EMAIL']
ENPHASE_PASSWORD = os.environ['ENPHASE_PASSWORD']

# Your system ID (you can get this after first authentication)
SYSTEM_ID = None  # Will be fetched automatically if None

# Token storage file location
TOKEN_FILE = os.path.join(this_dir, "enphase_tokens.json")


# ============================================================================
# API ENDPOINTS
# ============================================================================
BASE_URL = "https://api.enphaseenergy.com"
TOKEN_URL = f"{BASE_URL}/oauth/token"
SYSTEMS_URL = f"{BASE_URL}/api/v4/systems"


class EnphaseAPI:
    """Enphase API v4 Client"""
    
    def __init__(self, api_key: str, client_id: str, client_secret: str, token_file: str = TOKEN_FILE):
        self.api_key = api_key
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self.access_token = None
        self.refresh_token = None
        self.system_id = None
        
        # Try to load saved tokens
        self.load_tokens()
    
    def save_tokens(self):
        """Save tokens to file for reuse"""
        token_data = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "system_id": self.system_id,
            "saved_at": datetime.now().isoformat()
        }
        
        try:
            with open(self.token_file, 'w') as f:
                json.dump(token_data, f, indent=2)
            print(f"✅ Tokens saved to {self.token_file}")
        except Exception as e:
            print(f"⚠️  Could not save tokens: {e}")
    
    def load_tokens(self):
        """Load tokens from file if they exist"""
        if not os.path.exists(self.token_file):
            return False
        
        try:
            with open(self.token_file, 'r') as f:
                token_data = json.load(f)
            
            self.access_token = token_data.get("access_token")
            self.refresh_token = token_data.get("refresh_token")
            self.system_id = token_data.get("system_id")
            
            saved_at = token_data.get("saved_at", "unknown")
            # print(f"✅ Loaded tokens from {self.token_file} (saved at {saved_at})")
            return True
            
        except Exception as e:
            print(f"⚠️  Could not load tokens: {e}")
            return False
    
    def clear_tokens(self):
        """Delete saved token file"""
        if os.path.exists(self.token_file):
            os.remove(self.token_file)
            print(f"🗑️  Deleted {self.token_file}")
        
    def get_authorization_code(self) -> Optional[str]:
        """
        Step 1: Get authorization code (simplified flow for personal use)
        Note: For production apps, use the proper OAuth redirect flow
        """
        print("\n🔐 FIRST TIME SETUP - Authorization Required")
        print("="*60)
        print("\nTo authorize this application:")
        print(f"1. Visit this URL in your browser:\n")
        print(f"   {BASE_URL}/oauth/authorize?response_type=code&client_id={self.client_id}&redirect_uri={REDIRECT_URI}\n")
        print("2. Login with your Enphase account credentials")
        print("3. After authorization, you'll be redirected to a URL")
        print("4. Copy the 'code' parameter from that redirect URL")
        print("   (Example: https://...?code=ABC123...)")
        
        auth_code = input("\nPaste the authorization code here: ").strip()
        return auth_code if auth_code else None
    
    def get_access_token(self, authorization_code: str) -> bool:
        """
        Step 2: Exchange authorization code for access token
        """
        print("\n🔐 Requesting access token...")
        
        # Create Basic Auth header
        credentials = f"{self.client_id}:{self.client_secret}"
        b64_credentials = base64.b64encode(credentials.encode()).decode()
        
        headers = {
            "Authorization": f"Basic {b64_credentials}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        data = {
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": authorization_code
        }
        
        try:
            response = requests.post(TOKEN_URL, headers=headers, data=data)
            response.raise_for_status()
            
            token_data = response.json()
            self.access_token = token_data.get("access_token")
            self.refresh_token = token_data.get("refresh_token")
            
            print("✅ Authentication successful!")
            print(f"Access token expires in: {token_data.get('expires_in', 'N/A')} seconds")
            
            # Save tokens for future use
            self.save_tokens()
            
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Authentication failed: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return False
    
    def refresh_access_token(self) -> bool:
        """
        Refresh the access token using refresh token
        """
        if not self.refresh_token:
            print("❌ No refresh token available")
            return False
        
        print("\n🔄 Refreshing access token...")
        
        credentials = f"{self.client_id}:{self.client_secret}"
        b64_credentials = base64.b64encode(credentials.encode()).decode()
        
        headers = {
            "Authorization": f"Basic {b64_credentials}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }
        
        try:
            response = requests.post(TOKEN_URL, headers=headers, data=data)
            response.raise_for_status()
            
            token_data = response.json()
            self.access_token = token_data.get("access_token")
            self.refresh_token = token_data.get("refresh_token")
            
            print("✅ Token refreshed successfully!")
            
            # Save updated tokens
            self.save_tokens()
            
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Token refresh failed: {e}")
            return False
    
    def get_systems(self) -> Optional[list]:
        """
        Get list of systems associated with the account
        """
        if not self.access_token:
            print("❌ Not authenticated")
            return None
        
        print("\n📡 Fetching systems...")
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "key": self.api_key
        }
        
        try:
            response = requests.get(SYSTEMS_URL, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            systems = data.get("systems", [])
            
            print(f"✅ Found {len(systems)} system(s)")
            
            for i, system in enumerate(systems, 1):
                print(f"\n  System {i}:")
                print(f"    ID: {system.get('system_id')}")
                print(f"    Name: {system.get('system_name')}")
                print(f"    Size: {system.get('system_size', 'N/A')} W")
                print(f"    Status: {system.get('status')}")
            
            # Save first system ID if not already set
            if systems and not self.system_id:
                self.system_id = systems[0].get('system_id')
                self.save_tokens()
            
            return systems
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to fetch systems: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return None
    
    def ensure_authenticated(self) -> bool:
        """
        Ensure we have a valid access token, refreshing if needed
        Returns True if authenticated, False otherwise
        """
        # If no access token at all, need to authenticate
        if not self.access_token:
            print("❌ No access token found. Please run initial authentication.")
            return False
        
        # Try to use the existing token first
        # We'll test it by trying to fetch systems
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "key": self.api_key
        }
        
        try:
            response = requests.get(SYSTEMS_URL, headers=headers)
            if response.status_code == 200:
                # Token is still valid
                return True
            elif response.status_code == 401:
                # Token expired, try to refresh
                print("⚠️  Access token expired")
                return self.refresh_access_token()
            else:
                response.raise_for_status()
        except requests.exceptions.RequestException as e:
            # If we have a refresh token, try to use it
            if self.refresh_token:
                return self.refresh_access_token()
            else:
                print(f"❌ Authentication check failed: {e}")
                return False
        
        return False




    def fetch_at_url(self, url: str, system_id: str, start_date: str) -> Optional[Dict]:
        """
        Get data from the API at the given URL. 

        Args:
            url: Enphase API URL
            system_id: System ID
            start_date: Start date in format 'YYYY-MM-DD'
        """


        if not self.access_token:
            print("❌ Not authenticated")
            return None
        
        print(f"\n📊 Fetching data for {start_date} at {url}...")
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "key": self.api_key
        }
        
        params = {
            "start_date": start_date,
            "granularity": "day"
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            data = response.json()
            return data
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to fetch data at: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return None
    
    def calculate_daily_export(self, production_wh: float, consumption_wh: float) -> float:
        """
        Calculate export (grid feed-in) from production and consumption
        Export = Production - Consumption (when Production > Consumption)
        """
        export = max(0, production_wh - consumption_wh)
        return export
    
    def get_daily_summary(self, system_id: str, date: str = None):
        """
        Get daily production and export summary
        
        Args:
            system_id: System ID
            date: Date in format 'YYYY-MM-DD' (defaults to yesterday)
        """
        datetime.strptime(date, "%Y-%m-%d")
        
        print(f"\n{'='*60}")
        print(f"  DAILY SUMMARY FOR {date}")
        print(f"{'='*60}")
        
        # Fetch microinverter production

        micro_production_url = f"{BASE_URL}/api/v4/systems/{system_id}/telemetry/production_micro"
        micro_data = self.fetch_at_url(micro_production_url, system_id, date)

        # Fetch production data
        production_url = f"{BASE_URL}/api/v4/systems/{system_id}/telemetry/production_meter"
        production_data = self.fetch_at_url(production_url, system_id, date)
        
        # Fetch consumption data
        consumption_url = f"{BASE_URL}/api/v4/systems/{system_id}/telemetry/consumption_meter"
        consumption_data = self.fetch_at_url(consumption_url, system_id, date)

        import_url = f"{BASE_URL}/api/v4/systems/{system_id}/energy_import_telemetry"
        import_data = self.fetch_at_url(import_url, system_id, date)

        total_import_wh = np.sum([a['wh_imported'] for a in import_data['intervals'][0]])
        total_micro_prod_wh = np.sum([a['enwh'] for a in micro_data['intervals']])
        total_consumption_wh = np.sum([a['enwh'] for a in consumption_data['intervals']])
        total_production_wh = np.sum([a['wh_del'] for a in production_data['intervals']])
        
        # Calculate self-consumption
        self_consumption_wh = total_consumption_wh - total_import_wh

        # Calculate export
        total_export_wh = total_production_wh - self_consumption_wh
        net_export_wh = total_consumption_wh - total_production_wh
        
        print(f"\n📈 Production:")
        print(f"   Total: {total_production_wh:,.0f} Wh ({total_production_wh/1000:.2f} kWh)")
        
        print(f"\n📈 Production (micros):")
        print(f"   Total: {total_micro_prod_wh:,.0f} Wh ({total_micro_prod_wh/1000:.2f} kWh)")
        
        print(f"\n📉 Consumption:")
        print(f"   Total: {total_consumption_wh:,.0f} Wh ({total_consumption_wh/1000:.2f} kWh)")
            
        print(f"\n⚡ Export to Grid:")
        print(f"   Total: {total_export_wh:,.0f} Wh ({total_export_wh/1000:.2f} kWh)")
        
        print(f"\n⚡ Net grid consumption:")
        print(f"   Total: {net_export_wh:,.0f} Wh ({net_export_wh/1000:.2f} kWh)")
        
        print(f"\n🔌 Import from Grid:")
        print(f"   Total: {total_import_wh:,.0f} Wh ({total_import_wh/1000:.2f} kWh)")

        print(f"\n🏠 Self Consumption:")
        print(f"   Total: {self_consumption_wh:,.0f} Wh ({self_consumption_wh/1000:.2f} kWh)")
        
        if total_production_wh > 0:
            self_consumption_pct = (self_consumption_wh / total_production_wh) * 100
            print(f"   Percentage: {self_consumption_pct:.1f}%")

        

        enphase_data = {
            'wh_produced_meter': total_production_wh,
            'wh_produced_micros': total_micro_prod_wh,
            'wh_consumed_total': total_consumption_wh,
            'wh_exported': total_export_wh,
            'wh_net_consumption_from_grid': net_export_wh,
            'wh_grid_import': total_import_wh,
            'wh_self_consumed': self_consumption_wh,
        }
        
        print(f"\n{'='*60}\n")
        return enphase_data


if __name__ == "__main__":


    """
    Main function to demonstrate the API usage
    """
    print("=" * 60)
    print("  ENPHASE API v4 - PRODUCTION & EXPORT FETCHER")
    print("=" * 60)
    
    # Initialize API client (will automatically load saved tokens)
    api = EnphaseAPI(API_KEY, CLIENT_ID, CLIENT_SECRET)
    
    # Check if we need initial authentication
    if not api.access_token:
        print("\n🔐 First time setup required")
        
        # Step 1: Get authorization code
        auth_code = api.get_authorization_code()
        
        if not auth_code:
            print("❌ Authorization failed")
            exit()
        
        # Step 2: Get access token
        if not api.get_access_token(auth_code):
            exit()
        
        # Step 3: Get systems (will save system_id)
        systems = api.get_systems()
        
        if not systems:
            print("❌ No systems found")
            exit()
    else:
        print("\n✅ Using saved credentials")
        
        # Ensure token is still valid (will refresh if needed)
        if not api.ensure_authenticated():
            print("\n❌ Authentication failed. Please delete enphase_tokens.json and run again.")
            exit()
    
    # Use saved or configured system ID
    system_id = SYSTEM_ID if SYSTEM_ID else api.system_id
    
    if not system_id:
        print("❌ No system ID available")
        exit()
    
    # Step 4: Get daily summary for yesterday
    date_str = '2025-11-12'
    data_package = api.get_daily_summary(system_id, date_str)

    # api.get_telemetry_production(system_id, yesterday)
    # data = api.get_telemetry_import(system_id, yesterday)
    
    # Optional: Get summary for a specific date
    # api.get_daily_summary(system_id, "2025-11-20")
    
    print("\n💡 TIPS:")
    print("   • Tokens are saved in enphase_tokens.json")
    print("   • The script will automatically refresh expired tokens")
    print("   • Delete enphase_tokens.json to re-authenticate from scratch")


    # api = EnphaseAPI(API_KEY, CLIENT_ID, CLIENT_SECRET)
    # system_id = SYSTEM_ID if SYSTEM_ID else api.system_id
    # yesterday = '2025-11-08'
    # micro_production = f"{BASE_URL}/api/v4/systems/{system_id}/telemetry/production_micro"
    # data = api.fetch_at_url(micro_production, system_id, yesterday)
    # wh = [a['enwh'] for a in data['intervals']]

"""

    def get_telemetry_consumption(self, system_id: str, start_date: str) -> Optional[Dict]:
        
        if not self.access_token:
            print("❌ Not authenticated")
            return None
        
        print(f"\n📊 Fetching consumption data for {start_date}...")
        
        url = f"{BASE_URL}/api/v4/systems/{system_id}/telemetry/consumption_meter"
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "key": self.api_key
        }
        
        params = {
            "start_date": start_date,
            "granularity": "day"
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            data = response.json()
            return data
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to fetch consumption data: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return None


    def get_telemetry_production(self, system_id: str, start_date: str) -> Optional[Dict]:
        ""
        Get production telemetry data
        
        Args:
            system_id: System ID
            start_date: Start date in format 'YYYY-MM-DD'
        "
        if not self.access_token:
            print("❌ Not authenticated")
            return None
        
        print(f"\n📊 Fetching production data for {start_date}...")
        
        url = f"{BASE_URL}/api/v4/systems/{system_id}/telemetry/production_meter"
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "key": self.api_key
        }
        
        params = {
            "start_date": start_date,
            "granularity": "day"  # Options: day, week, lifetime
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            data = response.json()
            return data
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to fetch production data: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return None
    


    def get_telemetry_import(self, system_id: str, start_date: str) -> Optional[Dict]:
        ""
        Get consumption telemetry data (needed to calculate export)
        
        Args:
            system_id: System ID
            start_date: Start date in format 'YYYY-MM-DD'
        "
        if not self.access_token:
            print("❌ Not authenticated")
            return None
        
        print(f"\n📊 Fetching import data for {start_date}...")
        
        url = f"{BASE_URL}/api/v4/systems/{system_id}/energy_import_telemetry"
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "key": self.api_key
        }
        
        params = {
            "start_date": start_date,
            "granularity": "day"
        }
        
        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            data = response.json()
            return data
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to fetch consumption data: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return None
"""