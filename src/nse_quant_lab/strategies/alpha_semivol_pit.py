"""
alpha_semivol_pit.py -- Sortino Momentum + Dual Moving Average Stack Strategy (Point-in-Time, Full Liquid Tier).
================================================================================================================
Implements the Sortino-adjusted momentum strategy for the National Stock Exchange of India (NSE).
Ranks candidates by momentum divided by downside semivolatility, filtering by an institutional
Stage-2 moving average stack (SMA150 > MA200 and Close >= 1.02 x SMA150).

Entry Rules (Evaluated post-market on decision date close; fills at next morning open):
  1. Series: EQ equity only.
  2. Liquidity: 50-session median daily turnover >= Rs 10 Crore (UH liquid tier).
  3. Price: Close >= Rs 10.
  4. Point-in-time History: At least 226 sessions of trading history as of decision date (hist_n >= 226).
  5. Trend Stack Alignment:
     - Current Price >= 1.02 * 150-day SMA.
     - 150-day SMA > 200-day SMA (Stage-2 long-term alignment).
     - Current Price > 200-day SMA.
  6. Ranking: Ranked by Sortino Momentum = (0.5 * mom_6m + 0.5 * mom12_1) / (semi_vol126 + 0.001) descending.
  7. Macro Regime Filter (R1): Nifty 500 (^CRSLDX) Close < 200-session SMA blocks new entries.

Hold / Champion Rule:
  - Active holdings remain eligible to hold as long as Close >= 150-day SMA and series is EQ.
  - Bypasses entry-only filters (1.02 buffer) to prevent premature whipsaws on multi-bagger trends.
  - Bypasses bear regime new-entry blocks so established winners can continue trailing their stops.

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
from ..price_basis import nominal_price, BASIS_COLUMNS
from ..cost_model import slippage_fraction
from ..registry import register_strategy

logger = logging.getLogger("alpha_semivol_pit")

MIN_ADT = 1.0e8       # Rs 10 Cr 50-session median turnover (UH liquid tier)
MIN_PRICE = 10.0
MIN_HISTORY = 226     # sessions of point-in-time history as of decision date
REGIME_SMA = 200
ENTRY_SMA_BUFFER = 1.02


@register_strategy("alpha_semivol_pit")
class AlphaSemivolPITStrategy(BaseStrategy):
    """Sortino Momentum with Stage-2 SMA150/200 stack and 150-day SMA trailing exit."""

    name = "ALPHA_SEMIVOL_PIT"
    rebalance_frequency = "MONTHLY"
    paper_backup_slots = 60
    required_indicator_columns = [
        "sma150", "ma200", "raw_mom_6m", "mom_6m", "mom12_1", "semi_vol126", "alpha_mom12_1", "alpha_semi_vol126",
        "adt50m", "adt_prev", "sig_prev", "series", "hist_n"
    ]

    def __init__(
        self,
        config: Optional[Any] = None,
        benchmark: Optional[pd.Series] = None,
        use_regime: bool = True,
        delist_haircut: float = 0.0,
        cost_mult: float = 1.0,
        flat_slip: bool = False,
        **kwargs
    ):
        super().__init__(config=config, **kwargs)
        self._benchmark = benchmark
        self.use_regime = use_regime
        self.delist_haircut = delist_haircut
        self.cost_mult = cost_mult
        self.flat_slip = flat_slip

    # ------------------------------------------------------------------ indicators
    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        src = df.sort_values("date").reset_index(drop=True)
        out = src.copy()
        n = len(out)
        close = out["close"].values
        volume = out["volume"].values
        turnover = src["turnover"].values if "turnover" in src.columns else close * volume

        # Moving averages
        out["sma150"] = pd.Series(close).rolling(150, min_periods=150).mean().values
        out["ma200"] = pd.Series(close).rolling(200, min_periods=200).mean().values

        # Momentum inputs: unconditionally compute raw 6M price return into raw_mom_6m
        out["raw_mom_6m"] = (pd.Series(close) / pd.Series(close).shift(126) - 1.0).values
        # Formula-specific columns are computed unconditionally so a pre-computed cache with a differently defined
        # generic column (mom12_1, semi_vol126, ...) can never feed the ranking. Selection reads only alpha_* names.
        out["alpha_mom12_1"] = (pd.Series(close).shift(21) / pd.Series(close).shift(252) - 1.0).values

        # Downside semivolatility 126
        r = np.zeros(n)
        r[1:] = (close[1:] - close[:-1]) / np.where(close[:-1] > 0, close[:-1], 1.0)
        r_down = np.minimum(0.0, r)
        s_down = pd.Series(r_down ** 2).rolling(126, min_periods=60).mean().values
        out["alpha_semi_vol126"] = np.sqrt(s_down * 252.0).astype("float32")

        # Legacy-named aliases (same values), kept for existing consumers/tests; never read by selection.
        out["mom_6m"] = out["raw_mom_6m"]
        out["mom12_1"] = out["alpha_mom12_1"]
        out["semi_vol126"] = out["alpha_semi_vol126"]

        # Liquidity & slippage inputs
        adt = pd.Series(turnover).rolling(50, min_periods=50).median()
        out["adt50m"] = adt.values
        out["adt_prev"] = adt.shift(1).values
        out["sig_prev"] = pd.Series(close).pct_change().rolling(20, min_periods=10).std().shift(1).values
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

                # Fallback to local nse_clean_archive
                if b.empty:
                    try:
                        col = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)["nse_clean_archive"]["benchmark_daily_ohlcv"]
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
        """True when Nifty 500 closed below its 200-session SMA on decision_date."""
        bench = self._bench()
        if bench is None or bench.empty:
            logger.warning("[REGIME_GATE] Benchmark data (^CRSLDX) is missing or empty. Failing safe to Bear Regime.")
            return True
        if decision_date not in bench.index:
            max_b_date = bench.index.max().strftime("%Y-%m-%d") if len(bench) else "N/A"
            logger.warning(
                f"[REGIME_GATE] Decision date {decision_date.strftime('%Y-%m-%d')} not in benchmark (^CRSLDX) index "
                f"(latest date is {max_b_date}). Failing safe to Bear Regime."
            )
            return True
        hist = bench.loc[:decision_date]
        if len(hist) < REGIME_SMA:
            logger.warning(
                f"[REGIME_GATE] Benchmark history ({len(hist)}) < {REGIME_SMA} sessions on {decision_date.strftime('%Y-%m-%d')}. "
                f"Failing safe to Bear Regime."
            )
            return True
        sma200 = float(hist.iloc[-REGIME_SMA:].mean())
        return bool(float(hist.iloc[-1]) < sma200)

    def is_regime_transition_bull(self, decision_date: pd.Timestamp) -> bool:
        """True ONLY when Nifty 500 crossed from Close < SMA200 to Close >= SMA200."""
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
        # 1. Standard monthly rebalance
        if super().is_rebalance_decision_date(timeline, decision_date, config):
            return True
        # 2. Event-driven catch-up rebalance on regime transition Bear -> Bull
        if self.use_regime and self.is_regime_transition_bull(decision_date):
            logger.info(
                f"[REGIME_TRANSITION] Catch-up rebalance triggered on {decision_date.strftime('%Y-%m-%d')} "
                f"(Nifty 500 crossed above 200-day SMA)."
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

        # 2. Event-driven catch-up rebalances
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
        target_dt = np.datetime64(decision_date)
        rows = []
        for sym, df in universe.items():
            if df.empty:
                continue
            dates = df["date"].values
            k = int(np.searchsorted(dates, target_dt))
            if k >= len(dates) or dates[k] != target_dt:
                if sym in current_holdings and k > 0:
                    r = df.iloc[k - 1]
                    if (decision_date - pd.to_datetime(r["date"])).days > 5:
                        continue
                else:
                    continue
            else:
                r = df.iloc[k]

            rows.append((
                sym,
                str(r["series"]),
                float(r["close"]),
                float(r["sma150"]),
                float(r["ma200"]),
                float(r.get("raw_mom_6m", np.nan)),
                float(r.get("alpha_mom12_1", np.nan)),
                float(r.get("alpha_semi_vol126", np.nan)),
                float(r["adt50m"]),
                int(r["hist_n"]),
                nominal_price(r)
            ))

        empty = pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])
        if not rows:
            return empty

        snap = pd.DataFrame(
            rows,
            columns=["symbol", "series", "close", "sma150", "ma200", "raw_mom_6m", "mom12_1", "semi_vol126", "adt", "hist", "trade_close"]
        ).set_index("symbol")

        # 1. New entry criteria: Stage-2 stack, Sortino momentum, Bull regime
        is_bear = self.use_regime and self.regime_below(decision_date)
        if is_bear:
            new_mask = pd.Series(False, index=snap.index)
        else:
            new_mask = (
                (snap["series"] == "EQ")
                & (snap["adt"] >= MIN_ADT)
                & (snap["trade_close"] >= MIN_PRICE)
                & (snap["hist"] >= MIN_HISTORY)
                & (snap["close"] >= ENTRY_SMA_BUFFER * snap["sma150"])
                & (snap["sma150"] > snap["ma200"])
                & (snap["close"] > snap["ma200"])
                & snap["raw_mom_6m"].notna()
                & snap["semi_vol126"].notna()
                & (snap["semi_vol126"] >= 0)
            )

        # 2. Existing holdings criteria (Champion / HoldFix rule):
        # Open positions remain eligible to hold as long as Close >= SMA150 and series is EQ.
        # Bypasses entry buffer and bear regime new-buy blocks.
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

        # Rank all candidates by Sortino Momentum descending (fallback mom12_1 to raw_mom_6m for newer listings)
        mom6 = sel["raw_mom_6m"].fillna(0.0)
        mom12 = sel["mom12_1"].fillna(mom6)
        mom_blend = 0.5 * mom6 + 0.5 * mom12
        sel["score"] = mom_blend / (sel["semi_vol126"] + 0.001)
        sel["score"] = sel["score"].fillna(-9999.0)
        sel = sel.sort_values(by=["score", "adt"], ascending=[False, False])

        out = pd.DataFrame({
            "symbol": sel.index,
            "close": sel["trade_close"].values,
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
        config: Any
    ) -> Dict[str, str]:
        exits: Dict[str, str] = {}
        target_dt = np.datetime64(decision_date)
        for sym in sorted(open_positions):
            df = universe.get(sym)
            if df is None or df.empty:
                continue
            dates = df["date"].values
            k = int(np.searchsorted(dates, target_dt))
            if k >= len(dates) or dates[k] != target_dt:
                continue
            bar = df.iloc[k]
            sma150 = float(bar["sma150"])
            if not np.isnan(sma150) and float(bar["close"]) < sma150:
                exits[sym] = "STRUCTURAL_SMA150"
        return exits

    # ------------------------------------------------------------------ execution cost hook
    def slippage_pct_for(self, sym: str, bar: Any, price: float, shares: int, side: str) -> float:
        if getattr(self, "flat_slip", False):
            return float(self.config.slippage_pct)
        def g(k):
            try:
                v = bar[k]
                return float(v) if v is not None else float("nan")
            except (KeyError, TypeError, ValueError):
                return float("nan")
        adt = g("adt_prev")
        sig = g("sig_prev")
        if np.isnan(adt) or np.isnan(sig) or adt <= 0:
            return float(self.config.slippage_pct)
        f = slippage_fraction(adt, float(price), float(price) * int(shares), sig, self.cost_mult)
        return float(f + (self.delist_haircut if side == "SELL_DELIST" else 0.0))
