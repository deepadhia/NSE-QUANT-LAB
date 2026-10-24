"""
indicators.py -- Pure Causal Technical Indicators
"""

from __future__ import annotations
from typing import Optional, Any
import numpy as np
import pandas as pd


def compute_indicators(df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
    """
    Computes technical indicators strictly causal (backward-looking):
    1. 100-day EMA: Seeded by 100-day SMA, then recursive EMA formula
    2. 10-period ATR: True Range smoothed with alpha = 1 / 10
    3. Supertrend(10, 3.0): Volatility bands and regime flips
    4. 6-Month (126-session) Momentum: P_T / P_{T-126} - 1.0
    5. 50-day average daily turnover: rolling mean of (Close * Volume)
    """
    if "ema100" in df.columns and "supertrend_dir" in df.columns and "mom_6m" in df.columns and "turnover_50d" in df.columns:
        return df

    min_hist = getattr(config, "min_history_sessions", 226)
    if len(df) < min_hist:
        return pd.DataFrame()

    out = df.copy()
    out.sort_values("date", ascending=True, inplace=True)
    out.reset_index(drop=True, inplace=True)

    c = out["close"].values
    n = len(c)
    p = getattr(config, "ema_period", 100)

    # 1. 100-day EMA
    ema = np.full(n, np.nan, dtype=np.float64)
    if n >= p:
        ema[p - 1] = np.mean(c[:p])
        alpha = 2.0 / (p + 1.0)
        for i in range(p, n):
            ema[i] = alpha * c[i] + (1.0 - alpha) * ema[i - 1]
    out["ema100"] = ema

    # 2. 10-period ATR
    h = out["high"].values
    l = out["low"].values
    tr = np.zeros(n, dtype=np.float64)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        hl = h[i] - l[i]
        hpc = abs(h[i] - c[i - 1])
        lpc = abs(l[i] - c[i - 1])
        tr[i] = max(hl, hpc, lpc)

    atr_period = getattr(config, "atr_period", 10)
    atr = np.full(n, np.nan, dtype=np.float64)
    if n >= atr_period:
        atr[atr_period - 1] = np.mean(tr[:atr_period])
        alpha_atr = 1.0 / float(atr_period)
        for i in range(atr_period, n):
            atr[i] = alpha_atr * tr[i] + (1.0 - alpha_atr) * atr[i - 1]
    out["atr10"] = atr

    # 3. Supertrend (10, 3.0)
    mult = getattr(config, "supertrend_mult", 3.0)
    st_upper = np.full(n, np.nan, dtype=np.float64)
    st_lower = np.full(n, np.nan, dtype=np.float64)
    st_dir = np.zeros(n, dtype=np.int32)
    hl2 = (h + l) / 2.0

    for i in range(atr_period - 1, n):
        curr_atr = atr[i]
        basic_upper = hl2[i] + mult * curr_atr
        basic_lower = hl2[i] - mult * curr_atr

        if i == atr_period - 1:
            st_upper[i] = basic_upper
            st_lower[i] = basic_lower
            st_dir[i] = 1 if c[i] >= basic_lower else -1
            continue

        prev_upper = st_upper[i - 1]
        prev_lower = st_lower[i - 1]
        prev_dir = st_dir[i - 1]

        if basic_lower > prev_lower or c[i - 1] < prev_lower:
            st_lower[i] = basic_lower
        else:
            st_lower[i] = prev_lower

        if basic_upper < prev_upper or c[i - 1] > prev_upper:
            st_upper[i] = basic_upper
        else:
            st_upper[i] = prev_upper

        if prev_dir == 1:
            st_dir[i] = -1 if c[i] < st_lower[i] else 1
        else:
            st_dir[i] = 1 if c[i] > st_upper[i] else -1

    out["supertrend_dir"] = st_dir
    out["supertrend_lower"] = st_lower
    out["supertrend_upper"] = st_upper

    # 4. 6-Month Momentum
    mom_lookback = getattr(config, "mom_lookback", 126)
    mom_6m = np.full(n, np.nan)
    for i in range(mom_lookback, n):
        p_base = c[i - mom_lookback]
        if p_base > 0:
            mom_6m[i] = (c[i] / p_base) - 1.0
    out["mom_6m"] = mom_6m

    # 5. 50-day Turnover
    to = c * out["volume"].values
    out["turnover_50d"] = pd.Series(to).rolling(50, min_periods=50).mean().values

    return out
