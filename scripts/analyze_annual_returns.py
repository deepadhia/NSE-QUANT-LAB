"""
analyze_annual_returns.py -- Honest Year-by-Year Performance & Reality Check
=============================================================================
Runs TrendMomentumStrategy through the hardened BacktestEngine (with delisting
forced liquidation, circuit threshold protections, and exact turnover sell fees)
and computes honest annual returns, drawdowns, and fee drag.
"""

import os
import sys
import pandas as pd
import numpy as np
from tabulate import tabulate

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.data_loader import load_clean_universe


def main():
    print("=" * 80)
    print("RUNNING HONEST MULTI-YEAR AUDIT: TREND MOMENTUM STRATEGY")
    print("=" * 80)

    cfg = BacktestConfig(initial_capital=1_000_000.0, portfolio_size=12)
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    universe = load_clean_universe("2017-01-01", "2026-10-23")
    print(f">> Loaded clean universe: {len(universe)} stocks.")

    metrics, trades, orders, execs, nav_df = engine.run(
        universe=universe,
        eval_start_date="2018-01-01",
        eval_end_date="2026-10-23",
        save_results=False,
    )

    print("\n" + "=" * 80)
    print("LIFETIME SUMMARY (2018 - 2026):")
    print(f"  Starting NAV:     INR {metrics['initial_capital']:,.2f}")
    print(f"  Ending NAV:       INR {metrics['ending_nav']:,.2f}")
    print(f"  Total Return:     {metrics['total_return_pct']:.2f}%")
    print(f"  CAGR (Trading):   {metrics['cagr_pct']:.2f}%")
    print(f"  Max Drawdown:     {metrics['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe Ratio:     {metrics['sharpe_ratio']:.2f}")
    print(f"  Profit Factor:    {metrics['profit_factor']:.2f}")
    print(f"  Win Rate:         {metrics['win_rate_pct']:.2f}% ({metrics['total_trades']} trades)")
    print(f"  Total Friction:   INR {metrics['total_friction']:,.2f}")
    print("=" * 80)

    # Compute year-by-year breakdown
    nav_df["year"] = pd.to_datetime(nav_df["date"]).dt.year
    annual_stats = []

    years = sorted(nav_df["year"].unique())
    prev_year_end_nav = metrics["initial_capital"]

    for yr in years:
        sub = nav_df[nav_df["year"] == yr].sort_values("date")
        if sub.empty:
            continue
        start_nav = prev_year_end_nav
        end_nav = float(sub.iloc[-1]["nav"])
        yr_return = (end_nav / start_nav - 1.0) * 100.0

        # Peak-to-trough max drawdown within this specific calendar year
        cummax_yr = sub["nav"].cummax()
        dd_series = (sub["nav"] - cummax_yr) / cummax_yr * 100.0
        yr_max_dd = float(dd_series.min())

        # Trades closed in this year
        yr_trades = 0
        yr_win_rate = 0.0
        yr_pf = 0.0
        if not trades.empty:
            trades["exit_year"] = pd.to_datetime(trades["exit_date"]).dt.year
            t_sub = trades[trades["exit_year"] == yr]
            yr_trades = len(t_sub)
            if yr_trades > 0:
                wins = t_sub[t_sub["net_pnl"] > 0]
                yr_win_rate = len(wins) / yr_trades * 100.0
                net_wins = t_sub[t_sub["net_pnl"] > 0]["net_pnl"].sum()
                net_losses = abs(t_sub[t_sub["net_pnl"] < 0]["net_pnl"].sum())
                yr_pf = net_wins / net_losses if net_losses > 0 else (999.0 if net_wins > 0 else 0.0)

        annual_stats.append({
            "Year": yr,
            "Start NAV (INR)": f"{start_nav:,.0f}",
            "End NAV (INR)": f"{end_nav:,.0f}",
            "Return (%)": f"{yr_return:+.2f}%",
            "Max DD (%)": f"{yr_max_dd:.2f}%",
            "Closed Trades": yr_trades,
            "Win Rate (%)": f"{yr_win_rate:.1f}%" if yr_trades > 0 else "N/A",
            "Profit Factor": f"{yr_pf:.2f}" if yr_trades > 0 else "N/A",
        })
        prev_year_end_nav = end_nav

    print("\n" + "=" * 80)
    print("YEAR-BY-YEAR ANNUAL BREAKDOWN:")
    print("=" * 80)
    print(tabulate(annual_stats, headers="keys", tablefmt="pipe", showindex=False))
    print("=" * 80)


if __name__ == "__main__":
    main()
