#!/usr/bin/env python3
"""
minervini_20y.py -- single pre-declared 20-year run of the PRODUCTION classes (no research copies) on the real clean panel.

Pre-registration (written before running; no tuning, one configuration per strategy, results saved whatever they are):
  * Strategies: MinerviniStrategy (new) and TrendMomentumPITStrategy (control, same harness, same data, same config).
  * Data: data_loader.load_pit_universe("2026-10-30", include_live=False)  (clean_panel.parquet, delisted names included).
  * Config mirrors candidate_backtests.py: Rs 1 L, 15 slots, rank buffer 1500, TRY_NEXT, dated Indian tax (india_fy), 10% volume cap,
    strategy-level tiered slippage; no engine-level liquidity/price gating (the strategies gate themselves).
  * Windows: FULL 2007-01-01..2026-10-30, DEV 2007-2015, VAL 2016-2020, LATE 2021-2026, Y10 2016-10-01..; benchmark = Nifty 500 price (^CRSLDX).
  * Outputs: results/minervini_20y/ (summary.csv, yearly.csv, nav_*.csv, trades_*.csv). Counts as 1 more config in the tried set.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from pymongo import MongoClient

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.data_loader import load_pit_universe
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.minervini import MinerviniStrategy
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy

OUT = REPO / "research" / "audit_2026-10" / "results" / "minervini_20y"
OUT.mkdir(parents=True, exist_ok=True)
START, END = "2007-01-01", "2026-10-30"
WINDOWS = {"FULL": (START, END), "DEV_2007_15": ("2007-01-01", "2015-12-31"), "VAL_2016_20": ("2016-01-01", "2020-12-31"),
           "LATE_2021_26": ("2021-01-01", END), "Y10_2016-10": ("2016-10-01", END)}


def bench_series() -> pd.Series:
    col = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)["stock_scanner"]["benchmark_ohlcv"]
    b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    b["date"] = pd.to_datetime(b["date"])
    return b.drop_duplicates("date", keep="last").sort_values("date").set_index("date")["close"]


def stats(s: pd.Series, a: str, b: str) -> dict:
    s = s[(s.index >= a) & (s.index <= b)]
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    r = s.pct_change().dropna()
    return {"cagr": ((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, "mdd": float((s / s.cummax() - 1).min()) * 100,
            "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0}


def run(cls, name, uni, bench):
    cfg = BacktestConfig(
        initial_capital=100_000.0, portfolio_size=15, max_open_positions=15, rebalance_hold_rank_buffer=1500,
        rebalance_frequency="MONTHLY", min_turnover_50d=0.0, min_price=0.0, slippage_pct=0.002, unaffordable_policy="TRY_NEXT",
        enforce_pit_universe=False, pit_top_n_liquidity=0, brokerage_per_order=0.0, dp_charge_sell=14.75,
        max_volume_participation_pct=0.10, deduct_annual_tax=True, tax_model="india_fy",
    )
    t0 = time.time()
    strat = cls(config=cfg, benchmark=bench)
    metrics, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(uni, eval_start_date=START, eval_end_date=END)
    nav["date"] = pd.to_datetime(nav["date"])
    s = nav.set_index("date")["nav"]
    s.to_csv(OUT / f"nav_{name}.csv")
    trades.to_csv(OUT / f"trades_{name}.csv", index=False)
    row = {"strategy": name, "secs": round(time.time() - t0), "trades": len(trades), "final_nav": round(float(s.iloc[-1])),
           "tax_paid": round(float(metrics.get("total_tax_paid", 0.0))), "friction": round(float(metrics.get("total_friction", 0.0))),
           "avg_hold": round(float(trades.hold_days.mean()), 1) if len(trades) else 0.0,
           "win_pct": round(float((trades.net_pnl > 0).mean() * 100), 1) if len(trades) else 0.0,
           "sum_net_pnl": round(float(trades.net_pnl.sum())), "n_REBALANCE_DROP": int((trades.exit_reason == "REBALANCE_DROP").sum())}
    for w, (a, b) in WINDOWS.items():
        st = stats(s, a, b); bs = stats(bench, a, b)
        row.update({f"{w}_cagr": round(st["cagr"], 2), f"{w}_bench": round(bs["cagr"], 2), f"{w}_mdd": round(st["mdd"], 1), f"{w}_sharpe": round(st["sharpe"], 2)})
    print(row, flush=True)
    return row, s.resample("YE").last().pct_change() * 100


def main():
    t0 = time.time()
    bench = bench_series()
    uni = load_pit_universe(END, include_live=False)
    print(f"universe {len(uni)} symbols in {time.time() - t0:.0f}s; benchmark {bench.index[0].date()}..{bench.index[-1].date()}", flush=True)
    rows, yearly = [], {}
    for cls, name in ((MinerviniStrategy, "minervini_trend_template"), (TrendMomentumPITStrategy, "trend_momentum_pit_control")):
        r, y = run(cls, name, uni, bench)
        rows.append(r); yearly[name] = y.round(1)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False)
    by = bench.resample("YE").last().pct_change() * 100
    pd.DataFrame({**yearly, "nifty500_price": by.round(1)}).to_csv(OUT / "yearly.csv")
    print("done", flush=True)


if __name__ == "__main__":
    main()
