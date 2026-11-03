#!/usr/bin/env python3
"""
candidate_backtests.py -- 20-Year Historical Backtest & Parity Audit for Multi-Strategy Candidates.

Evaluates 4 candidate strategies alongside baseline S0 UH R1 15:
1. Baseline: S0_UH_R1_15 (trend_momentum_pit)
2. Candidate 1: fip_momentum_pit (Frog-in-the-Pan Continuous Momentum, ID <= 0, Semivol, Jump Filter)
3. Candidate 2: alpha_semivol_pit (Sortino Momentum + Stage 2 SMA150/200 Stack)
4. Candidate 3: pocket_pivot_pit (Pocket Pivot Volume Breakout + Turnover Surge Cap)
5. Candidate 4: uptrend_pullback_mr (Ultra-Liquid Daily Pullback Mean Reversion, RSI(2) < 10, SMA5 exit)

All backtests run through the repository's institutional BacktestEngine:
- Causal next-open fills
- Morning +2% buy limit gap price cap
- Upper circuit buy rejection & lower circuit sell deferral
- Max 10% volume participation cap
- Tiered slippage (cost_model.py)
- Full statutory Indian delivery charges (STT, stamp duty, SEBI, GST, DP charges)
- Dated Indian capital gains tax (tax_india.py) settled annually on March 31 with 8-year loss carry-forward
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from nse_quant_lab.base_strategy import BaseStrategy
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.cost_model import slippage_fraction
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy

ARCH = REPO / "external" / "nse_archive"
LAB_PATH = ARCH / "lab_universe.parquet"
AUGMENTED_PATH = ARCH / "augmented_lab_universe.parquet"
RESULTS_DIR = REPO / "research" / "audit_2026-10" / "results" / "candidate_evaluation"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

EVAL_START, EVAL_END = "2007-01-01", "2026-10-30"


# =============================================================================
# 1. DATA PREPARATION & INDICATOR AUGMENTATION
# =============================================================================

def augment_universe_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized calculation of candidate indicators per symbol."""
    print(f"Augmenting indicators for {df['symbol'].nunique():,} symbols...")
    t0 = time.time()
    
    outs = []
    grouped = df.groupby("symbol", sort=False)
    for i, (sym, g) in enumerate(grouped):
        g = g.sort_values("date").copy()
        c = g["close"].values
        v = g["volume"].values
        n = len(c)
        
        # Daily return
        r = np.zeros(n)
        r[1:] = (c[1:] - c[:-1]) / np.where(c[:-1] > 0, c[:-1], 1.0)
        
        # 1. Downside Semivolatility over 126 sessions
        r_down = np.minimum(0.0, r)
        r_down_sq = r_down ** 2
        s_down = pd.Series(r_down_sq).rolling(126, min_periods=60).mean().values
        semi_vol126 = np.sqrt(s_down * 252.0)
        
        # 2. Information Discreteness (ID) over 12-1M (231 window shifted by 21)
        is_pos = (r > 0).astype(float)
        is_neg = (r < 0).astype(float)
        n_pos_series = pd.Series(is_pos).rolling(231, min_periods=100).sum().shift(21).values
        n_neg_series = pd.Series(is_neg).rolling(231, min_periods=100).sum().shift(21).values
        tot_pos_neg = n_pos_series + n_neg_series
        mom12_1 = pd.Series(c).shift(21) / pd.Series(c).shift(252) - 1.0
        sign_mom = np.sign(mom12_1.values)
        with np.errstate(divide="ignore", invalid="ignore"):
            id_metric = np.where(tot_pos_neg > 0, sign_mom * (n_neg_series - n_pos_series) / tot_pos_neg, np.nan)
            
        # 3. SMAs: 5, 50, 150
        sma5 = pd.Series(c).rolling(5, min_periods=5).mean().values
        sma50 = pd.Series(c).rolling(50, min_periods=50).mean().values
        sma150 = pd.Series(c).rolling(150, min_periods=150).mean().values
        
        # 4. RSI(2) - Wilder smoothing
        delta = np.zeros(n)
        delta[1:] = c[1:] - c[:-1]
        gain = np.maximum(0.0, delta)
        loss = np.maximum(0.0, -delta)
        gain_s = pd.Series(gain).ewm(alpha=0.5, adjust=False).mean().values
        loss_s = pd.Series(loss).ewm(alpha=0.5, adjust=False).mean().values
        with np.errstate(divide="ignore", invalid="ignore"):
            rs = np.where(loss_s > 0, gain_s / loss_s, 100.0)
            rsi2 = np.where(loss_s == 0, 100.0, 100.0 - (100.0 / (1.0 + rs)))
            
        # 5. Pocket Pivot: up day, volume > max down volume of prior 10 days
        is_down_day = r < 0
        down_vol = np.where(is_down_day, v, 0.0)
        max_down_vol_10 = pd.Series(down_vol).rolling(10, min_periods=5).max().shift(1).values
        pocket_pivot = (r > 0) & (v > max_down_vol_10) & (max_down_vol_10 > 0)
        pocket_pivot_5d = pd.Series(pocket_pivot.astype(float)).rolling(5, min_periods=1).max().values
        
        # 6. Turnover surge: to5 / adt50m
        to = c * v
        to5 = pd.Series(to).rolling(5, min_periods=5).mean().values
        adt50m = g["adt50m"].values if "adt50m" in g.columns else pd.Series(to).rolling(50, min_periods=50).median().values
        with np.errstate(divide="ignore", invalid="ignore"):
            to_surge = np.where(adt50m > 0, to5 / adt50m, 1.0)
            
        # 7. 3-day down consecutive closes
        is_3d_down = (
            (pd.Series(c) < pd.Series(c).shift(1))
            & (pd.Series(c).shift(1) < pd.Series(c).shift(2))
            & (pd.Series(c).shift(2) < pd.Series(c).shift(3))
        ).values
        
        g["raw_mom_6m"] = (pd.Series(c) / pd.Series(c).shift(126) - 1.0).values.astype("float32")
        g["semi_vol126"] = semi_vol126.astype("float32")
        g["id_metric"] = id_metric.astype("float32")
        g["sma5"] = sma5.astype("float32")
        g["sma50"] = sma50.astype("float32")
        g["sma150"] = sma150.astype("float32")
        g["rsi2"] = rsi2.astype("float32")
        g["pocket_pivot_5d"] = pocket_pivot_5d.astype("int8")
        g["to_surge"] = to_surge.astype("float32")
        g["is_3d_down"] = is_3d_down.astype(bool)
        
        outs.append(g)
        if (i + 1) % 500 == 0:
            print(f"  {i + 1} symbols augmented ({time.time() - t0:.1f}s)...")
            
    res = pd.concat(outs, ignore_index=True)
    print(f"Augmentation complete: {len(res):,} rows in {time.time() - t0:.1f}s.")
    return res


