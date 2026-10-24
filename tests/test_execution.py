import pytest
import pandas as pd
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.data_loader import make_synthetic_stock


def test_gap_limit_cap_cancellation():
    """Verifies that if Open opens > +2% above Close, order is cancelled."""
    cfg = BacktestConfig(initial_capital=100_000.0, portfolio_size=1)
    df = make_synthetic_stock("GAP.NS", n_days=280, start_price=100.0, daily_ret=0.001, start_date="2018-01-01")

    # Force 2019-01-01 open to gap +5% above 2018-12-31 close
    idx_open = df[df["date"] == "2019-01-01"].index[0]
    prev_close = df.loc[idx_open - 1, "close"]
    df.loc[idx_open, "open"] = prev_close * 1.05

    universe = {"GAP.NS": df}
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    metrics, trades, orders, execs, nav = engine.run(
        universe, eval_start_date="2019-01-01", eval_end_date="2019-01-10"
    )

    gap_orders = orders[orders["status"] == "SKIPPED_GAP_LIMIT_EXCEEDED"]
    assert not gap_orders.empty, "Order must be cancelled on gap > 2%"
    assert execs.empty or "action" not in execs.columns or execs[execs["action"] == "BUY"].empty
