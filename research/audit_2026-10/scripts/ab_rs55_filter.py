#!/usr/bin/env python3
"""
ab_rs55_filter.py -- A/B test: does an RS55 entry filter improve trend_momentum_pit's win rate (and not hurt everything else)?
=========================================================================================================================
Pre-registration (written BEFORE running; every arm is saved whatever it shows; counts as 4 more configurations in the tried set):
  RS55 = (stock 55-session return - Nifty 500 55-session return) in percentage points, measured at the decision-date close.
  Filter applies to NEW entries only (HoldFix: a held name is never dropped or blocked by RS55, same as the 1.03x-EMA / Supertrend entry rules).
  Arms (threshold in pp):  A0 baseline (no filter) | A1 rs55 > 0 | A2 rs55 >= 5 | A3 rs55 >= 10 | A4 rs55 >= 20.
  Missing RS55 (insufficient history / missing benchmark) with a filter ON blocks the entry (fail safe, never assumed positive).
  Primary question (user's): win rate (% of closed trades with net_pnl > 0). Reported WITH profit factor, avg/median trade, CAGR, max DD,
  Sharpe and trade count, because win rate alone can rise while expectancy falls (cutting trades that are small losers AND big winners).
  Windows: FULL 2007-01-01..2026-10-30, DEV 2007-2015, VAL 2016-2020, LATE 2021-2026 (trades bucketed by ENTRY date; CAGR from the NAV slice).
  Decision rule (fixed in advance): adopt a threshold only if win rate AND profit factor AND CAGR are not worse than baseline in FULL and in
  at least 2 of the 3 sub-windows, and trade count stays >= 60% of baseline. Otherwise: do not adopt.
  Also writes an independent trade-bucket analysis of the BASELINE ledger by RS55 at signal date (descriptive; ignores slot substitution).

Uses the PRODUCTION class (subclassed only to add the opt-in filter; no copy of strategy logic), same config as minervini_20y.py (R1).
Data: results/ab_regime_catchup/universe_cache.pkl (cached load_pit_universe("2026-10-30", include_live=False) indicator frames, local only).
Run: python research/audit_2026-10/scripts/ab_rs55_filter.py [arm_key ...]      (default: all arms; ~5 min per arm)
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from pymongo import MongoClient

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy

OUT = REPO / "research" / "audit_2026-10" / "results" / "ab_rs55_filter"
CACHE = REPO / "research" / "audit_2026-10" / "results" / "ab_regime_catchup" / "universe_cache.pkl"
OUT.mkdir(parents=True, exist_ok=True)
START, END = "2007-01-01", "2026-10-30"
WINDOWS = {"FULL": (START, END), "DEV_2007_15": ("2007-01-01", "2015-12-31"), "VAL_2016_20": ("2016-01-01", "2020-12-31"),
           "LATE_2021_26": ("2021-01-01", END)}
RS_LOOKBACK = 55
# A5 (>= 40 pp) was ADDED POST-HOC (2026-11-08) after the baseline bucket table showed the >=40 bucket had the best win rate: it is a data-informed
# arm, not part of the original pre-registration, and adds to the tried set. A2/A3 were not run (user scoped the test to A1, then A4+A5).
ARMS = {"A0_baseline": None, "A1_rs55_gt0": 0.0, "A2_rs55_ge5": 5.0, "A3_rs55_ge10": 10.0, "A4_rs55_ge20": 20.0, "A5_rs55_ge40": 40.0}


def bench_series() -> pd.Series:
    col = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)["stock_scanner"]["benchmark_ohlcv"]
    b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    if b.empty:
        raise RuntimeError("benchmark ^CRSLDX missing in Mongo stock_scanner.benchmark_ohlcv")
    b["date"] = pd.to_datetime(b["date"])
    return b.drop_duplicates("date", keep="last").sort_values("date").set_index("date")["close"]


def rs55_series(df: pd.DataFrame, bench: pd.Series) -> np.ndarray:
    """Stock 55-session return minus benchmark return over the same two dates, in pp. Uses only data up to each row's own date."""
    c = df["close"].astype(float).reset_index(drop=True)
    dates = pd.to_datetime(df["date"]).reset_index(drop=True)
    b = bench.reindex(bench.index.union(dates)).ffill().reindex(dates).reset_index(drop=True)
    return (((c / c.shift(RS_LOOKBACK) - 1.0) - (b / b.shift(RS_LOOKBACK) - 1.0)) * 100.0).values


