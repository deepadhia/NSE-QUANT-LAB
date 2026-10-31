"""Hand-checkable fixtures for nse_quant_lab.tax_india (STT add-back neutralised by zero shares price)."""
import pytest

from nse_quant_lab.tax_india import compute_fy_tax


def tr(exit_date, hold_days, pnl):
    # shares=0 -> STT add-back is zero, so taxable pnl == pnl
    return {"exit_date": exit_date, "hold_days": hold_days, "net_pnl": pnl, "shares": 0,
            "entry_fill_price": 0.0, "exit_fill_price": 0.0}


def test_stcg_15pct_plus_4pct_cess_fy2020():
    tax, cs, cl, _ = compute_fy_tax(2021, [tr("2020-09-01", 100, 100_000)], [], [])
    assert tax == pytest.approx(100_000 * 0.15 * 1.04)
    assert cs == [] and cl == []


def test_stcg_20pct_after_budget_2024():
    tax, *_ = compute_fy_tax(2026, [tr("2025-09-01", 100, 100_000)], [], [])
    assert tax == pytest.approx(100_000 * 0.20 * 1.04)


def test_stcg_10pct_before_2008_with_3pct_cess():
    tax, *_ = compute_fy_tax(2008, [tr("2007-09-01", 100, 100_000)], [], [])
    assert tax == pytest.approx(100_000 * 0.10 * 1.03)


def test_ltcg_exempt_before_april_2018_and_loss_not_carried():
    tax, _, cl, _ = compute_fy_tax(2017, [tr("2016-09-01", 400, 500_000)], [], [])
    assert tax == 0.0
    tax, _, cl, _ = compute_fy_tax(2017, [tr("2016-09-01", 400, -500_000)], [], [])
    assert tax == 0.0 and cl == []


def test_ltcg_10pct_over_1_lakh_fy2020():
    tax, *_ = compute_fy_tax(2021, [tr("2020-09-01", 400, 300_000)], [], [])
    assert tax == pytest.approx((300_000 - 100_000) * 0.10 * 1.04)


def test_ltcg_12_5pct_over_1_25_lakh_fy2026():
    tax, *_ = compute_fy_tax(2026, [tr("2025-09-01", 400, 500_000)], [], [])
    assert tax == pytest.approx((500_000 - 125_000) * 0.125 * 1.04)


def test_short_term_loss_carries_forward_and_offsets_next_year_stcg():
    tax, cs, cl, _ = compute_fy_tax(2022, [tr("2021-09-01", 50, -80_000)], [], [])
    assert tax == 0.0 and cs == [(2022, 80_000)]
    tax, cs2, _, _ = compute_fy_tax(2023, [tr("2022-09-01", 50, 100_000)], cs, [])
    assert tax == pytest.approx(20_000 * 0.15 * 1.04)
    assert cs2 == []


def test_short_term_loss_offsets_long_term_gain_same_year():
    trades = [tr("2020-06-01", 50, -150_000), tr("2020-09-01", 400, 250_000)]
    tax, cs, _, _ = compute_fy_tax(2021, trades, [], [])
    # LTCG 250k - STCL 150k = 100k <= 1 lakh exemption -> no tax
    assert tax == 0.0 and cs == []


def test_long_term_loss_cannot_offset_short_term_gain():
    trades = [tr("2020-06-01", 400, -150_000), tr("2020-09-01", 50, 100_000)]
    tax, _, cl, _ = compute_fy_tax(2021, trades, [], [])
    assert tax == pytest.approx(100_000 * 0.15 * 1.04)
    assert cl == [(2021, 150_000)]


def test_carried_loss_expires_after_eight_years():
    tax, *_ = compute_fy_tax(2030, [tr("2029-09-01", 50, 100_000)], [(2020, 100_000)], [])
    assert tax == pytest.approx(100_000 * 0.20 * 1.04)


def test_stt_is_added_back():
    t = {"exit_date": "2020-09-01", "hold_days": 50, "net_pnl": 1000.0, "shares": 100,
         "entry_fill_price": 100.0, "exit_fill_price": 110.0}
    tax, *_ = compute_fy_tax(2021, [t], [], [])
    assert tax == pytest.approx((1000.0 + 0.001 * 100 * 210.0) * 0.15 * 1.04)
