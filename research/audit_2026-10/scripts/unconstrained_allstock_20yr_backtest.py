#!/usr/bin/env python3
"""
Unconstrained All-Stock 20-Year Point-in-Time Backtest (2006 Warmup, 2007-2026 Evaluation)
Strategy: TrendMomentumStrategy
Initial Capital: Rs 1,00,000 (Rs 1 Lakh)
Universe: Unconstrained Market-Wide NSE Common Equities (EQ, BE, BZ, SM)
Filters: Price >= Rs 10, 50-day Turnover >= Rs 25 Lakhs
Zero Index Inclusion Constraints (Irrespective of Nifty 50, 100, 500)
"""

from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import logging
import os
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

# Setup repository paths
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.indicators import compute_indicators
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("unconstrained_20yr")

RESULTS_DIR = REPO_ROOT / "research" / "audit_2026-10" / "results" / "unconstrained_allstock_20yr"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = REPO_ROOT / "data" / "external" / "nse_archive" / "universe_20yr_ind.pkl"


def load_universe() -> Dict[str, pd.DataFrame]:
    logger.info(f"Loading precomputed universe from {CACHE_PATH}...")
    with open(CACHE_PATH, "rb") as f:
        u = pickle.load(f)
    logger.info(f"Loaded {len(u):,} symbols with precomputed indicators.")
    return u


