"""
manage_strategy.py -- CLI Strategy Controller & Dashboard State Manager
=======================================================================
Commands:
  1. List all strategies:
     python scripts/manage_strategy.py --list
  2. Pause a strategy (cron safely skips it):
     python scripts/manage_strategy.py --strategy trend_momentum --status PAUSED
  3. Resume/Activate a strategy:
     python scripts/manage_strategy.py --strategy trend_momentum --status ACTIVE
  4. Initialize a new strategy in MongoDB:
     python scripts/manage_strategy.py --strategy donchian_breakout --init --capital 500000
"""

import os
import sys
import argparse
from datetime import datetime
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.registry import get_strategy, list_strategies
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.db.mongo_client import MongoDatabase


def list_all_strategies(db: MongoDatabase, mode: str = "paper"):
    strats = list(db.strategies.find({"mode": mode}))
    print("=" * 85)
    print(f"REGISTERED STRATEGIES IN DATABASE (Mode: {mode.upper()})")
    print("=" * 85)
    if not strats:
        print("  No strategies registered yet. Run with --init to register a strategy.")
        print("=" * 85)
        return

    for s in strats:
        status_badge = "ACTIVE" if s.get("status") == "ACTIVE" else ("PAUSED" if s.get("status") == "PAUSED" else "ARCHIVED")
        ret = s.get("total_return_pct", 0.0)
        ret_sign = "+" if ret >= 0 else ""
        print(f"  [{status_badge}] {s.get('name', s['strategy_id'])} (ID: {s['strategy_id']})")
        print(f"     NAV: INR {s.get('current_nav', 0):,.2f} | Return: {ret_sign}{ret:.2f}% | Win Rate: {s.get('win_rate_pct', 0):.1f}%")
        print(f"     Open Positions: {s.get('open_positions_count', 0)} | Closed Trades: {s.get('closed_trades_count', 0)} | Rebalance: {s.get('rebalance_frequency', 'N/A')}")
        print(f"     Last Run: {s.get('last_run_at', 'Never')} | Last Execution: {s.get('last_execution_date', 'None')}")
        print("-" * 85)


def set_strategy_status(db: MongoDatabase, strategy_id: str, status: str, mode: str = "paper"):
    res = db.strategies.update_one(
        {"strategy_id": strategy_id, "mode": mode},
        {"$set": {"status": status, "updated_at": datetime.utcnow().isoformat()}}
    )
    if res.matched_count > 0:
        print(f">> Strategy '{strategy_id}' status successfully updated to: {status}")
    else:
        print(f">> Warning: Strategy '{strategy_id}' not found in MongoDB. Use --init first.")


def init_strategy(strategy_id: str, capital: float, mode: str = "paper"):
    cfg = PaperTradingConfig(strategy_id=strategy_id, initial_capital=capital, mode=mode)
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    strat = get_strategy(strategy_id, cfg)
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=db)
    mgr.initialize_portfolio()
    print(f">> Strategy '{strategy_id}' initialized in MongoDB with capital: INR {capital:,.2f} (Status: ACTIVE)")


def main():
    parser = argparse.ArgumentParser(description="Manage Strategy Status & Profiles in Database")
    parser.add_argument("--list", action="store_true", help="List all registered strategies and live metrics")
    parser.add_argument("--strategy", default=None, help=f"Target strategy ID. Available: {list_strategies()}")
    parser.add_argument("--status", choices=["ACTIVE", "PAUSED", "ARCHIVED"], default=None, help="Update strategy status")
    parser.add_argument("--init", action="store_true", help="Initialize a strategy portfolio in MongoDB")
    parser.add_argument("--capital", type=float, default=1_000_000.0, help="Initial capital when using --init")
    parser.add_argument("--mode", default="paper", choices=["paper", "real"], help="Execution mode")
    args = parser.parse_args()

    cfg = PaperTradingConfig(strategy_id=args.strategy or "trend_momentum")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)

    if args.list:
        list_all_strategies(db, args.mode)
    elif args.init and args.strategy:
        init_strategy(args.strategy, args.capital, args.mode)
    elif args.status and args.strategy:
        set_strategy_status(db, args.strategy, args.status, args.mode)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
