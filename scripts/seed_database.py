"""
seed_database.py -- Database Seeder & Data Management Tool
==========================================================
Modes:
1. --mode sample : Seeds MongoDB from data/sample/nifty500_sample.parquet (instant offline setup)
2. --mode upstox : Pulls historical daily candles from public Upstox V2 API
3. --mode verify : Verifies database health, record counts, and index status
4. --mode export : Dumps MongoDB collection to data/sample/nifty500_sample.parquet
"""

import os
import sys
import json
import argparse
import logging
import datetime
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from pymongo import MongoClient, ReplaceOne

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_database")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DEFAULT_DB_NAME = os.environ.get("MONGO_DB", "stock_scanner")
DEFAULT_COLLECTION = os.environ.get("MONGO_COLLECTION", "upstox_daily_ohlcv")
DEFAULT_PARQUET = os.path.join(BASE_DIR, "data", "sample", "nifty500_sample.parquet")
SYMBOLS_FILE = os.path.join(BASE_DIR, "data", "symbols.json")


def seed_from_sample(mongo_uri: str, db_name: str, coll_name: str, parquet_path: str):
    logger.info(f">> Seeding MongoDB from sample parquet: {parquet_path}")
    if not os.path.exists(parquet_path):
        logger.error(f"Sample parquet not found at: {parquet_path}")
        sys.exit(1)

    df = pd.read_parquet(parquet_path)
    logger.info(f"   Loaded {len(df):,} records from parquet.")

    client = MongoClient(mongo_uri)
    col = client[db_name][coll_name]

    col.create_index([("symbol", 1), ("date", 1)], unique=True)
    col.create_index([("date_dt", 1)])

    records = df.to_dict("records")
    batch_size = 5000
    total_written = 0

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        ops = []
        for r in batch:
            if "date_dt" not in r:
                r["date_dt"] = pd.to_datetime(r["date"]).to_pydatetime()
            ops.append(ReplaceOne({"symbol": r["symbol"], "date": str(r["date"])[:10]}, r, upsert=True))
        if ops:
            res = col.bulk_write(ops, ordered=False)
            total_written += (res.upserted_count + res.modified_count)

    logger.info(f"   Successfully seeded {total_written:,} records into {db_name}.{coll_name}.")


def verify_database(mongo_uri: str, db_name: str, coll_name: str):
    logger.info(f">> Verifying MongoDB collection {db_name}.{coll_name}...")
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
        col = client[db_name][coll_name]
        count = col.count_documents({})
        symbols = col.distinct("symbol")
        sample_doc = col.find_one({}, {"_id": 0})
        indexes = [idx["name"] for idx in col.list_indexes()]

        print("=" * 70)
        print("DATABASE VERIFICATION SUMMARY")
        print("=" * 70)
        print(f"  Target:            {db_name}.{coll_name}")
        print(f"  Total Records:     {count:,}")
        print(f"  Distinct Symbols:  {len(symbols)}")
        print(f"  Indexes:           {indexes}")
        if sample_doc:
            print(f"  Sample Keys:       {list(sample_doc.keys())}")
        print("=" * 70)
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Database Seeder & Verifier")
    parser.add_argument("--mode", choices=["sample", "verify"], default="verify")
    parser.add_argument("--uri", default=DEFAULT_MONGO_URI)
    parser.add_argument("--db", default=DEFAULT_DB_NAME)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--parquet", default=DEFAULT_PARQUET)
    args = parser.parse_args()

    if args.mode == "sample":
        seed_from_sample(args.uri, args.db, args.collection, args.parquet)
    elif args.mode == "verify":
        verify_database(args.uri, args.db, args.collection)


if __name__ == "__main__":
    main()
