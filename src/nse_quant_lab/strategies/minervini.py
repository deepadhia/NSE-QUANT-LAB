"""
minervini.py -- Mark Minervini Trend Template Strategy (Point-in-Time, Full Liquid Tier).
========================================================================================
Implements the institutional-grade Mark Minervini Stage 2 Trend Template strategy
for the National Stock Exchange of India (NSE).

Entry Rules (Evaluated strictly post-market on decision date close; fills next open):
  1. Series: EQ equity only.
  2. Liquidity: 50-session median daily turnover >= Rs 10 Crore (UH liquid tier).
  3. Price: Close >= Rs 10.
  4. Point-in-time History: At least 252 sessions of trading history.
  5. Trend Template Alignment:
     - Current Price > 50-day SMA, 150-day SMA, and 200-day SMA.
     - 50-day SMA > 150-day SMA > 200-day SMA (Stage 2 moving average stack).
     - 200-day SMA trending upward (200-day SMA today > 200-day SMA 21 trading days ago).
     - Price >= 1.25 * 52-Week Low (at least 25% above 52-week low).
     - Price >= 0.75 * 52-Week High (within 25% of 52-week high).
     - 1-Year Relative Strength Percentile Rank >= 70 (top 30% performance in universe).
  6. Ranking: Ranked by intermediate 6-Month Rate of Change (change(126)) descending.
  7. Macro Regime Filter (R1): Nifty 500 (^CRSLDX) Close < 200-session SMA blocks new entries.

Hold / Champion Rule:
  - Active holdings remain eligible to hold as long as Close >= 150-day SMA and series is EQ.
  - Bypasses entry-only filters to prevent premature whipsaws on multi-bagger trends.
  - Bypasses bear regime new-entry blocks so established winners can continue trailing.

Exit Rules:
  - Daily: Close < 150-day SMA triggers exit at next morning open (STRUCTURAL_SMA150).
  - No rank-based drop exits (rebalance_hold_rank_buffer = 1500).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from ..base_strategy import BaseStrategy
from ..cost_model import slippage_fraction
from ..registry import register_strategy

logger = logging.getLogger("minervini_trend_template")

MIN_ADT = 1.0e8       # Rs 10 Cr 50-session median turnover (liquid tier)
MIN_PRICE = 10.0
MIN_HISTORY = 252     # sessions of point-in-time history as of decision date
REGIME_SMA = 200


@register_strategy("minervini_trend_template")
class MinerviniStrategy(BaseStrategy):
    """Mark Minervini Trend Template strategy for NSE equities with 150-day SMA trailing exit."""

    name = "MINERVINI_TREND_TEMPLATE"
    rebalance_frequency = "MONTHLY"
    paper_backup_slots = 60
    required_indicator_columns = [
        "sma50", "sma150", "sma200", "sma200_21d_ago",
        "w52low", "w52high", "ret_1y", "change_126",
        "adt50m", "adt_prev", "sig_prev", "series", "hist_n"
    ]

    def __init__(
        self,
        config: Optional[Any] = None,
        benchmark: Optional[pd.Series] = None,
        use_regime: bool = True,
        delist_haircut: float = 0.0,
        cost_mult: float = 1.0,
        **kwargs
    ):
        super().__init__(config=config, **kwargs)
        self._benchmark = benchmark
        self.use_regime = use_regime
        self.delist_haircut = delist_haircut
        self.cost_mult = cost_mult

    # ------------------------------------------------------------------ indicators
    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        src = df.sort_values("date").reset_index(drop=True)
        out = src.copy()
        n = len(out)
        close = out["close"]
        low = out["low"]
        high = out["high"]
        volume = out["volume"]
        turnover = src["turnover"] if "turnover" in src.columns else close * volume

        out["sma50"] = close.rolling(50, min_periods=50).mean()
        out["sma150"] = close.rolling(150, min_periods=150).mean()
        out["sma200"] = close.rolling(200, min_periods=200).mean()
        out["sma200_21d_ago"] = out["sma200"].shift(21)

        out["w52low"] = low.rolling(252, min_periods=200).min()
        out["w52high"] = high.rolling(252, min_periods=200).max()

        out["ret_1y"] = close / close.shift(252) - 1.0
        out["change_126"] = close / close.shift(126) - 1.0

        adt = turnover.rolling(50, min_periods=50).median()
        out["adt50m"] = adt.values
        out["adt_prev"] = adt.shift(1).values
        out["sig_prev"] = close.pct_change().rolling(20, min_periods=10).std().shift(1).values
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
                    col = MongoClient(uri, serverSelectionTimeoutMS=1000)[benchmark_db]["benchmark_ohlcv"]
                    b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
                except Exception:
                    pass

                # Fallback to local MongoDB stock_scanner if primary failed or was empty
                if b.empty:
                    try:
                        col = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=1000)["stock_scanner"]["benchmark_ohlcv"]
                        b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
                    except Exception:
                        pass

                if not b.empty:
                    b["date"] = pd.to_datetime(b["date"])
                    b = b.drop_duplicates(subset=["date"], keep="last").sort_values("date")
                    self._benchmark = b.set_index("date")["close"]
                else:
                    logger.warning("[REGIME_GATE] Could not load ^CRSLDX benchmark from MongoDB or localhost.")
                    self._benchmark = pd.Series(dtype=float)
            except Exception as e:
                logger.warning(f"[REGIME_GATE] Could not load benchmark from MongoDB: {e}")
                self._benchmark = pd.Series(dtype=float)
        return self._benchmark

    def regime_below(self, decision_date: pd.Timestamp) -> bool:
        """True when Nifty 500 closed below its 200-session SMA on decision_date.
        Missing benchmark data fails SAFE (treated as 'below': blocks new entries)."""
        bench = self._bench()
        if bench is None or bench.empty:
            logger.warning("[REGIME_GATE] Benchmark data (^CRSLDX) missing or empty. Failing safe to Bear Regime.")
            return True
        if decision_date not in bench.index:
            max_b_date = bench.index.max().strftime("%Y-%m-%d") if len(bench) else "N/A"
            logger.warning(
                f"[REGIME_GATE] Decision date {decision_date.strftime('%Y-%m-%d')} not in benchmark index "
                f"(latest date is {max_b_date}). Failing safe to Bear Regime."
            )
            return True
        hist = bench.loc[:decision_date]
        if len(hist) < REGIME_SMA:
            logger.warning(f"[REGIME_GATE] Benchmark history ({len(hist)}) < {REGIME_SMA} sessions. Defaulting to Bull.")
            return False
        sma200 = float(hist.iloc[-REGIME_SMA:].mean())
        return bool(hist.iloc[-1] < sma200)

    def is_regime_transition_bull(self, decision_date: pd.Timestamp) -> bool:
        """True on the exact decision_date where Nifty 500 crossed from Bear to Bull."""
        bench = self._bench()
        if bench is None or bench.empty or decision_date not in bench.index:
            return False
        idx = int(bench.index.get_loc(decision_date))
        if idx < REGIME_SMA:
            return False
        prev_date = bench.index[idx - 1]
        was_below = self.regime_below(prev_date)
        is_below_now = self.regime_below(decision_date)
        return was_below and (not is_below_now)

    def is_rebalance_decision_date(
        self,
        timeline: List[pd.Timestamp],
        decision_date: pd.Timestamp,
        config: Any,
    ) -> bool:
        if super().is_rebalance_decision_date(timeline, decision_date, config):
            return True
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

        # 2. Event-driven catch-up rebalances (transition from Bear to Bull)
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
    def select_entry_candidates(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        current_holdings: Dict[str, Any],
        config: Any,
    ) -> pd.DataFrame:
        rows = []
        for sym, df in universe.items():
            if df.empty:
                continue
            dates = df["date"].values
            k = int(np.searchsorted(dates, np.datetime64(decision_date)))
            if k >= len(dates) or dates[k] != np.datetime64(decision_date):
                if sym in current_holdings and k > 0:
                    r = df.iloc[k - 1]
                    if (decision_date - pd.to_datetime(r["date"])).days > 5:
                        continue
                else:
                    continue
            else:
                r = df.iloc[k]

            rows.append({
                "symbol": sym,
                "series": r["series"],
                "close": float(r["close"]),
                "sma50": float(r["sma50"]),
                "sma150": float(r["sma150"]),
                "sma200": float(r["sma200"]),
                "sma200_21d_ago": float(r["sma200_21d_ago"]),
                "w52low": float(r["w52low"]),
                "w52high": float(r["w52high"]),
                "ret_1y": float(r["ret_1y"]) if pd.notna(r["ret_1y"]) else np.nan,
                "change_126": float(r["change_126"]) if pd.notna(r["change_126"]) else np.nan,
                "adt": float(r["adt50m"]),
                "hist": int(r["hist_n"]),
            })

        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        if not rows:
            return empty

        snap = pd.DataFrame(rows).set_index("symbol")

        # Compute cross-sectional 1-year Relative Strength percentile rank (0 to 100)
        valid_ret = snap["ret_1y"].notna()
        snap["rs_rank"] = np.nan
        if valid_ret.sum() > 0:
            snap.loc[valid_ret, "rs_rank"] = snap.loc[valid_ret, "ret_1y"].rank(pct=True) * 100.0

        # 1. New entry criteria: Minervini Stage 2 Trend Template
        is_bear = self.use_regime and self.regime_below(decision_date)
        if is_bear:
            new_mask = pd.Series(False, index=snap.index)
        else:
            new_mask = (
                (snap["series"] == "EQ")
                & (snap["adt"] >= MIN_ADT)
                & (snap["close"] >= MIN_PRICE)
                & (snap["hist"] >= MIN_HISTORY)
                & (snap["close"] > snap["sma50"])
                & (snap["close"] > snap["sma150"])
                & (snap["close"] > snap["sma200"])
                & (snap["sma50"] > snap["sma150"])
                & (snap["sma150"] > snap["sma200"])
                & (snap["sma200"] > snap["sma200_21d_ago"])
                & (snap["close"] >= 1.25 * snap["w52low"])
                & (snap["close"] >= 0.75 * snap["w52high"])
                & (snap["rs_rank"] >= 70.0)
                & snap["change_126"].notna()
            )

        # 2. Existing holdings criteria (HoldFix / Champion Rule):
        # Open positions remain rankable and eligible to hold as long as Close >= SMA(150) and series is EQ.
        # Bypasses entry-only filters (alignment, 52W proximity, RS rank) and bear regime block.
        is_held = snap.index.isin(list(current_holdings.keys()))
        held_mask = (
            is_held
            & (snap["series"] == "EQ")
            & (snap["close"] >= snap["sma150"])
            & (snap["close"] > 0)
        )

        m = new_mask | held_mask
        sel = snap[m].copy()
        if sel.empty:
            return empty

        # Rank all eligible candidates by 6-month Rate of Change descending
        sel["sort_metric"] = sel["change_126"].fillna(-9999.0)
        sel = sel.sort_values(by=["sort_metric", "adt"], ascending=[False, False])

        out = pd.DataFrame({
            "symbol": sel.index,
            "close": sel["close"].values,
            "turnover_50d": sel["adt"].values,
            "rank": np.arange(1, len(sel) + 1),
        }, index=sel.index)
        out.index.name = None
        return out

    # ------------------------------------------------------------------ exits
    def evaluate_daily_exits(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        open_positions: Dict[str, Any],
        config: Any,
    ) -> Dict[str, str]:
        exits: Dict[str, str] = {}
        for sym in sorted(open_positions):
            df = universe.get(sym)
            if df is None or df.empty:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            bar = sub.iloc[0]
            sma150 = float(bar.get("sma150", np.nan))
            if not np.isnan(sma150) and float(bar["close"]) < sma150:
                exits[sym] = "STRUCTURAL_SMA150"
        return exits

    # ------------------------------------------------------------------ execution cost hook
    def slippage_pct_for(self, sym: str, bar: Any, price: float, shares: int, side: str) -> float:
        def g(k):
            try:
                v = bar[k]
                return float(v) if v is not None else float("nan")
            except (KeyError, TypeError, ValueError):
                return float("nan")

        f = slippage_fraction(g("adt_prev"), price, price * shares, g("sig_prev"), self.cost_mult)
        return f + (self.delist_haircut if side == "SELL_DELIST" else 0.0)