def load_benchmark_nifty500() -> pd.Series:
    from pymongo import MongoClient

    c = MongoClient("mongodb://localhost:27017/")
    b = pd.DataFrame(
        list(
            c["stock_scanner"]["benchmark_ohlcv"].find(
                {"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1}
            )
        )
    )
    b["date"] = pd.to_datetime(b["date"])
    return b.set_index("date")["close"].sort_index()


def calculate_period_stats(
    nav_df: pd.DataFrame,
    benchmark_s: pd.Series,
    start_date: str,
    end_date: str,
) -> Dict[str, Any]:
    sub_nav = nav_df.loc[start_date:end_date, "nav"]
    sub_bench = benchmark_s.loc[start_date:end_date]

    if sub_nav.empty or len(sub_nav) < 5:
        return {}

    years = (sub_nav.index[-1] - sub_nav.index[0]).days / 365.25
    if years <= 0:
        return {}

    start_val = float(sub_nav.iloc[0])
    end_val = float(sub_nav.iloc[-1])
    strat_cagr = ((end_val / start_val) ** (1.0 / years) - 1.0) * 100.0

    b_start = float(sub_bench.iloc[0])
    b_end = float(sub_bench.iloc[-1])
    bench_cagr = ((b_end / b_start) ** (1.0 / years) - 1.0) * 100.0
    alpha_cagr = strat_cagr - bench_cagr

    cum_max = sub_nav.cummax()
    dd = (sub_nav - cum_max) / cum_max
    strat_mdd = float(dd.min()) * 100.0

    b_cum_max = sub_bench.cummax()
    b_dd = (sub_bench - b_cum_max) / b_cum_max
    bench_mdd = float(b_dd.min()) * 100.0

    m_strat = sub_nav.resample("ME").last().pct_change().dropna()
    m_bench = sub_bench.resample("ME").last().pct_change().dropna()
    common_idx = m_strat.index.intersection(m_bench.index)
    excess = m_strat.loc[common_idx] - m_bench.loc[common_idx]

    info_ratio = (excess.mean() / excess.std() * np.sqrt(12)) if excess.std() > 0 else 0.0
    t_stat = (excess.mean() / (excess.std() / np.sqrt(len(excess)))) if excess.std() > 0 else 0.0

    return {
        "start": start_date,
        "end": end_date,
        "years": round(years, 2),
        "cagr_pct": round(strat_cagr, 2),
        "n500_cagr_pct": round(bench_cagr, 2),
        "alpha_cagr_pct": round(alpha_cagr, 2),
        "max_drawdown_pct": round(strat_mdd, 2),
        "n500_max_drawdown_pct": round(bench_mdd, 2),
        "annual_vol_pct": round(m_strat.std() * np.sqrt(12) * 100.0, 2),
        "info_ratio": round(info_ratio, 3),
        "excess_t_stat": round(t_stat, 2),
        "final_nav": round(end_val, 2),
    }


def no_skill_baseline(
    universe: Dict[str, pd.DataFrame],
    start_date: str,
    end_date: str,
    min_turnover: float = 2.5e6,
    min_price: float = 10.0,
) -> Dict[str, Any]:
    """
    Equal-weight all eligible unconstrained stocks monthly (open->open), gross.
    Eligibility: turnover_50d >= min_turnover (Rs 25L), close >= min_price (Rs 10).
    """
    logger.info(f"Computing Unconstrained No-Skill Equal-Weight Baseline for {start_date} -> {end_date}...")
    panel = pd.concat([d[["symbol", "date", "open", "close", "turnover_50d"]] for d in universe.values()])
    op = panel.pivot(index="date", columns="symbol", values="open")
    cl = panel.pivot(index="date", columns="symbol", values="close")
    to = panel.pivot(index="date", columns="symbol", values="turnover_50d")

    dates = cl.index
    mls = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max().tolist()
    returns_last_price = []
    returns_dead_to_zero = []
    last_date = {s: d["date"].max() for s, d in universe.items()}
    avg_stock_counts = []

    for i in range(len(mls) - 1):
        d = mls[i]
        ie, ix = dates.get_loc(d) + 1, dates.get_loc(mls[i + 1]) + 1
        if ix >= len(dates) or dates[ie] < pd.Timestamp(start_date) or dates[ix] > pd.Timestamp(end_date):
            continue
        t = to.loc[d].dropna()
        t = t[t >= min_turnover]
        elig = [s for s in t.index if cl.loc[d, s] >= min_price and s in op.columns]
        if not elig:
            continue

        avg_stock_counts.append(len(elig))
        o_in = op.loc[dates[ie], elig]
        o_out = op.loc[dates[ix], elig].fillna(cl.loc[: dates[ix], elig].ffill().iloc[-1])
        r = (o_out / o_in - 1).dropna()
        died = [s for s in r.index if last_date[s] < dates[ix]]

        returns_last_price.append(r.mean())
        returns_dead_to_zero.append(r.where(~r.index.isin(died), -1.0).mean())

    yrs = len(returns_last_price) / 12.0
    cagr_last = round((np.prod(1.0 + np.array(returns_last_price)) ** (1.0 / yrs) - 1.0) * 100.0, 2)
    cagr_zero = round((np.prod(1.0 + np.array(returns_dead_to_zero)) ** (1.0 / yrs) - 1.0) * 100.0, 2)

    return {
        "window": f"{start_date}..{end_date}",
        "months": len(returns_last_price),
        "avg_eligible_stocks_per_month": round(float(np.mean(avg_stock_counts)), 1),
        "basket_cagr_last_price": cagr_last,
        "basket_cagr_dead_to_zero": cagr_zero,
    }


def run_unconstrained_simulation(
    universe: Dict[str, pd.DataFrame],
    benchmark_s: pd.Series,
    slots: int,
    initial_capital: float = 100_000.0,
    min_turnover: float = 2.5e6,
    min_price: float = 10.0,
    run_name: str = "unconstrained",
) -> Dict[str, Any]:
    cfg = BacktestConfig(
        initial_capital=initial_capital,
        portfolio_size=slots,
        max_open_positions=slots,
        rebalance_hold_rank_buffer=int(round(2.5 * slots)),
        rebalance_frequency="MONTHLY",
        min_turnover_50d=min_turnover,
        min_price=min_price,
        slippage_pct=0.0020,
        unaffordable_policy="TRY_NEXT",
        enforce_pit_universe=False,  # Unconstrained across all common stocks
        pit_top_n_liquidity=0,       # No top-N restriction
        brokerage_per_order=0.0,
        dp_charge_sell=14.75,
        max_volume_participation_pct=0.10,
        deduct_annual_tax=False,
    )
    strat = TrendMomentumStrategy(cfg)
    strat.pit_universe.events_by_sym = {}  # Zero index constraints

    logger.info(f"Starting {run_name} (Slots: {slots}, Capital: Rs {initial_capital:,.0f}, Min Turnover: Rs {min_turnover:,.0f})...")
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(
        universe, eval_start_date="2007-01-01", eval_end_date="2026-10-23"
    )

    # Save outputs
    nav.to_csv(RESULTS_DIR / f"nav_{run_name}.csv", index=False)
    trades.to_csv(RESULTS_DIR / f"trades_{run_name}.csv", index=False)
    orders.to_csv(RESULTS_DIR / f"orders_{run_name}.csv", index=False)

    nav["date_dt"] = pd.to_datetime(nav["date"])
    nav.set_index("date_dt", inplace=True)

    windows = {
        "Full 20-Year (2007-2026)": ("2007-01-01", "2026-10-23"),
        "Decade 1 (2007-2015)": ("2007-01-01", "2015-12-31"),
        "Decade 2 (2016-2026)": ("2016-01-01", "2026-10-23"),
        "Recent 5-Year (2021-2026)": ("2021-10-01", "2026-10-23"),
    }

    window_stats = {}
    for wname, (wstart, wend) in windows.items():
        window_stats[wname] = calculate_period_stats(nav, benchmark_s, wstart, wend)

    # Year by year
    yearly_nav = nav["nav"].resample("YE").last()
    bench_nav = benchmark_s.resample("YE").last()
    yearly_returns = []
    prev_val = initial_capital
    prev_bench = benchmark_s.loc["2007-01-01":].iloc[0] if not benchmark_s.loc["2007-01-01":].empty else 1.0

    for yr_date, val in yearly_nav.items():
        yr = yr_date.year
        ret_strat = (val / prev_val - 1.0) * 100.0
        b_val = bench_nav.get(yr_date, np.nan)
        ret_bench = (b_val / prev_bench - 1.0) * 100.0 if not np.isnan(b_val) and prev_bench > 0 else np.nan
        yearly_returns.append({
            "year": yr,
            "strategy_return_pct": round(ret_strat, 2),
            "nifty500_return_pct": round(ret_bench, 2),
            "alpha_pct": round(ret_strat - ret_bench, 2) if not np.isnan(ret_bench) else np.nan,
            "ending_nav": round(val, 2),
        })
        prev_val = val
        if not np.isnan(b_val):
            prev_bench = b_val

    # Trade diagnostics
    top10_share = 0.0
    if not trades.empty and "net_pnl" in trades.columns:
        tot_prof = trades[trades["net_pnl"] > 0]["net_pnl"].sum()
        if tot_prof > 0:
            top10_prof = trades.sort_values(by="net_pnl", ascending=False).head(10)["net_pnl"].sum()
            top10_share = round((top10_prof / tot_prof) * 100.0, 1)

    unaffordable = len(orders[orders["reason"] == "UNAFFORDABLE_INTEGER_SIZING"]) if "reason" in orders.columns else 0
    circuit_def = len(orders[orders["reason"] == "CIRCUIT_LOCKED_LIMIT"]) if "reason" in orders.columns else 0
    delisted_ex = trades[trades["exit_reason"] == "DATA_ENDED_FORCED_EXIT"] if "exit_reason" in trades.columns else pd.DataFrame()

    return {
        "run_name": run_name,
        "slots": slots,
        "initial_capital": initial_capital,
        "min_turnover": min_turnover,
        "min_price": min_price,
        "total_trades": len(trades),
        "top10_share_pct": top10_share,
        "unaffordable_rejected_orders": unaffordable,
        "circuit_deferred_orders": circuit_def,
        "forced_delisted_exits": len(delisted_ex),
        "forced_delisted_symbols": delisted_ex["symbol"].tolist() if not delisted_ex.empty else [],
        "window_stats": window_stats,
        "yearly_returns": yearly_returns,
    }


def main():
    logger.info("=== Starting Unconstrained All-Stock 20-Year PIT Backtest ===")
    universe = load_universe()
    bench = load_benchmark_nifty500()

    # 1. Run 15 Slots
    res_15 = run_unconstrained_simulation(
        universe, bench, slots=15, initial_capital=100_000.0, min_turnover=2.5e6, min_price=10.0, run_name="unconstrained_slots15_1L"
    )

    # 2. Run 6 Slots
    res_6 = run_unconstrained_simulation(
        universe, bench, slots=6, initial_capital=100_000.0, min_turnover=2.5e6, min_price=10.0, run_name="unconstrained_slots6_1L"
    )

    # 3. No-Skill Baselines for Unconstrained Universe
    baseline_20yr = no_skill_baseline(universe, "2007-01-01", "2026-10-23", min_turnover=2.5e6, min_price=10.0)
    baseline_dec1 = no_skill_baseline(universe, "2007-01-01", "2015-12-31", min_turnover=2.5e6, min_price=10.0)
    baseline_dec2 = no_skill_baseline(universe, "2016-01-01", "2026-10-23", min_turnover=2.5e6, min_price=10.0)

    summary = {
        "unconstrained_slots_15": res_15,
        "unconstrained_slots_6": res_6,
        "no_skill_baselines": {
            "20yr": baseline_20yr,
            "decade1": baseline_dec1,
            "decade2": baseline_dec2,
        },
    }

    summary_file = RESULTS_DIR / "unconstrained_summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)

    logger.info(f"Unconstrained 20-Year Summary written to {summary_file}")
    logger.info("Done!")


if __name__ == "__main__":
    main()

