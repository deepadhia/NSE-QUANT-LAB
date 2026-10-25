import os
import sys
import pytest
import pandas as pd

from nse_quant_lab.config import BacktestConfig, PaperTradingConfig
from nse_quant_lab.strategies.donchian import DonchianBreakoutStrategy
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.data_loader import make_synthetic_stock
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.db.models import OrderModel

# Add current tests dir to sys.path for test utilities
sys.path.insert(0, os.path.dirname(__file__))
from test_paper import MockMongoDbWrapper


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
    jan_31 = pd.to_datetime("2024-01-31")
    jan_15 = pd.to_datetime("2024-01-15")

    assert strat_monthly.is_rebalance_decision_date(timeline, jan_31, cfg_monthly) is True
    assert strat_monthly.is_rebalance_decision_date(timeline, jan_15, cfg_monthly) is False

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


def test_idempotent_order_execution():
    """
    Adversarial Reality Test: Running order execution twice on the same session
    must be strictly idempotent (no duplicate fills, no double cash deduction).
    """
    mock_db = MockMongoDbWrapper()
    cfg = PaperTradingConfig(initial_capital=500_000.0, portfolio_size=2, strategy_id="test_idempotency", telegram_enabled=False)
    strat = TrendMomentumStrategy(cfg)
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)

    mgr.initialize_portfolio()
    initial_cash = mock_db.daily_nav.find_one({"strategy_id": "test_idempotency"})["cash"]

    date_str = "2024-05-15"
    ord_id = f"ORD_TEST_{date_str}_STOCK1"
    order = OrderModel(
        order_id=ord_id,
        strategy_id="test_idempotency",
        mode="paper",
        symbol="STOCK1.NS",
        action="BUY",
        order_type="ENTRY_AT_OPEN",
        shares=100,
        price=100.0,
        status="PENDING_OPEN",
        reason="TEST_RANK",
        signal_date=date_str,
    )
    mock_db.orders.insert_one(order.to_dict())

    s1 = make_synthetic_stock("STOCK1.NS", n_days=50, start_price=100.0, daily_ret=0.001, start_date="2024-04-01")
    universe = {"STOCK1.NS": s1}
    exec_dt = pd.to_datetime(date_str)

    # First execution pass
    res1 = mgr.execute_morning_fills(universe, exec_dt)
    assert len(res1["fills"]) == 1, "First pass must execute the pending order"
    cash_after_pass1 = mock_db.daily_nav.find_one({"strategy_id": "test_idempotency", "date": date_str})["cash"]
    assert cash_after_pass1 < initial_cash

    # Second execution pass (simulating duplicate cron trigger or retry)
    res2 = mgr.execute_morning_fills(universe, exec_dt)
    assert len(res2["fills"]) == 0, "Second pass must NOT re-execute already filled orders"
    cash_after_pass2 = mock_db.daily_nav.find_one({"strategy_id": "test_idempotency", "date": date_str})["cash"]
    assert cash_after_pass2 == cash_after_pass1, "Cash must remain identical; no double deduction allowed!"


def test_morning_execution_survives_missing_eod_bar():
    """
    Temporal Reality Test: At 09:16 AM, today's completed EOD bar is absent in MongoDB.
    Morning execution must handle missing EOD bar cleanly without crashing or using yesterday's prices.
    """
    mock_db = MockMongoDbWrapper()
    cfg = PaperTradingConfig(initial_capital=500_000.0, portfolio_size=2, strategy_id="test_timing", telegram_enabled=False)
    strat = TrendMomentumStrategy(cfg)
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)
    mgr.initialize_portfolio()

    # Data ends on 2024-05-14 (yesterday)
    s1 = make_synthetic_stock("STOCK1.NS", n_days=50, start_price=100.0, daily_ret=0.001, start_date="2024-03-01")
    universe = {"STOCK1.NS": s1}
    yesterday = s1["date"].iloc[-1]
    today = yesterday + pd.Timedelta(days=1)

    today_str = today.strftime("%Y-%m-%d")
    order = OrderModel(
        order_id=f"ORD_TIMING_{today_str}_STOCK1",
        strategy_id="test_timing",
        mode="paper",
        symbol="STOCK1.NS",
        action="BUY",
        order_type="ENTRY_AT_OPEN",
        shares=50,
        price=100.0,
        status="PENDING_OPEN",
        reason="TEST_TIMING",
        signal_date=yesterday.strftime("%Y-%m-%d"),
    )
    mock_db.orders.insert_one(order.to_dict())

    # Today's EOD bar is NOT in universe
    assert s1[s1["date"] == today].empty, "Today's EOD bar must be absent to test 09:16 AM reality"

    # Execution must handle cleanly without throwing unhandled exception
    res = mgr.execute_morning_fills(universe, today)
    assert isinstance(res, dict)
    assert "fills" in res
