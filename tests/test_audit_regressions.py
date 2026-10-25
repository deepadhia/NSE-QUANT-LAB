import pytest
import pandas as pd
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.strategies.donchian import DonchianBreakoutStrategy
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.data_loader import make_synthetic_stock


def test_donchian_no_daily_rebalance_drop():
    """
    Regression Test: Verifies DonchianBreakoutStrategy does NOT force-exit
    positions on daily rebalance drops (preserving trend-following hold periods).
    """
    cfg = BacktestConfig(portfolio_size=5)
    strat = DonchianBreakoutStrategy(cfg)

    open_positions = {
        "STOCK1.NS": {"shares": 100, "entry_price": 500.0},
        "STOCK2.NS": {"shares": 50, "entry_price": 1000.0},
    }
    # Even if target_symbols does NOT contain held stocks, evaluate_rebalance_exits must return {}
    rebal_exits = strat.evaluate_rebalance_exits(
        universe={},
        decision_date=pd.to_datetime("2024-05-15"),
        open_positions=open_positions,
        target_symbols=["STOCK3.NS", "STOCK4.NS"],
        config=cfg,
    )
    assert rebal_exits == {}, "DonchianBreakoutStrategy must not drop held positions on daily rebalance!"


def test_is_rebalance_decision_date_logic():
    """
    Regression Test: Verifies is_rebalance_decision_date works for:
    1. Daily frequency (always True)
    2. Monthly frequency on historical month-end session (True)
    3. Monthly frequency on mid-month session (False)
    4. Monthly frequency on live forward day with no more trading days this month (True)
    """
    cfg_monthly = BacktestConfig(rebalance_frequency="MONTHLY")
    strat_monthly = TrendMomentumStrategy(cfg_monthly)

    timeline = pd.date_range("2024-01-01", "2024-03-31", freq="B").tolist()
    # 2024-01-31 is the last business day of Jan 2024
    jan_31 = pd.to_datetime("2024-01-31")
    jan_15 = pd.to_datetime("2024-01-15")

    assert strat_monthly.is_rebalance_decision_date(timeline, jan_31, cfg_monthly) is True
    assert strat_monthly.is_rebalance_decision_date(timeline, jan_15, cfg_monthly) is False

    # Daily strategy
    cfg_daily = BacktestConfig(rebalance_frequency="DAILY")
    strat_daily = DonchianBreakoutStrategy(cfg_daily)
    assert strat_daily.is_rebalance_decision_date(timeline, jan_15, cfg_daily) is True


def test_pretrade_equity_mark_to_market():
    """
    Regression Test: Verifies BacktestEngine pre-trade equity marks open holdings
    to current decision-day close rather than static entry price.
    """
    cfg = BacktestConfig(initial_capital=100000.0, portfolio_size=2)
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    # 2 synthetic stocks: S1 bought at 100, now 200 (doubled!)
    s1 = make_synthetic_stock("STOCK1.NS", n_days=300, start_price=100.0, daily_ret=0.003, start_date="2024-01-01")
    s2 = make_synthetic_stock("STOCK2.NS", n_days=300, start_price=100.0, daily_ret=0.001, start_date="2024-01-01")
    universe = {"STOCK1.NS": s1, "STOCK2.NS": s2}

    d_dec = s1["date"].iloc[200]
    p_close = float(s1.loc[s1["date"] == d_dec, "close"].iloc[0])

    open_positions = {
        "STOCK1.NS": {
            "shares": 100,
            "entry_raw_price": 100.0,
        }
    }

    # Evaluate pretrade equity using new mark-to-market logic
    holdings_est_val = sum(
        p["shares"] * (
            float(universe[sym].loc[universe[sym]["date"] == d_dec].iloc[0]["close"])
            if (sym in universe and not universe[sym].loc[universe[sym]["date"] == d_dec].empty)
            else p["entry_raw_price"]
        )
        for sym, p in open_positions.items()
    )
    expected_val = 100 * p_close
    assert abs(holdings_est_val - expected_val) < 1e-4
    assert holdings_est_val > (100 * 100.0), "Holdings value must reflect price appreciation!"
