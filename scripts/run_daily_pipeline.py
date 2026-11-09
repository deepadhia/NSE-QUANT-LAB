"""
run_daily_pipeline.py -- Daily Post-Market Automation Pipeline for Cloud / VPS Deployment
========================================================================================
Designed to run via daily cron (e.g. 18:00 IST / 6:00 PM on Oracle Cloud server):
1. Syncs upcoming official corporate actions (splits, bonuses) from NSE.
2. Loads recent market universe from MongoDB.
3. Identifies all ACTIVE strategies registered in the database (or a targeted strategy).
4. Reads strategy configurations (capital, portfolio size, rebalance frequency) from DB.
5. Evaluates daily exits and scheduled rebalances.
6. Logs orders to MongoDB 'orders' collection with mode='paper' and status='PENDING_OPEN'.
7. Broadcasts formatted alerts to Telegram.
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timezone, timedelta
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.registry import get_strategy, list_strategies, strategy_class_of
from nse_quant_lab.data_loader import load_clean_universe, load_pit_universe
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.holidays import is_trading_day
from nse_quant_lab.corporate_actions import sync_corporate_actions

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("daily_pipeline")


def run_strategy_pipeline(strat_doc: dict, universe: dict, decision_dt: pd.Timestamp, db: MongoDatabase) -> list:
    """Returns a list of problem strings (empty == clean)."""
    strategy_id = strat_doc["strategy_id"]
    strategy_class = strategy_class_of(strat_doc)
    is_pit = strategy_class in ("trend_momentum_pit", "minervini_trend_template", "alpha_semivol_pit")
    portfolio_size = int(strat_doc.get("portfolio_size", 15 if is_pit else 6))
    initial_capital = float(strat_doc.get("initial_capital", 100000.0))
    hold_buffer = int(strat_doc.get("rebalance_hold_rank_buffer", 1500 if is_pit else 15))
    unaffordable_pol = strat_doc.get("unaffordable_policy", "TRY_NEXT")
    exec_approach = strat_doc.get("execution_approach", "APPROACH_3")
    mode = strat_doc.get("mode", "paper")
    rebal_freq = strat_doc.get("rebalance_frequency", "MONTHLY")
    max_entry_price = strat_doc.get("max_entry_price")          # entry-only price cap (Rs); None/absent = off

    logger.info("-" * 65)
    logger.info(f"PROCESSING STRATEGY: {strategy_id.upper()} (Mode: {mode.upper()})")
    logger.info(f"   Config from DB: Capital=INR {initial_capital:,.2f} | Slots={portfolio_size} | Rebalance={rebal_freq} | Approach={exec_approach} | PriceCap={max_entry_price or 'off'}")
    logger.info("-" * 65)

    cfg = PaperTradingConfig(
        strategy_id=strategy_id,
        portfolio_size=portfolio_size,
        initial_capital=initial_capital,
        rebalance_hold_rank_buffer=hold_buffer,
        unaffordable_policy=unaffordable_pol,
        execution_approach=exec_approach,
        brokerage_per_order=0.0,
        dp_charge_sell=14.75,
        mode=mode,
        rebalance_frequency=rebal_freq,
        max_entry_price=float(max_entry_price) if max_entry_price else None,
    )
    strategy = get_strategy(strategy_class, cfg)
    manager = PaperTradingManager(strategy=strategy, config=cfg, db=db)
    manager.initialize_portfolio()

    res = manager.generate_post_market_signals(universe, decision_dt)
    if res.get("status") == "REFUSED_STALE_DECISION_DATE":
        # Must be checked on its own: it is not PAUSED/ARCHIVED, and a refusal is a failure (exit non-zero), not a clean skip.
        logger.error(f"   Strategy '{strategy_id}': {res.get('message')}")
        return [f"{strategy_id}: {res['status']}"]
    if res.get("status") in ("PAUSED", "ARCHIVED"):
        logger.warning(f"   Strategy '{strategy_id}' is {res['status']}. Skipping signal generation.")
        return []

    logger.info(f"   Exits generated: {len(res.get('exits', []))}")
    logger.info(f"   Buys generated:  {len(res.get('buys', []))}")
    problems = []
    if res.get("missing_held_bars"):
        logger.critical(f"   [MISSING_HELD_BAR] Strategy '{strategy_id}' held position(s) unpriced: {res['missing_held_bars']}")
        problems.append(f"{strategy_id}: MISSING_HELD_BAR for {res['missing_held_bars']}")
    return problems


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
        if args.strategy in ("trend_momentum_pit", "minervini_trend_template", "alpha_semivol_pit"):  # researched setting: 15 slots, no rank-based drops (buffer = 100 x slots)
            default_doc.update(portfolio_size=15, rebalance_hold_rank_buffer=1500)
        strat_docs = [doc] if doc else [default_doc]

    logger.info(f">> Active strategies to process: {[d['strategy_id'] for d in strat_docs]}")

    # 1. Sync upcoming corporate actions from NSE (splits & bonuses)
    try:
        logger.info(">> Syncing upcoming corporate actions from NSE...")
        ca_stats = sync_corporate_actions(db, days_back=7, days_ahead=30)
        logger.info(f"   Corporate actions: {ca_stats['fetched']} fetched from NSE, {ca_stats['upserted']} new/updated in DB.")
        if ca_stats.get("unparsed"):
            logger.error(f"   [CORP_ACTION_UNPARSED] {ca_stats['unparsed']} announcement(s) need manual entry - see log above.")
    except Exception as e:
        logger.critical(f"[CORP_ACTION_SYNC_FAILED] Failed to sync corporate actions from NSE: {e}")
        try:
            from nse_quant_lab.notifications.telegram import TelegramNotifier
            TelegramNotifier().send_message(f"🚨 CRITICAL: Corporate actions sync from NSE failed: {e}")
        except Exception:
            pass

    # 2. Load recent universe for signal evaluation
    logger.info(">> Loading recent market universe...")
    now_ist = datetime.now(IST)
    target_end = args.date or now_ist.strftime("%Y-%m-%d")
    pit_strategy_ids = {"trend_momentum_pit", "minervini_trend_template", "alpha_semivol_pit"}
    pit_ids = [d["strategy_id"] for d in strat_docs if strategy_class_of(d) in pit_strategy_ids]
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
        today_dt = pd.to_datetime(now_ist.strftime("%Y-%m-%d"))
        if not args.force and is_trading_day(today_dt.strftime("%Y-%m-%d")) and now_ist.hour >= 16:
            if decision_dt < today_dt:
                logger.error(
                    f"[STALE UNIVERSE GATE] Today {today_dt.strftime('%Y-%m-%d')} is an NSE trading session that has completed, "
                    f"but loaded universe only contains bars up to {decision_dt.strftime('%Y-%m-%d')}! "
                    f"Halting pipeline to prevent generating signals on stale prices."
                )
                sys.exit(3)

    logger.info(f">> Evaluating post-market signals for decision date: {decision_dt.strftime('%Y-%m-%d')}")

    # Benchmark gate (Nifty 500 ^CRSLDX). The regime filter fails SAFE (blocks new entries) when the benchmark lacks the decision date, which is
    # silent capital drag AND loses an Arm-2 Bear->Bull crossing. Strategies still run (stop exits do not need the benchmark), but the run is
    # recorded as a problem so it exits non-zero and the scheduler retries after `ingest_benchmarks.py` (same-date reruns are idempotent).
    bench_problem = None
    try:
        decision_str = decision_dt.strftime("%Y-%m-%d")
        bench_doc = db.db["benchmark_ohlcv"].find_one({"symbol": "^CRSLDX", "date": decision_str})
        if bench_doc:
            logger.info(f">> Nifty 500 benchmark (^CRSLDX) has the decision date {decision_str}.")
        else:
            latest = db.db["benchmark_ohlcv"].find_one({"symbol": "^CRSLDX"}, sort=[("date", -1)])
            bench_problem = (f"BENCHMARK ^CRSLDX has no bar for decision date {decision_str} (latest: {latest['date'] if latest else 'none'}); "
                             f"regime filter is blocking new entries. Run `python scripts/ingest_benchmarks.py`.")
            logger.error("[CRITICAL BENCHMARK ALERT] %s", bench_problem)
    except Exception as e:
        bench_problem = f"BENCHMARK check failed: {e}"
        logger.error("[CRITICAL BENCHMARK ALERT] %s", bench_problem, exc_info=True)

    problems = [bench_problem] if bench_problem else []
    for s_doc in strat_docs:
        try:
            problems.extend(run_strategy_pipeline(s_doc, universe, decision_dt, db))
        except Exception as e:
            logger.error(f"Error processing strategy {s_doc.get('strategy_id')}: {e}", exc_info=True)
            problems.append(f"{s_doc.get('strategy_id')}: EXCEPTION {e}")

    logger.info("=" * 70)
    if problems:
        logger.error(">> Daily pipeline INCOMPLETE (%d problem(s)): %s", len(problems), "; ".join(problems))
        logger.info("=" * 70)
        sys.exit(4)
    logger.info(">> Daily post-market pipeline completed successfully.")
    logger.info("=" * 70)

    # Persist daily portfolio & slippage audit snapshot in MongoDB (Zero-Telegram)
    try:
        # `python scripts/run_daily_pipeline.py` puts scripts/ (not the repo root) first on sys.path, so
        # `from scripts.audit_daily_portfolio import ...` raised ModuleNotFoundError and the audit never ran.
        _repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if _repo_root not in sys.path:
            sys.path.insert(0, _repo_root)
        from scripts.audit_daily_portfolio import run_portfolio_audit
        run_portfolio_audit(decision_dt.strftime("%Y-%m-%d"))
    except Exception:
        # The snapshot is observability, not trading state: do not fail the run, but log the full trace at ERROR.
        logger.exception("Could not persist daily portfolio audit snapshot")


if __name__ == "__main__":
    main()
