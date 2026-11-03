#!/usr/bin/env python3
"""
open_positions_check.py -- re-runs the production-class control (same config as minervini_20y.py) ONLY to keep the execution
ledger and daily NAV frame, so the positions still open on 2026-10-30 (and their unrealised gains) can be listed and their price
histories checked for data / corporate-action errors. No parameter changes; not a new strategy trial.
Outputs: results/minervini_20y/execs_<name>.csv, navfull_<name>.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import minervini_20y as m  # noqa: E402  (reuses its config; main() is not called on import)
from nse_quant_lab.config import BacktestConfig  # noqa: E402
from nse_quant_lab.data_loader import load_pit_universe  # noqa: E402
from nse_quant_lab.engine import BacktestEngine  # noqa: E402
from nse_quant_lab.strategies.minervini import MinerviniStrategy  # noqa: E402
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy  # noqa: E402


def main(which: str) -> None:
    cls, name = {"control": (TrendMomentumPITStrategy, "trend_momentum_pit_control"),
                 "minervini": (MinerviniStrategy, "minervini_trend_template")}[which]
    bench = m.bench_series()
    uni = load_pit_universe(m.END, include_live=False)
    cfg = BacktestConfig(
        initial_capital=100_000.0, portfolio_size=15, max_open_positions=15, rebalance_hold_rank_buffer=1500,
        rebalance_frequency="MONTHLY", min_turnover_50d=0.0, min_price=0.0, slippage_pct=0.002, unaffordable_policy="TRY_NEXT",
        enforce_pit_universe=False, pit_top_n_liquidity=0, brokerage_per_order=0.0, dp_charge_sell=14.75,
        max_volume_participation_pct=0.10, deduct_annual_tax=True, tax_model="india_fy",
    )
    strat = cls(config=cfg, benchmark=bench)
    metrics, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(uni, eval_start_date=m.START, eval_end_date=m.END)
    execs.to_csv(m.OUT / f"execs_{name}.csv", index=False)
    nav.to_csv(m.OUT / f"navfull_{name}.csv", index=False)
    print("final nav", float(nav["nav"].iloc[-1]), "trades", len(trades), "exec cols", list(execs.columns), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "control")