def load_augmented_universe() -> Dict[str, pd.DataFrame]:
    """Load cached augmented universe or build and cache it."""
    if AUGMENTED_PATH.exists():
        print(f"Loading cached augmented universe from {AUGMENTED_PATH}...")
        df = pd.read_parquet(AUGMENTED_PATH)
        if "raw_mom_6m" not in df.columns:
            print("Augmenting missing 'raw_mom_6m' in cached universe...")
            df["raw_mom_6m"] = (df.groupby("symbol")["close"].pct_change(126)).values.astype("float32")
            df.to_parquet(AUGMENTED_PATH)
    else:
        print(f"Loading raw lab universe from {LAB_PATH}...")
        raw = pd.read_parquet(LAB_PATH)
        df = augment_universe_indicators(raw)
        print(f"Saving augmented universe cache to {AUGMENTED_PATH}...")
        df.to_parquet(AUGMENTED_PATH)
        
    df["series"] = df["series"].astype("category")
    return {s: g.reset_index(drop=True) for s, g in df.groupby("symbol", sort=False)}


def load_benchmark() -> pd.Series:
    """Load Nifty 500 (^CRSLDX) benchmark from TrendMomentumPITStrategy."""
    s = TrendMomentumPITStrategy()._bench()
    if s is None or s.empty:
        raise RuntimeError("Failed to load benchmark ^CRSLDX")
    return s


# =============================================================================
# 2. STRATEGY DEFINITIONS
# =============================================================================

