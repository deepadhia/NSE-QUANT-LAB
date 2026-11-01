#!/usr/bin/env python3
"""
ab_regime_catchup.py -- Empirical A/B test of Macro Regime Catch-Up Rebalancing.
=============================================================================
Pre-registered investigation into eliminating Monthly Entry Latency:
  Arm 0: Baseline Champion (Monthly-only rebalance at month-end close).
  Arm 1: User's Rule (If month-end blocked by Bear, catch-up rebalance on first Bull flip in days 1-10).
  Arm 2: Pure Event Flip (Any day regime transitions Bear -> Bull, trigger catch-up rebalance).
  Arm 3: Filtered Flip (Regime transition with 2-day consecutive Bull confirmation).

All catch-up rebalances fill ONLY open cash slots (never dropping healthy existing holdings).
Evaluated across:
  - Full 20-Year (2007-01-01 to 2026-10-23)
  - W1 In-Sample (2007-01-01 to 2015-12-31)
  - W2 Validation (2016-01-01 to 2020-12-31)
  - W3 Holdout (2021-01-01 to 2026-10-23)
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pymongo import MongoClient

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.indicators import compute_indicators
from nse_quant_lab.data_loader import load_pit_universe
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ab_catchup")

OUT_DIR = REPO / "research" / "audit_2026-10" / "results" / "ab_regime_catchup"
CACHE_FILE = OUT_DIR / "universe_cache.pkl"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------------- Strategy Variant
class RegimeCatchupStrategy(TrendMomentumPITStrategy):
    def __init__(self, arm_name: str, benchmark_series: pd.Series, config: Any):
        super().__init__(config=config, benchmark=benchmark_series, use_regime=True)
        self.arm_name = arm_name
        self.bench_series = benchmark_series
        self.sma200 = benchmark_series.rolling(200, min_periods=200).mean()

    def get_rebalance_schedule(
        self,
        timeline: List[pd.Timestamp],
        eval_start: pd.Timestamp,
        eval_end: pd.Timestamp,
        config: Optional[Any] = None,
    ) -> Dict[pd.Timestamp, pd.Timestamp]:
        date_to_idx = {d: i for i, d in enumerate(timeline)}
        df_dates = pd.DataFrame({"date": timeline})
        df_dates["year_month"] = df_dates["date"].dt.to_period("M")
        month_last_sessions = df_dates.groupby("year_month")["date"].max().tolist()

        decision_to_exec_map: Dict[pd.Timestamp, pd.Timestamp] = {}

        # 1. Standard month-end decision dates
        for d_dec in month_last_sessions:
            if d_dec in date_to_idx:
                idx = date_to_idx[d_dec]
                if idx + 1 < len(timeline):
                    d_exec = timeline[idx + 1]
                    if eval_start <= d_exec <= eval_end:
                        decision_to_exec_map[d_dec] = d_exec

        def is_bull(d: pd.Timestamp) -> bool:
            if d not in self.bench_series.index or d not in self.sma200.index:
                return False
            px = self.bench_series.loc[d]
            sma = self.sma200.loc[d]
            return bool(not np.isnan(sma) and px >= sma)

        # 2. Arm-specific catch-up decision dates
        if self.arm_name == "arm1_first_10_days":
            for ym, grp in df_dates.groupby("year_month"):
                prev_ym = ym - 1
                prev_grp = df_dates[df_dates["year_month"] == prev_ym]
                if prev_grp.empty:
                    continue
                prev_me = prev_grp.iloc[-1]["date"]
                if not is_bull(prev_me):  # Previous month-end was Bear
                    first_10 = grp.head(10)["date"].tolist()
                    for d_catchup in first_10:
                        if is_bull(d_catchup):
                            idx = date_to_idx[d_catchup]
                            if idx + 1 < len(timeline):
                                d_exec = timeline[idx + 1]
                                if eval_start <= d_exec <= eval_end:
                                    decision_to_exec_map[d_catchup] = d_exec
                            break  # only one catch-up trigger per month

        elif self.arm_name == "arm2_pure_flip":
            for i in range(1, len(timeline)):
                d_prev = timeline[i - 1]
                d_curr = timeline[i]
                if not is_bull(d_prev) and is_bull(d_curr):
                    if d_curr not in decision_to_exec_map:
                        if i + 1 < len(timeline):
                            d_exec = timeline[i + 1]
                            if eval_start <= d_exec <= eval_end:
                                decision_to_exec_map[d_curr] = d_exec

        elif self.arm_name == "arm3_filtered_flip":
            for i in range(2, len(timeline)):
                d_prev2 = timeline[i - 2]
                d_prev = timeline[i - 1]
                d_curr = timeline[i]
                if not is_bull(d_prev2) and is_bull(d_prev) and is_bull(d_curr):
                    if d_curr not in decision_to_exec_map:
                        if i + 1 < len(timeline):
                            d_exec = timeline[i + 1]
                            if eval_start <= d_exec <= eval_end:
                                decision_to_exec_map[d_curr] = d_exec

        return {v: k for k, v in decision_to_exec_map.items()}


# ----------------------------------------------------------------------------- Data Preparation
def load_data() -> Tuple[Dict[str, pd.DataFrame], pd.Series]:
    # Benchmark
    col_bench = MongoClient("mongodb://localhost:27017/")["stock_scanner"]["benchmark_ohlcv"]
    b_df = pd.DataFrame(list(col_bench.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    b_df["date"] = pd.to_datetime(b_df["date"])
    bench = b_df.set_index("date")["close"].sort_index()

    if CACHE_FILE.exists():
        logger.info(f"Loading cached universe from {CACHE_FILE}...")
        with open(CACHE_FILE, "rb") as f:
            universe = pickle.load(f)
        return universe, bench

    logger.info("Building universe from clean_panel.parquet via load_pit_universe()...")
    t0 = time.time()
    raw_universe = load_pit_universe("2026-10-30", include_live=False)

    cfg = SimpleNamespace(min_history_sessions=0)
    universe = {}
    for sym, g in raw_universe.items():
        ind = compute_indicators(g.reset_index(drop=True), cfg)
        if not ind.empty:
            turnover = ind["close"] * ind["volume"]
            ind["adt50m"] = turnover.rolling(50, min_periods=50).median()
            ind["adt_prev"] = ind["adt50m"].shift(1)
            ind["sig_prev"] = ind["close"].pct_change().rolling(20, min_periods=10).std().shift(1)
            ind["series"] = g["series"] if "series" in g else "EQ"
            ind["hist_n"] = range(1, len(ind) + 1)
            universe[sym] = ind

    logger.info(f"Cached {len(universe)} symbols in {time.time() - t0:.2f}s")
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(universe, f)

    return universe, bench


# ----------------------------------------------------------------------------- Runner
def run_evaluation():
    universe, bench = load_data()

    windows = {
        "Full_2007_2026": ("2007-01-01", "2026-10-23"),
        "W1_2007_2015": ("2007-01-01", "2015-12-31"),
        "W2_2016_2020": ("2016-01-01", "2020-12-31"),
        "W3_2021_2026": ("2021-01-01", "2026-10-23"),
    }

    arms = [
        ("Arm0_Baseline_Monthly", "arm0_baseline"),
        ("Arm1_Catchup_First10D", "arm1_first_10_days"),
        ("Arm2_Pure_Regime_Flip", "arm2_pure_flip"),
        ("Arm3_Filtered_2Day_Flip", "arm3_filtered_flip"),
    ]

    results = []

    for arm_label, arm_key in arms:
        logger.info("=" * 70)
        logger.info(f"STARTING EVALUATION: {arm_label.upper()}")
        logger.info("=" * 70)

        for w_name, (start_dt, end_dt) in windows.items():
            logger.info(f"  Running {arm_label} on {w_name} ({start_dt} to {end_dt})...")

            cfg = BacktestConfig(
                initial_capital=100_000.0,
                portfolio_size=15,
                rebalance_hold_rank_buffer=1500,  # Champion HoldFix rule
                unaffordable_policy="TRY_NEXT",
                slippage_pct=0.002,
                fees_buffer_pct=0.002,
                gap_buffer_pct=0.015,
                deduct_annual_tax=True,  # Exact Indian post-tax calculation
                rebalance_frequency="MONTHLY",
            )

            strat = RegimeCatchupStrategy(arm_key, bench, cfg)
            engine = BacktestEngine(strategy=strat, config=cfg)

            m, trades, orders, execs, nav_df = engine.run(
                universe, eval_start_date=start_dt, eval_end_date=end_dt
            )

            # Metrics
            nav_s = nav_df["nav"]
            start_nav = nav_s.iloc[0]
            end_nav = nav_s.iloc[-1]
            n_days = (pd.to_datetime(end_dt) - pd.to_datetime(start_dt)).days
            cagr = ((end_nav / start_nav) ** (365.25 / n_days) - 1.0) * 100.0

            # Max Drawdown
            roll_max = nav_s.cummax()
            dd = (nav_s - roll_max) / roll_max
            max_dd = float(dd.min()) * 100.0

            # Daily Sharpe
            rets = nav_s.pct_change().dropna()
            sharpe = float((rets.mean() / rets.std()) * np.sqrt(252)) if rets.std() > 0 else 0.0

            # Trades metrics
            n_trades = len(trades)
            win_rate = (trades["net_pnl"] > 0).mean() * 100.0 if n_trades > 0 else 0.0
            gains = trades.loc[trades["net_pnl"] > 0, "net_pnl"].sum()
            losses = abs(trades.loc[trades["net_pnl"] < 0, "net_pnl"].sum())
            pf = gains / losses if losses > 0 else 0.0

            tot_friction = m.get("total_friction", 0.0)
            tot_tax = m.get("total_tax_paid", 0.0)

            res_entry = {
                "arm": arm_label,
                "window": w_name,
                "start": start_dt,
                "end": end_dt,
                "start_nav": round(start_nav, 2),
                "end_nav": round(end_nav, 2),
                "cagr_pct": round(cagr, 2),
                "max_dd_pct": round(max_dd, 2),
                "sharpe": round(sharpe, 2),
                "trades": n_trades,
                "win_rate_pct": round(win_rate, 2),
                "profit_factor": round(pf, 2),
                "friction_rs": round(tot_friction, 2),
                "tax_rs": round(tot_tax, 2),
            }
            results.append(res_entry)
            logger.info(
                f"    -> CAGR: {cagr:.2f}% | MaxDD: {max_dd:.2f}% | Sharpe: {sharpe:.2f} | "
                f"Trades: {n_trades} | PF: {pf:.2f} | End NAV: ₹{end_nav:,.0f}"
            )

    df_res = pd.DataFrame(results)
    out_csv = OUT_DIR / "ab_results_summary.csv"
    df_res.to_csv(out_csv, index=False)
    logger.info(f"Results saved to {out_csv}")
    print("\n" + "=" * 80)
    print("A/B EXPERIMENT SUMMARY (ALL ARMS ACROSS 20 YEARS)")
    print("=" * 80)
    print(df_res[["arm", "window", "cagr_pct", "max_dd_pct", "sharpe", "trades", "win_rate_pct", "profit_factor", "end_nav"]].to_string(index=False))


if __name__ == "__main__":
    run_evaluation()

