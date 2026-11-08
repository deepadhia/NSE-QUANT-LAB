"""Strategy-agnostic ENTRY-ONLY filters shared by the backtest engine and the paper-trading manager.

One implementation, called from both places (engine.run and PaperTradingManager.generate_post_market_signals), so a filter that is
switched on in research behaves identically in paper trading and any future live mode. Strategies stay untouched: the filter runs on
the candidate frame that ``strategy.select_entry_candidates`` returns, and works for every strategy that returns a ``close`` column.

max_entry_price (config field, default None = off)
    A NEW entry is allowed only if the candidate's nominal ``close`` on the decision date is strictly below the cap (Rs per share).
    * Held names are exempt: a position that has risen above the cap is never dropped or blocked (same convention as every
      other entry-only rule in this repo; the rebalance-exit logic still sees them in the ranking).
    * Candidates are re-ranked 1..N after filtering so the next-ranked eligible name takes the slot.
    * The cap tests the SIGNAL-day close. The morning fill may open up to +2% higher (limit_cap_pct), so a fill slightly above the cap is
      expected and is not a violation.
    * ``close`` must be the NOMINAL (actual rupee) price. The point-in-time strategies (trend_momentum_pit, alpha_semivol_pit,
      minervini_trend_template) return nominal prices; legacy strategies on split-adjusted frames would apply the cap to an adjusted price.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd


def entry_price_cap(config: Any) -> Optional[float]:
    """The configured cap as a positive float, or None when the filter is off."""
    cap = getattr(config, "max_entry_price", None)
    if cap is None:
        return None
    cap = float(cap)
    if not np.isfinite(cap) or cap < 0:
        raise ValueError(f"max_entry_price must be a positive number or None, got {cap!r}")
    return cap if cap > 0 else None


def apply_entry_price_cap(candidates: pd.DataFrame, current_holdings: Mapping[str, Any], config: Any) -> pd.DataFrame:
    """Drop NEW candidates whose nominal close is at or above ``config.max_entry_price``; keep held names; re-rank.

    No-op (returns the input unchanged) when the cap is off or there are no candidates. Fails loudly if the cap is on but a candidate has no
    usable ``close`` (an unknown price must never be treated as cheap)."""
    cap = entry_price_cap(config)
    if cap is None or candidates is None or candidates.empty:
        return candidates
    if "close" not in candidates.columns:
        raise ValueError("max_entry_price is set but the strategy's candidate frame has no 'close' column")
    close = pd.to_numeric(candidates["close"], errors="coerce")
    if close.isna().any():
        bad = list(candidates.index[close.isna()])[:5]
        raise ValueError(f"max_entry_price is set but candidates {bad} have no usable close")
    held = candidates.index.isin(list(current_holdings))
    keep = np.asarray((close < cap) | held)
    out = candidates[keep].copy()
    if "rank" in out.columns:
        out["rank"] = np.arange(1, len(out) + 1)
    return out