class RS55FilterStrategy(TrendMomentumPITStrategy):
    """Production strategy + opt-in RS55 entry filter. rs55_min=None reproduces production exactly."""

    def __init__(self, config: Any, benchmark: pd.Series, rs55_min: Optional[float] = None):
        super().__init__(config=config, benchmark=benchmark, use_regime=True)
        self.rs55_min = rs55_min

    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        out = super().compute_indicators(df, config)
        if not out.empty:
            out["rs55_pct"] = rs55_series(out, self._benchmark)
        return out

    def select_entry_candidates(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp,
                                current_holdings: Dict[str, Any], config: Any) -> pd.DataFrame:
        out = super().select_entry_candidates(universe, decision_date, current_holdings, config)
        if self.rs55_min is None or out.empty:
            return out
        keep = []
        for sym in out.index:
            if sym in current_holdings:           # held names are never blocked by an entry-only filter
                keep.append(True)
                continue
            df = universe[sym]
            dates = df["date"].values
            k = int(np.searchsorted(dates, np.datetime64(decision_date)))
            ok = k < len(dates) and dates[k] == np.datetime64(decision_date)
            v = float(df["rs55_pct"].iloc[k]) if ok else float("nan")
            keep.append(bool(np.isfinite(v) and v >= self.rs55_min and (self.rs55_min > 0 or v > 0)))
        out = out[np.array(keep, dtype=bool)].copy()
        out["rank"] = np.arange(1, len(out) + 1)
        return out


def window_stats(nav: pd.Series, bench: pd.Series, trades: pd.DataFrame, a: str, b: str) -> dict:
    s = nav[(nav.index >= a) & (nav.index <= b)]
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    r = s.pct_change().dropna()
    t = trades[(pd.to_datetime(trades["entry_date"]) >= a) & (pd.to_datetime(trades["entry_date"]) <= b)]
    gains, losses = t.loc[t.net_pnl > 0, "net_pnl"].sum(), -t.loc[t.net_pnl < 0, "net_pnl"].sum()
    bs = bench[(bench.index >= a) & (bench.index <= b)]
    return {"cagr": round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 2), "mdd": round(float((s / s.cummax() - 1).min()) * 100, 1),
            "sharpe": round(float(r.mean() / r.std() * np.sqrt(252)), 2) if r.std() > 0 else 0.0, "trades": len(t),
            "win_pct": round(float((t.net_pnl > 0).mean() * 100), 1) if len(t) else 0.0,
            "pf": round(float(gains / losses), 2) if losses > 0 else 0.0,
            "mean_ret_pct": round(float((t.net_pnl / (t.shares * t.entry_fill_price)).mean() * 100), 2) if len(t) else 0.0,
            "median_ret_pct": round(float((t.net_pnl / (t.shares * t.entry_fill_price)).median() * 100), 2) if len(t) else 0.0,
            "bench_cagr": round(((bs.iloc[-1] / bs.iloc[0]) ** (1 / yrs) - 1) * 100, 2)}


