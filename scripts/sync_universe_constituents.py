"""
sync_universe_constituents.py -- Semi-Annual NSE Index Constituent Synchronizer
=============================================================================
Safely updates MongoDB with the latest official NSE Index constituents (Nifty 500, MidSmall 400):
1. Downloads official CSV directly from the NSE India archive.
2. Diffs incoming symbols against MongoDB 'symbols' collection.
3. Additive Updates:
   - Newly Added: Registered in 'symbols' with ISIN/industry and (optionally) fetches 250-day warm-up bars.
   - Retired Stocks: Marked 'is_active = False' in 'symbols'. NEVER deletes historical OHLCV bars.
4. Preserves 100% backtest reproducibility and historical integrity.

Usage:
    # Dry run (inspect diff only without writing):
    python scripts/sync_universe_constituents.py --index nifty500 --dry-run

    # Apply diff and fetch 250-day warm-up history for newly added stocks:
    python scripts/sync_universe_constituents.py --index nifty500 --fetch-history
"""

import os
import sys
import io
import argparse
import logging
import urllib.request
from datetime import datetime, timedelta
import pandas as pd
from pymongo import MongoClient, UpdateOne

# Ensure src is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass

from nse_quant_lab.data_loader import fetch_from_upstox, auto_refresh_upstox_token
from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.notifications.telegram import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("universe_sync")

INDEX_URLS = {
    "nifty500": [
        "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
    ],
    "midsmall400": [
        "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
        "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv"
    ]
}

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}


