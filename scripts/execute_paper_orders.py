"""
execute_paper_orders.py -- Morning Execution Pipeline for Cloud / VPS Deployment
================================================================================
Designed to run via daily cron (e.g. 09:16 IST on Oracle Cloud server):
1. Ingests today's opening market candles.
2. Identifies all ACTIVE strategies registered in the database (or a targeted strategy).
3. Reads strategy configurations (capital, portfolio size, rebalance frequency) from DB.
4. Executes pending exits and entries with circuit breaker and gap limit checks.
5. Updates cash, positions, trades, executions, and daily_nav collections in MongoDB.
6. Broadcasts execution confirmations to Telegram.
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
from nse_quant_lab.holidays import is_trading_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("morning_execution")


def execute_strategy_morning_fills(strat_doc: dict, universe: dict, exec_dt: pd.Timestamp, db: MongoDatabase):
    strategy_id = strat_doc["strategy_id"]
    portfolio_size = int(strat_doc.get("portfolio_size", 6))
    initial_capital = float(strat_doc.get("initial_capital", 100000.0))
    hold_buffer = int(strat_doc.get("rebalance_hold_rank_buffer", 15))
    unaffordable_pol = strat_doc.get("unaffordable_policy", "TRY_NEXT")
    mode = strat_doc.get("mode", "paper")
    rebal_freq = strat_doc.get("rebalance_frequency", "MONTHLY")

    logger.info("-" * 65)
    logger.info(f"EXECUTING STRATEGY MORNING FILLS: {strategy_id.upper()} (Mode: {mode.upper()})")
    logger.info(f"   Config from DB: Capital=INR {initial_capital:,.2f} | Slots={portfolio_size} | Rebalance={rebal_freq}")
    logger.info("-" * 65)

    cfg = PaperTradingConfig(
        strategy_id=strategy_id,
        portfolio_size=portfolio_size,
        initial_capital=initial_capital,
        rebalance_hold_rank_buffer=hold_buffer,
        unaffordable_policy=unaffordable_pol,
        brokerage_per_order=0.0,
        dp_charge_sell=14.75,
        mode=mode,
        rebalance_frequency=rebal_freq,
    )
    strategy = get_strategy(strategy_id, cfg)
    manager = PaperTradingManager(strategy=strategy, config=cfg, db=db)

    res = manager.execute_morning_fills(universe, exec_dt)
    if res.get("status") in ("PAUSED", "ARCHIVED"):
        logger.warning(f"   Strategy '{strategy_id}' is {res['status']}. Skipping morning execution.")
        return

    logger.info(f"   Orders filled:   {len(res.get('fills', []))}")
    logger.info(f"   Orders deferred: {len(res.get('deferred', []))}")
    logger.info(f"   Cash Balance:    INR {res.get('cash', 0):,.2f}")
    logger.info(f"   Holdings Value:  INR {res.get('holdings_val', 0):,.2f}")
    logger.info(f"   Total NAV:       INR {res.get('nav', 0):,.2f}")


def main():
    parser = argparse.ArgumentParser(description="NSE-Quant-Lab Morning Execution Pipeline")
    parser.add_argument("--strategy", default="all", help=f"Strategy to run ('all' for all active DB strategies). Available: {list_strategies()}")
    parser.add_argument("--date", default=None, help="Execution date (YYYY-MM-DD). Defaults to latest in universe.")
    parser.add_argument("--force", action="store_true", help="Force run even on weekends or market holidays.")
    args = parser.parse_args()

    cfg = PaperTradingConfig(strategy_id="system")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    if not db.ping():
        logger.error(f"Cannot connect to MongoDB at {cfg.mongo_uri}. Exiting.")
        sys.exit(1)

    logger.info("=" * 70)
    logger.info("STARTING MORNING EXECUTION PIPELINE (09:16 IST)")
    if not args.force and not args.date and not is_trading_day():
        logger.info("Today is a weekend or official NSE holiday. Skipping morning open fills.")
        sys.exit(0)
    logger.info("=" * 70)

    # Determine strategies to run directly from MongoDB documents
    if args.strategy.lower() == "all":
        strat_docs = list(db.strategies.find({"status": "ACTIVE"}))
        if not strat_docs:
            logger.info("No active strategies found in DB. Defaulting to 'trend_momentum'...")
            strat_docs = [{"strategy_id": "trend_momentum", "portfolio_size": 6, "initial_capital": 100000.0, "rebalance_hold_rank_buffer": 15, "unaffordable_policy": "TRY_NEXT", "mode": "paper"}]
    else:
        doc = db.strategies.find_one({"strategy_id": args.strategy})
        strat_docs = [doc] if doc else [{"strategy_id": args.strategy, "portfolio_size": 6, "initial_capital": 100000.0, "rebalance_hold_rank_buffer": 15, "unaffordable_policy": "TRY_NEXT", "mode": "paper"}]

    logger.info(f">> Active strategies to process: {[d['strategy_id'] for d in strat_docs]}")

    # Load recent universe for execution
    logger.info(">> Loading recent market universe...")
    target_end = args.date or datetime.now().strftime("%Y-%m-%d")
    universe = load_clean_universe("2024-01-01", target_end)
    logger.info(f"   Loaded {len(universe)} stocks.")

    # Determine execution date
    all_dates = sorted(list(set(d for df in universe.values() for d in df["date"])))
    if args.date:
        exec_dt = pd.to_datetime(args.date)
    else:
        exec_dt = pd.to_datetime(datetime.now().strftime("%Y-%m-%d"))

    logger.info(f">> Executing morning open fills for: {exec_dt.strftime('%Y-%m-%d')}")

    for s_doc in strat_docs:
        try:
            execute_strategy_morning_fills(s_doc, universe, exec_dt, db)
        except Exception as e:
            logger.error(f"Error executing morning fills for {s_doc.get('strategy_id')}: {e}", exc_info=True)

    logger.info("=" * 70)
    logger.info(">> Morning execution completed successfully.")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
