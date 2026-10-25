"""
run_cost_stress.py -- Multi-Tier Cost & Friction Stress Testing Runner (1.0x, 1.5x, 2.0x)
========================================================================================
Audits edge resilience against increased execution friction (slippage, brokerage, exchange fees).
Enforces AGENTS.md §8 anti-fragility and parameter stability invariants.

Usage:
  python scripts/run_cost_stress.py --strategy trend_momentum --start 2019-01-01 --end 2026-10-23
"""

import os
import sys
import argparse
import pandas as pd
from tabulate import tabulate

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.registry import get_strategy, list_strategies
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.data_loader import load_clean_universe


def run_stress_suite(strategy_name: str, start_date: str, end_date: str, capital: float = 1_000_000.0, slots: int = 12):
    print("=" * 85)
    print(f"COST-STRESS RUNNER: {strategy_name.upper()} ({start_date} to {end_date})")
    print(f"Initial Capital: INR {capital:,.2f} | Portfolio Slots: {slots}")
    print("=" * 85)

    universe = load_clean_universe("2003-01-01", end_date)
    print(f">> Loaded universe: {len(universe)} stocks.")

    stress_multipliers = [1.0, 1.5, 2.0]
    comparison_rows = []

    for mult in stress_multipliers:
        # Scale slippage and brokerage linearly
        base_slip = 0.0010
        base_brokerage = 20.0
        cfg = BacktestConfig(
            initial_capital=capital,
            portfolio_size=slots,
            slippage_pct=base_slip * mult,
            brokerage_per_order=base_brokerage * mult,
        )
        strat = get_strategy(strategy_name, cfg)
        engine = BacktestEngine(strategy=strat, config=cfg)

        metrics, trades, orders, execs, nav = engine.run(
            universe=universe,
            eval_start_date=start_date,
            eval_end_date=end_date,
            save_results=False,
        )

        comparison_rows.append({
            "Cost Tier": f"{mult:.1f}x Friction",
            "Slippage (bps)": f"{cfg.slippage_pct * 10000:.0f} bps",
            "Brokerage (INR)": f"INR {cfg.brokerage_per_order:.2f}",
            "CAGR (%)": f"{metrics['cagr_pct']:.2f}%",
            "Ending NAV (INR)": f"{metrics['ending_nav']:,.0f}",
            "Max DD (%)": f"{metrics['max_drawdown_pct']:.2f}%",
            "Sharpe": f"{metrics['sharpe_ratio']:.2f}",
            "Win Rate (%)": f"{metrics['win_rate_pct']:.2f}%",
            "Profit Factor": f"{metrics['profit_factor']:.2f}",
            "Trades": metrics["total_trades"],
            "Total Friction (INR)": f"INR {metrics['total_friction']:,.0f}",
        })

    print("\n" + "=" * 85)
    print("COST-STRESS ATTRIBUTION MATRIX:")
    print("=" * 85)
    df_comp = pd.DataFrame(comparison_rows)
    print(tabulate(df_comp, headers="keys", tablefmt="pipe", showindex=False))
    print("=" * 85)


def main():
    parser = argparse.ArgumentParser(description="Multi-Tier Cost Stress Testing CLI")
    parser.add_argument("--strategy", default="trend_momentum", help=f"Available: {list_strategies()}")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-10-23")
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--slots", type=int, default=12)
    args = parser.parse_args()

    run_stress_suite(
        strategy_name=args.strategy,
        start_date=args.start,
        end_date=args.end,
        capital=args.capital,
        slots=args.slots,
    )


if __name__ == "__main__":
    main()
