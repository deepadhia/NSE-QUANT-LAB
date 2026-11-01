"""
run_daily_pipeline.py -- Daily Post-Market Automation Pipeline for Cloud / VPS Deployment
========================================================================================
Designed to run via daily cron (e.g. 18:00 IST / 6:00 PM on Oracle Cloud server):
1. Loads recent market universe from MongoDB.
2. Identifies all ACTIVE strategies registered in the database (or a targeted strategy).
3. Reads strategy configurations (capital, portfolio size, rebalance frequency) from DB.
4. Evaluates daily exits and scheduled rebalances.
5. Logs orders to MongoDB 'orders' collection with mode='paper' and status='PENDING_OPEN'.
6. Broadcasts formatted alerts to Telegram.
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
from nse_quant_lab.data_loader import load_clean_universe, load_pit_universe
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.holidays import is_trading_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("daily_pipeline")


def run_strategy_pipeline(strat_doc: dict, universe: dict, decision_dt: pd.Timestamp, db: MongoDatabase):
    strategy_id = strat_doc["strategy_id"]
    portfolio_size = int(strat_doc.get("portfolio_size", 6))
    initial_capital = float(strat_doc.get("initial_capital", 100000.0))
    hold_buffer = int(strat_doc.get("rebalance_hold_rank_buffer", 15))
    unaffordable_pol = strat_doc.get("unaffordable_policy", "TRY_NEXT")
    mode = strat_doc.get("mode", "paper")
    rebal_freq = strat_doc.get("rebalance_frequency", "MONTHLY")

    logger.info("-" * 65)
    logger.info(f"PROCESSING STRATEGY: {strategy_id.upper()} (Mode: {mode.upper()})")
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
    parser.add_argument("--force", action="store_true", help="Force run even on weekends or market holidays.")
    args = parser.parse_args()

    cfg = PaperTradingConfig(strategy_id="system")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    if not db.ping():
        logger.error(f"Cannot connect to MongoDB at {cfg.mongo_uri}. Exiting.")
        sys.exit(1)

    logger.info("=" * 70)
    logger.info("STARTING DAILY POST-MARKET PIPELINE (15:45 IST)")
    if not args.force and not args.date and not is_trading_day():
        logger.info("Today is a weekend or official NSE holiday. Skipping daily signal scan.")
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
        default_doc = {"strategy_id": args.strategy, "portfolio_size": 6, "initial_capital": 100000.0, "rebalance_hold_rank_buffer": 15, "unaffordable_policy": "TRY_NEXT", "mode": "paper"}
        if args.strategy in ("trend_momentum_pit", "minervini_trend_template"):  # researched setting: 15 slots, no rank-based drops (buffer = 100 x slots)
            default_doc.update(portfolio_size=15, rebalance_hold_rank_buffer=1500)
        strat_docs = [doc] if doc else [default_doc]

    logger.info(f">> Active strategies to process: {[d['strategy_id'] for d in strat_docs]}")

    # Load recent universe for signal evaluation
    logger.info(">> Loading recent market universe...")
    target_end = args.date or datetime.now().strftime("%Y-%m-%d")
    pit_strategy_ids = {"trend_momentum_pit", "minervini_trend_template"}
    pit_ids = [d["strategy_id"] for d in strat_docs if d["strategy_id"] in pit_strategy_ids]
    if pit_ids and len(pit_ids) != len(strat_docs):
        raise SystemExit("PIT strategies need the full-market PIT universe; run them separately from other legacy strategies.")
    if pit_ids:
        universe = load_pit_universe(target_end)   # full market, split-adjusted, series-aware (same data as the research)
    else:
        universe = load_clean_universe("2024-01-01", target_end, active_only=True)
    logger.info(f"   Loaded {len(universe)} stocks.")

    # Determine decision date
    all_dates = sorted(list(set(d for df in universe.values() for d in df["date"])))
    if args.date:
        decision_dt = pd.to_datetime(args.date)
    else:
        decision_dt = all_dates[-1]

    logger.info(f">> Evaluating post-market signals for decision date: {decision_dt.strftime('%Y-%m-%d')}")

    # Benchmark Recency Gate / Alert (Nifty 500 ^CRSLDX)
    try:
        bench_doc = db.db["benchmark_ohlcv"].find_one({"symbol": "^CRSLDX"}, sort=[("date", -1)])
        if bench_doc:
            max_bench_dt = pd.to_datetime(bench_doc["date"])
            logger.info(f">> Latest Nifty 500 benchmark (^CRSLDX) in DB: {max_bench_dt.strftime('%Y-%m-%d')}")
            if max_bench_dt < decision_dt:
                logger.warning(
                    f"[CRITICAL BENCHMARK ALERT] Benchmark (^CRSLDX) is STALE! "
                    f"Latest benchmark date in DB: {max_bench_dt.strftime('%Y-%m-%d')} < Decision date: {decision_dt.strftime('%Y-%m-%d')}. "
                    f"Regime filter in 'trend_momentum_pit' will fail safe and BLOCK ALL NEW ENTRIES until refreshed! "
                    f"Run `python scripts/ingest_benchmarks.py` to refresh benchmark candles."
                )
        else:
            logger.warning("[CRITICAL BENCHMARK ALERT] No records for ^CRSLDX found in 'benchmark_ohlcv' collection!")
    except Exception as e:
        logger.warning(f"Failed to check benchmark recency: {e}")

    for s_doc in strat_docs:
        try:
            run_strategy_pipeline(s_doc, universe, decision_dt, db)
        except Exception as e:
            logger.error(f"Error processing strategy {s_doc.get('strategy_id')}: {e}", exc_info=True)

    logger.info("=" * 70)
    logger.info(">> Daily post-market pipeline completed successfully.")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
