"""
test_holidays.py -- Unit tests for NSE market holiday and trading session calendar
"""

import pytest
from datetime import datetime
from nse_quant_lab.holidays import is_trading_day, get_nse_holidays


def test_weekend_rejection():
    # Saturday
    sat = datetime(2024, 10, 19)
    assert not is_trading_day(sat)

    # Sunday
    sun = datetime(2024, 10, 20)
    assert not is_trading_day(sun)


def test_official_nse_holiday_rejection():
    # Republic Day (Jan 26)
    assert not is_trading_day("2024-01-26")

    # Gandhi Jayanti (Oct 2)
    assert not is_trading_day("2024-10-02")

    # Christmas (Dec 25)
    assert not is_trading_day("2024-12-25")

    # Maharashtra Day (May 1)
    assert not is_trading_day("2025-05-01")


def test_normal_trading_day_acceptance():
    # Normal trading Wednesday
    assert is_trading_day("2024-05-15")

    # Normal trading Tuesday
    assert is_trading_day("2024-10-22")
