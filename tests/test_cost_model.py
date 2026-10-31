import math

import pytest

from nse_quant_lab.cost_model import MAX_FRACTION, slippage_fraction, spread_bps


def test_tiers():
    assert spread_bps(2e8) == 8.0
    assert spread_bps(5e7) == 15.0
    assert spread_bps(1.5e7) == 25.0
    assert spread_bps(7e6) == 40.0
    assert spread_bps(3e6) == 60.0
    assert spread_bps(1e6) == 90.0
    assert spread_bps(None) == 90.0 and spread_bps(float("nan")) == 90.0


def test_hand_checked_midcap_order():
    # ADT Rs 2 Cr (tier 25 bps), price 200, order Rs 20k, sigma 2%: impact = 0.7*0.02*sqrt(20000/2e7)*1e4 = 4.427 bps
    got = slippage_fraction(2e7, 200.0, 20_000.0, 0.02)
    assert got == pytest.approx((25.0 + 0.7 * 0.02 * math.sqrt(20_000 / 2e7) * 1e4) / 1e4)


def test_tick_floor_for_cheap_stock():
    # Rs 5 stock: half tick = 0.025/5 = 50 bps, above the 8 bps tier
    got = slippage_fraction(2e8, 5.0, 1_000.0, 0.02)
    assert got >= 0.0050


def test_cost_rises_with_order_size_and_illiquidity():
    small = slippage_fraction(3e6, 100.0, 10_000.0, 0.03)
    big = slippage_fraction(3e6, 100.0, 300_000.0, 0.03)
    liquid = slippage_fraction(2e8, 100.0, 10_000.0, 0.03)
    assert big > small > liquid


def test_multiplier_and_cap():
    base = slippage_fraction(3e6, 100.0, 10_000.0, 0.03)
    assert slippage_fraction(3e6, 100.0, 10_000.0, 0.03, mult=1.5) == pytest.approx(base * 1.5)
    assert slippage_fraction(1e3, 100.0, 1e9, 0.5) == MAX_FRACTION