class BaseLabCandidate(BaseStrategy):
    """Common candidate base with tiered slippage, benchmark regime, and precomputed snapshot."""
    
    rebalance_frequency = "MONTHLY"
    cost_mult = 1.0
    
    def __init__(self, name: str, bench: pd.Series, config: Any = None, cost_mult: float = 1.0, flat_slip: bool = False):
        super().__init__(config=config)
        self.name = name
        self.bench = bench
        self.cost_mult = cost_mult
        self.flat_slip = flat_slip
        sma200 = bench.rolling(200, min_periods=200).mean()
        self.regime_below = (bench < sma200).to_dict()
        self.snap: Optional[pd.DataFrame] = None
        
    def compute_indicators(self, df, config=None):
        return df
        
    def slippage_pct_for(self, sym, bar, price, shares, side):
        if getattr(self, "flat_slip", False):
            return float(self.config.slippage_pct)
        try:
            adt = float(bar["adt_prev"])
            sig = float(bar["sig_prev"])
        except (KeyError, TypeError, ValueError):
            return float(self.config.slippage_pct)
        return slippage_fraction(adt, price, price * shares, sig, self.cost_mult)
        
    def _build_snapshot(self, universe: Dict[str, pd.DataFrame]) -> None:
        """Pre-index monthly or daily slices for rapid candidate evaluation."""
        all_dates = pd.DatetimeIndex(np.unique(np.concatenate([df["date"].values for df in universe.values()])))
        if self.rebalance_frequency == "DAILY":
            parts = [df for df in universe.values()]
        else:
            me = set(pd.Series(all_dates, index=all_dates).groupby(all_dates.to_period("M")).max().tolist())
            parts = [df[df["date"].isin(me)] for df in universe.values()]
        self.snap = pd.concat(parts, ignore_index=True).set_index(["date", "symbol"]).sort_index()


# -----------------------------------------------------------------------------
# 1. Baseline: S0_UH_R1_15 (trend_momentum_pit)
# -----------------------------------------------------------------------------
class S0BaselineCandidate(BaseLabCandidate):
    """Baseline S0 UH R1 15: 6M/12M Momentum / Vol, Close >= 1.03 EMA100, ST Bullish."""
    
    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        if self.snap is None:
            self._build_snapshot(universe)
        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        try:
            rows = self.snap.loc[decision_date]
        except KeyError:
            return empty
            
        m = (
            (rows["series"] == "EQ")
            & (rows["adt50m"] >= 1.0e8)
            & (rows["close"] >= 10.0)
            & (rows["hist_n"] >= 226)
            & (rows["close"] >= 1.03 * rows["ema100"])
            & (rows["supertrend_dir"] == 1)
            & rows["mom_6m"].notna()
        )
        
        is_bear = self.regime_below.get(decision_date, True)
        if is_bear:
            m &= rows.index.isin(list(current_holdings.keys()))
            
        is_held = rows.index.isin(list(current_holdings.keys()))
        held_m = (
            is_held
            & (rows["series"] == "EQ")
            & (rows["close"] >= rows["ema100"])
            & (rows["close"] > 0)
        )
        sel = rows[m | held_m].copy()
        if sel.empty:
            return empty
            
        sel["score"] = sel["mom_6m"].fillna(-9999.0)
        sel = sel.sort_values(by=["score", "adt50m"], ascending=[False, False])
        sel["symbol"] = sel.index
        sel["turnover_50d"] = sel["adt50m"].astype(float)
        sel["close"] = sel["close"].astype(float)
        sel["rank"] = np.arange(1, len(sel) + 1)
        sel.index.name = None
        return sel[["symbol", "close", "turnover_50d", "rank"]]
        
    def evaluate_daily_exits(self, universe, decision_date, open_positions, config):
        exits = {}
        for sym in sorted(open_positions):
            df = universe.get(sym)
            if df is None:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            bar = sub.iloc[0]
            if float(bar["close"]) < float(bar["ema100"]):
                exits[sym] = "STRUCTURAL_EMA100"
        return exits


