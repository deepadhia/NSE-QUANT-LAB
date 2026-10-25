"""
donchian.py -- Donchian Channel 50-Day High Breakout Strategy
"""

from __future__ import annotations
from typing import Dict, Any, Optional, List
import pandas as pd
from ..base_strategy import BaseStrategy
from ..registry import register_strategy


@register_strategy("donchian_breakout")
class DonchianBreakoutStrategy(BaseStrategy):
    """
    Donchian Breakout Trend Following:
    - Entry: Close breaks above 50-day High with 50-day avg turnover >= Rs 1 Crore
    - Exit: Close falls below 20-day Low
    - Schedule: Daily evaluation
    """
    name: str = "DONCHIAN_BREAKOUT"
    rebalance_frequency: str = "DAILY"

    def __init__(self, config: Optional[Any] = None, **kwargs):
        super().__init__(config=config, **kwargs)

    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        df = df.copy()
        df["high_50"] = df["high"].rolling(50).max().shift(1)
        df["low_20"] = df["low"].rolling(20).min().shift(1)
        df["turnover"] = df["close"] * df["volume"]
        df["avg_turnover"] = df["turnover"].rolling(50).mean()
        return df

    def select_entry_candidates(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        current_holdings: Dict[str, Any],
        config: Any,
    ) -> pd.DataFrame:
        candidates = []
        for sym, df in universe.items():
            if sym in current_holdings:
                continue
            rows = df.loc[df["date"] == decision_date]
            if rows.empty:
                continue
            r = rows.iloc[0]
            if pd.notna(r["high_50"]) and r["close"] > r["high_50"] and r.get("avg_turnover", 1e8) >= 10_000_000.0:
                score = (r["close"] - r["high_50"]) / r["high_50"]
                candidates.append({"symbol": sym, "score": score, "close": r["close"]})

        if not candidates:
            return pd.DataFrame(columns=["symbol", "score", "close"]).set_index("symbol")

        df_c = pd.DataFrame(candidates)
        df_c["rank"] = df_c["score"].rank(ascending=False)
        return df_c.sort_values(by="score", ascending=False).set_index("symbol", drop=False)

    def evaluate_rebalance_exits(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        open_positions: Dict[str, Any],
        target_symbols: List[str],
        config: Any,
    ) -> Dict[str, str]:
        """
        Donchian breakout positions exit strictly on technical stop breaches (evaluate_daily_exits:
        close < low_20), never on daily rebalance rank drops.
        """
        return {}

    def evaluate_daily_exits(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        open_positions: Dict[str, Any],
        config: Any,
    ) -> Dict[str, str]:
        exits = {}
        for sym in list(open_positions.keys()):
            df = universe.get(sym)
            if df is None:
                continue
            rows = df.loc[df["date"] == decision_date]
            if rows.empty:
                continue
            r = rows.iloc[0]
            if pd.notna(r["low_20"]) and r["close"] < r["low_20"]:
                exits[sym] = "DONCHIAN_20D_LOW_BREACH"
        return exits
