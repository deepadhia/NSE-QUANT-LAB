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
from datetime import datetime, timezone, timedelta
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.registry import get_strategy, list_strategies
from nse_quant_lab.data_loader import load_clean_universe, load_pit_universe
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.holidays import is_trading_day, execution_window_open, SPECIAL_NSE_SESSIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("morning_execution")

PIT_STRATEGY_IDS = ("trend_momentum_pit", "minervini_trend_template", "alpha_semivol_pit")


def execute_strategy_morning_fills(strat_doc: dict, universe: dict, exec_dt: pd.Timestamp, db: MongoDatabase, quote_provider=None) -> list:
    """Returns a list of problem strings (empty == clean). Unpriced orders are problems: the run must fail so the scheduler retries."""
    strategy_id = strat_doc["strategy_id"]
    is_pit = strategy_id in PIT_STRATEGY_IDS
    portfolio_size = int(strat_doc.get("portfolio_size", 15 if is_pit else 6))
    initial_capital = float(strat_doc.get("initial_capital", 100000.0))
    hold_buffer = int(strat_doc.get("rebalance_hold_rank_buffer", 1500 if is_pit else 15))
    unaffordable_pol = strat_doc.get("unaffordable_policy", "TRY_NEXT")
    exec_approach = strat_doc.get("execution_approach", "APPROACH_3")
    mode = strat_doc.get("mode", "paper")
    rebal_freq = strat_doc.get("rebalance_frequency", "MONTHLY")

    logger.info("-" * 65)
    logger.info(f"EXECUTING STRATEGY MORNING FILLS: {strategy_id.upper()} (Mode: {mode.upper()})")
    logger.info(f"   Config from DB: Capital=INR {initial_capital:,.2f} | Slots={portfolio_size} | Rebalance={rebal_freq} | Approach={exec_approach}")
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
    )
    strategy = get_strategy(strategy_id, cfg)
    manager = PaperTradingManager(strategy=strategy, config=cfg, db=db)
    if quote_provider is not None and strategy_id in PIT_STRATEGY_IDS:
        manager.quote_provider = quote_provider  # real 09:16 opening price + bid/ask logged to fill_audit

    res = manager.execute_morning_fills(universe, exec_dt)
    if res.get("status") in ("PAUSED", "ARCHIVED"):
        logger.warning(f"   Strategy '{strategy_id}' is {res['status']}. Skipping morning execution.")
        return []

    logger.info(f"   Orders filled:   {len(res.get('fills', []))}")
    logger.info(f"   Orders deferred: {len(res.get('deferred', []))}")
    logger.info(f"   Cash Balance:    INR {res.get('cash', 0):,.2f}")
    logger.info(f"   Holdings Value:  INR {res.get('holdings_val', 0):,.2f}")
    logger.info(f"   Total NAV:       INR {res.get('nav', 0):,.2f}")
    return [f"{strategy_id}: {q['symbol']} {q['reason']}" for q in res.get("no_quote", [])]