# -----------------------------------------------------------------------------
# 2. Candidate 1: fip_momentum_pit (Frog-in-the-Pan Continuous Momentum)
# -----------------------------------------------------------------------------
class FIPMomentumCandidate(BaseLabCandidate):
    """
    Frog-in-the-Pan (FIP) Continuous Momentum:
    - Reversal Skip: 12-1M Momentum
    - Information Discreteness: ID <= 0.0 (discrete jump shock protection)
    - Jump Filter: maxgap90 < 0.15
    - Downside Semivolatility: mom12_1 / semi_vol126
    - Trend: Close >= 1.03 * EMA100, Supertrend Bullish
    """
    
    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        if self.snap is None:
            self._build_snapshot(universe)
        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        try:
            rows = self.snap.loc[decision_date]
        except KeyError:
            return empty
            
        # New entry criteria
        m = (
            (rows["series"] == "EQ")
            & (rows["adt50m"] >= 1.0e8)
            & (rows["close"] >= 10.0)
            & (rows["hist_n"] >= 226)
            & (rows["close"] >= 1.03 * rows["ema100"])
            & (rows["supertrend_dir"] == 1)
            & (rows["maxgap90"] < 0.15)       # Jump filter
            & (rows["id_metric"] <= 0.0)      # Frog in the pan: continuous drift, not earnings/news jump
            & (rows["mom12_1"] > 0)
            & (rows["semi_vol126"] > 0)
        )
        
        is_bear = self.regime_below.get(decision_date, True)
        if is_bear:
            m &= rows.index.isin(list(current_holdings.keys()))
            
        # Held positions: hold until Close < EMA100
        is_held = rows.index.isin(list(current_holdings.keys()))
        held_m = (
            is_held
            & (rows["series"] == "EQ")
            & (rows["close"] >= rows["ema100"])
            & (rows["close"] > 0)
        )
        sel = rows[m | held_m].copy()
        if sel.empty:
            return empty
            
        # Sortino-adjusted FIP momentum
        score = sel["mom12_1"] / (sel["semi_vol126"] + 0.001)
        sel["score"] = score.fillna(-9999.0)
        sel = sel.sort_values(by=["score", "adt50m"], ascending=[False, False])
        sel["symbol"] = sel.index
        sel["turnover_50d"] = sel["adt50m"].astype(float)
        sel["close"] = sel["close"].astype(float)
        sel["rank"] = np.arange(1, len(sel) + 1)
        sel.index.name = None
        return sel[["symbol", "close", "turnover_50d", "rank"]]
        
    def evaluate_daily_exits(self, universe, decision_date, open_positions, config):
        exits = {}
        for sym in sorted(open_positions):
            df = universe.get(sym)
            if df is None:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            bar = sub.iloc[0]
            if float(bar["close"]) < float(bar["ema100"]):
                exits[sym] = "STRUCTURAL_EMA100"
        return exits


# -----------------------------------------------------------------------------
# 3. Candidate 2: alpha_semivol_pit (Sortino Momentum + Stage-2 Dual MA Stack)
# -----------------------------------------------------------------------------
class AlphaSemivolCandidate(BaseLabCandidate):
    """
    Sortino Momentum + Dual MA Stack:
    - Trend Stack: Close >= 1.02 * SMA150 & SMA150 > MA200 & Close > MA200
    - Ranking: Sortino Momentum = (0.5 * mom_6m + 0.5 * mom12_1) / semi_vol126
    - Exit: Close < SMA150
    """
    
    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        if self.snap is None:
            self._build_snapshot(universe)
        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        try:
            rows = self.snap.loc[decision_date]
        except KeyError:
            return empty
            
        raw_mom6 = rows["raw_mom_6m"] if "raw_mom_6m" in rows.columns else (rows["close"] / rows["close"].shift(126) - 1.0)
        m = (
            (rows["series"] == "EQ")
            & (rows["adt50m"] >= 1.0e8)
            & (rows["close"] >= 10.0)
            & (rows["hist_n"] >= 226)
            & (rows["close"] >= 1.02 * rows["sma150"])
            & (rows["sma150"] > rows["ma200"])
            & (rows["close"] > rows["ma200"])
            & raw_mom6.notna()
            & (rows["semi_vol126"] >= 0)
        )
        
        is_bear = self.regime_below.get(decision_date, True)
        if is_bear:
            m &= rows.index.isin(list(current_holdings.keys()))
            
        is_held = rows.index.isin(list(current_holdings.keys()))
        held_m = (
            is_held
            & (rows["series"] == "EQ")
            & (rows["close"] >= rows["sma150"])
            & (rows["close"] > 0)
        )
        sel = rows[m | held_m].copy()
        if sel.empty:
            return empty
            
        # Parity fix: use raw_mom_6m and fallback mom12_1 to raw_mom_6m for new listings
        mom6 = sel["raw_mom_6m"].fillna(0.0) if "raw_mom_6m" in sel.columns else sel["close"].pct_change(126).fillna(0.0)
        mom12 = sel["mom12_1"].fillna(mom6)
        mom_blend = 0.5 * mom6 + 0.5 * mom12
        score = mom_blend / (sel["semi_vol126"] + 0.001)
        sel["score"] = score.fillna(-9999.0)
        sel = sel.sort_values(by=["score", "adt50m"], ascending=[False, False])
        sel["symbol"] = sel.index
        sel["turnover_50d"] = sel["adt50m"].astype(float)
        sel["close"] = sel["close"].astype(float)
        sel["rank"] = np.arange(1, len(sel) + 1)
        sel.index.name = None
        return sel[["symbol", "close", "turnover_50d", "rank"]]
        
    def evaluate_daily_exits(self, universe, decision_date, open_positions, config):
        exits = {}
        for sym in sorted(open_positions):
            df = universe.get(sym)
            if df is None:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            bar = sub.iloc[0]
            if float(bar["close"]) < float(bar["sma150"]):
                exits[sym] = "STRUCTURAL_SMA150"
        return exits


