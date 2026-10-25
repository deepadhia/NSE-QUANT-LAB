"""
execution.py -- Indian Equity Market Circuit & Limit Execution Rules
"""

from typing import Tuple


def is_locked_down(open_px: float, high_px: float, low_px: float, close_px: float, prev_close: float) -> bool:
    """
    Checks if bar is locked limit-down (Lower Circuit).
    Conditions:
    1. Price drop from prev_close must meet or exceed minimum exchange circuit threshold (4.8%).
    2. Entire bar traded at single price: Open == High == Low == Close OR opened at low with negligible range.
    """
    if open_px <= 0 or prev_close <= 0:
        return False
    pct_down = (prev_close - close_px) / prev_close
    if pct_down < 0.048:
        return False
    is_flat_bar = (open_px == high_px == low_px == close_px)
    is_open_low_lock = (open_px == low_px and abs(high_px - low_px) / prev_close < 0.001)
    return is_flat_bar or is_open_low_lock


def is_locked_up(open_px: float, high_px: float, low_px: float, close_px: float, prev_close: float) -> bool:
    """
    Checks if bar is locked limit-up (Upper Circuit).
    Conditions:
    1. Price rise from prev_close must meet or exceed minimum exchange circuit threshold (4.8%).
    2. Entire bar traded at single price: Open == High == Low == Close OR opened at high with negligible range.
    """
    if open_px <= 0 or prev_close <= 0:
        return False
    pct_up = (close_px - prev_close) / prev_close
    if pct_up < 0.048:
        return False
    is_flat_bar = (open_px == high_px == low_px == close_px)
    is_open_high_lock = (open_px == high_px and abs(high_px - low_px) / prev_close < 0.001)
    return is_flat_bar or is_open_high_lock
