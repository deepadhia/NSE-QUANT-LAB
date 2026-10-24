import numpy as np
import pandas as pd
from nse_quant_lab.indicators import compute_indicators
from nse_quant_lab.data_loader import make_synthetic_stock


def test_indicator_calculations():
    df = make_synthetic_stock("TEST.NS", n_days=250, start_price=100.0, daily_ret=0.002)
    ind = compute_indicators(df)

    assert not ind.empty
    assert "ema100" in ind.columns
    assert "supertrend_dir" in ind.columns
    assert "mom_6m" in ind.columns
    assert "turnover_50d" in ind.columns

    # First 99 bars of EMA must be NaN
    assert np.isnan(ind["ema100"].iloc[0])
    assert not np.isnan(ind["ema100"].iloc[99])