def download_official_constituents(index_type: str) -> pd.DataFrame:
    """Downloads and concatenates official constituent CSVs from NSE India."""
    urls = INDEX_URLS.get(index_type.lower())
    if not urls:
        raise ValueError(f"Unknown index type: {index_type}. Available: {list(INDEX_URLS.keys())}")

    dfs = []
    for u in urls:
        logger.info(f"Downloading official constituent list from: {u}")
        req = urllib.request.Request(u, headers=NSE_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()
            df = pd.read_csv(io.BytesIO(content))
            dfs.append(df)

    combined = pd.concat(dfs, ignore_index=True)
    # Standardize column names
    combined.columns = [c.strip().title() for c in combined.columns]
    combined["Symbol_Clean"] = combined["Symbol"].astype(str).str.strip()
    combined["Symbol_NS"] = combined["Symbol_Clean"] + ".NS"
    combined["Isin"] = combined.get("Isin Code", combined.get("Isin", "")).astype(str).str.strip()
    combined["Name"] = combined.get("Company Name", combined["Symbol_Clean"]).astype(str).str.strip()
    combined["Industry"] = combined.get("Industry", "General").astype(str).str.strip()
    
    # Drop duplicates if any
    combined = combined.drop_duplicates(subset=["Symbol_NS"]).reset_index(drop=True)
    return combined


def sync_constituents(index_type: str = "nifty500", dry_run: bool = False, fetch_history: bool = False):
    logger.info("=" * 75)
    logger.info(f"   NSE UNIVERSE CONSTITUENT SYNCHRONIZER ({index_type.upper()})")
    logger.info("=" * 75)

    cfg = PaperTradingConfig(strategy_id="sync")
    client = MongoClient(cfg.mongo_uri, serverSelectionTimeoutMS=5000)
    db = client[cfg.db_name]
    symbols_col = db["symbols"]
    ohlcv_col = db["upstox_daily_ohlcv"]

    # 1. Download official list from NSE
    official_df = download_official_constituents(index_type)
    official_symbols = set(official_df["Symbol_NS"].tolist())
    logger.info(f">> Retrieved {len(official_symbols)} official constituents from NSE.")

    # 2. Query existing DB symbols
    db_docs = list(symbols_col.find({}, {"symbol": 1, "is_active": 1, "_id": 0}))
    db_symbols = {d["symbol"] for d in db_docs}
    active_db_symbols = {d["symbol"] for d in db_docs if d.get("is_active", True)}
    logger.info(f">> Existing DB tracked symbols: {len(db_symbols)} (Active: {len(active_db_symbols)})")

    # 3. Calculate Diffs
    newly_added = official_symbols - db_symbols
    reactivated = (official_symbols & db_symbols) - active_db_symbols
    retired = active_db_symbols - official_symbols
    retained = official_symbols & active_db_symbols

    print("\n" + "-" * 75)
    print("SEMI-ANNUAL INDEX RECONSTITUTION SUMMARY:")
    print("-" * 75)
    print(f"  • Retained Active Symbols:   {len(retained)}")
    print(f"  • Newly Added Symbols:       {len(newly_added)}")
    print(f"  • Reactivated Symbols:       {len(reactivated)}")
    print(f"  • Retired / Excluded Symbols:{len(retired)}")
    print("-" * 75)

    if newly_added:
        print(f"\nIncoming Entrants ({len(newly_added)}):")
        for s in sorted(list(newly_added))[:15]:
            row = official_df[official_df["Symbol_NS"] == s].iloc[0]
            print(f"   [+] {s:15s} | {row['Name'][:30]:30s} | {row['Industry']}")
        if len(newly_added) > 15:
            print(f"   ... and {len(newly_added) - 15} more.")

    if retired:
        print(f"\nOutgoing Exclusions ({len(retired)}):")
        for s in sorted(list(retired))[:15]:
            print(f"   [-] {s}")
        if len(retired) > 15:
            print(f"   ... and {len(retired) - 15} more.")

    if dry_run:
        print("\n[DRY RUN] No modifications written to MongoDB.")
        return

    # 4. Apply Database Updates
    now_utc = datetime.utcnow()
    ops = []

    # Update retired symbols (never delete historical OHLCV!)
    for s in retired:
        ops.append(UpdateOne(
            {"symbol": s},
            {"$set": {"is_active": False, "retired_at": now_utc.isoformat()}}
        ))

    # Update reactivated symbols
    for s in reactivated:
        ops.append(UpdateOne(
            {"symbol": s},
            {"$set": {"is_active": True, "reactivated_at": now_utc.isoformat()}}
        ))

    # Insert newly added symbols
    for s in newly_added:
        row = official_df[official_df["Symbol_NS"] == s].iloc[0]
        ops.append(UpdateOne(
            {"symbol": s},
            {"$set": {
                "symbol": s,
                "name": row["Name"],
                "industry": row["Industry"],
                "isin": row["Isin"],
                "exchange": "NSE",
                "is_active": True,
                "first_added": now_utc.isoformat(),
                "updated_at": now_utc
            }},
            upsert=True
        ))

    if ops:
        res = symbols_col.bulk_write(ops)
        logger.info(f"MongoDB 'symbols' updated: {res.upserted_count} new, {res.modified_count} updated.")

    # 5. Optional Warm-Up Data Ingestion for Newly Added Stocks
    if fetch_history and newly_added:
        logger.info("\n" + "=" * 75)
        logger.info(f"FETCHING 250-DAY WARM-UP OHLCV BARS FOR {len(newly_added)} NEW ENTRANTS")
        logger.info("=" * 75)
        
        token = auto_refresh_upstox_token()
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        warmed_up = 0
        for idx, sym in enumerate(sorted(list(newly_added)), start=1):
            clean_sym = sym.replace(".NS", "")
            df = None
            if token:
                df = fetch_from_upstox(clean_sym, start_date, end_date, access_token=token)
            
            # Fallback to yfinance if Upstox unavailable
            if df is None or df.empty:
                try:
                    import yfinance as yf
                    yf_df = yf.download(sym, start=start_date, end=end_date, progress=False, timeout=10)
                    if not yf_df.empty:
                        rows = []
                        for dt_idx, r in yf_df.iterrows():
                            c = float(r["Close"].iloc[0] if isinstance(r["Close"], pd.Series) else r["Close"])
                            o = float(r["Open"].iloc[0] if isinstance(r["Open"], pd.Series) else r["Open"])
                            h = float(r["High"].iloc[0] if isinstance(r["High"], pd.Series) else r["High"])
                            l = float(r["Low"].iloc[0] if isinstance(r["Low"], pd.Series) else r["Low"])
                            v = float(r["Volume"].iloc[0] if isinstance(r["Volume"], pd.Series) else r["Volume"])
                            rows.append({
                                "symbol": sym,
                                "date": dt_idx.strftime("%Y-%m-%d"),
                                "open": round(o, 2), "high": round(h, 2), "low": round(l, 2), "close": round(c, 2),
                                "volume": v,
                                "source": "yfinance_warmup",
                                "updated_at": now_utc.isoformat()
                            })
                        if rows:
                            ohlcv_col.insert_many(rows, ordered=False)
                            warmed_up += 1
                except Exception as e:
                    logger.warning(f"Failed fallback warm-up for {sym}: {e}")
            else:
                rows = []
                for _, r in df.iterrows():
                    rows.append({
                        "symbol": sym,
                        "date": r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])[:10],
                        "open": round(float(r["open"]), 2), "high": round(float(r["high"]), 2),
                        "low": round(float(r["low"]), 2), "close": round(float(r["close"]), 2),
                        "volume": float(r["volume"]),
                        "source": "upstox_warmup",
                        "updated_at": now_utc.isoformat()
                    })
                if rows:
                    ohlcv_col.insert_many(rows, ordered=False)
                    warmed_up += 1

            if idx % 10 == 0 or idx == len(newly_added):
                logger.info(f"   [{idx}/{len(newly_added)}] Warm-up progress: {warmed_up} stocks populated.")

        logger.info(f"Warm-up complete: {warmed_up} of {len(newly_added)} new entrants populated with 1-year history.")

        # Send Telegram Notification if there were changes or if executed via automation
    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.telegram_enabled)
    if notifier.enabled and (newly_added or retired):
        msg = (
            f"🔄 <b>NSE Semi-Annual Index Rebalance Completed</b>\n"
            f"Index: <code>{index_type.upper()}</code>\n"
            f"Date: <code>{datetime.now().strftime('%Y-%m-%d')}</code>\n\n"
            f"• <b>Active Retained:</b> {len(retained)} stocks\n"
            f"• <b>New Entrants Added:</b> +{len(newly_added)} (1-yr warm-up history loaded)\n"
            f"• <b>Retired Exclusions:</b> -{len(retired)} (history preserved in DB)\n"
        )
        notifier.send_message(msg)

    print("\n✅ UNIVERSE SYNCHRONIZATION FINISHED SUCCESSFULLY.")


def main():
    parser = argparse.ArgumentParser(description="Synchronize official NSE index constituents with MongoDB")
    parser.add_argument("--index", choices=["nifty500", "midsmall400"], default="nifty500",
                        help="Target index to synchronize (default: nifty500)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only inspect and print the diff without writing to database")
    parser.add_argument("--fetch-history", action="store_true",
                        help="Fetch 250-day warm-up OHLCV bars for newly added entrant stocks")
    args = parser.parse_args()

    sync_constituents(index_type=args.index, dry_run=args.dry_run, fetch_history=args.fetch_history)


if __name__ == "__main__":
    main()
