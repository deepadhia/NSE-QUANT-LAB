import pytest
import pandas as pd
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.data_loader import make_synthetic_stock


def test_balance_sheet_identity_and_cash_conservation():
    """Asserts Cash + Holdings == NAV to machine precision on every session."""
    cfg = BacktestConfig(initial_capital=200_000.0, portfolio_size=2)
    s1 = make_synthetic_stock("STOCK1.NS", n_days=300, start_price=100.0, daily_ret=0.001)
    s2 = make_synthetic_stock("STOCK2.NS", n_days=300, start_price=200.0, daily_ret=0.0015)
    universe = {"STOCK1.NS": s1, "STOCK2.NS": s2}

    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)
    dates = s1["date"].tolist()

    metrics, trades, orders, execs, nav = engine.run(
        universe, eval_start_date=str(dates[240].date()), eval_end_date=str(dates[-1].date())
    )

    diffs = (nav["cash"] + nav["holdings_val"]) - nav["nav"]
    max_diff = diffs.abs().max()
    assert max_diff < 1e-9, f"Balance sheet discrepancy: {max_diff}"
    assert (nav["cash"] >= 0).all(), "Cash must never become negative"
