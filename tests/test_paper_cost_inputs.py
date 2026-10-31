import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy


def bars(n=80, vol=1_000_000, px=100.0):
    return pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=n), "close": px * (1 + 0.001 * np.arange(n)),
                         "volume": float(vol)})


def test_cost_inputs_rebuilt_from_raw_bars_when_indicators_missing():
    d = bars()
    got = PaperTradingManager._cost_inputs(d)
    expected = float((d["close"] * d["volume"]).tail(50).median())
    assert got["adt_prev"] == pytest.approx(expected)
    assert got["sig_prev"] > 0 and got["series"] == "EQ"


def test_cost_inputs_prefers_precomputed_adt_and_uses_turnover_column():
    d = bars()
    d["adt50m"] = 123.0
    assert PaperTradingManager._cost_inputs(d)["adt_prev"] == 123.0
    d2 = bars()
    d2["turnover"] = 5_000.0
    assert PaperTradingManager._cost_inputs(d2)["adt_prev"] == pytest.approx(5_000.0)


def test_short_history_gives_nan_adt_not_a_made_up_number():
    assert np.isnan(PaperTradingManager._cost_inputs(bars(n=10))["adt_prev"])


def test_liquid_stock_gets_a_realistic_cost_after_the_fix():
    d = bars(vol=2_000_000)  # ~Rs 20+ Cr/day
    strat = TrendMomentumPITStrategy()
    bar = {**PaperTradingManager._cost_inputs(d)}
    f = strat.slippage_pct_for("X", bar, 100.0, 70, "BUY")
    assert f < 0.0030  # well under 30 bps, nowhere near the 175-415 bps seen with the missing-input bug


def test_strategy_asks_for_many_paper_backups():
    assert TrendMomentumPITStrategy.paper_backup_slots >= 30
