"""
reproduce_baseline.py -- Reproduces Baseline 2019-2026 (INR 10L, 12 Slots)
"""

import sys
import os
import json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.data_loader import load_clean_universe


def main():
    print("=" * 70)
    print("REPRODUCING REPORTED BASELINE: 2019-01-01 to 2026-10-23 (INR 10L, 12 Slots)")
    print("=" * 70)

    cfg = BacktestConfig(initial_capital=1000000.0, portfolio_size=12)
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    universe = load_clean_universe("2017-01-01", "2026-10-24")
    print(f">> Loaded {len(universe)} stocks.")

    if len(universe) < 200:
        target_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results", "sample_run"))
        print(f">> NOTE: Universe contains {len(universe)} symbols (< 200).")
        print(f"   Saving output to '{target_dir}' to preserve the authoritative 500-stock baseline in results/baseline_reproduced/.")
    else:
        target_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results", "baseline_reproduced"))

    metrics, trades, orders, execs, nav = engine.run(
        universe=universe,
        eval_start_date="2019-01-01",
        eval_end_date="2026-10-23",
        save_results=True,
        output_dir=target_dir,
    )

    print("\n" + "=" * 70)
    print("BASELINE REPRODUCTION SUMMARY:")
    print(f"  Starting Capital: INR {metrics['initial_capital']:,.2f}")
    print(f"  Ending NAV:       INR {metrics['ending_nav']:,.2f}")
    print(f"  CAGR (Trading):   {metrics['cagr_pct']:.2f}% (Reported: 28.04%)")
    print(f"  CAGR (Calendar):  {metrics['cagr_calendar_pct']:.2f}% (Reported: 27.79%)")
    print(f"  Max Drawdown:     {metrics['max_drawdown_pct']:.2f}% (Reported: -23.00%)")
    print(f"  Sharpe Ratio:     {metrics['sharpe_ratio']:.2f} (Reported: 1.10)")
    print(f"  Closed Trades:    {metrics['total_trades']} (Reported: 628)")
    print(f"  Profit Factor:    {metrics['profit_factor']:.2f}")
    print(f"  Total Friction:   INR {metrics['total_friction']:,.2f}")
    print("=" * 70)


if __name__ == "__main__":
    main()
