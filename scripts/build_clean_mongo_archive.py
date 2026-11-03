#!/usr/bin/env python3
"""
build_clean_mongo_archive.py -- Ingests 20-year sanitized market data into a dedicated MongoDB database.

Target Database: nse_clean_archive (localhost:27017)
Collections:
  1. clean_daily_ohlcv      : 8.36M rows from clean_panel.parquet (split/bonus adjusted, EQ only)
  2. benchmark_daily_ohlcv  : 15.7K rows (^CRSLDX, ^NSEI)
  3. corporate_actions      : Official NSE splits/bonuses from all_corporate_actions.parquet
  4. symbol_changes         : Canonical ticker rename history from symbolchange.csv

This database is 100% isolated and self-contained, allowing single-command mongodump export:
  mongodump --db nse_clean_archive --gzip --archive=nse_clean_archive_20yr.dump.gz
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Dict, Any

import numpy as np
import pandas as pd
from pymongo import MongoClient, ASCENDING

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIR = REPO_ROOT / "external" / "nse_archive"
CLEAN_PANEL_PATH = ARCHIVE_DIR / "clean_panel.parquet"
CORP_ACTIONS_PATH = ARCHIVE_DIR / "all_corporate_actions.parquet"
SYMBOL_CHANGE_PATH = ARCHIVE_DIR / "lists" / "symbolchange.csv"

TARGET_DB = "nse_clean_archive"


def get_mongo_db(uri: str = "mongodb://localhost:27017/", db_name: str = TARGET_DB):
    client = MongoClient(uri, serverSelectionTimeoutMS=2000)
    client.admin.command("ping")
    return client[db_name]


def import_clean_equity_ohlcv(db, batch_size: int = 50_000):
    coll = db["clean_daily_ohlcv"]
    print(f"\n[1/4] Importing clean equity OHLCV from {CLEAN_PANEL_PATH}...")
    t0 = time.time()
    
    if not CLEAN_PANEL_PATH.exists():
        raise FileNotFoundError(f"Clean panel not found at {CLEAN_PANEL_PATH}")
        
    df = pd.read_parquet(CLEAN_PANEL_PATH)
    total_rows = len(df)
    print(f"  Loaded {total_rows:,} records across {df['symbol'].nunique():,} symbols in {time.time()-t0:.1f}s.")
    
    # Format date as YYYY-MM-DD string for standardized ISO representation
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        df["date_str"] = df["date"].dt.strftime("%Y-%m-%d")
    else:
        df["date_str"] = df["date"].astype(str).str.slice(0, 10)
        
    cols = ["symbol", "date_str", "series", "open", "high", "low", "close", "volume", "turnover"]
    
    print(f"  Inserting {total_rows:,} documents in batches of {batch_size:,}...")
    inserted = 0
    t_insert = time.time()
    
    # Check if collection already populated
    existing_cnt = coll.estimated_document_count()
    if existing_cnt >= total_rows:
        print(f"  Collection already contains {existing_cnt:,} documents. Skipping re-insert.")
    else:
        # Batch insert
        for i in range(0, total_rows, batch_size):
            chunk = df.iloc[i : i + batch_size]
            docs = []
            for row in chunk.itertuples(index=False):
                docs.append({
                    "symbol": str(row.symbol),
                    "date": str(row.date_str),
                    "series": str(row.series),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                    "turnover": float(row.turnover),
                })
            coll.insert_many(docs, ordered=False)
            inserted += len(docs)
            if inserted % 250_000 == 0 or inserted == total_rows:
                pct = (inserted / total_rows) * 100
                rate = inserted / (time.time() - t_insert)
                print(f"    Inserted {inserted:,} / {total_rows:,} ({pct:.1f}%) -- {rate:,.0f} docs/sec")
                
    # Create compound indexes for instant backtesting lookups
    print("  Creating compound indexes on clean_daily_ohlcv...")
    coll.create_index([("symbol", ASCENDING), ("date", ASCENDING)], unique=True)
    coll.create_index([("date", ASCENDING)])
    print(f"  clean_daily_ohlcv complete in {time.time()-t0:.1f}s.")


def import_benchmarks(db, src_uri: str = "mongodb://localhost:27017/", src_db: str = "stock_scanner"):
    coll = db["benchmark_daily_ohlcv"]
    print(f"\n[2/4] Copying benchmark candles (^CRSLDX, ^NSEI) from {src_db}...")
    t0 = time.time()
    
    src_client = MongoClient(src_uri, serverSelectionTimeoutMS=2000)
    src_coll = src_client[src_db]["benchmark_ohlcv"]
    
    cursor = src_coll.find({}, {"_id": 0})
    bench_docs = list(cursor)
    
    if bench_docs:
        # Standardize date to YYYY-MM-DD string
        for d in bench_docs:
            if isinstance(d.get("date"), pd.Timestamp):
                d["date"] = d["date"].strftime("%Y-%m-%d")
            else:
                d["date"] = str(d.get("date"))[:10]
        
        coll.delete_many({})
        coll.insert_many(bench_docs)
        coll.create_index([("symbol", ASCENDING), ("date", ASCENDING)], unique=True)
        print(f"  Copied {len(bench_docs):,} benchmark bars in {time.time()-t0:.1f}s.")
    else:
        print("  Warning: No benchmark documents found in source DB.")


def import_corporate_actions(db):
    coll = db["corporate_actions"]
    print(f"\n[3/4] Importing official corporate actions from {CORP_ACTIONS_PATH}...")
    t0 = time.time()
    
    if not CORP_ACTIONS_PATH.exists():
        print(f"  Warning: Corporate actions parquet not found at {CORP_ACTIONS_PATH}")
        return
        
    df = pd.read_parquet(CORP_ACTIONS_PATH)
    df["ex_date"] = df["ex_date"].astype(str).str.slice(0, 10)
    
    docs = []
    for r in df.itertuples(index=False):
        docs.append({
            "symbol": str(r.symbol),
            "ex_date": str(r.ex_date),
            "kind": str(r.kind),
            "price_factor": float(r.price_factor),
            "subject": str(getattr(r, "subject", "")),
        })
        
    coll.delete_many({})
    if docs:
        coll.insert_many(docs)
        coll.create_index([("symbol", ASCENDING), ("ex_date", ASCENDING)])
    print(f"  Imported {len(docs):,} corporate actions in {time.time()-t0:.1f}s.")


def import_symbol_changes(db):
    coll = db["symbol_changes"]
    print(f"\n[4/4] Importing official symbol change history from {SYMBOL_CHANGE_PATH}...")
    t0 = time.time()
    
    if not SYMBOL_CHANGE_PATH.exists():
        print(f"  Warning: symbolchange.csv not found at {SYMBOL_CHANGE_PATH}")
        return
        
    df = pd.read_csv(SYMBOL_CHANGE_PATH, encoding="latin-1")
    docs = []
    for r in df.itertuples(index=False):
        # Columns: ['NAME OF THE COMPANY', 'OLD SYMBOL', 'NEW SYMBOL', 'EFFECTIVE DATE']
        docs.append({
            "company_name": str(getattr(r, "_0", "")).strip(),
            "old_symbol": str(getattr(r, "_1", "")).strip(),
            "new_symbol": str(getattr(r, "_2", "")).strip(),
            "effective_date": str(getattr(r, "_3", "")).strip(),
        })
        
    coll.delete_many({})
    if docs:
        coll.insert_many(docs)
        coll.create_index([("old_symbol", ASCENDING)])
        coll.create_index([("new_symbol", ASCENDING)])
    print(f"  Imported {len(docs):,} symbol change mappings in {time.time()-t0:.1f}s.")


def main():
    parser = argparse.ArgumentParser(description="Build dedicated nse_clean_archive MongoDB database.")
    parser.add_argument("--uri", default="mongodb://localhost:27017/", help="MongoDB URI")
    parser.add_argument("--db", default=TARGET_DB, help="Target database name")
    parser.add_argument("--batch-size", type=int, default=50_000, help="Batch size for bulk insert")
    args = parser.parse_args()
    
    print("=" * 70)
    print(f"BUILDING SELF-CONTAINED 20-YEAR MONGO ARCHIVE: {args.db}")
    print("=" * 70)
    
    t_start = time.time()
    db = get_mongo_db(args.uri, args.db)
    
    import_clean_equity_ohlcv(db, batch_size=args.batch_size)
    import_benchmarks(db, src_uri=args.uri)
    import_corporate_actions(db)
    import_symbol_changes(db)
    
    print("\n" + "=" * 70)
    print("DATABASE BUILD COMPLETE -- VERIFICATION SUMMARY:")
    print("=" * 70)
    for c in sorted(db.list_collection_names()):
        cnt = db[c].estimated_document_count()
        print(f"  - {args.db}.{c:25s}: {cnt:,} documents")
    print(f"\nTotal elapsed time: {time.time() - t_start:.1f}s")
    print(f"\nTo export this single database for distribution:")
    print(f"  mongodump --db {args.db} --gzip --archive=nse_clean_archive_20yr.dump.gz")
    print(f"\nTo restore on any machine:")
    print(f"  mongorestore --gzip --archive=nse_clean_archive_20yr.dump.gz")


if __name__ == "__main__":
    main()