# -----------------------------------------------------------------------------
# 4. Candidate 3: pocket_pivot_pit (Pocket Pivot Breakout + Turnover Surge Cap)
# -----------------------------------------------------------------------------
class PocketPivotCandidate(BaseLabCandidate):
    """
    Pocket Pivot Breakout + Turnover Velocity Surge Cap:
    - Setup: Pocket pivot triggered in last 5 days
    - Base / Trend: Close >= SMA50 & Close > MA200 & SMA50 > MA200
    - Surge Cap: to_surge <= 3.5 (avoids climax exhaustion tops)
    - Exit: Close < SMA50
    """
    
    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        if self.snap is None:
            self._build_snapshot(universe)
        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        try:
            rows = self.snap.loc[decision_date]
        except KeyError:
            return empty
            
        m = (
            (rows["series"] == "EQ")
            & (rows["adt50m"] >= 1.0e8)
            & (rows["close"] >= 10.0)
            & (rows["hist_n"] >= 226)
            & (rows["pocket_pivot_5d"] == 1)      # Pocket pivot in last 5 sessions
            & (rows["close"] >= rows["sma50"])
            & (rows["sma50"] > rows["ma200"])
            & (rows["close"] > rows["ma200"])
            & (rows["to_surge"] <= 3.5)           # Velocity surge cap
            & rows["mom_6m"].notna()
        )
        
        is_bear = self.regime_below.get(decision_date, True)
        if is_bear:
            m &= rows.index.isin(list(current_holdings.keys()))
            
        is_held = rows.index.isin(list(current_holdings.keys()))
        held_m = (
            is_held
            & (rows["series"] == "EQ")
            & (rows["close"] >= rows["sma50"])
            & (rows["close"] > 0)
        )
        sel = rows[m | held_m].copy()
        if sel.empty:
            return empty
            
        sel["score"] = sel["mom_6m"].fillna(-9999.0)
        sel = sel.sort_values(by=["score", "adt50m"], ascending=[False, False])
        sel["symbol"] = sel.index
        sel["turnover_50d"] = sel["adt50m"].astype(float)
        sel["close"] = sel["close"].astype(float)
        sel["rank"] = np.arange(1, len(sel) + 1)
        sel.index.name = None
        return sel[["symbol", "close", "turnover_50d", "rank"]]
        
    def evaluate_daily_exits(self, universe, decision_date, open_positions, config):
        exits = {}
        for sym in sorted(open_positions):
            df = universe.get(sym)
            if df is None:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            bar = sub.iloc[0]
            if float(bar["close"]) < float(bar["sma50"]):
                exits[sym] = "STRUCTURAL_SMA50"
        return exits


