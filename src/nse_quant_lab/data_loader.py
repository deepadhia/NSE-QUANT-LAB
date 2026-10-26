"""
data_loader.py -- Data Ingestion with Hybrid MongoDB, Upstox API v2 & Parquet Fallback
"""

from __future__ import annotations
import os
import time
import json
import logging
from typing import Dict, Optional, List, Any
import urllib.request
import urllib.parse
import pandas as pd
import numpy as np

logger = logging.getLogger("data_loader")



def auto_refresh_upstox_token() -> Optional[str]:
    """
    Automatically refreshes Upstox access token using refresh_token grant type,
    matching Alpha-Pulse's autonomous token renewal mechanism.
    Does NOT require manual daily browser OTP verification.
    """
    refresh_token = os.environ.get("UPSTOX_REFRESH_TOKEN")
    client_id = os.environ.get("UPSTOX_API_KEY") or os.environ.get("UPSTOX_CLIENT_ID")
    client_secret = os.environ.get("UPSTOX_API_SECRET") or os.environ.get("UPSTOX_CLIENT_SECRET")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI", "https://127.0.0.1")

    if not refresh_token or not client_id or not client_secret:
        return os.environ.get("UPSTOX_ACCESS_TOKEN")

    url = "https://api.upstox.com/v2/login/authorization/token"
    headers = {
        "Accept": "application/json",
        "Api-Version": "2.0",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                new_token = body.get("access_token")
                if new_token:
                    os.environ["UPSTOX_ACCESS_TOKEN"] = new_token
                    logger.info("Successfully refreshed Upstox access token via refresh_token grant.")
                    return new_token
    except Exception as e:
        logger.warning(f"Could not auto-refresh Upstox token: {e}. Falling back to UPSTOX_ACCESS_TOKEN.")

    return os.environ.get("UPSTOX_ACCESS_TOKEN")


def fetch_from_upstox(
    symbol: str,
    start_date: str,
    end_date: str,
    access_token: Optional[str] = None,
    api_base: str = "https://api.upstox.com/v2",
) -> Optional[pd.DataFrame]:
    """
    Fetches historical daily candles directly from Upstox API v2 (matching Alpha-Pulse pipeline).
    - Enforces 100ms rate limiting delay
    - Handles HTTP 429 Too Many Requests backoff
    - Returns standardized DataFrame ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume']
    """
    token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        logger.warning("Upstox access token not found in environment (UPSTOX_ACCESS_TOKEN).")
        return None

    clean_sym = symbol.replace(".NS", "").replace(".BO", "")
    if clean_sym.startswith("INE"):
        instrument_key = f"NSE_EQ|{clean_sym}"
    else:
        # Check if ISIN is available from local database mapping
        instrument_key = f"NSE_EQ|{clean_sym}"
        try:
            from pymongo import MongoClient
            mongo_u = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
            m_db = os.environ.get("MONGO_DB", "stock_scanner")
            cli = MongoClient(mongo_u, serverSelectionTimeoutMS=500)
            sym_doc = cli[m_db]["symbols"].find_one({"symbol": {"$in": [symbol, f"{clean_sym}.NS", clean_sym]}})
            if sym_doc and sym_doc.get("isin"):
                instrument_key = f"NSE_EQ|{sym_doc['isin']}"
        except Exception:
            pass

    headers = {
        "Accept": "application/json",
        "Api-Version": "2.0",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    url = f"{api_base}/historical-candle/{urllib.parse.quote(instrument_key)}/day/{end_date}/{start_date}"

    # Global rate limit delay: 100ms
    time.sleep(0.10)

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                candles = body.get("data", {}).get("candles", [])
                if not candles:
                    return None

                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
                df["date"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
                df["symbol"] = symbol
                df = df[["symbol", "date", "open", "high", "low", "close", "volume"]]
                df = df.sort_values("date").reset_index(drop=True)
                return df
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.warning(f"Upstox API rate limited (429) for {symbol}. Waiting 1s and retrying...")
            time.sleep(1.0)
            return fetch_from_upstox(symbol, start_date, end_date, token, api_base)
        logger.warning(f"Upstox API HTTP error {e.code} for {symbol}: {e.reason}")
    except Exception as e:
        logger.warning(f"Upstox API exception for {symbol}: {e}")

    return None


def load_clean_universe(
    start_date: str = "2017-01-01",
    end_date: str = "2026-10-24",
    mongo_uri: Optional[str] = None,
    db_name: str = "stock_scanner",
    collection_name: str = "upstox_daily_ohlcv",
    parquet_path: Optional[str] = None,
    active_only: bool = False,
) -> Dict[str, pd.DataFrame]:
    """
    Loads daily OHLCV dataset.
    Tries MongoDB first; if unavailable, seamlessly falls back to local sample parquet.
    """
    uri = mongo_uri or os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
    db_n = os.environ.get("MONGO_DB", db_name)
    coll_n = os.environ.get("MONGO_COLLECTION", collection_name)

    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=15000)
        client.admin.command("ping")
        col = client[db_n][coll_n]

        query_filter: Dict[str, Any] = {
            "date": {"$gte": start_date, "$lte": end_date},
            "symbol": {"$nin": ["^NSEI", "NIFTY 50", "^CRSLDX", "NIFTY 500"]},
            "open": {"$gt": 0},
            "high": {"$gt": 0},
            "low": {"$gt": 0},
            "close": {"$gt": 0},
            "volume": {"$gte": 0},
        }
        if active_only:
            try:
                active_syms = [
                    d["symbol"] for d in client[db_n]["symbols"].find({"is_active": True}, {"symbol": 1})
                ]
                if active_syms:
                    query_filter["symbol"] = {"$in": active_syms}
            except Exception as e:
                logger.warning(f"Failed to query active symbols for filter: {e}")

        cursor = col.find(
            query_filter,
            {"_id": 0, "symbol": 1, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        )
        raw_df = pd.DataFrame(list(cursor))
        if not raw_df.empty:
            raw_df = raw_df[
                (raw_df["high"] >= raw_df["low"]) &
                (raw_df["high"] >= raw_df["open"]) &
                (raw_df["high"] >= raw_df["close"]) &
                (raw_df["low"] <= raw_df["open"]) &
                (raw_df["low"] <= raw_df["close"])
            ]
            raw_df["date"] = pd.to_datetime(raw_df["date"])
            universe: Dict[str, pd.DataFrame] = {}
            for sym, group in raw_df.groupby("symbol"):
                universe[str(sym)] = group.sort_values("date").reset_index(drop=True)
            return universe
    except Exception:
        pass

    # Offline Parquet Fallback
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    pq_path = parquet_path or os.path.join(base_dir, "data", "sample", "nifty500_sample.parquet")
    if os.path.exists(pq_path):
        df_pq = pd.read_parquet(pq_path)
        df_pq["date"] = pd.to_datetime(df_pq["date"])
        s_dt = pd.to_datetime(start_date)
        e_dt = pd.to_datetime(end_date)
        df_pq = df_pq[(df_pq["date"] >= s_dt) & (df_pq["date"] <= e_dt)]
        df_pq = df_pq[~df_pq["symbol"].isin(["^NSEI", "^CRSLDX", "NIFTY 50", "NIFTY 500"])]

        universe = {}
        for sym, g in df_pq.groupby("symbol"):
            universe[str(sym)] = g.sort_values("date").reset_index(drop=True)
        return universe

    raise ConnectionError("Failed to load market data: MongoDB unavailable and sample parquet not found.")


def make_synthetic_stock(
    symbol: str,
    n_days: int = 300,
    start_price: float = 100.0,
    daily_ret: float = 0.001,
    start_date: str = "2018-01-01",
    volume: float = 200_000.0,
) -> pd.DataFrame:
    """Generates synthetic stock data for deterministic unit testing."""
    dates = pd.date_range(start_date, periods=n_days, freq="B")
    prices = [start_price]
    for _ in range(1, n_days):
        prices.append(prices[-1] * (1.0 + daily_ret))

    return pd.DataFrame({
        "symbol": symbol,
        "date": dates,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [volume] * n_days,
    })
