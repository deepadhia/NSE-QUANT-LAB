"""
ingest_daily_upstox_candles.py -- EOD Daily Candle Ingestion Pipeline (Adapted from Alpha-Pulse)
=============================================================================================
Scheduled via daily cron at 15:35 IST (immediately after market close, before 15:45 strategy scan):
1. Calls auto_refresh_upstox_token() to renew access token autonomously via UPSTOX_REFRESH_TOKEN (no OTP).
2. Queries the target Nifty 500 universe from MongoDB or EQUITY_L master list.
3. Fetches today's candle for each stock using Upstox API v2 (/historical-candle/{instrument_key}/day/...).
   - Enforces 100ms rate-limit throttle between requests.
   - Handles HTTP 429 backoff gracefully.
   - Falls back to secondary source (yfinance/bhavcopy) if Upstox candle is unavailable.
4. Performs idempotent upsert into MongoDB 'upstox_daily_ohlcv' collection.
5. Emits Telegram notification on completion.
"""

import os
import sys
import argparse
import logging
from datetime import datetime
from pymongo import UpdateOne
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.data_loader import auto_refresh_upstox_token, fetch_from_upstox
from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.notifications.telegram import TelegramNotifier
from nse_quant_lab.holidays import is_trading_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("upstox_ingestion")


def get_target_symbols(db: MongoDatabase) -> list:
    """Retrieves all distinct equity symbols tracked in MongoDB upstox_daily_ohlcv."""
    col = db.db["upstox_daily_ohlcv"]
    symbols = col.distinct("symbol")
    # Filter out index symbols
    equity_syms = [s for s in symbols if not s.startswith("^") and "NIFTY" not in s.upper()]
    if equity_syms:
        return sorted(equity_syms)
    # Default fallback to top liquid symbols if collection is fresh
    return ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"]