# -----------------------------------------------------------------------------
# 5. Candidate 4: uptrend_pullback_mr (Ultra-Liquid Daily Pullback Mean Reversion)
# -----------------------------------------------------------------------------
class UptrendPullbackMRCandidate(BaseLabCandidate):
    """
    Ultra-Liquid Daily Pullback Mean Reversion:
    - Daily Cadence (rebalance_frequency = "DAILY")
    - Universe: Ultra-liquid EQ (adt50m >= Rs 25 Cr)
    - Trend: Close > MA200 & Nifty 500 > SMA200
    - Pullback: 3 consecutive lower closes & RSI(2) < 10
    - Ranking: Lowest RSI(2)
    - Exits: Close > SMA5 (profit), -7% Stop, or 10-day Time Stop
    """
    
    rebalance_frequency = "DAILY"
    
    def __init__(self, name: str, bench: pd.Series, config: Any = None):
        super().__init__(name, bench, config)
        self.daily_candidates: Optional[Dict[pd.Timestamp, pd.DataFrame]] = None
        
    def _build_snapshot(self, universe: Dict[str, pd.DataFrame]) -> None:
        parts = []
        for df in universe.values():
            m = (
                (df["series"] == "EQ")
                & (df["adt50m"] >= 2.5e8)
                & (df["close"] >= 20.0)
                & (df["hist_n"] >= 226)
                & (df["close"] > df["ma200"])
                & (df["is_3d_down"] == True)
                & (df["rsi2"] < 10.0)
            )
            if m.any():
                parts.append(df.loc[m, ["date", "symbol", "close", "adt50m", "rsi2"]])
        if parts:
            full = pd.concat(parts, ignore_index=True)
            full = full.sort_values(by=["date", "rsi2", "adt50m"], ascending=[True, True, False])
            full["rank"] = full.groupby("date").cumcount() + 1
            full["turnover_50d"] = full["adt50m"].astype(float)
            self.daily_candidates = {}
            for d, g in full.groupby("date"):
                g_sym = g[["symbol", "close", "turnover_50d", "rank"]].copy().set_index("symbol")
                g_sym["symbol"] = g_sym.index
                self.daily_candidates[pd.to_datetime(d)] = g_sym
        else:
            self.daily_candidates = {}

    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        if self.daily_candidates is None:
            self._build_snapshot(universe)
        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        is_bear = self.regime_below.get(decision_date, True)
        if is_bear:
            return empty
        return self.daily_candidates.get(decision_date, empty)
        
    def evaluate_rebalance_exits(self, universe, decision_date, open_positions, target_symbols, config=None, *args, **kwargs):
        # Exits are strictly rule-driven via evaluate_daily_exits
        return {}

    def evaluate_daily_exits(self, universe, decision_date, open_positions, config):
        exits = {}
        target_dt = np.datetime64(decision_date)
        for sym, pos in open_positions.items():
            df = universe.get(sym)
            if df is None:
                continue
            dates = df["date"].values
            k = int(np.searchsorted(dates, target_dt))
            if k >= len(dates) or dates[k] != target_dt:
                continue
            bar = df.iloc[k]
            c = float(bar["close"])
            entry_px = float(pos["entry_raw_price"])
            hold_days = (decision_date - pos["entry_date"]).days
            
            # 1. Profit Target: Close > SMA5
            sma5 = float(bar["sma5"])
            if not np.isnan(sma5) and c > sma5:
                exits[sym] = "PROFIT_TARGET_SMA5"
                continue
                
            # 2. Hard Disaster Stop: -7%
            if c <= entry_px * 0.93:
                exits[sym] = "STOP_LOSS_7PCT"
                continue
                
            # 3. Time Stop: 10 calendar/session days
            if hold_days >= 10:
                exits[sym] = "TIME_STOP_10D"
                continue
                
        return exits



# =============================================================================
# 3. BACKTEST RUNNER & STATS
# =============================================================================

