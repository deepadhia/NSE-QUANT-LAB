"""
run_capital_matrix.py -- 9-Cell Capital Feasibility Matrix
=========================================================
Runs the strategy across 9 capital-slot combinations:
Capitals: 1 Lakh, 5 Lakh, 10 Lakh
Slots: 8, 12, 16
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.data_loader import load_clean_universe


def main():
    print("=" * 80)
    print("NSE-Quant-Lab: 9-Cell Capital Feasibility Matrix (2019 - 2026)")
    print("=" * 80)

    universe = load_clean_universe("2017-01-01", "2026-10-24")
    print(f">> Loaded {len(universe)} stocks.")

    capitals = [100_000.0, 500_000.0, 1_000_000.0]
    slots_list = [8, 12, 16]
    results = []

    for cap in capitals:
        for slots in slots_list:
            print(f"\n>> Running Cell: Capital = INR {cap:,.0f} | Slots = {slots} ...")
            cfg = BacktestConfig(initial_capital=cap, portfolio_size=slots)
            strat = TrendMomentumStrategy(cfg)
            engine = BacktestEngine(strategy=strat, config=cfg)

            metrics, trades, orders, execs, nav = engine.run(
                universe=universe,
                eval_start_date="2019-01-01",
                eval_end_date="2026-10-23",
            )

            res = {
                "Capital_INR": cap,
                "Slots": slots,
                "Ending_NAV": metrics["ending_nav"],
                "CAGR_Pct": metrics["cagr_trading_pct"],
                "Max_DD_Pct": metrics["max_drawdown_pct"],
                "Sharpe": metrics["sharpe_ratio"],
                "Closed_Trades": metrics["total_trades"],
                "Win_Rate_Pct": metrics["win_rate_pct"],
                "Profit_Factor": metrics["profit_factor"],
                "Total_Friction_INR": metrics["total_friction"],
            }
            results.append(res)
            print(f"   Ending NAV: INR {res['Ending_NAV']:,.2f} | CAGR: {res['CAGR_Pct']:.2f}% | Max DD: {res['Max_DD_Pct']:.2f}% | Trades: {res['Closed_Trades']}")

    df_res = pd.DataFrame(results)
    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results"))
    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(out_dir, "capital_matrix_results.csv")
    df_res.to_csv(out_csv, index=False)
    print(f"\n>> Matrix results saved to: {out_csv}")


if __name__ == "__main__":
    main()
