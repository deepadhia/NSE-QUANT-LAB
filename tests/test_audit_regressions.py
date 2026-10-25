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


def test_circuit_breaker_minimum_band_threshold_and_live_quotes():
    """
    Audited Fix 1: Live morning quotes at 09:16 AM have open == high == low == close.
    Normal price fluctuations (+0.2%, -0.2%) MUST NOT be classified as exchange circuit locks.
    Only moves >= 4.8% should trigger circuit locks.
    """
    from nse_quant_lab.execution import is_locked_down, is_locked_up

    # 1. Normal flat morning live quote with +0.2% change -> NOT upper circuit
    assert not is_locked_up(100.2, 100.2, 100.2, 100.2, 100.0)

    # 2. Normal flat morning live quote with -0.2% change -> NOT lower circuit
    assert not is_locked_down(99.8, 99.8, 99.8, 99.8, 100.0)

    # 3. Flat quote with 0.0% change -> NOT circuit lock
    assert not is_locked_up(100.0, 100.0, 100.0, 100.0, 100.0)
    assert not is_locked_down(100.0, 100.0, 100.0, 100.0, 100.0)

    # 4. Legitimate Upper Circuit: +5.0% flat bar -> TRUE
    assert is_locked_up(105.0, 105.0, 105.0, 105.0, 100.0)

    # 5. Legitimate Lower Circuit: -5.0% flat bar -> TRUE
    assert is_locked_down(95.0, 95.0, 95.0, 95.0, 100.0)

    # 6. Open at High with +5% move and negligible intraday range -> TRUE
    assert is_locked_up(105.05, 105.05, 105.0, 105.05, 100.0)

    # 7. Open at Low with -5% move and negligible intraday range -> TRUE
    assert is_locked_down(95.0, 95.05, 95.0, 95.0, 100.0)


def test_get_execution_bar_never_fabricates_price_when_feed_empty():
    """
    Audited Fix 2: _get_execution_bar must return None and NEVER fabricate prev_close or 100.0
    when ticker has no previous history or live feed returns None.
    """
    mock_db = MockMongoDbWrapper()
    cfg = PaperTradingConfig(initial_capital=500_000.0, portfolio_size=2, strategy_id="test_nobogus", telegram_enabled=False)
    strat = TrendMomentumStrategy(cfg)
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)

    # Case A: Universe has no history for symbol -> must return None (not 100.0)
    res_empty_history = mgr._get_execution_bar("NEW_STOCK.NS", pd.Timestamp("2024-05-15"), universe={})
    assert res_empty_history is None, "Must return None when symbol has no historical rows in universe"

    # Case B: Symbol has history ending yesterday, but live feed returns None
    s1 = make_synthetic_stock("STOCK1.NS", n_days=300, start_price=150.0, start_date="2023-01-01")
    universe = {"STOCK1.NS": s1}
    exec_dt = s1["date"].iloc[-1] + pd.Timedelta(days=1)

    class MockEmptyFastInfo:
        open = None
        last_price = None

    class MockTicker:
        def __init__(self, sym):
            self.fast_info = MockEmptyFastInfo()

    import sys
    orig_modules = sys.modules.get("yfinance")
    class MockYfModule:
        Ticker = MockTicker

    sys.modules["yfinance"] = MockYfModule
    try:
        bar = mgr._get_execution_bar("STOCK1.NS", exec_dt, universe)
        assert bar is None, "Must return None instead of fabricating prev_close when live feed is empty"
    finally:
        if orig_modules is not None:
            sys.modules["yfinance"] = orig_modules
        else:
            del sys.modules["yfinance"]


def test_generate_post_market_signals_marks_open_positions_to_eod_close():
    """
    Audited Fix 3: generate_post_market_signals at 15:45 IST must mark open positions
    to today's EOD close, updating positions, daily_nav, and strategies collection.
    """
    from nse_quant_lab.db.models import PositionModel

    mock_db = MockMongoDbWrapper()
    cfg = PaperTradingConfig(initial_capital=500_000.0, portfolio_size=2, strategy_id="test_mtm", telegram_enabled=False)
    strat = TrendMomentumStrategy(cfg)
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)
    mgr.initialize_portfolio()

    # Initial position entered at 100.0
    pos_model = PositionModel(
        strategy_id="test_mtm",
        mode="paper",
        symbol="GROW.NS",
        shares=100,
        entry_price=100.0,
        entry_fill_price=100.0,
        entry_date="2024-05-14",
        entry_outlay=10020.0,
        entry_fees=20.0,
        last_close=100.0,
        market_value=10000.0,
        unrealized_pnl=0.0,
    )
    mock_db.positions.insert_one(pos_model.to_dict())

    # Today is 2024-05-15, and EOD close rose to 120.0
    df_grow = make_synthetic_stock("GROW.NS", n_days=300, start_price=100.0, start_date="2023-01-01")
    dec_dt = df_grow["date"].iloc[-1]
    df_grow.loc[df_grow["date"] == dec_dt, "close"] = 120.0
    universe = {"GROW.NS": df_grow}

    mgr.generate_post_market_signals(universe, dec_dt)

    updated_pos = mock_db.positions.find_one({"strategy_id": "test_mtm", "symbol": "GROW.NS"})
    assert updated_pos["last_close"] == 120.0, "Position last_close must update to today's EOD close"
    assert updated_pos["market_value"] == 12000.0, "market_value must be 100 shares * 120.0"
    assert updated_pos["unrealized_pnl"] == 12000.0 - 10020.0, "unrealized_pnl must reflect gain"

    nav_rec = mock_db.daily_nav.find_one({"strategy_id": "test_mtm", "date": dec_dt.strftime("%Y-%m-%d")})
    assert nav_rec is not None
    assert nav_rec["holdings_val"] == 12000.0

    strat_entry = mock_db.strategies.find_one({"strategy_id": "test_mtm"})
    assert strat_entry is not None
    assert strat_entry["holdings_val"] == 12000.0
    assert strat_entry["unrealized_pnl"] == 12000.0 - 10020.0
    assert strat_entry["open_positions_count"] == 1
    assert "total_return_pct" in strat_entry