def compute_detailed_stats(nav: pd.Series, bench: pd.Series, a: str, b: str) -> Dict[str, float]:
    """Compute CAGR, MDD, Volatility, Sharpe, and Benchmark Excess."""
    s = nav.loc[a:b]
    if len(s) < 40:
        return {}
    be = bench.loc[s.index[0]:s.index[-1]]
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    cagr = (s.iloc[-1] / s.iloc[0]) ** (1.0 / yrs) - 1.0
    bc = (be.iloc[-1] / be.iloc[0]) ** (1.0 / yrs) - 1.0
    ms = s.resample("ME").last().pct_change().dropna()
    mb = be.resample("ME").last().pct_change().reindex(ms.index)
    ex = (ms - mb).dropna()
    t = ex.mean() / ex.std() * np.sqrt(len(ex)) if len(ex) > 2 and ex.std() > 0 else np.nan
    mdd = ((s / s.cummax()) - 1.0).min()
    bmdd = ((be / be.cummax()) - 1.0).min()
    vol = ms.std() * np.sqrt(12)
    sharpe = (cagr - 0.05) / vol if vol > 0 else np.nan
    
    return {
        "cagr": round(cagr * 100, 2),
        "bench_cagr": round(bc * 100, 2),
        "excess_cagr": round((cagr - bc) * 100, 2),
        "mdd": round(mdd * 100, 2),
        "bmdd": round(bmdd * 100, 2),
        "vol": round(vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "t_stat": round(t, 2),
    }


def compute_yearly_returns(nav: pd.Series) -> pd.Series:
    """Compute calendar year return series."""
    y = nav.resample("YE").last().pct_change()
    # first year return
    first_yr = nav.index[0].year
    first_nav = nav.iloc[0]
    nav_end_yr1 = nav.loc[str(first_yr)].iloc[-1]
    y.iloc[0] = (nav_end_yr1 / first_nav) - 1.0
    y.index = y.index.year
    return y * 100.0


def run_strategy_backtest(
    strat_cls: Any,
    strat_name: str,
    uni: Dict[str, pd.DataFrame],
    bench: pd.Series,
    slots: int = 15,
    hold_rank_buf: int = 1500,
    force: bool = False,
) -> Dict[str, Any]:
    """Execute 20-year backtest through BacktestEngine with institutional Indian frictions."""
    print(f"\n{'='*70}\nRUNNING 20-YEAR BACKTEST: {strat_name} (Slots={slots}, Buffer={hold_rank_buf})\n{'='*70}")
    t0 = time.time()
    
    nav_path = RESULTS_DIR / f"nav_{strat_name}.csv"
    trades_path = RESULTS_DIR / f"trades_{strat_name}.csv"
    
    if nav_path.exists() and trades_path.exists() and strat_name not in ("uptrend_pullback_mr", "alpha_semivol_pit") and not force:
        print(f"Loading cached backtest results for {strat_name}...")
        nav_df = pd.read_csv(nav_path)
        nav_df["date"] = pd.to_datetime(nav_df["date"])
        navs = nav_df.set_index("date")["nav"]
        trades = pd.read_csv(trades_path)
        elapsed = 0.0
        tax_paid = 0.0
    else:
        cfg = BacktestConfig(
            initial_capital=100_000.0,
            portfolio_size=slots,
            max_open_positions=slots,
            rebalance_hold_rank_buffer=hold_rank_buf,
            rebalance_frequency="DAILY" if getattr(strat_cls, "rebalance_frequency", "MONTHLY") == "DAILY" else "MONTHLY",
            min_turnover_50d=0.0,
            min_price=0.0,
            slippage_pct=0.002,
            unaffordable_policy="TRY_NEXT",
            enforce_pit_universe=False,
            pit_top_n_liquidity=0,
            brokerage_per_order=0.0,
            dp_charge_sell=14.75,
            max_volume_participation_pct=0.10,
            deduct_annual_tax=True,
            tax_model="india_fy",
        )
        
        strat = strat_cls(strat_name, bench, cfg)
        engine = BacktestEngine(strat, cfg)
        
        metrics, trades, orders, execs, nav = engine.run(
            uni, eval_start_date=EVAL_START, eval_end_date=EVAL_END
        )
        
        nav["date"] = pd.to_datetime(nav["date"])
        navs = nav.set_index("date")["nav"]
        elapsed = time.time() - t0
        tax_paid = float(metrics.get("total_tax_paid", 0.0))
        
        # Save nav and trades
        navs.to_csv(nav_path)
        trades.to_csv(trades_path, index=False)
        
    final_nav = float(navs.iloc[-1])
    
    # Calculate stats
    full_stats = compute_detailed_stats(navs, bench, EVAL_START, EVAL_END)
    y10_stats = compute_detailed_stats(navs, bench, "2016-10-01", EVAL_END)
    dev_stats = compute_detailed_stats(navs, bench, "2007-01-01", "2015-12-31")
    late_stats = compute_detailed_stats(navs, bench, "2021-01-01", EVAL_END)
    yearly = compute_yearly_returns(navs)
    
    # Trade statistics
    n_trades = len(trades)
    win_rate = (trades["net_pnl"] > 0).mean() * 100.0 if n_trades > 0 else 0.0
    avg_hold = trades["hold_days"].mean() if n_trades > 0 else 0.0
    
    print(f"--> {strat_name} Completed in {elapsed:.1f}s:")
    print(f"    20y Post-Tax CAGR: {full_stats.get('cagr')}% (MDD: {full_stats.get('mdd')}%, Vol: {full_stats.get('vol')}%)")
    print(f"    10y Post-Tax CAGR: {y10_stats.get('cagr')}% (MDD: {y10_stats.get('mdd')}%)")
    print(f"    Trades: {n_trades:,} | Win Rate: {win_rate:.1f}% | Avg Hold: {avg_hold:.1f} days")
    print(f"    Tax Paid: Rs {tax_paid:,.0f} | Final NAV: Rs {final_nav:,.0f}")
    
    return {
        "strategy": strat_name,
        "slots": slots,
        "elapsed_sec": round(elapsed, 1),
        "final_nav": round(final_nav, 2),
        "tax_paid": round(tax_paid, 2),
        "trades": n_trades,
        "win_rate": round(win_rate, 2),
        "avg_hold_days": round(avg_hold, 1),
        "full_cagr": full_stats.get("cagr"),
        "full_excess": full_stats.get("excess_cagr"),
        "full_mdd": full_stats.get("mdd"),
        "full_sharpe": full_stats.get("sharpe"),
        "y10_cagr": y10_stats.get("cagr"),
        "y10_mdd": y10_stats.get("mdd"),
        "dev_cagr": dev_stats.get("cagr"),
        "late_cagr": late_stats.get("cagr"),
        "nav_series": navs,
        "yearly_returns": yearly,
    }


# =============================================================================
# 4. MAIN ORCHESTRATION
# =============================================================================

def main():
    print("=" * 70)
    print("STARTING 20-YEAR MULTI-STRATEGY BACKTEST SUITE (2007 - 2026)")
    print("=" * 70)
    
    bench = load_benchmark()
    uni = load_augmented_universe()
    
    strategies_to_test = [
        (S0BaselineCandidate, "baseline_s0_uh_r1_15", 15, 1500),
        (FIPMomentumCandidate, "fip_momentum_pit", 15, 1500),
        (AlphaSemivolCandidate, "alpha_semivol_pit", 15, 1500),
        (PocketPivotCandidate, "pocket_pivot_pit", 15, 1500),
        (UptrendPullbackMRCandidate, "uptrend_pullback_mr", 10, 1500),
    ]
    
    results = []
    nav_dict = {}
    yearly_dict = {}
    
    import argparse
    parser = argparse.ArgumentParser(description="Multi-Strategy 20-Year Historical Backtest Suite")
    parser.add_argument("--force", action="store_true", help="Force re-run backtests ignoring cache")
    args, _ = parser.parse_known_args()

    for strat_cls, name, slots, buf in strategies_to_test:
        res = run_strategy_backtest(strat_cls, name, uni, bench, slots=slots, hold_rank_buf=buf, force=args.force)
        results.append(res)
        nav_dict[name] = res["nav_series"]
        yearly_dict[name] = res["yearly_returns"]
        
    # Summary Comparison Table
    summary_cols = [
        "strategy", "slots", "full_cagr", "y10_cagr", "dev_cagr", "late_cagr",
        "full_mdd", "full_sharpe", "win_rate", "trades", "avg_hold_days",
        "final_nav", "tax_paid", "elapsed_sec"
    ]
    df_summary = pd.DataFrame([{col: r[col] for col in summary_cols} for r in results])
    summary_csv = RESULTS_DIR / "candidate_strategies_comparison.csv"
    df_summary.to_csv(summary_csv, index=False)
    
    print("\n" + "=" * 70)
    print("20-YEAR HISTORICAL BACKTEST PERFORMANCE COMPARISON (POST-TAX, TIERED COST)")
    print("=" * 70)
    print(df_summary.to_string(index=False))
    
    # Monthly Return Correlation Matrix
    df_navs = pd.DataFrame(nav_dict)
    df_monthly = df_navs.resample("ME").last().pct_change().dropna()
    corr_matrix = df_monthly.corr()
    corr_csv = RESULTS_DIR / "monthly_returns_correlation_matrix.csv"
    corr_matrix.to_csv(corr_csv)
    
    print("\n" + "=" * 70)
    print("MONTHLY RETURN CORRELATION MATRIX (DIVERSIFICATION ASSESSMENT)")
    print("=" * 70)
    print(corr_matrix.round(3).to_string())
    
    # Calendar Year Returns Table
    df_yearly = pd.DataFrame(yearly_dict)
    yearly_csv = RESULTS_DIR / "calendar_year_returns.csv"
    df_yearly.to_csv(yearly_csv)
    
    print("\n" + "=" * 70)
    print("CALENDAR YEAR RETURNS (%)")
    print("=" * 70)
    print(df_yearly.round(1).to_string())
    
    print("\nBacktest artifacts saved to:")
    print(f"  Summary Table: {summary_csv}")
    print(f"  Correlation Matrix: {corr_csv}")
    print(f"  Calendar Year Returns: {yearly_csv}")


if __name__ == "__main__":
    main()

