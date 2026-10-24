"""
run_backtest.py -- Universal Backtesting CLI Runner with Cryptographic Proof
=============================================================================
Usage:
  python scripts/run_backtest.py --strategy km_momentum --start 2019-01-01 --end 2026-10-23
"""

import os
import sys
import json
import argparse
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.registry import get_strategy, list_strategies
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.data_loader import load_clean_universe


def main():
    parser = argparse.ArgumentParser(description="Universal Backtesting Engine CLI")
    parser.add_argument("--strategy", default="trend_momentum", help=f"Available: {list_strategies()}")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-10-23")
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--slots", type=int, default=12)
    parser.add_argument("--save", action="store_true", default=True)
    args = parser.parse_args()

    print("=" * 75)
    print(f"RUNNING BACKTEST: {args.strategy.upper()} ({args.start} to {args.end})")
    print(f"Capital: INR {args.capital:,.2f} | Slots: {args.slots}")
    print("=" * 75)

    cfg = BacktestConfig(initial_capital=args.capital, portfolio_size=args.slots)
    strat = get_strategy(args.strategy, cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    universe = load_clean_universe("2017-01-01", args.end)
    print(f">> Loaded {len(universe)} stocks.")

    metrics, trades, orders, execs, nav = engine.run(
        universe=universe,
        eval_start_date=args.start,
        eval_end_date=args.end,
        save_results=args.save,
    )

    print("\n" + "=" * 75)
    print("BACKTEST RESULTS SUMMARY:")
    print(f"  Ending NAV:       INR {metrics['ending_nav']:,.2f}")
    print(f"  CAGR (Trading):   {metrics['cagr_pct']:.2f}%")
    print(f"  CAGR (Calendar):  {metrics['cagr_calendar_pct']:.2f}%")
    print(f"  Max Drawdown:     {metrics['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe Ratio:     {metrics['sharpe_ratio']:.2f}")
    print(f"  Closed Trades:    {metrics['total_trades']}")
    print(f"  Win Rate:         {metrics['win_rate_pct']:.2f}%")
    print(f"  Profit Factor:    {metrics['profit_factor']:.2f}")
    print(f"  Total Friction:   INR {metrics['total_friction']:,.2f}")
    print("=" * 75)


if __name__ == "__main__":
    main()
