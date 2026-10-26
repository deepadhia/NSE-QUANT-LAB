"""
trend_momentum.py -- Dual-Filter Trend Momentum Strategy
"""

from __future__ import annotations
from typing import Dict, Any, Optional, List
import numpy as np
import pandas as pd

from ..base_strategy import BaseStrategy
from ..config import BacktestConfig
from ..indicators import compute_indicators
from ..registry import register_strategy
from ..pit_universe import PointInTimeUniverse


@register_strategy("trend_momentum")
class TrendMomentumStrategy(BaseStrategy):
    """
    Dual-Filter Trend Momentum Strategy:
    - Universe: Nifty 500 equities (Price >= 50, 50-day Turnover >= 1 Crore)
    - Structural Filters: Close >= 1.03 * EMA(100) AND Bullish Supertrend(10, 3)
    - Ranking: 6-Month (126-session) Momentum descending
    - Rebalance: Scheduled monthly on month-end close -> next-day Open fills
    - Daily Exits: Close < EMA(100) OR Supertrend turns Bearish
    """
    name: str = "TREND_MOMENTUM"
    rebalance_frequency: str = "MONTHLY"

    def __init__(self, config: Optional[Any] = None, **kwargs):
        super().__init__(config=config, **kwargs)
        self.pit_universe = PointInTimeUniverse(enforce_pit=True)

    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        return compute_indicators(df, config or self.config)

    def select_entry_candidates(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        current_holdings: Dict[str, Any],
        config: Any,
    ) -> pd.DataFrame:
        candidates = []
        cfg = config or self.config
        min_price = getattr(cfg, "min_price", 20.0)
        min_to = getattr(cfg, "min_turnover_50d", 10_000_000.0)
        ema_buf = getattr(cfg, "ema_buffer_pct", 0.03)
        enforce_pit = getattr(cfg, "enforce_pit_universe", True)
        top_n = getattr(cfg, "pit_top_n_liquidity", 200)
        max_pct = getattr(cfg, "pit_max_universe_pct", 0.45)

        top_liquid_set = None
        if enforce_pit and top_n > 0:
            top_liquid_set = self.pit_universe.get_top_liquid_universe(universe, decision_date, top_n=top_n, max_universe_pct=max_pct)

        for sym, df in universe.items():
            if enforce_pit and not self.pit_universe.is_eligible(sym, decision_date, top_liquid_set=top_liquid_set):
                continue
            if df.empty:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            row = sub.iloc[0]

            close = float(row["close"])
            ema = float(row["ema100"])
            st_dir = int(row["supertrend_dir"])
            mom = float(row["mom_6m"])
            to50 = float(row["turnover_50d"])

            if np.isnan(close) or np.isnan(ema) or np.isnan(mom) or np.isnan(to50):
                continue

            if close < min_price or to50 < min_to or close < (1.0 + ema_buf) * ema or st_dir != 1:
                continue

            candidates.append({
                "symbol": sym,
                "close": close,
                "ema100": ema,
                "supertrend_dir": st_dir,
                "mom_6m": mom,
                "turnover_50d": to50,
            })

        if not candidates:
            return pd.DataFrame(columns=["symbol", "close", "ema100", "supertrend_dir", "mom_6m", "turnover_50d", "rank"])

        cdf = pd.DataFrame(candidates)
        cdf = cdf.sort_values(by=["mom_6m", "turnover_50d", "symbol"], ascending=[False, False, True]).reset_index(drop=True)
        cdf["rank"] = np.arange(1, len(cdf) + 1)
        return cdf.set_index("symbol", drop=False)

    def evaluate_daily_exits(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        open_positions: Dict[str, Any],
        config: Any,
    ) -> Dict[str, str]:
        exits: Dict[str, str] = {}
        for sym in sorted(list(open_positions.keys())):
            df = universe.get(sym)
            if df is None or df.empty:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            bar = sub.iloc[0]
            close_today = float(bar["close"])
            ema_today = float(bar["ema100"])
            st_dir_today = int(bar["supertrend_dir"])

            if np.isnan(close_today) or np.isnan(ema_today):
                continue

            # Structural Exit: Close below 100-day EMA (removes tight 10-day Supertrend whipsaw)
            if close_today < ema_today:
                exits[sym] = "STRUCTURAL_EMA100"
        return exits

    def evaluate_rebalance_exits(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        open_positions: Dict[str, Any],
        target_symbols: List[str],
        config: Any,
    ) -> Dict[str, str]:
        target_set = set(target_symbols)
        exits = {}
        for sym in sorted(list(open_positions.keys())):
            if sym not in target_set:
                exits[sym] = "REBALANCE_DROP"
        return exits
