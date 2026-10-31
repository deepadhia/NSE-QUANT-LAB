#!/usr/bin/env python3
"""
alpha_lab.py -- 2026-10-31 research harness (see PROTOCOL_2026-10-31.md).

  python alpha_lab.py prep                 # clean_panel.parquet -> lab_universe.parquet (indicators)
  python alpha_lab.py grid [--workers 2]   # the pre-registered 54-run grid (+ basket baselines)
  python alpha_lab.py one S0 UB R1 15 [--slip 0.002] [--capital 100000] [--tax]

All execution goes through the repo BacktestEngine (next-open fills, gap cap, circuit rules, integer shares, fees,
10% ADT cap, dated Indian tax). Strategies here only decide WHAT to hold; they never see future bars.
"""
from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing as mp
from types import SimpleNamespace
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from nse_quant_lab.base_strategy import BaseStrategy  # noqa: E402
from nse_quant_lab.config import BacktestConfig  # noqa: E402
from nse_quant_lab.cost_model import slippage_fraction  # noqa: E402
from nse_quant_lab.engine import BacktestEngine  # noqa: E402
from nse_quant_lab.indicators import compute_indicators  # noqa: E402

ARCH = REPO / "data" / "external" / "nse_archive"
PANEL = ARCH / "clean_panel.parquet"
LAB = ARCH / "lab_universe.parquet"
OUT = REPO / "research" / "audit_2026-10" / "results" / "lab_2026-10-31"
EVAL_START, EVAL_END = "2007-01-01", "2026-10-30"

UNIVERSES = {
    # name: (allowed series, min 50d median turnover Rs, min price)
    "UA": (("EQ",), 1.0e7, 20.0),
    "UB": (("EQ",), 2.5e6, 10.0),
    "UD": (("EQ", "BE", "BZ", "SM"), 2.5e6, 10.0),
    "UH": (("EQ",), 1.0e8, 10.0),    # liquidity tiers for the 'is the edge an illiquidity premium?' test
    "UM": (("EQ",), 1.0e7, 10.0),
    "UL": (("EQ",), 2.5e6, 10.0),
    "UT200": (("EQ",), 2.5e6, 10.0),  # Nifty-200 stand-in: 200 most liquid EQ names on the date
}
UNIV_MAX = {"UM": 1.0e8, "UL": 1.0e7}
UNIV_TOPN = {"UT200": 200}
EXTRA_COLS = ["ma100", "ma200", "mom12_1", "slope_r2", "maxgap90", "adt50m", "adt_prev", "sig_prev", "hist_n", "hi252", "vol_6m"]


# ----------------------------------------------------------------------------- prep
def _rolling_slope_r2(logp: np.ndarray, w: int = 90) -> np.ndarray:
    n = len(logp)
    out = np.full(n, np.nan)
    if n < w:
        return out
    x = np.arange(w, dtype=float)
    xc = x - x.mean()
    sxx = (xc ** 2).sum()
    win = np.lib.stride_tricks.sliding_window_view(logp, w)
    yc = win - win.mean(axis=1, keepdims=True)
    sxy = yc @ xc
    syy = (yc ** 2).sum(axis=1)
    slope = sxy / sxx
    with np.errstate(divide="ignore", invalid="ignore"):
        r2 = np.where(syy > 0, (sxy ** 2) / (sxx * syy), 0.0)
    out[w - 1:] = (np.exp(slope * 252.0) - 1.0) * r2  # annualised exp slope x R^2 (Clenow)
    return out


