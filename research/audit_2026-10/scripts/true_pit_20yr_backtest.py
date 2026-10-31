#!/usr/bin/env python3
"""
True Point-in-Time 20-Year Backtest (2006 Warmup, 2007-2026 Evaluation)
Strategy: TrendMomentumStrategy
Initial Capital: Rs 1,00,000 (Rs 1 Lakh)
Universe: 100% official NSE market-wide bhavcopy archives (2006-2026)
Corporate Actions: 100% official NSE split/bonus/demerger adjustments
Symbol Changes: Date-aware canonical ticker chaining
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
from collections import defaultdict
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
logger = logging.getLogger("true_pit_20yr")

RESULTS_DIR = REPO_ROOT / "research" / "audit_2026-10" / "results" / "true_pit_20yr"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================================
# 1. SYMBOL CHANGES & CORPORATE ACTIONS MAPPING
# =====================================================================

def load_symbol_changes() -> Dict[str, List[Tuple[dt.date, str]]]:
    """Load official NSE symbol change mapping."""
    path = REPO_ROOT / "data" / "external" / "nse_archive" / "lists" / "symbolchange.csv"
    changes = defaultdict(list)
    if not path.exists():
        logger.warning(f"symbolchange.csv not found at {path}")
        return changes

    with open(path, encoding="latin-1") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 4 and row[1].strip() and row[1].strip() != "SM_KEY_SYMBOL":
                old_sym, new_sym, when_s = row[1].strip(), row[2].strip(), row[3].strip()
                try:
                    when = dt.datetime.strptime(when_s, "%d-%b-%Y").date()
                    changes[old_sym].append((when, new_sym))
                except Exception:
                    pass

    for k in changes:
        changes[k].sort()
    return changes


def get_canonical_symbol(
    sym: str,
    on_date: dt.date,
    changes: Dict[str, List[Tuple[dt.date, str]]],
) -> str:
    """Walk forward through rename chains to find canonical symbol."""
    cur = sym
    for _ in range(15):
        nxt = [(w, n) for w, n in changes.get(cur, []) if w > on_date]
        if not nxt:
            return cur
        on_date, cur = nxt[0]
    return cur


def load_corporate_actions() -> pd.DataFrame:
    """Load structural corporate actions (splits, bonuses) for price back-adjustment."""
    pq_path = REPO_ROOT / "data" / "external" / "nse_archive" / "all_corporate_actions.parquet"
    if pq_path.exists():
        ca = pd.read_parquet(pq_path)
    else:
        csv_path = REPO_ROOT / "data" / "external" / "niftyshop_ca" / "corporate_actions.csv"
        ca = pd.read_csv(csv_path)

    ca = ca[ca["kind"].isin(["split", "bonus"])].copy()
    ca["ex_date"] = pd.to_datetime(ca["ex_date"])
    ca = ca[ca["price_factor"] < 0.99]  # Significant factor
    return ca


# =====================================================================
# 2. DATA PREPARATION & UNIVERSE BUILDING
# =====================================================================

def build_prepared_universe(
    start_year: int = 2006,
    end_year: int = 2026,
    cache_path: Optional[Path] = None,
) -> Dict[str, pd.DataFrame]:
    """Load all Parquet files, apply symbol changes and corporate actions, compute indicators."""
    if cache_path and cache_path.exists():
        logger.info(f"Loading precomputed universe from {cache_path}...")
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    logger.info("Building 20-year universe from raw Parquet files...")
    changes = load_symbol_changes()
    ca_df = load_corporate_actions()
    comb_ca = (
        ca_df.groupby(["symbol", "ex_date"])["price_factor"].prod().reset_index()
    )

    parquet_files = sorted(
        glob.glob(str(REPO_ROOT / "data" / "external" / "nse_archive" / "parquet" / "bhavcopy_*.parquet"))
    )
    if not parquet_files:
        raise FileNotFoundError("No annual Parquet files found in data/external/nse_archive/parquet/")

    all_dfs = []
    for pf in parquet_files:
        yr = int(Path(pf).stem.split("_")[1])
        if start_year <= yr <= end_year:
            logger.info(f"Reading {Path(pf).name}...")
            df = pd.read_parquet(pf)
            all_dfs.append(df)

    raw_data = pd.concat(all_dfs, ignore_index=True)
    logger.info(f"Raw bars loaded: {len(raw_data):,} rows, {raw_data['symbol'].nunique()} symbols")

    # Map symbols to canonical names
    logger.info("Applying date-aware symbol changes to canonical tickers...")
    raw_data["date_dt"] = raw_data["date"].dt.date
    canonical_symbols = []
    for row in raw_data[["symbol", "date_dt"]].itertuples(index=False):
        canonical_symbols.append(get_canonical_symbol(row.symbol, row.date_dt, changes))
    raw_data["symbol"] = canonical_symbols
    raw_data.drop(columns=["date_dt"], inplace=True)

    # Clean data (OHLC invariant check)
    logger.info("Cleaning and validating OHLC invariants...")
    valid_mask = (
        (raw_data["open"] > 0)
        & (raw_data["high"] > 0)
        & (raw_data["low"] > 0)
        & (raw_data["close"] > 0)
        & (raw_data["volume"] >= 0)
        & (raw_data["high"] >= raw_data["low"])
        & (raw_data["high"] >= raw_data["open"])
        & (raw_data["high"] >= raw_data["close"])
        & (raw_data["low"] <= raw_data["open"])
        & (raw_data["low"] <= raw_data["close"])
    )
    clean_data = raw_data[valid_mask].copy()

    # Deduplicate in case of ticker merges on same date
    clean_data.sort_values(by=["symbol", "date", "volume"], ascending=[True, True, False], inplace=True)
    clean_data.drop_duplicates(subset=["symbol", "date"], keep="first", inplace=True)

    # Filter to symbols with potential liquidity:
    # Must have traded at least 40 sessions with turnover >= 20 Lakhs
    logger.info("Pre-filtering candidate universe for liquidity candidates...")
    sym_counts = (
        clean_data[clean_data["turnover"] >= 2e6]
        .groupby("symbol")["date"]
        .count()
    )
    eligible_syms = set(sym_counts[sym_counts >= 40].index)
    logger.info(f"Eligible liquid symbols: {len(eligible_syms):,}")

    clean_data = clean_data[clean_data["symbol"].isin(eligible_syms)]

    # Apply corporate actions per symbol
    logger.info("Applying official split/bonus price adjustments...")
    ca_by_sym = {sym: g for sym, g in comb_ca.groupby("symbol")}

    cfg = BacktestConfig()
    universe_ind: Dict[str, pd.DataFrame] = {}
    adjusted_dfs = []

    grouped = clean_data.groupby("symbol")
    total_syms = len(grouped)
    logger.info(f"Processing and computing indicators for {total_syms:,} symbols...")

    for i, (sym, g) in enumerate(grouped):
        g = g.sort_values("date").reset_index(drop=True)
        if sym in ca_by_sym:
            ev = ca_by_sym[sym]
            for e in ev.itertuples():
                idx = g.index[g["date"] >= e.ex_date]
                if len(idx) > 0 and idx[0] > 0:
                    split_idx = int(idx[0])
                    # Multiply prior prices, divide volume
                    for col in ("open", "high", "low", "close", "prev_close"):
                        g.loc[: split_idx - 1, col] *= e.price_factor
                    g.loc[: split_idx - 1, "volume"] /= e.price_factor

        # Standard columns
        ind_df = compute_indicators(g, cfg)
        if not ind_df.empty:
            ind_df["symbol"] = sym
            universe_ind[sym] = ind_df

        if (i + 1) % 500 == 0 or (i + 1) == total_syms:
            logger.info(f"Indicator progress: {i + 1}/{total_syms} symbols processed")

    if cache_path:
        logger.info(f"Saving universe cache to {cache_path}...")
        with open(cache_path, "wb") as f:
            pickle.dump(universe_ind, f)

    return universe_ind


# =====================================================================
# 3. BENCHMARKS & STATS
# =====================================================================

def load_benchmark_nifty500() -> pd.Series:
    """Load official Nifty 500 price index from MongoDB."""
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
    """Compute financial statistics: CAGR, MDD, Vol, Excess t-stat."""
    nav = nav_df.set_index(pd.to_datetime(nav_df["date"]))["nav"]
    sub_nav = nav.loc[start_date:end_date]
    if len(sub_nav) < 2:
        return {}

    b = benchmark_s.loc[sub_nav.index[0] : sub_nav.index[-1]]
    yrs = (sub_nav.index[-1] - sub_nav.index[0]).days / 365.25

    strat_cagr = ((sub_nav.iloc[-1] / sub_nav.iloc[0]) ** (1.0 / yrs) - 1.0) * 100.0
    bench_cagr = ((b.iloc[-1] / b.iloc[0]) ** (1.0 / yrs) - 1.0) * 100.0 if len(b) > 1 else 0.0

    strat_mdd = ((sub_nav / sub_nav.cummax()) - 1.0).min() * 100.0
    bench_mdd = ((b / b.cummax()) - 1.0).min() * 100.0 if len(b) > 1 else 0.0

    # Monthly resampling for Excess Return t-stat
    m_strat = sub_nav.resample("ME").last().pct_change().dropna()
    m_bench = b.resample("ME").last().pct_change().reindex(m_strat.index).dropna()
    common_idx = m_strat.index.intersection(m_bench.index)
    m_strat = m_strat.loc[common_idx]
    m_bench = m_bench.loc[common_idx]

    excess = m_strat - m_bench
    excess_mean = excess.mean()
    excess_std = excess.std()
    t_stat = (excess_mean / excess_std * np.sqrt(len(excess))) if excess_std > 0 else 0.0
    info_ratio = (excess_mean / excess_std * np.sqrt(12)) if excess_std > 0 else 0.0

    return {
        "start": start_date,
        "end": end_date,
        "years": round(yrs, 2),
        "cagr_pct": round(strat_cagr, 2),
        "n500_cagr_pct": round(bench_cagr, 2),
        "alpha_cagr_pct": round(strat_cagr - bench_cagr, 2),
        "max_drawdown_pct": round(strat_mdd, 2),
        "n500_max_drawdown_pct": round(bench_mdd, 2),
        "annual_vol_pct": round(m_strat.std() * np.sqrt(12) * 100.0, 2),
        "info_ratio": round(info_ratio, 3),
        "excess_t_stat": round(t_stat, 2),
        "final_nav": round(float(sub_nav.iloc[-1]), 2),
    }


def no_skill_baseline(
    universe: Dict[str, pd.DataFrame],
    start_date: str,
    end_date: str,
) -> Dict[str, Any]:
    """
    Equal-weight all eligible stocks monthly (open->open), gross.
    Eligibility mirrors the strategy filter: top-200 by 50d turnover, close>=10, turnover>=1e7.
    """
    logger.info(f"Computing No-Skill Equal-Weight Baseline for {start_date} -> {end_date}...")
    panel = pd.concat([d[["symbol", "date", "open", "close", "turnover_50d"]] for d in universe.values()])
    op = panel.pivot(index="date", columns="symbol", values="open")
    cl = panel.pivot(index="date", columns="symbol", values="close")
    to = panel.pivot(index="date", columns="symbol", values="turnover_50d")

    dates = cl.index
    mls = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max().tolist()
    returns_last_price = []
    returns_dead_to_zero = []
    last_date = {s: d["date"].max() for s, d in universe.items()}

    for i in range(len(mls) - 1):
        d = mls[i]
        ie, ix = dates.get_loc(d) + 1, dates.get_loc(mls[i + 1]) + 1
        if ix >= len(dates) or dates[ie] < pd.Timestamp(start_date) or dates[ix] > pd.Timestamp(end_date):
            continue
        t = to.loc[d].dropna()
        t = t[t > 0]
        n = min(200, max(1, int(len(t) * 0.45)))
        liq = t.sort_values(ascending=False).index[:n]
        elig = [s for s in liq if cl.loc[d, s] >= 10 and t[s] >= 1e7 and s in op.columns]
        if not elig:
            continue

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
        "basket_cagr_last_price": cagr_last,
        "basket_cagr_dead_to_zero": cagr_zero,
    }


# =====================================================================
# 4. MAIN EXECUTION CONTROLLER
# =====================================================================

def run_simulation(
    universe: Dict[str, pd.DataFrame],
    benchmark_s: pd.Series,
    slots: int,
    initial_capital: float = 100_000.0,
    slippage: float = 0.0020,
    tax: bool = False,
    run_name: str = "run",
) -> Dict[str, Any]:
    """Execute BacktestEngine across the complete 20-year timeline."""
    cfg = BacktestConfig(
        initial_capital=initial_capital,
        portfolio_size=slots,
        max_open_positions=slots,
        rebalance_hold_rank_buffer=int(round(2.5 * slots)),
        rebalance_frequency="MONTHLY",
        min_turnover_50d=1e7,
        min_price=10.0,
        slippage_pct=slippage,
        unaffordable_policy="TRY_NEXT",
        enforce_pit_universe=True,
        pit_top_n_liquidity=200,
        pit_max_universe_pct=0.45,
        brokerage_per_order=0.0,
        dp_charge_sell=14.75,
        max_volume_participation_pct=0.10,
        deduct_annual_tax=tax,
    )
    strat = TrendMomentumStrategy(cfg)
    strat.pit_universe.events_by_sym = {}  # Pure liquidity-driven PIT universe

    logger.info(f"Starting {run_name} (Slots: {slots}, Capital: Rs {initial_capital:,.0f})...")
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(
        universe, eval_start_date="2007-01-01", eval_end_date="2026-10-23"
    )

    # Save detailed trade logs
    nav.to_csv(RESULTS_DIR / f"nav_{run_name}.csv", index=False)
    trades.to_csv(RESULTS_DIR / f"trades_{run_name}.csv", index=False)
    orders.to_csv(RESULTS_DIR / f"orders_{run_name}.csv", index=False)

    # Calculate statistics across key windows
    windows = {
        "Full 20-Year (2007-2026)": ("2007-01-01", "2026-10-23"),
        "Decade 1 (2007-2015)": ("2007-01-01", "2015-12-31"),
        "Decade 2 (2016-2026)": ("2016-01-01", "2026-10-23"),
        "Recent 5-Year (2021-2026)": ("2021-10-01", "2026-10-23"),
    }

    window_stats = {}
    for wname, (wstart, wend) in windows.items():
        window_stats[wname] = calculate_period_stats(nav, benchmark_s, wstart, wend)

    # Calculate year-by-year annual returns
    nav["date_dt"] = pd.to_datetime(nav["date"])
    nav.set_index("date_dt", inplace=True)
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

    # Forensic analysis: Unaffordable skipped orders, circuit locks, forced exits
    unaffordable_orders = orders[orders["status"] == "REJECTED_UNAFFORDABLE"]
    circuit_deferred = orders[orders["status"] == "DEFERRED_CIRCUIT_LOCK"]
    forced_delisted = trades[trades["exit_reason"] == "DATA_ENDED_FORCED_EXIT"]

    # Trade concentration
    top10_pnl = trades["net_pnl"].nlargest(10).sum() if not trades.empty else 0.0
    total_pnl = trades["net_pnl"].sum() if not trades.empty else 1.0
    top10_share = (top10_pnl / total_pnl * 100.0) if total_pnl > 0 else 0.0

    return {
        "run_name": run_name,
        "slots": slots,
        "initial_capital": initial_capital,
        "total_trades": len(trades),
        "top10_share_pct": round(top10_share, 1),
        "unaffordable_rejected_orders": len(unaffordable_orders),
        "circuit_deferred_orders": len(circuit_deferred),
        "forced_delisted_exits": len(forced_delisted),
        "forced_delisted_symbols": sorted(forced_delisted["symbol"].unique().tolist()) if not forced_delisted.empty else [],
        "window_stats": window_stats,
        "yearly_returns": yearly_returns,
    }


def main():
    cache_file = REPO_ROOT / "data" / "external" / "nse_archive" / "universe_20yr_ind.pkl"
    universe = build_prepared_universe(start_year=2006, end_year=2026, cache_path=cache_file)
    benchmark_s = load_benchmark_nifty500()

    logger.info("Executing 20-Year True Point-in-Time Simulation Grid (Rs 1 Lakh Capital)...")

    # Run 15 Positions at Rs 1 Lakh
    res_15 = run_simulation(
        universe, benchmark_s, slots=15, initial_capital=100_000.0, run_name="pit_20yr_slots15_1L"
    )

    # Run 6 Positions at Rs 1 Lakh
    res_6 = run_simulation(
        universe, benchmark_s, slots=6, initial_capital=100_000.0, run_name="pit_20yr_slots6_1L"
    )

    # Baseline comparison
    baseline_20yr = no_skill_baseline(universe, "2007-01-01", "2026-10-23")
    baseline_dec1 = no_skill_baseline(universe, "2007-01-01", "2015-12-31")
    baseline_dec2 = no_skill_baseline(universe, "2016-01-01", "2026-10-23")

    report = {
        "slots_15": res_15,
        "slots_6": res_6,
        "no_skill_baselines": {
            "20yr": baseline_20yr,
            "decade1": baseline_dec1,
            "decade2": baseline_dec2,
        },
    }

    report_path = RESULTS_DIR / "true_pit_20yr_summary.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info(f"Summary report written to {report_path}")
    print("\n" + "=" * 80)
    print("TRUE POINT-IN-TIME 20-YEAR BACKTEST REPORT (Rs 1,00,000 Capital)")
    print("=" * 80)

    for run in [res_15, res_6]:
        print(f"\nConfiguration: {run['run_name']} | Slots: {run['slots']} | Capital: Rs {run['initial_capital']:,.0f}")
        print(f"Total Trades: {run['total_trades']} | Top-10 Profit Share: {run['top10_share_pct']}%")
        print(f"Loophole Forensics: Unaffordable Skipped Orders: {run['unaffordable_rejected_orders']} | "
              f"Circuit Deferred: {run['circuit_deferred_orders']} | Delisted Forced Exits: {run['forced_delisted_exits']}")
        print("\n--- Period Statistics vs Nifty 500 ---")
        p_df = pd.DataFrame(run["window_stats"]).T
        print(p_df[["years", "cagr_pct", "n500_cagr_pct", "alpha_cagr_pct", "max_drawdown_pct", "excess_t_stat", "final_nav"]].to_string())

        print("\n--- Year-by-Year Returns ---")
        y_df = pd.DataFrame(run["yearly_returns"])
        print(y_df[["year", "strategy_return_pct", "nifty500_return_pct", "alpha_pct", "ending_nav"]].to_string())


if __name__ == "__main__":
    main()

