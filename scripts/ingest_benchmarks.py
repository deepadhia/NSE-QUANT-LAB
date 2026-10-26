"""
scripts/ingest_benchmarks.py -- Ingests Official Nifty 50 & Nifty 500 TRI Benchmarks
===================================================================================
Fetches daily index OHLCV candles from Yahoo Finance (^NSEI for Nifty 50, ^CRSLDX for Nifty 500)
and upserts them into MongoDB 'benchmark_ohlcv' collection.

Ensures every historical backtest can be compared head-to-head on identical dates:
- Returns, CAGR, Sharpe ratio
- Maximum Drawdown
- Active Alpha and Information Ratio
"""

import os
import sys
import logging
from datetime import datetime
import pandas as pd
from pymongo import MongoClient, UpdateOne

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_benchmarks")

BENCHMARK_SYMBOLS = {
    "^NSEI": "NIFTY 50",
    "^CRSLDX": "NIFTY 500",
}


def ingest_benchmarks(
    start_date: str = "2018-01-01",
    end_date: str = "2026-10-25",
    mongo_uri: str = "mongodb://localhost:27017/",
    db_name: str = "stock_scanner",
    collection_name: str = "benchmark_ohlcv",
) -> None:
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance is required. Run: pip install yfinance")
        sys.exit(1)

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
    col = client[db_name][collection_name]

    # Ensure indexes
    col.create_index([("symbol", 1), ("date", 1)], unique=True)
    col.create_index([("date", 1)])

    now_utc = datetime.utcnow().isoformat()

    for ticker, display_name in BENCHMARK_SYMBOLS.items():
        logger.info(f"Downloading benchmark {display_name} ({ticker}) from {start_date} to {end_date}...")
        try:
            df = yf.download(ticker, start=start_date, end=end_date, progress=False, timeout=15)
            if df.empty:
                logger.warning(f"No data returned for benchmark {ticker}.")
                continue

            # Flatten MultiIndex columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]

            upsert_ops = []
            for dt_idx, r in df.iterrows():
                dt_str = dt_idx.strftime("%Y-%m-%d")
                o = float(r["Open"].iloc[0] if isinstance(r["Open"], pd.Series) else r["Open"])
                h = float(r["High"].iloc[0] if isinstance(r["High"], pd.Series) else r["High"])
                l = float(r["Low"].iloc[0] if isinstance(r["Low"], pd.Series) else r["Low"])
                c = float(r["Close"].iloc[0] if isinstance(r["Close"], pd.Series) else r["Close"])
                v = float(r["Volume"].iloc[0] if isinstance(r["Volume"], pd.Series) else r.get("Volume", 0.0))

                if pd.isna(c) or c <= 0:
                    continue

                doc = {
                    "symbol": ticker,
                    "name": display_name,
                    "date": dt_str,
                    "open": round(o, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "close": round(c, 2),
                    "volume": float(v) if not pd.isna(v) else 0.0,
                    "updated_at": now_utc,
                }
                upsert_ops.append(
                    UpdateOne({"symbol": ticker, "date": dt_str}, {"$set": doc}, upsert=True)
                )

            if upsert_ops:
                res = col.bulk_write(upsert_ops, ordered=False)
                total_in_db = col.count_documents({"symbol": ticker})
                logger.info(
                    f"Successfully synced {display_name} ({ticker}): "
                    f"{len(upsert_ops)} rows processed (upserted: {res.upserted_count}, modified: {res.modified_count}). "
                    f"Total records in DB: {total_in_db}."
                )

        except Exception as e:
            logger.error(f"Failed to ingest benchmark {ticker}: {e}", exc_info=True)


if __name__ == "__main__":
    uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
    db = os.environ.get("MONGO_DB", "stock_scanner")
    ingest_benchmarks(mongo_uri=uri, db_name=db)