def prep() -> None:
    panel = pd.read_parquet(PANEL)
    panel["series"] = panel["series"].astype(str)
    cfg = BacktestConfig()
    outs = []
    t0 = time.time()
    for i, (sym, g) in enumerate(panel.groupby("symbol", sort=False)):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < 226:
            continue
        adt_med = g["turnover"].rolling(50, min_periods=50).median()
        if not (adt_med >= 1.5e6).any():
            continue
        ind = compute_indicators(g[["date", "open", "high", "low", "close", "volume"]].copy(), SimpleNamespace(min_history_sessions=0))
        if ind.empty:
            continue
        ind["symbol"] = sym
        ind["series"] = g["series"].values
        c = ind["close"]
        ind["ma100"] = c.rolling(100, min_periods=100).mean()
        ind["ma200"] = c.rolling(200, min_periods=200).mean()
        ind["mom12_1"] = c.shift(21) / c.shift(252) - 1.0
        ind["slope_r2"] = _rolling_slope_r2(np.log(c.values))
        ind["maxgap90"] = c.pct_change().abs().rolling(90, min_periods=90).max()
        ind["adt50m"] = adt_med.values
        ind["hist_n"] = np.arange(1, len(ind) + 1)  # point-in-time history length
        ind["hi252"] = c.rolling(252, min_periods=252).max()
        ind["adt_prev"] = adt_med.shift(1).values  # point-in-time inputs for the cost model (previous close)
        ind["sig_prev"] = c.pct_change().rolling(20, min_periods=10).std().shift(1).values
        keep = ["symbol", "series", "date", "open", "high", "low", "close", "volume", "ema100", "supertrend_dir",
                "mom_6m", *EXTRA_COLS]
        first_liquid = int(np.argmax((adt_med >= 1.5e6).values))
        ind = ind.iloc[max(0, first_liquid - 1):][keep].copy()
        for col in keep:
            if col not in ("symbol", "series", "date", "supertrend_dir"):
                ind[col] = ind[col].astype("float32")
        ind["supertrend_dir"] = ind["supertrend_dir"].astype("int8")
        ind["series"] = ind["series"].astype("category")
        outs.append(ind)
        if (i + 1) % 500 == 0:
            print(f"  {i + 1} symbols, {time.time() - t0:.0f}s", flush=True)
    uni = pd.concat(outs, ignore_index=True)
    uni["series"] = uni["series"].astype(str)
    uni.to_parquet(LAB)
    print("lab universe:", uni.symbol.nunique(), "symbols,", len(uni), "rows")


# ----------------------------------------------------------------------------- data
def load_universe() -> Dict[str, pd.DataFrame]:
    u = pd.read_parquet(LAB)
    u["series"] = u["series"].astype("category")
    return {s: g.reset_index(drop=True) for s, g in u.groupby("symbol", sort=False)}


