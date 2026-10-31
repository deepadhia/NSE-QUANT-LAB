"""
cost_model.py -- liquidity-tiered, order-size-aware execution cost for NSE cash equities (per leg, as a price fraction).

Why: a flat 20 bps understates small-cap costs and overstates large-cap costs. Real cost at the open has three parts:
  1. spread/latency cost, which depends on how liquid the stock is (tier table below, ASSUMED values, not measured),
  2. a tick-size floor (a Rs 12 stock cannot trade tighter than half a Rs 0.05 tick),
  3. market impact that grows with sqrt(order value / average daily turnover) (square-root law, coefficient ASSUMED 0.7).
Inputs are point-in-time: ADT and daily volatility are measured up to the PREVIOUS close, never the fill day.

All numbers here are assumptions until the forward paper test measures real fills versus the next-open price.
Use `mult` to stress them (1.5 = 50% worse than assumed).
"""

from __future__ import annotations

import math
from typing import Optional

# (minimum 50-day median turnover in Rs, per-leg spread+latency cost in basis points for a tiny order)
SPREAD_TIERS_BPS = (
    (1.0e8, 8.0),    # >= Rs 10 Cr / day
    (3.0e7, 15.0),   # Rs 3 - 10 Cr
    (1.0e7, 25.0),   # Rs 1 - 3 Cr
    (5.0e6, 40.0),   # Rs 50 L - 1 Cr
    (2.5e6, 60.0),   # Rs 25 - 50 L
    (0.0, 90.0),     # below Rs 25 L
)
TICK = 0.05
IMPACT_COEF = 0.7
DEFAULT_SIGMA = 0.03
MIN_BPS = 5.0
MAX_FRACTION = 0.08


def spread_bps(adt: Optional[float]) -> float:
    if adt is None or not (adt == adt) or adt <= 0:
        return SPREAD_TIERS_BPS[-1][1]
    for floor, bps in SPREAD_TIERS_BPS:
        if adt >= floor:
            return bps
    return SPREAD_TIERS_BPS[-1][1]


def slippage_fraction(
    adt_prev: Optional[float],
    price: float,
    order_value: float,
    sigma_prev: Optional[float] = None,
    mult: float = 1.0,
) -> float:
    """Per-leg cost as a fraction of price (0.002 == 20 bps)."""
    sp = spread_bps(adt_prev)
    tick_floor = (0.5 * TICK / price) * 1e4 if price > 0 else 0.0
    base_bps = max(sp, tick_floor)
    sig = sigma_prev if (sigma_prev is not None and sigma_prev == sigma_prev and sigma_prev > 0) else DEFAULT_SIGMA
    part = order_value / adt_prev if (adt_prev is not None and adt_prev == adt_prev and adt_prev > 0) else 1.0
    impact_bps = IMPACT_COEF * sig * math.sqrt(max(part, 0.0)) * 1e4
    total = max(MIN_BPS, (base_bps + impact_bps) * mult) / 1e4
    return min(total, MAX_FRACTION)
