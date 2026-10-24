import pandas as pd
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.fees import calc_buy_fees_exact, calc_sell_fees_exact


def test_statutory_fee_decomposition():
    cfg = BacktestConfig(brokerage_per_order=20.0, stt_pct=0.100, stamp_duty_buy_pct=0.015, gst_pct=18.0)
    gross = 100_000.0
    dt_legacy = pd.Timestamp("2024-05-15")
    dt_revised = pd.Timestamp("2024-10-15")

    buy_leg = calc_buy_fees_exact(gross, dt_legacy, cfg)
    buy_rev = calc_buy_fees_exact(gross, dt_revised, cfg)

    assert round(buy_leg["stt"], 2) == 100.0
    assert round(buy_leg["stamp_duty"], 2) == 15.0
    assert buy_rev["exchange_txn"] < buy_leg["exchange_txn"], "Revised exchange fee must be lower post Oct 1, 2024"

    sell_fees = calc_sell_fees_exact(gross, dt_revised, cfg)
    assert sell_fees["stamp_duty"] == 0.0, "Stamp duty must not apply to sell deliveries"
    assert sell_fees["dp_charges"] == 15.93, "DP charge must apply to sells"
