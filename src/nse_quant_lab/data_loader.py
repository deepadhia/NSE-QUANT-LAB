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
    allow_sample_fallback: bool = False,
) -> Dict[str, pd.DataFrame]:
    """
    Loads daily OHLCV dataset from MongoDB.
    Fails loudly (ConnectionError) if MongoDB is unreachable, the query errors, or no
    candles are returned. The local sample parquet is used only when the caller
    explicitly opts in with allow_sample_fallback=True, and that switch is logged.
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
        if raw_df.empty:
            raise ValueError(f"no candles returned from {db_n}.{coll_n} for {start_date} to {end_date}")
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
    except Exception as exc:
        # Never include the URI in the message: it may carry credentials.
        msg = f"Failed to load market data from MongoDB ({db_n}.{coll_n}): {type(exc).__name__}: {exc}"
        if not allow_sample_fallback:
            logger.error(msg)
            raise ConnectionError(msg) from exc
        logger.warning("%s -- SAMPLE DATA MODE: falling back to local sample parquet (NOT the full market universe).", msg)

    # Explicit opt-in sample parquet fallback
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
        logger.warning("SAMPLE DATA MODE: loaded %d symbols from %s", len(universe), pq_path)
        return universe

    raise ConnectionError(f"Failed to load market data: MongoDB unavailable and sample parquet not found at {pq_path}.")


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


def _mongo_live_source(uri: str = "mongodb://localhost:27017/", db_name: str = "stock_scanner", collection: str = "upstox_fulltier_daily"):
    """Default live-bar reader: bars written by scripts/ingest_upstox_fulltier.py."""
    def read(from_date: str, to_date: str) -> pd.DataFrame:
        from pymongo import MongoClient

        col = MongoClient(uri, serverSelectionTimeoutMS=5000)[db_name][collection]
        docs = list(col.find({"date": {"$gte": from_date, "$lte": to_date}}, {"_id": 0, "symbol": 1, "date": 1, "open": 1, "high": 1,
                                                                              "low": 1, "close": 1, "volume": 1}))
        return pd.DataFrame(docs, columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    return read


def load_pit_universe(
    end_date: Optional[str] = None,
    panel_path: Optional[str] = None,
    min_adt_ever: float = 1.5e6,
    min_rows: int = 226,
    include_live: bool = True,
    live_source=None,
    live_coverage_gate: float = 0.90,
) -> Dict[str, pd.DataFrame]:
    """
    Full-market, split-adjusted daily universe for the researched strategy (trend_momentum_pit).
    History: data/external/nse_archive/clean_panel.parquet (rebuilt by research/audit_2026-10/scripts/build_clean_panel.py from the NSE
    bhavcopy archive: all series, delisted names included, ETFs removed, official split/bonus factors applied).
    Live segment: for sessions AFTER the panel's last date, bars from Mongo `upstox_fulltier_daily` (scripts/ingest_upstox_fulltier.py) are appended
    with turnover = close x volume and the symbol's last known series. Fails loudly (ValueError) if a required session is missing for
    more than (1 - live_coverage_gate) of the liquid tier, instead of trading on stale data. When the live bars carry the panel's last date
    (anchor bar) and its close differs from the panel's by more than 5%, the panel history is re-scaled to Upstox's (split-adjusted) basis.
    Columns per symbol: date, open, high, low, close, volume, turnover (Rs), series.
    """
    import datetime as _dt

    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    path = panel_path or os.path.join(base, "data", "external", "nse_archive", "clean_panel.parquet")
    if not os.path.exists(path):
        raise FileNotFoundError(f"clean panel not found at {path}; run research/audit_2026-10/scripts/build_clean_panel.py")
    panel = pd.read_parquet(path)
    panel["series"] = panel["series"].astype(str)
    panel["date"] = pd.to_datetime(panel["date"])
    panel_last = panel["date"].max()
    end = pd.to_datetime(end_date) if end_date is not None else None
    need_live = include_live and (end is None or end > panel_last)
    if end is not None and end > panel_last and not include_live:
        raise ValueError(f"clean panel ends {panel_last.date()} but {end.date()} was requested: ingest the bhavcopy and rebuild the panel first")
    if end is not None:
        panel = panel[panel["date"] <= end]
    universe: Dict[str, pd.DataFrame] = {}
    liquid: set = set()
    for sym, g in panel.groupby("symbol", sort=False):
        if len(g) < min_rows:
            continue
        g = g.sort_values("date").reset_index(drop=True)
        med = g["turnover"].rolling(50, min_periods=50).median()
        if not (med >= min_adt_ever).any():
            continue
        universe[str(sym)] = g
        if g["date"].iloc[-1] == panel_last and med.iloc[-1] >= 1.0e8 and g["series"].iloc[-1] == "EQ":
            liquid.add(str(sym))

    if need_live:
        src = live_source or _mongo_live_source()
        live = src(panel_last.strftime("%Y-%m-%d"), (end or pd.Timestamp("2100-01-01")).strftime("%Y-%m-%d"))
        live["date"] = pd.to_datetime(live["date"])
        new = live[live["date"] > panel_last]
        anchors = live[live["date"] == panel_last].set_index("symbol")["close"].to_dict()
        # session gate: every NSE trading day after the panel (up to the requested date, or up to yesterday) needs near-full coverage
        from .holidays import is_trading_day

        last_needed = end.date() if end is not None else (_dt.datetime.now().date() - _dt.timedelta(days=1))
        day, expected = panel_last.date() + _dt.timedelta(days=1), []
        while day <= last_needed:
            if is_trading_day(day):
                expected.append(day)
            day += _dt.timedelta(days=1)
        have = new[new["symbol"].isin(liquid)].groupby(new["date"].dt.date)["symbol"].nunique() if len(new) else pd.Series(dtype=int)
        short = [str(d) for d in expected if liquid and have.get(d, 0) < live_coverage_gate * len(liquid)]
        if short:
            raise ValueError(f"live data incomplete for sessions {short}: Upstox bars cover < {live_coverage_gate:.0%} of the {len(liquid)} liquid-tier "
                             f"names. Run scripts/ingest_upstox_fulltier.py (and check its coverage) before trading.")
        by_sym = {k: v for k, v in new.groupby("symbol")}
        rescaled = 0
        for sym, g in list(universe.items()):
            ratio = None
            if sym in anchors and g["date"].iloc[-1] == panel_last and g["close"].iloc[-1] > 0:
                r = float(anchors[sym]) / float(g["close"].iloc[-1])
                if abs(r - 1.0) > 0.05:
                    ratio = r
            if ratio is not None:
                for col in ("open", "high", "low", "close"):
                    g[col] = g[col] * ratio
                g["volume"] = g["volume"] / ratio
                rescaled += 1
            add = by_sym.get(sym)
            if add is not None and len(add):
                add = add.sort_values("date")[["date", "open", "high", "low", "close", "volume"]].copy()
                add["turnover"] = add["close"] * add["volume"]
                add["series"] = g["series"].iloc[-1]
                add.insert(0, "symbol", sym)
                g = pd.concat([g, add[g.columns.intersection(add.columns)]], ignore_index=True)[list(g.columns)]
            universe[sym] = g
        logger.info("PIT universe: live bars appended through %s; %d histories re-scaled to Upstox's adjusted basis",
                    live["date"].max().date() if len(live) else panel_last.date(), rescaled)
    logger.info("PIT universe: %d symbols, last bar %s", len(universe), max(g["date"].iloc[-1] for g in universe.values()).date())
    return universe
