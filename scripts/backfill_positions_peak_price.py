"""
backfill_positions_peak_price.py
================================
One-time / maintenance script to backfill `peak_price` (highest intraday high since entry)
for all active positions in MongoDB `positions` collection from `upstox_fulltier_daily`.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "src"))

from nse_quant_lab.db.mongo_client import MongoDatabase


def backfill_peak_prices():
    db = MongoDatabase().db
    positions = list(db.positions.find())
    print(f"Total open positions to examine: {len(positions)}")

    updated_count = 0
    for p in positions:
        sym = p["symbol"]
        entry_date = str(p.get("entry_date", ""))[:10]
        entry_px = float(
            p.get("entry_fill_price")
            or p.get("entry_price")
            or p.get("last_close")
            or 0.0
        )
        last_close = float(p.get("last_close") or entry_px)

        bars = list(
            db.upstox_fulltier_daily.find({
                "symbol": sym,
                "date": {"$gte": entry_date},
            })
        )
        high_values = [
            float(b.get("high") or b.get("close") or 0.0)
            for b in bars
            if (b.get("high") or b.get("close"))
        ]

        if high_values:
            peak = max(entry_px, last_close, max(high_values))
        else:
            peak = max(entry_px, last_close)

        peak = round(peak, 2)
        db.positions.update_one(
            {
                "strategy_id": p["strategy_id"],
                "mode": p.get("mode", "paper"),
                "symbol": sym,
            },
            {"$set": {"peak_price": peak}},
        )
        updated_count += 1

    print(f"Successfully backfilled peak_price for {updated_count} positions.")


if __name__ == "__main__":
    backfill_peak_prices()