def main():
    parser = argparse.ArgumentParser(description="NSE-Quant-Lab Morning Execution Pipeline")
    parser.add_argument("--strategy", default="all", help=f"Strategy to run ('all' for all active DB strategies). Available: {list_strategies()}")
    parser.add_argument("--date", default=None, help="Execution date (YYYY-MM-DD). Defaults to latest in universe.")
    parser.add_argument("--force", action="store_true", help="Force run even on weekends or market holidays.")
    parser.add_argument("--special-session-only", action="store_true",
                        help="Run only on a verified special-session date (e.g. Muhurat); a no-op on regular days. Used by the afternoon cron entry.")
    args = parser.parse_args()

    if args.special_session_only and datetime.now(IST).strftime("%Y-%m-%d") not in SPECIAL_NSE_SESSIONS:
        logger.info("Not a special-session date; the 09:16 run covers regular days.")
        return

    if not args.date and not execution_window_open(datetime.now(IST)):
        logger.info("Outside the verified NSE continuous-trading execution window; no fills attempted.")
        return

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
        default_doc = {"strategy_id": args.strategy, "portfolio_size": 6, "initial_capital": 100000.0, "rebalance_hold_rank_buffer": 15, "unaffordable_policy": "TRY_NEXT", "mode": "paper"}
        if args.strategy in PIT_STRATEGY_IDS:  # researched setting: 15 slots, no rank-based drops (buffer = 100 x slots)
            default_doc.update(portfolio_size=15, rebalance_hold_rank_buffer=1500)
        strat_docs = [doc] if doc else [default_doc]

    logger.info(f">> Active strategies to process: {[d['strategy_id'] for d in strat_docs]}")

    # Short-circuit: if no orders are pending across active strategies, skip heavy universe load
    active_strategy_ids = [d["strategy_id"] for d in strat_docs]
    pending_orders_count = db.orders.count_documents({
        "strategy_id": {"$in": active_strategy_ids},
        "mode": "paper",
        "status": {"$in": ["PENDING_OPEN", "DEFERRED_LOWER_CIRCUIT"]},
    })
    if not args.date and pending_orders_count == 0:
        logger.info(f">> No pending or deferred orders found for active strategies: {active_strategy_ids}")
        logger.info("=" * 70)
        logger.info(">> Morning execution completed successfully: 0 orders to execute.")
        logger.info("=" * 70)
        sys.exit(0)

    # Load recent universe for execution
    logger.info(">> Loading recent market universe...")
    now_ist = datetime.now(IST)
    target_end = args.date or now_ist.strftime("%Y-%m-%d")
    pit_ids = [d["strategy_id"] for d in strat_docs if d["strategy_id"] in PIT_STRATEGY_IDS]
    if pit_ids and len(pit_ids) != len(strat_docs):
        raise SystemExit("PIT strategies need the full-market PIT universe; run them separately from other legacy strategies.")
    if pit_ids:
        # Morning run: today's bar does not exist yet, so load everything available (no end date); a dated replay (--date) is strict.
        universe = load_pit_universe(target_end if args.date else None)  # full market, split-adjusted, series-aware
    else:
        universe = load_clean_universe("2024-01-01", target_end)
    logger.info(f"   Loaded {len(universe)} stocks.")

    # Determine execution date
    all_dates = sorted(list(set(d for df in universe.values() for d in df["date"])))
    if args.date:
        exec_dt = pd.to_datetime(args.date)
    else:
        exec_dt = pd.to_datetime(now_ist.strftime("%Y-%m-%d"))

    logger.info(f">> Executing morning open fills for: {exec_dt.strftime('%Y-%m-%d')}")

    quote_provider = None
    if pit_ids and not args.date:  # live morning run (a dated --date run is a replay from the stored bars)
        try:
            from nse_quant_lab import upstox_feed as uf
            from nse_quant_lab.data_loader import auto_refresh_upstox_token

            token = auto_refresh_upstox_token()
            if token:
                p1 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "external", "nse_archive", "parquet")
                p2 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "external", "nse_archive", "parquet")
                arch = p1 if os.path.exists(p1) else p2
                isins = uf.symbol_isin_map(arch, [exec_dt.year - 1, exec_dt.year])
                quote_provider = uf.UpstoxQuoteProvider(token, isins)
                quote_provider.prefetch([o["symbol"] for o in db.orders.find({"strategy_id": {"$in": list(PIT_STRATEGY_IDS)}, "mode": "paper", "status": {"$in": ["PENDING_OPEN", "DEFERRED_LOWER_CIRCUIT"]}}, {"symbol": 1})])
                logger.info(">> Upstox quote provider attached for the 09:16 fills")
            else:
                logger.error(">> No Upstox token: orders cannot be priced (yfinance fallback is disabled unless ALLOW_YFINANCE_FALLBACK=true); the run will exit non-zero and retry.")
        except Exception as exc:
            logger.error(">> Could not attach the Upstox quote provider (%s); orders stay unpriced (yfinance fallback is disabled unless ALLOW_YFINANCE_FALLBACK=true).", exc)
            quote_provider = None

    problems = []
    for s_doc in strat_docs:
        try:
            problems.extend(execute_strategy_morning_fills(s_doc, universe, exec_dt, db, quote_provider))
        except Exception as e:
            logger.error(f"Error executing morning fills for {s_doc.get('strategy_id')}: {e}", exc_info=True)
            problems.append(f"{s_doc.get('strategy_id')}: EXCEPTION {e}")

    logger.info("=" * 70)
    if problems:
        # Fail loudly: the scheduler retries (fills are idempotent) and alerts after the final attempt.
        logger.error(">> Morning execution INCOMPLETE (%d problem(s)):", len(problems))
        for p in problems:
            logger.error("   - %s", p)
        logger.info("=" * 70)
        sys.exit(2)
    logger.info(">> Morning execution completed successfully.")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
