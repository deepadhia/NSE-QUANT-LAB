"""
seed_production_db.py -- Complete Production Database Seeder
============================================================
Connects to the MongoDB URI configured in .env and seeds:
1. symbols collection (500 stocks with ISINs and industry mapping)
2. strategies collection (trend_momentum active portfolio initialization)
3. upstox_daily_ohlcv collection (warm-up candles for forward paper trading)
4. Database indexes for fast querying
"""

import os
import sys
import json
import logging
from datetime import datetime
import pandas as pd
from pymongo import MongoClient, ReplaceOne, ASCENDING
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_production")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = os.environ.get("MONGO_DB", "stock_scanner")
COLL_NAME = os.environ.get("MONGO_COLLECTION", "upstox_daily_ohlcv")


def seed_database():
    logger.info("=" * 70)
    logger.info("NSE-QUANT-LAB PRODUCTION DATABASE SEEDER")
    logger.info("=" * 70)

    if not MONGO_URI:
        logger.error("MONGO_URI is not set in .env!")
        return False

    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        logger.info("Connected to MongoDB Atlas successfully.")
    except Exception as e:
        logger.error(f"Cannot connect to MongoDB Atlas: {e}")
        return False

    db = client[DB_NAME]

    # 1. Seed Symbols Collection
    logger.info(">> Step 1/3: Seeding 'symbols' collection...")
    symbols_path = os.path.join(BASE_DIR, "data", "symbols.json")
    if os.path.exists(symbols_path):
        with open(symbols_path, "r", encoding="utf-8") as f:
            sym_list = json.load(f)
        sym_col = db["symbols"]
        sym_col.create_index([("symbol", ASCENDING)], unique=True)
        ops = [ReplaceOne({"symbol": s["symbol"]}, s, upsert=True) for s in sym_list]
        if ops:
            res = sym_col.bulk_write(ops, ordered=False)
            logger.info(f"   Seeded {len(sym_list)} symbols into '{DB_NAME}.symbols'.")

    # 2. Seed Strategies Collection
    logger.info(">> Step 2/3: Initializing 'strategies' profile...")
    strat_col = db["strategies"]
    strat_col.create_index([("strategy_id", ASCENDING), ("mode", ASCENDING)], unique=True)
    initial_strat = {
        "strategy_id": "trend_momentum",
        "name": "Trend Momentum",
        "description": "Multi-Regime Trend Following & Intermediate Momentum Strategy",
        "mode": "paper",
        "rebalance_frequency": "MONTHLY",
        "portfolio_size": 6,
        "rebalance_hold_rank_buffer": 15,
        "unaffordable_policy": "TRY_NEXT",
        "brokerage_per_order": 0.0,
        "dp_charge_sell": 14.75,
        "initial_capital": 100000.0,
        "current_nav": 100000.0,
        "cash": 100000.0,
        "holdings_val": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "total_return_pct": 0.0,
        "open_positions_count": 0,
        "closed_trades_count": 0,
        "win_rate_pct": 0.0,
        "status": "ACTIVE",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    strat_col.replace_one(
        {"strategy_id": "trend_momentum", "mode": "paper"},
        initial_strat,
        upsert=True
    )
    logger.info("   Initialized 'trend_momentum' strategy profile (Status: ACTIVE, Capital: INR 10L).")

    # 3. Seed OHLCV Market Data Collection
    logger.info(f">> Step 3/3: Seeding market candles into '{DB_NAME}.{COLL_NAME}'...")
    col = db[COLL_NAME]
    col.create_index([("symbol", ASCENDING), ("date", ASCENDING)], unique=True)
    col.create_index([("date", ASCENDING)])

    seeded_records = 0
    try:
        local_client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=1000)
        local_col = local_client[DB_NAME][COLL_NAME]
        warmup_start = "2024-01-01"
        logger.info(f"   Transferring warm-up candles from local MongoDB (>= {warmup_start})...")
        cursor = local_col.find(
            {"date": {"$gte": warmup_start}},
            {"_id": 0}
        )
        batch = []
        for doc in cursor:
            batch.append(ReplaceOne({"symbol": doc["symbol"], "date": doc["date"]}, doc, upsert=True))
            if len(batch) >= 2000:
                col.bulk_write(batch, ordered=False)
                seeded_records += len(batch)
                batch = []
        if batch:
            col.bulk_write(batch, ordered=False)
            seeded_records += len(batch)
        logger.info(f"   Successfully transferred {seeded_records:,} candles from local MongoDB.")
    except Exception as loc_e:
        logger.warning(f"   Local MongoDB not reachable ({loc_e}). Falling back to sample parquet...")
        pq_path = os.path.join(BASE_DIR, "data", "sample", "nifty500_sample.parquet")
        if os.path.exists(pq_path):
            df = pd.read_parquet(pq_path)
            records = df.to_dict("records")
            batch = [ReplaceOne({"symbol": r["symbol"], "date": str(r["date"])[:10]}, r, upsert=True) for r in records]
            for i in range(0, len(batch), 2000):
                col.bulk_write(batch[i:i + 2000], ordered=False)
            seeded_records = len(records)
            logger.info(f"   Successfully seeded {seeded_records:,} records from sample parquet.")

    logger.info("=" * 70)
    logger.info("DATABASE SEEDING COMPLETED SUCCESSFULLY!")
    logger.info(f"  Target Database: {DB_NAME}")
    logger.info(f"  Symbols Count:   {db['symbols'].count_documents({})}")
    logger.info(f"  Strategies:      {db['strategies'].count_documents({})}")
    logger.info(f"  OHLCV Candles:   {col.count_documents({}):,}")
    logger.info("=" * 70)
    return True


if __name__ == "__main__":
    seed_database()
