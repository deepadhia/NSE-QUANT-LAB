#!/usr/bin/env python3
"""
sync_corporate_actions.py -- Corporate Actions Manager & Sync CLI
================================================================
Fetches official upcoming and recent corporate actions (splits, bonuses, consolidations)
from NSE, parses their share-multiplier ratios, and stores them in MongoDB.

Usage:
  1. Sync upcoming 30 days from NSE:
     python scripts/sync_corporate_actions.py

  2. Sync custom window:
     python scripts/sync_corporate_actions.py --days-back 30 --days-ahead 60

  3. List recorded actions in MongoDB:
     python scripts/sync_corporate_actions.py --list

  4. Seed historical actions from external parquet archive:
     python scripts/sync_corporate_actions.py --seed-historical

  5. Manually insert or override an action:
     python scripts/sync_corporate_actions.py --add --symbol TCS --ex-date 2026-11-20 --ratio 2.0 --action-type split
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root and src to path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.corporate_actions import NSECorporateActionClient, sync_corporate_actions

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_corporate_actions")


def list_actions(db: MongoDatabase, limit: int = 50):
    coll = db.get_collection("corporate_actions")
    actions = list(coll.find().sort("ex_date", -1).limit(limit))
    print("=" * 85)
    print(f"RECORDED CORPORATE ACTIONS IN MONGODB (Showing up to {limit})")
    print("=" * 85)
    if not actions:
        print("  No corporate actions recorded in MongoDB. Run with --sync or --seed-historical.")
        print("=" * 85)
        return

    print(f"{'SYMBOL':<12} {'EX-DATE':<12} {'TYPE':<15} {'RATIO':<10} {'DETAILS'}")
    print("-" * 85)
    for a in actions:
        sym = a.get("symbol", "")
        ex_d = str(a.get("ex_date", ""))[:10]
        a_type = a.get("action_type", "")
        ratio = a.get("ratio", 1.0)
        details = a.get("details") or a.get("description", "")
        print(f"{sym:<12} {ex_d:<12} {a_type:<15} {ratio:<10.4f} {details[:35]}")
    print("=" * 85)


def seed_historical_parquet(db: MongoDatabase):
    """Loads historical verified actions from external/nse_archive/all_corporate_actions.parquet into MongoDB."""
    parquet_path = REPO_ROOT / "external" / "nse_archive" / "all_corporate_actions.parquet"
    if not parquet_path.exists():
        logger.error("Historical parquet file not found at: %s", parquet_path)
        return

    import pandas as pd
    df = pd.read_parquet(parquet_path)
    coll = db.get_collection("corporate_actions")
    coll.create_index([("symbol", 1), ("ex_date", 1), ("action_type", 1)], unique=True)

    upserted = 0
    for _, row in df.iterrows():
        sym = str(row["symbol"]).strip().upper()
        ex_d = str(pd.to_datetime(row["ex_date"]).strftime("%Y-%m-%d"))
        kind = str(row["kind"]).strip().lower()
        price_factor = float(row["price_factor"])
        if price_factor <= 0:
            continue
        # price_factor is price multiplier; ratio is share multiplier (1 / price_factor)
        ratio = round(1.0 / price_factor, 6)
        subj = str(row.get("subject", ""))

        action_type = "split" if kind == "split" else ("bonus" if kind == "bonus" else kind)
        doc = {
            "symbol": sym,
            "ex_date": ex_d,
            "action_type": action_type,
            "ratio": ratio,
            "description": subj,
            "details": f"Historical {action_type} factor {price_factor:g}",
            "source": "HISTORICAL_PARQUET",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        coll.update_one(
            {"symbol": sym, "ex_date": ex_d, "action_type": action_type},
            {"$set": doc},
            upsert=True
        )
        upserted += 1

    logger.info("Successfully seeded %d historical corporate actions from parquet into MongoDB.", upserted)


def add_manual_action(db: MongoDatabase, symbol: str, ex_date: str, ratio: float, action_type: str, details: str):
    coll = db.get_collection("corporate_actions")
    coll.create_index([("symbol", 1), ("ex_date", 1), ("action_type", 1)], unique=True)
    sym = symbol.strip().upper()
    doc = {
        "symbol": sym,
        "ex_date": ex_date,
        "action_type": action_type.strip().lower(),
        "ratio": float(ratio),
        "description": details or f"Manual {action_type} entry",
        "details": details or f"Manual {action_type} entry",
        "source": "MANUAL_OVERRIDE",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    coll.update_one(
        {"symbol": sym, "ex_date": ex_date, "action_type": doc["action_type"]},
        {"$set": doc},
        upsert=True
    )
    logger.info("Successfully recorded manual action: %s ex %s x%.4f (%s)", sym, ex_date, ratio, action_type)


def main():
    parser = argparse.ArgumentParser(description="NSE Corporate Actions Synchronizer & Database Manager")
    parser.add_argument("--sync", action="store_true", default=True, help="Sync upcoming/recent actions from NSE (default)")
    parser.add_argument("--days-back", type=int, default=7, help="Days in past to query NSE (default: 7)")
    parser.add_argument("--days-ahead", type=int, default=30, help="Days ahead to query NSE (default: 30)")
    parser.add_argument("--list", action="store_true", help="List recorded corporate actions in MongoDB")
    parser.add_argument("--seed-historical", action="store_true", help="Seed historical actions from external parquet archive")
    parser.add_argument("--add", action="store_true", help="Manually insert or override a corporate action")
    parser.add_argument("--symbol", type=str, help="Stock symbol for manual entry (e.g. TCS)")
    parser.add_argument("--ex-date", type=str, help="Ex-date in YYYY-MM-DD format for manual entry")
    parser.add_argument("--ratio", type=float, help="Share multiplier ratio for manual entry (e.g. 2.0 for 1:2 split)")
    parser.add_argument("--action-type", type=str, default="split", choices=["split", "bonus", "consolidation"], help="Action type")
    parser.add_argument("--details", type=str, default="", help="Description notes for manual entry")
    args = parser.parse_args()

    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB", "stock_scanner")
    db = MongoDatabase.get_instance(mongo_uri, db_name)

    if args.add:
        if not args.symbol or not args.ex_date or not args.ratio:
            logger.error("Manual action requires --symbol, --ex-date (YYYY-MM-DD), and --ratio.")
            sys.exit(1)
        add_manual_action(db, args.symbol, args.ex_date, args.ratio, args.action_type, args.details)
        return

    if args.seed_historical:
        seed_historical_parquet(db)
        return

    if args.list:
        list_actions(db)
        return

    # Default: sync
    logger.info("Connecting to NSE to sync corporate actions (-%dd to +%dd)...", args.days_back, args.days_ahead)
    stats = sync_corporate_actions(db, days_back=args.days_back, days_ahead=args.days_ahead)
    logger.info("Sync complete: %d records fetched, %d equity actions parsed, %d upserted in MongoDB.",
                stats["fetched"], stats["parsed"], stats["upserted"])


if __name__ == "__main__":
    main()

