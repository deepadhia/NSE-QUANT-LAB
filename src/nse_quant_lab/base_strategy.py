"""
base_strategy.py -- Strategy Protocol & Abstract Base Class
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
import pandas as pd


class BaseStrategy(ABC):
    """
    Abstract Base Class for quantitative trading strategies.
    Implement this interface to plug a strategy into the universal engine
    or forward paper trading harness.
    """
    name: str = "BASE_STRATEGY"
    rebalance_frequency: str = "MONTHLY"  # "MONTHLY" or "DAILY"

    def __init__(self, config: Optional[Any] = None, **kwargs):
        self.config = config
        for k, v in kwargs.items():
            setattr(self, k, v)

    @abstractmethod
    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        """Enriches raw OHLCV dataframe with causal indicators."""
        pass

    @abstractmethod
    def select_entry_candidates(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        current_holdings: Dict[str, Any],
        config: Any,
    ) -> pd.DataFrame:
        """Returns qualifying candidates on decision_date sorted descending by rank."""
        pass

    @abstractmethod
    def evaluate_daily_exits(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        open_positions: Dict[str, Any],
        config: Any,
    ) -> Dict[str, str]:
        """Returns {symbol: exit_reason} for active positions triggering an exit."""
        pass

    def evaluate_rebalance_exits(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        open_positions: Dict[str, Any],
        target_symbols: List[str],
        config: Any,
    ) -> Dict[str, str]:
        """Returns {symbol: exit_reason} for held stocks dropped during rebalancing."""
        target_set = set(target_symbols)
        exits = {}
        for sym in sorted(list(open_positions.keys())):
            if sym not in target_set:
                exits[sym] = "REBALANCE_DROP"
        return exits

    def is_rebalance_decision_date(
        self,
        timeline: List[pd.Timestamp],
        decision_date: pd.Timestamp,
        config: Any,
    ) -> bool:
        """
        Determines if decision_date is a rebalancing decision date (e.g. today at 15:45 IST).
        Works seamlessly for both historical replay and live/forward paper trading.
        """
        freq = str(getattr(self, "rebalance_frequency", None) or getattr(config, "rebalance_frequency", "MONTHLY")).upper()
        if freq == "DAILY":
            return True

        if freq == "MONTHLY":
            sorted_dates = sorted(timeline)
            # If timeline extends beyond decision_date, check if next session is in a new month
            if decision_date in sorted_dates:
                idx = sorted_dates.index(decision_date)
                if idx + 1 < len(sorted_dates):
                    return sorted_dates[idx + 1].month != decision_date.month

            # Live/Forward mode: decision_date is the latest available date in universe.
            # Check if there are any remaining trading days in the current calendar month.
            try:
                from nse_quant_lab.holidays import is_trading_day
                curr_month = decision_date.month
                test_dt = decision_date + pd.Timedelta(days=1)
                while test_dt.month == curr_month:
                    if is_trading_day(test_dt):
                        return False
                    test_dt += pd.Timedelta(days=1)
                return True
            except Exception:
                # Conservative fallback: check if decision_date is within last 3 days of month
                return decision_date.day >= 26

        return False

    def get_rebalance_schedule(
        self,
        timeline: List[pd.Timestamp],
        eval_start: pd.Timestamp,
        eval_end: pd.Timestamp,
        config: Optional[Any] = None,
    ) -> Dict[pd.Timestamp, pd.Timestamp]:
        """Maps execution dates to their corresponding decision dates."""
        freq = getattr(self, "rebalance_frequency", None) or getattr(config, "rebalance_frequency", "MONTHLY")
        date_to_idx = {d: i for i, d in enumerate(timeline)}
        decision_to_exec_map: Dict[pd.Timestamp, pd.Timestamp] = {}

        if str(freq).upper() == "DAILY":
            for i in range(len(timeline) - 1):
                d_dec = timeline[i]
                d_exec = timeline[i + 1]
                if eval_start <= d_exec <= eval_end:
                    decision_to_exec_map[d_dec] = d_exec
        else:
            df_dates = pd.DataFrame({"date": timeline})
            df_dates["year_month"] = df_dates["date"].dt.to_period("M")
            month_last_sessions = df_dates.groupby("year_month")["date"].max().tolist()
            for d_dec in month_last_sessions:
                if d_dec in date_to_idx:
                    idx = date_to_idx[d_dec]
                    if idx + 1 < len(timeline):
                        d_exec = timeline[idx + 1]
                        if eval_start <= d_exec <= eval_end:
                            decision_to_exec_map[d_dec] = d_exec

        return {v: k for k, v in decision_to_exec_map.items()}