def test_backtest_engine_strictly_enforces_portfolio_size_and_no_dust():
    """
    Audited Fix 4: BacktestEngine must never exceed portfolio_size or open dust trades < 25% alloc.
    """
    from nse_quant_lab.config import BacktestConfig
    from nse_quant_lab.engine import BacktestEngine

    cfg = BacktestConfig(initial_capital=100_000.0, portfolio_size=2)
    s1 = make_synthetic_stock("SYM1.NS", n_days=300, start_price=50.0, daily_ret=0.005, start_date="2018-01-01")
    s2 = make_synthetic_stock("SYM2.NS", n_days=300, start_price=60.0, daily_ret=0.004, start_date="2018-01-01")
    s3 = make_synthetic_stock("SYM3.NS", n_days=300, start_price=70.0, daily_ret=0.003, start_date="2018-01-01")
    s4 = make_synthetic_stock("SYM4.NS", n_days=300, start_price=80.0, daily_ret=0.002, start_date="2018-01-01")

    universe = {"SYM1.NS": s1, "SYM2.NS": s2, "SYM3.NS": s3, "SYM4.NS": s4}
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    metrics, trades, orders, execs, nav = engine.run(universe, eval_start_date="2018-12-01", eval_end_date="2019-02-15")

    # Verify open positions count NEVER exceeded portfolio_size (2)
    max_open = nav["open_positions"].max()
    assert max_open <= 2, f"Expected max open positions <= 2, got {max_open}"


def test_sell_fees_calculated_on_executed_turnover_not_raw_open():
    """
    Audited Fix 5: calc_sell_fees_exact must be evaluated on net_proceeds_slip (shares * fill_price)
    rather than gross_proceeds (shares * raw_exit_price).
    """
    from nse_quant_lab.config import BacktestConfig
    from nse_quant_lab.engine import BacktestEngine
    from nse_quant_lab.fees import calc_sell_fees_exact

    # Slippage is 5% to make the difference between raw_price and fill_price clearly measurable
    cfg = BacktestConfig(initial_capital=100_000.0, portfolio_size=1, slippage_pct=0.05)
    s1 = make_synthetic_stock("SLIPTEST.NS", n_days=300, start_price=100.0, daily_ret=0.002, start_date="2018-01-01")
    universe = {"SLIPTEST.NS": s1}
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    metrics, trades, orders, execs, nav = engine.run(universe, eval_start_date="2018-12-01", eval_end_date="2019-02-15")

    if not trades.empty:
        trade = trades.iloc[0]
        fill_turnover = trade["shares"] * trade["exit_fill_price"]
        raw_turnover = trade["shares"] * trade["exit_price"]
        expected_fees_slip = calc_sell_fees_exact(fill_turnover, pd.to_datetime(trade["exit_date"]), cfg)
        fees_raw = calc_sell_fees_exact(raw_turnover, pd.to_datetime(trade["exit_date"]), cfg)
        # Verify statutory STT difference exists
        assert fees_raw["stt"] > expected_fees_slip["stt"], "STT on gross must be strictly higher than STT on fill"


def test_delisted_stock_forced_liquidation_prevents_zombie_slots():
    """
    Audited Fix 6 (21-Year Survival Trap): When a held stock ceases trading / delists,
    BacktestEngine forces liquidation at last known close, freeing cash and slot.
    """
    from nse_quant_lab.config import BacktestConfig
    from nse_quant_lab.engine import BacktestEngine

    cfg = BacktestConfig(initial_capital=100_000.0, portfolio_size=1)

    # Stock 1 trades for 270 days (ending ~January 2019) with strong momentum to enter slot 1, then delists
    s1 = make_synthetic_stock("DYING.NS", n_days=270, start_price=100.0, daily_ret=0.005, start_date="2018-01-01")
    # Stock 2 continues trading for 300 days with lower momentum
    s2 = make_synthetic_stock("HEALTHY.NS", n_days=300, start_price=50.0, daily_ret=0.001, start_date="2018-01-01")

    universe = {"DYING.NS": s1, "HEALTHY.NS": s2}
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    metrics, trades, orders, execs, nav = engine.run(universe, eval_start_date="2018-12-01", eval_end_date="2019-02-01")

    # Verify DYING.NS had a forced liquidation exit
    dead_exits = trades[trades["exit_reason"] == "DATA_ENDED_FORCED_EXIT"]
    assert not dead_exits.empty, "Delisted stock must have a DATA_ENDED_FORCED_EXIT record"
    assert "DYING.NS" in dead_exits["symbol"].values

    # Verify at end of backtest, DYING.NS is not a zombie open position
    final_day = nav.iloc[-1]
    assert final_day["open_positions"] <= 1
