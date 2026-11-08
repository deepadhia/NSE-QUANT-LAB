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


def init_strategy(strategy_id: str, capital: float, mode: str = "paper", max_entry_price: float = None, portfolio_size: int = None,
                  strategy_class: str = None, reinit: bool = False):
    """Create a paper ACCOUNT. `strategy_id` namespaces its orders/positions/cash; `strategy_class` (registry key, default = strategy_id) picks the logic,
    so several accounts of one class can run side by side at different capital / slots / price caps."""
    strategy_class = strategy_class or strategy_id
    cfg0 = PaperTradingConfig(strategy_id=strategy_id, mode=mode)
    existing = MongoDatabase.get_instance(cfg0.mongo_uri, cfg0.db_name).strategies.find_one({"strategy_id": strategy_id, "mode": mode})
    if existing and not reinit:
        # --init overwrites capital/cash/NAV on the account document: never do that to a live account by accident.
        print(f">> REFUSED: account '{strategy_id}' ({mode}) already exists (capital INR {existing.get('initial_capital')}, status {existing.get('status')}). "
              f"Use a NEW --strategy id with --strategy-class for a forward-test account, or pass --reinit to deliberately overwrite it.")
        sys.exit(2)
    is_pit = strategy_class in ("trend_momentum_pit", "minervini_trend_template", "alpha_semivol_pit")
    p_size = int(portfolio_size) if portfolio_size else (15 if is_pit else 6)
    h_buf = 1500 if is_pit else 15
    cfg = PaperTradingConfig(strategy_id=strategy_id, initial_capital=capital, mode=mode,
                             portfolio_size=p_size, rebalance_hold_rank_buffer=h_buf, unaffordable_policy="TRY_NEXT",
                             max_entry_price=max_entry_price)
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    strat = get_strategy(strategy_class, cfg)
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=db)
    mgr.initialize_portfolio()
    db.strategies.update_one(
        {"strategy_id": strategy_id, "mode": mode},
        {"$set": {
            "initial_capital": float(capital),
            "current_nav": float(capital),
            "cash": float(capital),
            "portfolio_size": p_size,
            "rebalance_hold_rank_buffer": h_buf,
            "unaffordable_policy": "TRY_NEXT",
            "max_entry_price": float(max_entry_price) if max_entry_price else None,
            "strategy_class": strategy_class,
            "updated_at": datetime.utcnow().isoformat(),
        }}
    )
    if db.positions.count_documents({"strategy_id": strategy_id, "mode": mode}) == 0:
        db.daily_nav.update_many(
            {"strategy_id": strategy_id, "mode": mode},
            {"$set": {"cash": float(capital), "nav": float(capital), "holdings_val": 0.0}}
        )
    print(f">> Strategy '{strategy_id}' initialized in MongoDB with capital: INR {capital:,.2f} (Status: ACTIVE)")




def show_positions(db: MongoDatabase, strategy_id: str = "trend_momentum", mode: str = "paper"):
    positions = list(db.positions.find({"strategy_id": strategy_id, "mode": mode}))
    print("=" * 105)
    print(f"ACTIVE POSITIONS & TRAILING STOPS: {strategy_id.upper()} (Mode: {mode.upper()})")
    print("=" * 105)
    if not positions:
        print("  No open positions currently held in portfolio.")
        print("=" * 105)
        return

    from tabulate import tabulate
    table_data = []
    for p in positions:
        sym = p["symbol"]
        sh = p["shares"]
        entry_px = p.get("entry_price", 0.0)
        c_px = p.get("last_close", entry_px)
        pnl = p.get("unrealized_pnl", 0.0)
        pnl_pct = ((c_px - entry_px) / entry_px * 100.0) if entry_px > 0 else 0.0
        stop_px = p.get("stop_loss_price", "N/A")
        buf = p.get("stop_buffer_pct")
        buf_str = f"{buf:+.1f}%" if buf is not None else "N/A"
        warn = " [NEAR STOP]" if buf is not None and buf <= 2.5 else ""
        table_data.append([
            sym, sh, f"₹{entry_px:,.2f}", f"₹{c_px:,.2f}",
            f"{pnl_pct:+.2f}%", f"₹{stop_px:,.2f}" if isinstance(stop_px, (int, float)) else stop_px,
            buf_str + warn, p.get("entry_date", "N/A")
        ])
    headers = ["Symbol", "Shares", "Entry Px", "Last Close", "Unrealized %", "Effective Stop", "Buffer %", "Entry Date"]
    print(tabulate(table_data, headers=headers, tablefmt="simple"))
    print("=" * 105)


def main():
    parser = argparse.ArgumentParser(description="Manage Strategy Status & Profiles in Database")
    parser.add_argument("--list", action="store_true", help="List all registered strategies and live metrics")
    parser.add_argument("--strategy", default=None, help=f"Target strategy ID. Available: {list_strategies()}")
    parser.add_argument("--status", choices=["ACTIVE", "PAUSED", "ARCHIVED"], default=None, help="Update strategy status")
    parser.add_argument("--init", action="store_true", help="Initialize a strategy portfolio in MongoDB")
    parser.add_argument("--capital", type=float, default=100_000.0, help="Initial capital when using --init")
    parser.add_argument("--max-entry-price", type=float, default=None, help="With --init: entry-only price cap in Rs (new entries must be priced below it; held names exempt). Omit = off.")
    parser.add_argument("--strategy-class", default=None, help="With --init: registry key of the strategy logic when --strategy is a NEW account id (e.g. --strategy tmpit_fwd50k --strategy-class trend_momentum_pit)")
    parser.add_argument("--reinit", action="store_true", help="With --init: allow overwriting an EXISTING account's capital/cash/NAV (destructive)")
    parser.add_argument("--slots", type=int, default=None, help="With --init: number of portfolio slots (default 15 for PIT strategies, 6 otherwise)")
    parser.add_argument("--mode", default="paper", choices=["paper", "real"], help="Execution mode")
    parser.add_argument("--positions", action="store_true", help="Display all active positions and dynamic trailing stop levels")
    args = parser.parse_args()

    cfg = PaperTradingConfig(strategy_id=args.strategy or "trend_momentum")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    if not db.ping():
        print(f">> Error: Cannot connect to MongoDB at {cfg.mongo_uri}. Please verify network connection or MONGO_URI in .env.")
        sys.exit(1)

    if args.list:
        list_all_strategies(db, args.mode)
    elif args.positions:
        show_positions(db, args.strategy or "trend_momentum", args.mode)
    elif args.init and args.strategy:
        init_strategy(args.strategy, args.capital, args.mode, args.max_entry_price, args.slots, args.strategy_class, args.reinit)
    elif args.status and args.strategy:
        set_strategy_status(db, args.strategy, args.status, args.mode)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
