"""
run_daily_pipeline.py -- Daily Post-Market Automation Pipeline for Cloud / VPS Deployment
========================================================================================
Designed to run via daily cron (e.g. 15:45 IST on Oracle Cloud server):
1. Loads recent market universe from MongoDB.
2. Identifies all ACTIVE strategies registered in the database (or a targeted strategy).
3. Evaluates daily exits and scheduled rebalances.
4. Logs orders to MongoDB 'orders' collection with mode='paper' and status='PENDING_OPEN'.
5. Broadcasts formatted alerts to Telegram.
"""

import os
import sys
import argparse
import logging
from datetime import datetime
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.registry import get_strategy, list_strategies
from nse_quant_lab.data_loader import load_clean_universe
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.db.mongo_client import MongoDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("daily_pipeline")


def run_strategy_pipeline(strategy_id: str, universe: dict, decision_dt: pd.Timestamp, db: MongoDatabase):
    logger.info("-" * 65)
    logger.info(f"PROCESSING STRATEGY: {strategy_id.upper()}")
    logger.info("-" * 65)

    cfg = PaperTradingConfig(strategy_id=strategy_id)
    strategy = get_strategy(strategy_id, cfg)
    manager = PaperTradingManager(strategy=strategy, config=cfg, db=db)
    manager.initialize_portfolio()

    res = manager.generate_post_market_signals(universe, decision_dt)
    if res.get("status") in ("PAUSED", "ARCHIVED"):
        logger.warning(f"   Strategy '{strategy_id}' is {res['status']}. Skipping signal generation.")
        return

    logger.info(f"   Exits generated: {len(res.get('exits', []))}")
    logger.info(f"   Buys generated:  {len(res.get('buys', []))}")


def main():
    parser = argparse.ArgumentParser(description="NSE-Quant-Lab Daily Automation Pipeline")
    parser.add_argument("--strategy", default="all", help=f"Strategy to run ('all' for all active DB strategies). Available: {list_strategies()}")
    parser.add_argument("--date", default=None, help="Decision date (YYYY-MM-DD). Defaults to latest in universe.")
    args = parser.parse_args()

    cfg = PaperTradingConfig(strategy_id="system")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    if not db.ping():
        logger.error(f"Cannot connect to MongoDB at {cfg.mongo_uri}. Exiting.")
        sys.exit(1)

    logger.info("=" * 70)
    logger.info("STARTING DAILY POST-MARKET PIPELINE (15:45 IST)")
    logger.info("=" * 70)

    # Determine strategies to run
    if args.strategy.lower() == "all":
        active_docs = list(db.strategies.find({"mode": "paper", "status": "ACTIVE"}))
        if active_docs:
            strat_ids = [d["strategy_id"] for d in active_docs]
        else:
            strat_ids = ["trend_momentum"]
    else:
        strat_ids = [args.strategy]

    logger.info(f">> Active strategies to process: {strat_ids}")

    # Load recent universe for signal evaluation
    logger.info(">> Loading recent market universe...")
    universe = load_clean_universe("2024-01-01", "2026-10-24")
    logger.info(f"   Loaded {len(universe)} stocks.")

    # Determine decision date
    all_dates = sorted(list(set(d for df in universe.values() for d in df["date"])))
    if args.date:
        decision_dt = pd.to_datetime(args.date)
    else:
        decision_dt = all_dates[-1]

    logger.info(f">> Evaluating post-market signals for decision date: {decision_dt.strftime('%Y-%m-%d')}")

    for s_id in strat_ids:
        try:
            run_strategy_pipeline(s_id, universe, decision_dt, db)
        except Exception as e:
            logger.error(f"Error processing strategy {s_id}: {e}", exc_info=True)

    logger.info("=" * 70)
    logger.info(">> Daily post-market pipeline completed successfully.")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
