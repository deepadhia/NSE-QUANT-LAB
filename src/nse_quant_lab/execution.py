"""
execution.py -- Indian Equity Market Circuit & Limit Execution Rules
"""

from typing import Tuple


def is_locked_down(open_px: float, high_px: float, low_px: float, close_px: float, prev_close: float) -> bool:
    """
    Checks if bar is locked limit-down (Lower Circuit).
    Conditions:
    1. Entire bar traded at single price: Open == High == Low == Close
    2. Price is strictly below previous close (negative return)
    """
    if open_px <= 0 or prev_close <= 0:
        return False
    is_flat_bar = (open_px == high_px == low_px == close_px)
    is_down = (close_px < prev_close)
    is_open_low_lock = (open_px == low_px and close_px < prev_close and abs(high_px - low_px) / prev_close < 0.001)
    return is_flat_bar and is_down or is_open_low_lock


def is_locked_up(open_px: float, high_px: float, low_px: float, close_px: float, prev_close: float) -> bool:
    """
    Checks if bar is locked limit-up (Upper Circuit).
    Conditions:
    1. Entire bar traded at single price: Open == High == Low == Close
    2. Price is strictly above previous close (positive return)
    """
    if open_px <= 0 or prev_close <= 0:
        return False
    is_flat_bar = (open_px == high_px == low_px == close_px)
    is_up = (close_px > prev_close)
    is_open_high_lock = (open_px == high_px and close_px > prev_close and abs(high_px - low_px) / prev_close < 0.001)
    return is_flat_bar and is_up or is_open_high_lock
