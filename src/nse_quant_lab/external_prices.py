"""
external_prices.py -- Secondary-Source Daily Prices for Stocks Missing from the Primary Feed
===========================================================================================
Used to fill survivorship gaps (delisted, merged, renamed index members) that Upstox no
longer serves. Data lands in its own collection ('external_daily_ohlcv') tagged with its
source, never mixed into 'upstox_daily_ohlcv'.

Adjustment basis: yfinance history(auto_adjust=False) OHLC is split/bonus-adjusted but not
dividend-adjusted -- verify against the primary feed with price_validation.compare_sources
before relying on it.
"""

from __future__ import annotations
import logging
from typing import Any, Callable, Dict, Optional, Tuple
import pandas as pd

logger = logging.getLogger("external_prices")

EXTERNAL_COLLECTION = "external_daily_ohlcv"
OUTPUT_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "source"]


def _default_yf_history(symbol: str, start: str, end_exclusive: str) -> pd.DataFrame:
    import yfinance as yf
    return yf.Ticker(symbol).history(start=start, end=end_exclusive, auto_adjust=False)


def normalize_yfinance(hist: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Converts a yfinance history frame to the project OHLCV schema (tz-naive session dates)."""
    if hist is None or hist.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    idx = pd.DatetimeIndex(hist.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    out = pd.DataFrame({
        "symbol": symbol,
        "date": idx.normalize(),
        "open": hist["Open"].to_numpy(dtype=float),
        "high": hist["High"].to_numpy(dtype=float),
        "low": hist["Low"].to_numpy(dtype=float),
        "close": hist["Close"].to_numpy(dtype=float),
        "volume": hist["Volume"].to_numpy(dtype=float),
        "source": "yfinance",
    })
    return out.sort_values("date").reset_index(drop=True)


def fetch_yfinance(
    symbol: str,
    start: str,
    end: str,
    history_fn: Optional[Callable[[str, str, str], pd.DataFrame]] = None,
) -> Tuple[pd.DataFrame, str]:
    """
    Fetches [start, end] inclusive. Returns (frame, status) where status is "OK", "EMPTY",
    or "ERROR: <type>: <message>". EMPTY and ERROR are gaps to report, never treated as data.
    """
    fn = history_fn or _default_yf_history
    end_exclusive = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        hist = fn(symbol, start, end_exclusive)
    except Exception as exc:
        logger.error("yfinance fetch failed for %s: %s", symbol, exc)
        return pd.DataFrame(columns=OUTPUT_COLUMNS), f"ERROR: {type(exc).__name__}: {exc}"
    df = normalize_yfinance(hist, symbol)
    if df.empty:
        return df, "EMPTY"
    return df, "OK"


def upsert_prices(collection: Any, df: pd.DataFrame) -> Dict[str, int]:
    """Idempotent upsert keyed on (symbol, date, source); re-running never duplicates rows."""
    from pymongo import ASCENDING, UpdateOne
    collection.create_index([("symbol", ASCENDING), ("date", ASCENDING), ("source", ASCENDING)], unique=True)
    if df.empty:
        return {"upserted": 0, "modified": 0, "matched": 0}
    ops = []
    for row in df.itertuples(index=False):
        doc = {
            "symbol": row.symbol,
            "date": pd.Timestamp(row.date).strftime("%Y-%m-%d"),
            "open": float(row.open), "high": float(row.high), "low": float(row.low),
            "close": float(row.close), "volume": float(row.volume), "source": row.source,
        }
        ops.append(UpdateOne({"symbol": doc["symbol"], "date": doc["date"], "source": doc["source"]},
                             {"$set": doc}, upsert=True))
    res = collection.bulk_write(ops, ordered=False)
    return {"upserted": res.upserted_count, "modified": res.modified_count, "matched": res.matched_count}