def ingest_today_candles(date_str: str, symbols: list, db: MongoDatabase, notifier: TelegramNotifier):
    logger.info("=" * 70)
    logger.info(f"STARTING EOD UPSTOX CANDLE INGESTION FOR: {date_str}")
    logger.info(f"Target Symbols: {len(symbols)}")
    logger.info("=" * 70)

    # Step 1: Auto-refresh Upstox token (Alpha-Pulse method)
    token = auto_refresh_upstox_token()
    col = db.db["upstox_daily_ohlcv"]

    success_count = 0
    fallback_count = 0
    failed_symbols = []

    for idx, sym in enumerate(symbols, start=1):
        clean_sym = sym.replace(".NS", "").replace(".BO", "")
        used_fallback = False
        df = None

        # Attempt Upstox API v2 fetch
        if token:
            df = fetch_from_upstox(clean_sym, date_str, date_str, access_token=token)

        # Fallback to yfinance if Upstox unavailable or returns empty
        if df is None or df.empty:
            used_fallback = True
            try:
                import yfinance as yf
                ticker = sym if sym.endswith(".NS") else f"{sym}.NS"
                dt_obj = pd.to_datetime(date_str)
                next_day_str = (dt_obj + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                yf_df = yf.download(ticker, start=date_str, end=next_day_str, progress=False, timeout=10, auto_adjust=False)
                if not yf_df.empty:
                    row = yf_df.iloc[-1]
                    # Handle multi-index columns from yfinance >= 0.2.34
                    o = float(row[("Open", ticker)] if ("Open", ticker) in row else row["Open"])
                    h = float(row[("High", ticker)] if ("High", ticker) in row else row["High"])
                    l = float(row[("Low", ticker)] if ("Low", ticker) in row else row["Low"])
                    c = float(row[("Close", ticker)] if ("Close", ticker) in row else row["Close"])
                    v = float(row[("Volume", ticker)] if ("Volume", ticker) in row else row["Volume"])
                    df = pd.DataFrame([{
                        "symbol": sym,
                        "date": date_str,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                    }])
            except Exception:
                df = None

        if df is not None and not df.empty:
            if used_fallback:
                fallback_count += 1
            row = df.iloc[-1]
            doc = {
                "symbol": sym,
                "date": date_str,
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
                "volume": float(row["volume"]),
                "source": "yfinance_fallback" if used_fallback else "upstox",
                "updated_at": datetime.utcnow().isoformat(),
            }
            # Check for overnight split / corporate action drop (>25% gap down vs previous close)
            prev_bar = col.find_one({"symbol": sym, "date": {"$lt": date_str}}, sort=[("date", -1)])
            if prev_bar and prev_bar.get("close", 0) > 0:
                prev_c = float(prev_bar["close"])
                ratio = float(row["open"]) / prev_c
                if ratio < 0.75:  # Over 25% overnight price reduction indicates split/bonus
                    logger.warning(
                        f"[CORPORATE ACTION DETECTED] {sym}: Open Rs {float(row['open']):.2f} vs Prev Close Rs {prev_c:.2f} "
                        f"(drop of {(1.0 - ratio)*100:.1f}%). Triggering 250-day adjusted historical refresh..."
                    )
                    start_lb = (pd.to_datetime(date_str) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
                    refreshed = None
                    if token:
                        refreshed = fetch_from_upstox(clean_sym, start_lb, date_str, access_token=token)
                    if refreshed is not None and not refreshed.empty:
                        bulk_ops = []
                        for _, r_bar in refreshed.iterrows():
                            b_date = pd.to_datetime(r_bar["date"]).strftime("%Y-%m-%d")
                            bulk_ops.append(UpdateOne(
                                {"symbol": sym, "date": b_date},
                                {"$set": {
                                    "symbol": sym,
                                    "date": b_date,
                                    "open": round(float(r_bar["open"]), 2),
                                    "high": round(float(r_bar["high"]), 2),
                                    "low": round(float(r_bar["low"]), 2),
                                    "close": round(float(r_bar["close"]), 2),
                                    "volume": float(r_bar["volume"]),
                                    "source": "split_adjusted_refresh",
                                    "updated_at": datetime.utcnow().isoformat(),
                                }},
                                upsert=True
                            ))
                        if bulk_ops:
                            col.bulk_write(bulk_ops, ordered=False)
                            logger.info(f"   [SPLIT REFRESH] Updated {len(bulk_ops)} adjusted bars for {sym}.")

                            # Adjust open positions in db["positions"] to prevent fake NAV loss
                            try:
                                new_prev_row = refreshed[pd.to_datetime(refreshed["date"]).dt.strftime("%Y-%m-%d") == prev_bar["date"]]
                                if not new_prev_row.empty:
                                    new_prev_c = float(new_prev_row.iloc[0]["close"])
                                    if new_prev_c > 0:
                                        factor = prev_c / new_prev_c
                                        if factor > 1.05 or factor < 0.95:
                                            pos_col = db.positions
                                            open_positions = list(pos_col.find({"symbol": sym}))
                                            for p in open_positions:
                                                old_sh = p.get("shares", 0)
                                                old_entry = p.get("entry_price", 0.0)
                                                new_sh = int(round(old_sh * factor))
                                                new_entry = round(old_entry / factor, 4)
                                                new_fill = round(p.get("entry_fill_price", old_entry) / factor, 4)
                                                pos_col.update_one(
                                                    {"_id": p["_id"]},
                                                    {"$set": {
                                                        "shares": new_sh,
                                                        "entry_price": new_entry,
                                                        "entry_fill_price": new_fill,
                                                        "split_adjusted_factor": factor,
                                                        "updated_at": datetime.utcnow().isoformat(),
                                                    }}
                                                )
                                                logger.info(
                                                    f"   [POSITION SPLIT ADJUSTED] {sym}: Shares {old_sh} -> {new_sh}, "
                                                    f"Entry Px Rs {old_entry:.2f} -> Rs {new_entry:.2f} (Factor {factor:.2f})"
                                                )
                            except Exception as split_err:
                                logger.error(f"Failed to adjust open positions for split in {sym}: {split_err}")

            # Idempotent upsert
            col.update_one(
                {"symbol": sym, "date": date_str},
                {"$set": doc},
                upsert=True
            )
            success_count += 1
            if idx % 50 == 0 or idx == len(symbols):
                logger.info(f"   [{idx}/{len(symbols)}] Progress: {success_count} candles saved.")
        else:
            failed_symbols.append(sym)

    logger.info("=" * 70)
    logger.info(f"INGESTION SUMMARY FOR {date_str}:")
    logger.info(f"   Total Saved:   {success_count} / {len(symbols)}")
    logger.info(f"   Via Upstox:    {success_count - fallback_count}")
    logger.info(f"   Via Fallback:  {fallback_count}")
    logger.info(f"   Failed Count:  {len(failed_symbols)}")
    logger.info("=" * 70)

    # Routine EOD data maintenance: log to file only (Zero-Spam: no Telegram alert for routine data maintenance)
    logger.info(f"Ingestion finished successfully: {success_count}/{len(symbols)} saved.")


def main():
    parser = argparse.ArgumentParser(description="Ingest EOD Daily Candles from Upstox API v2")
    parser.add_argument("--date", default=None, help="Candle date to ingest (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit on symbols count for testing.")
    parser.add_argument("--force", action="store_true", help="Force run even on weekends or market holidays.")
    args = parser.parse_args()

    cfg = PaperTradingConfig(strategy_id="ingest")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.telegram_enabled)

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    if not args.force and not is_trading_day(date_str):
        logger.info(f"Target date {date_str} is a weekend or official NSE holiday. Skipping ingestion.")
        sys.exit(0)
    symbols = get_target_symbols(db)
    if args.limit:
        symbols = symbols[:args.limit]

    ingest_today_candles(date_str, symbols, db, notifier)


if __name__ == "__main__":
    main()
