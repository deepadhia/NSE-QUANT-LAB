"""
seed_upstox_history.py -- Seed / Backfill Historical Daily Candles from Upstox to MongoDB
========================================================================================
Fetches trailing N days of daily OHLCV bars directly from Upstox API v2 / v3
and idempotently upserts them into MongoDB 'upstox_daily_ohlcv'.

Usage:
  # Dry-run check for 5 symbols
  python scripts/seed_upstox_history.py --limit 5 --dry-run

  # Seed specific symbols
  python scripts/seed_upstox_history.py --symbols RELIANCE,TCS,HDFCBANK --days 365

  # Seed full tracked universe (Nifty 500) for past 1 year
  python scripts/seed_upstox_history.py --days 365
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime, timezone
import pandas as pd
from pymongo import UpdateOne

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.data_loader import auto_refresh_upstox_token, fetch_from_upstox
from nse_quant_lab.db.mongo_client import MongoDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_upstox_history")


def get_symbols_to_seed(db: MongoDatabase, symbols_arg: str = "", limit: int = 0) -> list:
    if symbols_arg:
        syms = [s.strip() for s in symbols_arg.split(",") if s.strip()]
        return [s if s.endswith(".NS") else f"{s}.NS" for s in syms]

    col_syms = db.db["symbols"]
    db_symbols = col_syms.distinct("symbol")
    equity_syms = [s for s in db_symbols if not s.startswith("^") and "NIFTY" not in s.upper()]

    if not equity_syms:
        # Fallback to distinct in upstox_daily_ohlcv
        equity_syms = [s for s in db.db["upstox_daily_ohlcv"].distinct("symbol") if not s.startswith("^")]

    if not equity_syms:
        equity_syms = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"]

    equity_syms = sorted(list(set(equity_syms)))
    if limit > 0:
        equity_syms = equity_syms[:limit]
    return equity_syms


def seed_history(days: int, symbols: list, db: MongoDatabase, dry_run: bool = False):
    token = auto_refresh_upstox_token()
    if not token and not dry_run:
        logger.error("No valid Upstox access token available. Set UPSTOX_ACCESS_TOKEN or UPSTOX_REFRESH_TOKEN.")
        sys.exit(1)

    end_dt = datetime.now()
    start_dt = end_dt - pd.Timedelta(days=days)
    end_str = end_dt.strftime("%Y-%m-%d")
    start_str = start_dt.strftime("%Y-%m-%d")

    logger.info("=" * 70)
    logger.info(f"SEEDING UPSTOX HISTORICAL DAILY CANDLES")
    logger.info(f"Window: {start_str} to {end_str} ({days} calendar days)")
    logger.info(f"Target Symbols: {len(symbols)}")
    logger.info(f"Dry Run: {dry_run}")
    logger.info("=" * 70)

    if dry_run:
        logger.info(f"Sample targets: {symbols[:10]}")
        logger.info("Dry-run complete. No network or database calls made.")
        return

    col = db.db["upstox_daily_ohlcv"]
    total_bars_saved = 0
    failed_syms = []

    for idx, sym in enumerate(symbols, start=1):
        clean_sym = sym.replace(".NS", "").replace(".BO", "")
        t0 = time.time()
        try:
            df = fetch_from_upstox(clean_sym, start_str, end_str, access_token=token)
            if df is None or df.empty:
                logger.warning(f"[{idx}/{len(symbols)}] {clean_sym}: No candles returned from Upstox.")
                failed_syms.append(clean_sym)
                continue

            bulk_ops = []
            for _, row in df.iterrows():
                b_date = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
                bulk_ops.append(UpdateOne(
                    {"symbol": sym, "date": b_date},
                    {"$set": {
                        "symbol": sym,
                        "ticker": clean_sym,
                        "date": b_date,
                        "open": round(float(row["open"]), 2),
                        "high": round(float(row["high"]), 2),
                        "low": round(float(row["low"]), 2),
                        "close": round(float(row["close"]), 2),
                        "volume": float(row["volume"]),
                        "source": "upstox_seed",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }},
                    upsert=True
                ))

            if bulk_ops:
                col.bulk_write(bulk_ops, ordered=False)
                total_bars_saved += len(bulk_ops)

            elapsed = time.time() - t0
            logger.info(f"[{idx}/{len(symbols)}] {clean_sym}: Saved {len(bulk_ops)} bars ({elapsed:.2f}s)")
        except Exception as exc:
            logger.error(f"[{idx}/{len(symbols)}] {clean_sym} error: {exc}")
            failed_syms.append(clean_sym)

    logger.info("=" * 70)
    logger.info(f"COMPLETED SEEDING: Saved {total_bars_saved} bars across {len(symbols) - len(failed_syms)} symbols.")
    if failed_syms:
        logger.warning(f"Failed symbols ({len(failed_syms)}): {failed_syms}")
    logger.info("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Seed Upstox Historical Daily Candles into MongoDB")
    parser.add_argument("--days", type=int, default=365, help="Number of calendar days of history to fetch (default: 365)")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to seed (default: all from symbols collection)")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of symbols (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Show targets without fetching or writing")
    args = parser.parse_args()

    mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
    db_name = os.environ.get("MONGO_DB", os.environ.get("MONGO_DB_NAME", "stock_scanner"))
    db = MongoDatabase.get_instance(mongo_uri, db_name)

    symbols = get_symbols_to_seed(db, symbols_arg=args.symbols, limit=args.limit)
    seed_history(days=args.days, symbols=symbols, db=db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

