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
             (b) month-end: it is no longer EQ or its close is below EMA(100) ("HoldFix": a HELD name does not have to re-pass the
                 1.03x-EMA / Supertrend entry filter; rank is NOT used to drop winners), or
             (c) regime R1 only blocks NEW entries: if Nifty 500 close < its 200-session SMA at the month-end,
                 nothing new is bought and held names stay under (b).
           Arm 2: when Nifty 500 crosses from below to at/above its 200-session SMA, an extra decision date fills open slots (holdings stay under rule (b)).
           (The 17.1% research figure in HANDOFF_2026-10-31 predates HoldFix and Arm 2; see docs/RESULTS_REGISTER.md.)
  Costs  : liquidity-tiered, order-size-aware slippage from cost_model.py (inputs from the previous close only).
Set config.rebalance_hold_rank_buffer = 100 * portfolio_size (the researched setting) so no rank-based drops occur.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..base_strategy import BaseStrategy
from ..price_basis import nominal_price, BASIS_COLUMNS
from ..cost_model import slippage_fraction
from ..indicators import compute_indicators
from ..registry import register_strategy

logger = logging.getLogger("trend_momentum_pit")

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
        for col in BASIS_COLUMNS:
            if col in src:
                out[col] = src[col].values
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
            try:
                import os
                from pymongo import MongoClient

                uri = getattr(self.config, "mongo_uri", None) or os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
                benchmark_db = getattr(self.config, "benchmark_db", None) or "stock_scanner"
                b = pd.DataFrame()
                try:
                    col = MongoClient(uri, serverSelectionTimeoutMS=5000)[benchmark_db]["benchmark_ohlcv"]
                    b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
                except Exception:
                    pass

                # Fallback to local MongoDB stock_scanner if primary failed or was empty
                if b.empty:
                    try:
                        col = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)["stock_scanner"]["benchmark_ohlcv"]
                        b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
                    except Exception:
                        pass

                if not b.empty:
                    b["date"] = pd.to_datetime(b["date"])
                    b = b.drop_duplicates(subset=["date"], keep="last").sort_values("date")
                    self._benchmark = b.set_index("date")["close"]
                else:
                    # Do NOT cache the failure: the next call (a scheduler retry in the same process, or a later decision date) must retry.
                    logger.error("[REGIME_GATE] Could not load ^CRSLDX benchmark from MongoDB or localhost.")
                    return pd.Series(dtype=float)
            except Exception as e:
                logger.error(f"[REGIME_GATE] Could not load benchmark from MongoDB: {e}")
                return pd.Series(dtype=float)
        return self._benchmark

    def regime_below(self, decision_date: pd.Timestamp) -> bool:
        """True when Nifty 500 closed below its 200-session SMA on decision_date. Missing/stale benchmark data fails
        SAFE (treated as 'below': no new entries) rather than silently assuming a bull market."""
        bench = self._bench()
        if bench is None or bench.empty:
            logger.warning("[REGIME_GATE] Benchmark data (^CRSLDX) is missing or empty. Failing safe to Bear Regime (blocking new entries).")
            return True
        if decision_date not in bench.index:
            max_b_date = bench.index.max().strftime("%Y-%m-%d") if len(bench) else "N/A"
            logger.warning(
                f"[REGIME_GATE] Decision date {decision_date.strftime('%Y-%m-%d')} not in benchmark (^CRSLDX) index "
                f"(latest date is {max_b_date}). Failing safe to Bear Regime (blocking new entries)."
            )
            return True
        hist = bench.loc[:decision_date]
        if len(hist) < REGIME_SMA:
            logger.warning(
                f"[REGIME_GATE] Benchmark history ({len(hist)}) < {REGIME_SMA} sessions on {decision_date.strftime('%Y-%m-%d')}. "
                f"Failing safe to Bear Regime (blocking new entries)."
            )
            return True
        sma200 = float(hist.iloc[-REGIME_SMA:].mean())
        return bool(float(hist.iloc[-1]) < sma200)

    def is_regime_transition_bull(self, decision_date: pd.Timestamp) -> bool:
        """True ONLY when both decision_date and the immediately preceding market session have valid >=200-bar
        benchmark history and Nifty 500 genuinely crossed from Close < SMA200 to Close >= SMA200 (V4-A)."""
        bench = self._bench()
        if bench is None or bench.empty or decision_date not in bench.index:
            return False
        hist = bench.loc[:decision_date]
        if len(hist) < REGIME_SMA + 1:
            return False
        prev_hist = hist.iloc[:-1]
        prev_sma200 = float(prev_hist.iloc[-REGIME_SMA:].mean())
        curr_sma200 = float(hist.iloc[-REGIME_SMA:].mean())
        was_below = float(prev_hist.iloc[-1]) < prev_sma200
        is_below_now = float(hist.iloc[-1]) < curr_sma200
        return bool(was_below and (not is_below_now))

    def is_rebalance_decision_date(
        self,
        timeline: List[pd.Timestamp],
        decision_date: pd.Timestamp,
        config: Any,
    ) -> bool:
        # 1. Standard monthly rebalance (month-end close)
        if super().is_rebalance_decision_date(timeline, decision_date, config):
            return True
        # 2. Arm 2 Event-driven catch-up rebalance on regime transition from Bear to Bull
        if self.use_regime and self.is_regime_transition_bull(decision_date):
            logger.info(
                f"[REGIME_TRANSITION] Event-driven catch-up rebalance triggered on {decision_date.strftime('%Y-%m-%d')} "
                f"(Nifty 500 crossed above 200-day SMA; filling open cash slots)."
            )
            return True
        return False

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

        # 2. Arm 2 Event-driven catch-up rebalances (transition from Bear to Bull)
        if self.use_regime:
            for i in range(1, len(timeline)):
                d_curr = timeline[i]
                if self.is_regime_transition_bull(d_curr):
                    if d_curr not in decision_to_exec_map:
                        if i + 1 < len(timeline):
                            d_exec = timeline[i + 1]
                            if eval_start <= d_exec <= eval_end:
                                decision_to_exec_map[d_curr] = d_exec

        return {v: k for k, v in decision_to_exec_map.items()}

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
                if sym in current_holdings and k > 0:
                    # Fallback to latest known bar prior to decision_date so a 1-day missing bar does not trigger REBALANCE_DROP
                    r = df.iloc[k - 1]
                    if (decision_date - pd.to_datetime(r["date"])).days > 5:
                        continue
                else:
                    continue
            else:
                r = df.iloc[k]
            rows.append((sym, r["series"], float(r["close"]), float(r["ema100"]), int(r["supertrend_dir"]),
                         float(r["mom_6m"]), float(r["adt50m"]), int(r["hist_n"]), nominal_price(r)))
        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        if not rows:
            return empty
        snap = pd.DataFrame(rows, columns=["symbol", "series", "close", "ema100", "st", "mom", "adt", "hist", "trade_close"]).set_index("symbol")

        # 1. New entry criteria: requires full liquidity, price, history, 3% EMA buffer, bullish Supertrend, valid momentum, and Bull regime
        is_bear = self.use_regime and self.regime_below(decision_date)
        if is_bear:
            new_mask = pd.Series(False, index=snap.index)
        else:
            new_mask = (
                (snap["series"] == "EQ")
                & (snap["adt"] >= MIN_ADT)
                & (snap["trade_close"] >= MIN_PRICE)
                & (snap["hist"] >= MIN_HISTORY)
                & (snap["close"] >= ENTRY_EMA_BUFFER * snap["ema100"])
                & (snap["st"] == 1)
                & snap["mom"].notna()
            )

        # 2. Existing holdings criteria (HoldFix / Champion Rule):
        # Active holdings remain rankable and eligible to hold as long as Close >= EMA(100) and series is EQ.
        # They bypass entry-only filters (3% buffer and Supertrend direction) to avoid premature stop-outs,
        # and bypass the bear regime new-entry block so open winners can continue trailing their EMA(100) stops.
        is_held = snap.index.isin(list(current_holdings.keys()))
        held_mask = (
            is_held
            & (snap["series"] == "EQ")
            & (snap["close"] >= snap["ema100"])
            & (snap["close"] > 0)
        )

        m = new_mask | held_mask
        sel = snap[m].copy()
        if sel.empty:
            return empty

        # Rank all candidates (both new and held) by momentum descending.
        # Held positions with missing momentum fill with -9999.0 so they rank at the bottom of the hold buffer rather than drop.
        sel["mom_sort"] = sel["mom"].fillna(-9999.0)
        sel = sel.sort_values(by=["mom_sort", "adt"], ascending=[False, False])

        out = pd.DataFrame({
            "symbol": sel.index,
            "close": sel["trade_close"].values,
            "turnover_50d": sel["adt"].values,
            "rank": np.arange(1, len(sel) + 1),
        }, index=sel.index)
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
