"""
test_upstox_connectivity.py -- Upstox API v2 Live Diagnostic & Connectivity Test
==============================================================================
Tests and verifies from the current host (local machine or Oracle cloud server):
1. Environment variables & credential formats (.env).
2. Outgoing Public IP (useful for verifying Upstox developer console static IP whitelist).
3. Upstox OAuth2 profile API authentication (User ID, Broker, Account status).
4. Upstox Historical Candle API fetch (NSE_EQ equities).
5. Fallback data provider sanity (yfinance).

Usage:
    python scripts/test_upstox_connectivity.py
"""

import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

# Ensure src is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass

from nse_quant_lab.data_loader import auto_refresh_upstox_token, fetch_from_upstox

HEADERS_BASE = {
    "Accept": "application/json",
    "Api-Version": "2.0",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def mask_string(s: str) -> str:
    if not s:
        return "[NOT SET]"
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def get_public_ip() -> str:
    """Attempts to retrieve the host's outgoing public IP address."""
    services = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com"
    ]
    for url in services:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8").strip()
        except Exception:
            continue
    return "[Could not determine public IP]"


def test_upstox():
    print("=" * 75)
    print("        NSE QUANT LAB - UPSTOX API CONNECTIVITY TEST")
    print("=" * 75)
    print("Timestamp (Local): " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    # 1. Check Public IP
    public_ip = get_public_ip()
    print("Outgoing Public IP: " + public_ip)
    print("-" * 75)

    # 2. Inspect Credentials in .env
    api_key = os.environ.get("UPSTOX_API_KEY") or os.environ.get("UPSTOX_CLIENT_ID", "")
    api_secret = os.environ.get("UPSTOX_API_SECRET", "")
    refresh_token = os.environ.get("UPSTOX_REFRESH_TOKEN", "")
    access_token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")

    print("1. CREDENTIALS INSPECTION:")
    print(f"   * UPSTOX_API_KEY:       {mask_string(api_key)} (len: {len(api_key)})")
    print(f"   * UPSTOX_API_SECRET:    {mask_string(api_secret)} (len: {len(api_secret)})")
    print(f"   * UPSTOX_REFRESH_TOKEN: {mask_string(refresh_token)} (len: {len(refresh_token)})")
    print(f"   * UPSTOX_ACCESS_TOKEN:  {mask_string(access_token)} (len: {len(access_token)})")
    
    if not access_token and not refresh_token:
        print("")
        print("FAIL: Neither UPSTOX_ACCESS_TOKEN nor UPSTOX_REFRESH_TOKEN is set in .env.")
        print("Please generate a token or set UPSTOX_ACCESS_TOKEN.")
        sys.exit(1)

    # 3. Token Auto-Refresh Check (if refresh token exists)
    print("")
    print("2. TESTING AUTO-REFRESH GRANT:")
    if refresh_token:
        try:
            new_token = auto_refresh_upstox_token()
            if new_token:
                access_token = new_token
                print(f"   SUCCESS: Auto-refresh succeeded. Active token: {mask_string(access_token)}")
            else:
                print("   WARNING: Auto-refresh returned None. Using existing UPSTOX_ACCESS_TOKEN.")
        except Exception as e:
            print(f"   WARNING: Auto-refresh exception: {e}")
    else:
        print("   INFO: UPSTOX_REFRESH_TOKEN not configured. Relying on UPSTOX_ACCESS_TOKEN.")

    # 4. User Profile API Verification
    print("")
    print("3. TESTING UPSTOX USER PROFILE API (/v2/user/profile):")
    profile_url = "https://api.upstox.com/v2/user/profile"
    req_headers = dict(HEADERS_BASE)
    req_headers["Authorization"] = f"Bearer {access_token}"

    profile_passed = False
    try:
        req = urllib.request.Request(profile_url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                p_data = body.get("data", {})
                print("   HTTP 200 OK - Authentication Successful!")
                print(f"   * User ID:       {p_data.get('user_id')}")
                print(f"   * User Name:     {p_data.get('user_name')}")
                print(f"   * Broker:        {p_data.get('broker')}")
                print(f"   * User Type:     {p_data.get('user_type')}")
                print(f"   * Exchanges:     {p_data.get('exchanges')}")
                profile_passed = True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"   HTTP Error {e.code}: {e.reason}")
        try:
            err_json = json.loads(body)
            err_list = err_json.get("errors", [])
            for err in err_list:
                print(f"      Code: {err.get('errorCode')} - {err.get('message')}")
            
            # Specific hint for Static IP Whitelisting
            if any(err.get("errorCode") == "UDAPI1221" for err in err_list):
                print("")
                print("   IP WHITELIST HINT (UDAPI1221):")
                print(f"      Your request came from IP: {public_ip}")
                print("      Upstox developer account requires this exact IP to be whitelisted.")
                print("      Note: This error is EXPECTED on local machines if your Upstox App")
                print("      is bound to your Oracle server's static IP.")
        except Exception:
            print(f"      Raw Response: {body[:300]}")
    except Exception as e:
        print(f"   Exception connecting to profile API: {e}")

    # 5. Historical Candle API Verification
    print("")
    print("4. TESTING HISTORICAL CANDLE API (/v2/historical-candle):")
    test_sym = "BSE.NS"
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    
    print(f"   Fetching candles for {test_sym} ({start_date} to {end_date})...")
    df_candle = fetch_from_upstox(test_sym, start_date, end_date, access_token=access_token)
    
    candle_passed = False
    if df_candle is not None and not df_candle.empty:
        latest = df_candle.iloc[-1]
        print(f"   SUCCESS: Fetched {len(df_candle)} daily candles via Upstox API!")
        print(f"   * Latest Candle Date:  {latest['date'].strftime('%Y-%m-%d')}")
        print(f"   * Open:  INR {latest['open']:.2f}")
        print(f"   * High:  INR {latest['high']:.2f}")
        print(f"   * Low:   INR {latest['low']:.2f}")
        print(f"   * Close: INR {latest['close']:.2f}")
        print(f"   * Volume: {int(latest['volume']):,}")
        candle_passed = True
    else:
        print("   Could not fetch candle via Upstox API directly (may be blocked by IP or invalid token).")

    # 6. Fallback Ingestion Provider Check
    print("")
    print("5. TESTING SECONDARY DATA FALLBACK (yfinance):")
    fallback_passed = False
    try:
        import yfinance as yf
        yf_df = yf.download("RELIANCE.NS", period="5d", progress=False, timeout=8)
        if not yf_df.empty:
            print(f"   SUCCESS: Fallback provider active (fetched {len(yf_df)} rows for RELIANCE.NS).")
            fallback_passed = True
        else:
            print("   WARNING: Fallback provider returned empty DataFrame.")
    except Exception as e:
        print(f"   WARNING: Fallback provider exception: {e}")

    # 7. Summary
    print("")
    print("=" * 75)
    print("                    UPSTOX CONNECTIVITY SUMMARY")
    print("=" * 75)
    print(f"  * Upstox Profile Authentication: {'PASSED' if profile_passed else 'FAILED'}")
    print(f"  * Upstox Daily Candle API:       {'PASSED' if candle_passed else 'FAILED'}")
    print(f"  * Secondary Ingestion Fallback:  {'PASSED' if fallback_passed else 'FAILED'}")
    print("=" * 75)

    if profile_passed and candle_passed:
        print("\nALL UPSTOX CHECKS PASSED. SYSTEM IS READY FOR PRODUCTION INGESTION.")
        sys.exit(0)
    elif profile_passed and not candle_passed:
        print("\nProfile authenticated, but candle fetch failed.")
        sys.exit(1)
    else:
        # On local machines where IP is not whitelisted, return 0 if fallback works, but warn clearly
        print("\nNote: If running on local machine and IP is not whitelisted, run this script")
        print("directly on your Oracle Cloud server where your static IP is configured.")
        sys.exit(0 if fallback_passed else 1)


if __name__ == "__main__":
    test_upstox()