def run_arm(name: str, thr: Optional[float], uni: Dict[str, pd.DataFrame], bench: pd.Series) -> list:
    cfg = BacktestConfig(
        initial_capital=100_000.0, portfolio_size=15, max_open_positions=15, rebalance_hold_rank_buffer=1500,
        rebalance_frequency="MONTHLY", min_turnover_50d=0.0, min_price=0.0, slippage_pct=0.002, unaffordable_policy="TRY_NEXT",
        enforce_pit_universe=False, pit_top_n_liquidity=0, brokerage_per_order=0.0, dp_charge_sell=14.75,
        max_volume_participation_pct=0.10, deduct_annual_tax=True, tax_model="india_fy",
    )
    t0 = time.time()
    strat = RS55FilterStrategy(cfg, bench, thr)
    _, trades, _, _, nav = BacktestEngine(strat, cfg).run(uni, eval_start_date=START, eval_end_date=END)
    nav["date"] = pd.to_datetime(nav["date"])
    s = nav.set_index("date")["nav"]
    s.to_csv(OUT / f"nav_{name}.csv")
    trades.to_csv(OUT / f"trades_{name}.csv", index=False)
    rows = []
    for w, (a, b) in WINDOWS.items():
        rows.append({"arm": name, "rs55_min": thr, "window": w, "final_nav": round(float(s.iloc[-1])), **window_stats(s, bench, trades, a, b)})
    print(f"[{name}] {time.time() - t0:.0f}s  FULL: {rows[0]}", flush=True)
    return rows


def bucket_analysis(uni: Dict[str, pd.DataFrame], bench: pd.Series) -> None:
    """Baseline ledger bucketed by RS55 at the signal date (last session before entry_date). Descriptive only."""
    f = OUT / "trades_A0_baseline.csv"
    if not f.exists():
        return
    t = pd.read_csv(f, parse_dates=["entry_date", "exit_date"])
    rs = []
    for sym, ed in zip(t["symbol"], t["entry_date"]):
        v = np.nan
        df = uni.get(sym)
        if df is not None:
            d = pd.to_datetime(df["date"]).values
            k = int(np.searchsorted(d, np.datetime64(ed))) - 1   # signal session = last session strictly before the fill date
            if k >= RS_LOOKBACK:
                v = float(rs55_series(df.iloc[: k + 1], bench)[-1])
        rs.append(v)
    t["rs55_signal"] = rs
    t["ret_pct"] = t["net_pnl"] / (t["shares"] * t["entry_fill_price"]) * 100
    t["bucket"] = pd.cut(t["rs55_signal"], [-np.inf, 0, 5, 10, 20, 40, np.inf], labels=["<0", "0-5", "5-10", "10-20", "20-40", ">=40"])
    g = t.groupby("bucket", observed=False)
    res = pd.DataFrame({"trades": g.size(), "win_pct": g["net_pnl"].apply(lambda x: (x > 0).mean() * 100).round(1),
                        "mean_ret_pct": g["ret_pct"].mean().round(2), "median_ret_pct": g["ret_pct"].median().round(2),
                        "sum_net_pnl": g["net_pnl"].sum().round(0),
                        "pf": g["net_pnl"].apply(lambda x: x[x > 0].sum() / -x[x < 0].sum() if (x < 0).any() else np.nan).round(2)})
    res.to_csv(OUT / "baseline_buckets_by_rs55.csv")
    t.to_csv(OUT / "trades_A0_baseline_with_rs55.csv", index=False)
    print("\nBASELINE LEDGER BY RS55 AT SIGNAL DATE\n", res.to_string(), f"\nNaN rs55: {int(t.rs55_signal.isna().sum())} of {len(t)}", flush=True)


def main() -> None:
    keys = sys.argv[1:] or list(ARMS)
    bench = bench_series()
    t0 = time.time()
    with open(CACHE, "rb") as fh:
        uni = pickle.load(fh)
    print(f"universe {len(uni)} symbols loaded in {time.time() - t0:.0f}s; bench {bench.index[0].date()}..{bench.index[-1].date()}", flush=True)
    for k in keys:
        rows = run_arm(k, ARMS[k], uni, bench)
        pd.DataFrame(rows).to_csv(OUT / f"summary_{k}.csv", index=False)
    parts = [pd.read_csv(OUT / f"summary_{k}.csv") for k in ARMS if (OUT / f"summary_{k}.csv").exists()]
    allr = pd.concat(parts, ignore_index=True)
    allr.to_csv(OUT / "summary_all.csv", index=False)
    print("\n" + allr[["arm", "window", "cagr", "mdd", "sharpe", "trades", "win_pct", "pf", "mean_ret_pct", "median_ret_pct"]].to_string(index=False), flush=True)
    bucket_analysis(uni, bench)


if __name__ == "__main__":
    main()