def load_bench() -> pd.Series:
    from pymongo import MongoClient

    c = MongoClient("mongodb://localhost:27017/")["stock_scanner"]["benchmark_ohlcv"]
    b = pd.DataFrame(list(c.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    b["date"] = pd.to_datetime(b["date"])
    return b.set_index("date")["close"].sort_index()


# ----------------------------------------------------------------------------- strategy
class LabStrategy(BaseStrategy):
    rebalance_frequency = "MONTHLY"

    def __init__(self, kind: str, uni: str, regime: str, bench: pd.Series, config: Any = None, freq: str = "M",
                 order: str = "mom", seed: int = 0, exit_st: bool = False, cost: str = "flat",
                 cost_mult: float = 1.0, haircut: float = 0.0):
        super().__init__(config=config)
        self.cost, self.cost_mult, self.haircut = cost, cost_mult, haircut
        self.freq = freq
        self.order, self.seed, self.exit_st = order, seed, exit_st
        if kind == "F":  # Fortune Alpha 13 rules: daily cycle, no rank drops
            self.rebalance_frequency = "DAILY"
        self.kind, self.uni, self.regime = kind, uni, regime
        self.name = f"{kind}_{uni}_{regime}"
        sma200 = bench.rolling(200, min_periods=200).mean()
        self.below = (bench < sma200).to_dict()  # date -> bool (False while SMA undefined)
        self.snap: Optional[pd.DataFrame] = None

    def compute_indicators(self, df, config=None):
        return df

    def slippage_pct_for(self, sym, bar, price, shares, side):
        if self.cost == "tiered":
            f = slippage_fraction(float(bar["adt_prev"]), price, price * shares, float(bar["sig_prev"]), self.cost_mult)
        else:
            f = self.config.slippage_pct
        return f + (self.haircut if side == "SELL_DELIST" else 0.0)

    def get_rebalance_schedule(self, timeline, eval_start, eval_end, config=None):
        sched = super().get_rebalance_schedule(timeline, eval_start, eval_end, config)
        if self.freq == "H":  # June / December month-ends only (NSE momentum index cadence)
            sched = {e: d for e, d in sched.items() if d.month in (6, 12)}
        if self.freq == "Q":  # decisions only at Mar/Jun/Sep/Dec month-ends; daily trend exits still run every day
            sched = {e: d for e, d in sched.items() if d.month in (3, 6, 9, 12)}
        return sched

    def _build_snap(self, universe: Dict[str, pd.DataFrame]) -> None:
        all_dates = pd.DatetimeIndex(np.unique(np.concatenate([df["date"].values for df in universe.values()])))
        me = set(pd.Series(all_dates, index=all_dates).groupby(all_dates.to_period("M")).max().tolist())
        if self.kind == "F":
            cols = ["date", "symbol", "series", "close", "ema100", "supertrend_dir", "mom_6m", "adt50m", "hist_n"]
            parts = [df[cols] for df in universe.values()]
        else:
            parts = [df[df["date"].isin(me)] for df in universe.values()]
        self.snap = pd.concat(parts, ignore_index=True).set_index(["date", "symbol"]).sort_index()

    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        if self.snap is None:
            self._build_snap(universe)
        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        try:
            rows = self.snap.loc[decision_date]
        except KeyError:
            return empty
        series_ok, min_adt, min_px = UNIVERSES[self.uni]
        m = rows["series"].isin(series_ok) & (rows["adt50m"] >= min_adt) & (rows["close"] >= min_px) & (rows["hist_n"] >= 226)
        mx = UNIV_MAX.get(self.uni)
        if mx:
            m &= rows["adt50m"] < mx
        tn = UNIV_TOPN.get(self.uni)
        if tn:
            m &= rows["adt50m"].where(m).rank(ascending=False) <= tn
        k = self.kind
        if k == "S0":
            m &= (rows["close"] >= 1.03 * rows["ema100"]) & (rows["supertrend_dir"] == 1) & rows["mom_6m"].notna()
            score = rows["mom_6m"]
        elif k == "S1":
            m &= (rows["close"] > rows["ma100"]) & (rows["maxgap90"] < 0.15) & (rows["slope_r2"] > 0)
            score = rows["slope_r2"]
        elif k == "S2":
            m &= (rows["close"] > rows["ma200"]) & (rows["mom12_1"] > 0)
            score = rows["mom12_1"]
        elif k == "S3":   # 52-week-high proximity
            m &= rows["hi252"].notna() & (rows["close"] >= 0.90 * rows["hi252"])
            score = rows["close"] / rows["hi252"]
        elif k == "S4":   # momentum, then calmest
            m &= (rows["close"] > rows["ma200"]) & (rows["mom12_1"] > 0) & rows["vol_6m"].notna()
            m &= rows["mom12_1"] >= rows["mom12_1"].where(m).quantile(0.70)
            score = -rows["vol_6m"]
        elif k == "S5":   # NSE Momentum30-style: blended 6M/12M return over volatility
            m &= rows["mom_6m"].notna()
            score = rows["mom_6m"]
        elif k == "F":
            m &= (rows["close"] >= 1.03 * rows["ema100"]) & (rows["supertrend_dir"] == 1) & rows["mom_6m"].notna()
            if self.order == "rand":
                rng = np.random.default_rng(self.seed * 1_000_003 + int(pd.Timestamp(decision_date).strftime("%Y%m%d")))
                score = pd.Series(rng.random(len(rows)), index=rows.index)
            elif self.order == "ema":  # notebook: ascending distance above EMA, i.e. least extended first
                score = -(rows["close"] / rows["ema100"] - 1.0)
            else:
                score = rows["mom_6m"]
        else:
            raise ValueError(k)
        if self.regime in ("R1", "R2") and self.below.get(decision_date, False):
            if self.regime == "R2":
                return empty  # engine then drops every holding at next open
            m &= rows.index.isin(list(current_holdings.keys()))  # R1: nothing new, keep qualifying holdings
        sel = rows[m].copy()
        if sel.empty:
            return empty
        sel["score"] = score[m]
        sel["turnover_50d"] = sel["adt50m"].astype(float)
        sel["close"] = sel["close"].astype(float)
        sel = sel.sort_values("score", ascending=False)
        sel["symbol"] = sel.index
        sel.index.name = None
        sel["rank"] = np.arange(1, len(sel) + 1)
        return sel[["symbol", "close", "turnover_50d", "rank"]]

    def evaluate_rebalance_exits(self, universe, decision_date, open_positions, target_symbols, config):
        if self.kind == "F":
            return {}  # Fortune has no rank-based drops
        return super().evaluate_rebalance_exits(universe, decision_date, open_positions, target_symbols, config)

    def evaluate_daily_exits(self, universe, decision_date, open_positions, config):
        if self.kind in ("S3", "S4", "S5"):
            return {}  # rebalance-only strategies: no daily trend exit
        exits = {}
        for sym in sorted(open_positions):
            df = universe.get(sym)
            if df is None:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            bar = sub.iloc[0]
            if self.kind in ("S0", "F"):
                lvl, why = bar["ema100"], "STRUCTURAL_EMA100"
                if (self.kind == "F" or self.exit_st) and int(bar["supertrend_dir"]) != 1:
                    exits[sym] = "SUPERTREND_FLIP"
                    continue
            elif self.kind == "S1":
                lvl, why = bar["ma100"], "TREND_MA100"
            else:
                lvl, why = bar["ma200"], "TREND_MA200"
            if not np.isnan(lvl) and float(bar["close"]) < float(lvl):
                exits[sym] = why
        return exits


# ----------------------------------------------------------------------------- metrics
def stats(nav: pd.Series, bench: pd.Series, a: str, b: str) -> Dict[str, float]:
    s = nav.loc[a:b]
    if len(s) < 40:
        return {}
    be = bench.loc[s.index[0]:s.index[-1]]
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1
    bc = (be.iloc[-1] / be.iloc[0]) ** (1 / yrs) - 1
    ms = s.resample("ME").last().pct_change().dropna()
    mb = be.resample("ME").last().pct_change().reindex(ms.index)
    ex = (ms - mb).dropna()
    t = ex.mean() / ex.std() * np.sqrt(len(ex)) if len(ex) > 2 and ex.std() > 0 else np.nan
    return {
        "cagr": cagr * 100, "bench": bc * 100, "excess": (cagr - bc) * 100,
        "mdd": ((s / s.cummax()) - 1).min() * 100, "bmdd": ((be / be.cummax()) - 1).min() * 100,
        "vol": ms.std() * np.sqrt(12) * 100, "t": t,
    }


def run_one(spec: Dict[str, Any], uni_dict, bench) -> Dict[str, Any]:
    cfg = BacktestConfig(
        initial_capital=spec.get("capital", 100_000.0), portfolio_size=spec["slots"], max_open_positions=spec["slots"],
        rebalance_hold_rank_buffer=int(round(spec.get("buf", 2.5) * spec["slots"])), rebalance_frequency=("DAILY" if spec["kind"] == "F" else "MONTHLY"),
        min_turnover_50d=0.0, min_price=0.0, slippage_pct=spec.get("slip", 0.002), unaffordable_policy="TRY_NEXT",
        enforce_pit_universe=False, pit_top_n_liquidity=0, brokerage_per_order=0.0, dp_charge_sell=14.75,
        max_volume_participation_pct=0.10, deduct_annual_tax=spec.get("tax", False), tax_model="india_fy",
    )
    strat = LabStrategy(spec["kind"], spec["uni"], spec["regime"], bench, cfg, spec.get("freq", "M"),
                        spec.get("order", "mom"), spec.get("seed", 0), spec.get("exit_st", False),
                        spec.get("cost", "flat"), spec.get("cost_mult", 1.0), spec.get("haircut", 0.0))
    t0 = time.time()
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(uni_dict, eval_start_date=EVAL_START, eval_end_date=EVAL_END)
    nav["date"] = pd.to_datetime(nav["date"])
    navs = nav.set_index("date")["nav"]
    row = dict(spec)
    row["secs"] = round(time.time() - t0)
    row["trades"] = len(trades)
    row["final_nav"] = float(navs.iloc[-1])
    row["tax_paid"] = float(m.get("total_tax_paid", 0.0))
    for name, (a, b) in {"FULL": (EVAL_START, EVAL_END), "DEV": ("2007-01-01", "2015-12-31"),
                         "VAL": ("2016-01-01", "2020-12-31"), "LATE": ("2021-01-01", EVAL_END),
                         "Y10": ("2016-10-01", EVAL_END)}.items():
        for k, v in stats(navs, bench, a, b).items():
            row[f"{name}_{k}"] = v
    if len(trades):
        pos = trades.loc[trades.net_pnl > 0, "net_pnl"]
        row["top10_share"] = float(trades.nlargest(10, "net_pnl").net_pnl.sum() / pos.sum() * 100) if pos.sum() > 0 else np.nan
        row["forced_exits"] = int((trades.exit_reason == "DATA_ENDED_FORCED_EXIT").sum())
        row["avg_hold_days"] = float(trades.hold_days.mean())
        row["win_rate"] = float((trades.net_pnl > 0).mean() * 100)
        row["pct_trades_lt1y"] = float((trades.hold_days < 365).mean() * 100)
    tag = spec_tag(spec)
    OUT.mkdir(parents=True, exist_ok=True)
    navs.to_csv(OUT / f"nav_{tag}.csv")
    trades.to_csv(OUT / f"trades_{tag}.csv", index=False)
    return row


def spec_tag(s: Dict[str, Any]) -> str:
    var = f"_{s.get('freq', 'M')}b{s.get('buf', 2.5)}" if (s.get("freq", "M") != "M" or s.get("buf", 2.5) != 2.5) else ""
    if s.get("order") or s.get("exit_st"):
        var += f"_o{s.get('order', 'mom')}{s.get('seed', 0)}{'_st' if s.get('exit_st') else ''}"
    if s.get("cost") == "tiered":
        var += f"_tier{s.get('cost_mult', 1.0)}"
    if s.get("haircut"):
        var += f"_hc{s['haircut']}"
    return f"{s['kind']}_{s['uni']}_{s['regime']}_{s['slots']}{var}_s{int(s.get('slip', 0.002) * 1e4)}_c{int(s.get('capital', 100000))}_{'tax' if s.get('tax') else 'pre'}"


_G: Dict[str, Any] = {}


def _init() -> None:
    _G["u"] = load_universe()
    _G["b"] = load_bench()


def _work(spec):
    try:
        return run_one(spec, _G["u"], _G["b"])
    except Exception as e:  # fail loud, keep going
        return {**spec, "error": repr(e)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode")
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--slip", type=float, default=0.002)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--tax", action="store_true")
    ap.add_argument("--out", default="grid_results.csv")
    a = ap.parse_args()
    if a.mode == "prep":
        prep()
    elif a.mode == "one":
        k, u, r, n = a.rest
        _init()
        row = run_one({"kind": k, "uni": u, "regime": r, "slots": int(n), "slip": a.slip, "capital": a.capital, "tax": a.tax}, _G["u"], _G["b"])
        print(json.dumps(row, indent=1, default=str))
    elif a.mode == "batch":
        specs = json.loads(Path(a.rest[0]).read_text())
        OUT.mkdir(parents=True, exist_ok=True)
        rows = []
        with mp.get_context("spawn").Pool(a.workers, initializer=_init) as pool:
            for r in pool.imap_unordered(_work, specs):
                rows.append(r)
                pd.DataFrame(rows).to_csv(OUT / a.out, index=False)
                print(spec_tag(r), "FULL", round(r.get("FULL_cagr", float("nan")), 2), "err" if "error" in r else "", r.get("error", ""), flush=True)
    elif a.mode == "grid":
        specs = [{"kind": k, "uni": u, "regime": r, "slots": n, "slip": a.slip, "capital": a.capital, "tax": a.tax}
                 for k, u, r, n in itertools.product(["S0", "S1", "S2"], ["UA", "UB", "UD"], ["R0", "R1", "R2"], [10, 15])]
        OUT.mkdir(parents=True, exist_ok=True)
        rows = []
        with mp.get_context("spawn").Pool(a.workers, initializer=_init) as pool:
            for r in pool.imap_unordered(_work, specs):
                rows.append(r)
                pd.DataFrame(rows).to_csv(OUT / a.out, index=False)
                print(r.get("kind"), r.get("uni"), r.get("regime"), r.get("slots"), "FULL cagr", round(r.get("FULL_cagr", float("nan")), 2),
                      "err" if "error" in r else "", r.get("error", ""), flush=True)
    else:
        raise SystemExit("unknown mode")


if __name__ == "__main__":
    main()
