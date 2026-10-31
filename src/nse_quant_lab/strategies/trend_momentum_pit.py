"""
trend_momentum_pit.py -- production version of the researched strategy (research kind "S0", universe "UH" (liquid tier), regime "R1",
15 slots, no rank-drop buffer). Same decision code is used by the backtest engine and by the paper-trading manager.

Rules (all evaluated with data up to the decision date's close; fills happen at the NEXT open):
  Scan   : every symbol that has a bar on the decision date.
  Filter : at least 226 sessions of history as of that date, series == EQ on that date, 50-session MEDIAN turnover >= Rs 10 crore, close >= Rs 10,
           close >= 1.03 x EMA(100), Supertrend(10, 3) bullish, momentum score defined.
  Rank   : blended 6M/12M return divided by 6M annualised volatility (indicators.mom_6m), highest first.
  Hold   : up to `portfolio_size` (15) names; a holding is sold when
             (a) daily: close < EMA(100)  [sell next open], or
             (b) month-end: it no longer passes the entry filter (rank is NOT used to drop winners), or
             (c) regime R1 only blocks NEW entries: if Nifty 500 close < its 200-session SMA at the month-end,
                 only current holdings that still qualify are kept and nothing new is bought.
  Costs  : liquidity-tiered, order-size-aware slippage from cost_model.py (inputs from the previous close only).
Set config.rebalance_hold_rank_buffer = 100 * portfolio_size (the researched setting) so no rank-based drops occur.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..base_strategy import BaseStrategy
from ..cost_model import slippage_fraction
from ..indicators import compute_indicators
from ..registry import register_strategy

MIN_ADT = 1.0e8   # Rs 10 Cr 50-session median turnover: the researched "UH" liquid tier (UB was Rs 25 L)
MIN_PRICE = 10.0
ENTRY_EMA_BUFFER = 1.03
REGIME_SMA = 200
MIN_HISTORY = 226  # sessions of history AS OF the decision date (point-in-time; never judged from the stock's future length)


@register_strategy("trend_momentum_pit")
class TrendMomentumPITStrategy(BaseStrategy):
    """Trend momentum, point-in-time: EQ stocks with >= Rs 10 Cr median turnover and 226+ sessions of history, close >= 1.03 x EMA(100)
    with a bullish Supertrend, ranked by risk-adjusted 6M/12M momentum, 15 slots, monthly entries, daily EMA(100) exits, Nifty 500
    200-day regime filter on new entries (see module docstring and docs/STRATEGY_TREND_MOMENTUM_PIT.md)."""

    name = "TREND_MOMENTUM_PIT"
    rebalance_frequency = "MONTHLY"
    paper_backup_slots = 60   # queue many backups so paper fills every free slot like the backtest's TRY_NEXT (unused orders expire)
    required_indicator_columns = ["ema100", "supertrend_dir", "mom_6m", "adt50m", "adt_prev", "sig_prev", "series", "hist_n"]

    def __init__(self, config: Optional[Any] = None, benchmark: Optional[pd.Series] = None,
                 use_regime: bool = True, delist_haircut: float = 0.0, cost_mult: float = 1.0, **kwargs):
        super().__init__(config=config, **kwargs)
        self._benchmark = benchmark
        self.use_regime = use_regime
        self.delist_haircut = delist_haircut
        self.cost_mult = cost_mult

    # ------------------------------------------------------------------ indicators
    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        cfg = config or self.config
        needed = ["date", "open", "high", "low", "close", "volume"]
        # min_history_sessions=0: history length is enforced per decision date via hist_n, never from total series length
        out = compute_indicators(df[needed].copy(), SimpleNamespace(min_history_sessions=0))
        if out.empty:
            return out
        out = out.sort_values("date").reset_index(drop=True)
        src = df.sort_values("date").reset_index(drop=True)
        n = len(out)
        if len(src) != n:  # compute_indicators never drops rows once history is sufficient
            raise ValueError("indicator frame length mismatch")
        turnover = src["turnover"] if "turnover" in src.columns else src["close"] * src["volume"]
        adt = turnover.rolling(50, min_periods=50).median()
        out["adt50m"] = adt.values
        out["adt_prev"] = adt.shift(1).values
        out["sig_prev"] = out["close"].pct_change().rolling(20, min_periods=10).std().shift(1).values
        out["series"] = src["series"].astype(str).values if "series" in src.columns else "EQ"
        out["hist_n"] = np.arange(1, n + 1)
        return out

    # ------------------------------------------------------------------ regime
    def _bench(self) -> Optional[pd.Series]:
        if self._benchmark is None:
            from pymongo import MongoClient

            col = MongoClient("mongodb://localhost:27017/")["stock_scanner"]["benchmark_ohlcv"]
            b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
            b["date"] = pd.to_datetime(b["date"])
            self._benchmark = b.set_index("date")["close"].sort_index()
        return self._benchmark

    def regime_below(self, decision_date: pd.Timestamp) -> bool:
        """True when Nifty 500 closed below its 200-session SMA on decision_date. Missing/stale benchmark data fails
        SAFE (treated as 'below': no new entries) rather than silently assuming a bull market."""
        bench = self._bench()
        if bench is None or decision_date not in bench.index:
            return True
        hist = bench.loc[:decision_date]
        if len(hist) < REGIME_SMA:
            return False
        return bool(hist.iloc[-1] < hist.iloc[-REGIME_SMA:].mean())

    # ------------------------------------------------------------------ selection
    def select_entry_candidates(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp,
                                current_holdings: Dict[str, Any], config: Any) -> pd.DataFrame:
        rows = []
        for sym, df in universe.items():
            if df.empty:
                continue
            dates = df["date"].values
            k = int(np.searchsorted(dates, np.datetime64(decision_date)))
            if k >= len(dates) or dates[k] != np.datetime64(decision_date):
                continue
            r = df.iloc[k]
            rows.append((sym, r["series"], float(r["close"]), float(r["ema100"]), int(r["supertrend_dir"]),
                         float(r["mom_6m"]), float(r["adt50m"]), int(r["hist_n"])))
        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        if not rows:
            return empty
        snap = pd.DataFrame(rows, columns=["symbol", "series", "close", "ema100", "st", "mom", "adt", "hist"]).set_index("symbol")
        m = (snap["series"] == "EQ") & (snap["adt"] >= MIN_ADT) & (snap["close"] >= MIN_PRICE) & (snap["hist"] >= MIN_HISTORY)
        m &= (snap["close"] >= ENTRY_EMA_BUFFER * snap["ema100"]) & (snap["st"] == 1) & snap["mom"].notna()
        if self.use_regime and self.regime_below(decision_date):
            m &= snap.index.isin(list(current_holdings.keys()))
        sel = snap[m].sort_values("mom", ascending=False)
        if sel.empty:
            return empty
        out = pd.DataFrame({"symbol": sel.index, "close": sel["close"].values, "turnover_50d": sel["adt"].values,
                            "rank": np.arange(1, len(sel) + 1)}, index=sel.index)
        out.index.name = None
        return out

    # ------------------------------------------------------------------ exits
    def evaluate_daily_exits(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp,
                             open_positions: Dict[str, Any], config: Any) -> Dict[str, str]:
        exits: Dict[str, str] = {}
        for sym in sorted(open_positions):
            df = universe.get(sym)
            if df is None or df.empty:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            bar = sub.iloc[0]
            ema = float(bar["ema100"])
            if not np.isnan(ema) and float(bar["close"]) < ema:
                exits[sym] = "STRUCTURAL_EMA100"
        return exits

    # ------------------------------------------------------------------ execution cost hook (engine + paper manager)
    def slippage_pct_for(self, sym: str, bar: Any, price: float, shares: int, side: str) -> float:
        def g(k):
            try:
                v = bar[k]
                return float(v) if v is not None else float("nan")
            except (KeyError, TypeError, ValueError):
                return float("nan")

        f = slippage_fraction(g("adt_prev"), price, price * shares, g("sig_prev"), self.cost_mult)
        return f + (self.delist_haircut if side == "SELL_DELIST" else 0.0)
