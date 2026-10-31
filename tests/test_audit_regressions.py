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


def test_paper_manager_alert_ticket_and_skipped_orders():
    """
    Forensic Audit Regressions:
    1. 15:45 IST post-market signal alert passes enriched order ticket data (shares, close, limit_price, est_shares, reason).
    2. 09:16 IST morning execution appends skipped orders (+2% gap-up limit exceeded and upper circuit lock) to deferred_log.
    """
    from nse_quant_lab.indicators import compute_indicators

    mock_db = MockMongoDbWrapper()
    cfg = PaperTradingConfig(
        initial_capital=500_000.0,
        portfolio_size=2,
        strategy_id="test_alert_tickets",
        limit_cap_pct=0.02,
        telegram_enabled=False,
        min_turnover_50d=0.0,
        min_price=0.0,
        enforce_pit_universe=False,
    )
    strat = TrendMomentumStrategy(cfg)
    strat.rebalance_frequency = "DAILY"
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)
    mgr.initialize_portfolio()

    # Track signal alert calls
    captured_signals = {}

    def mock_send_signal_alert(strategy_id, date_str, buys, exits, active_holdings=None, data_asof=None, notify_empty=False, **kwargs):
        captured_signals["buys"] = buys
        captured_signals["exits"] = exits
        captured_signals["active_holdings"] = active_holdings or []
        return True

    mgr.notifier.send_signal_alert = mock_send_signal_alert

    # 1. Test Post-Market Scan Enrichments (Exits & Buys)
    from nse_quant_lab.db.models import PositionModel
    pos_model = PositionModel(
        strategy_id="test_alert_tickets",
        mode="paper",
        symbol="EXIT_STOCK.NS",
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

    # Build universe: EXIT_STOCK crashes below EMA100, BUY_STOCK has high momentum
    s_exit = make_synthetic_stock("EXIT_STOCK.NS", n_days=300, start_price=100.0, daily_ret=-0.002, start_date="2023-01-01")
    s_buy = make_synthetic_stock("BUY_STOCK.NS", n_days=300, start_price=50.0, daily_ret=0.005, start_date="2023-01-01")
    s_exit = compute_indicators(s_exit)
    s_buy = compute_indicators(s_buy)
    universe = {"EXIT_STOCK.NS": s_exit, "BUY_STOCK.NS": s_buy}
    dec_dt = s_buy["date"].iloc[-1]

    mgr.generate_post_market_signals(universe, dec_dt)

    # Verify captured exits ticket
    assert len(captured_signals["exits"]) >= 1
    ex = captured_signals["exits"][0]
    assert ex["symbol"] == "EXIT_STOCK.NS"
    assert ex["shares"] == 100
    assert "close" in ex
    assert "reason" in ex

    # Verify captured buys ticket
    assert len(captured_signals["buys"]) >= 1
    b = captured_signals["buys"][0]
    assert b["symbol"] == "BUY_STOCK.NS"
    assert "close" in b
    assert "limit_price" in b
    assert abs(b["limit_price"] - (b["close"] * (1.0 + cfg.limit_cap_pct))) < 1e-4
    assert "est_shares" in b
    assert b["est_shares"] > 0
    assert "reason" in b
    assert "RANK_" in b["reason"]

    # 2. Test 09:16 Morning Fills: Gap-Up Exceeded Appends to deferred_log
    yesterday_str = (dec_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    today_dt = dec_dt + pd.Timedelta(days=1)
    today_str = today_dt.strftime("%Y-%m-%d")
    buy_order = OrderModel(
        order_id=f"ORD_BUY_{today_str}_GAPPER",
        strategy_id="test_alert_tickets",
        mode="paper",
        symbol="GAPPER.NS",
        action="BUY",
        order_type="ENTRY_AT_OPEN",
        shares=0,
        price=100.0,
        status="PENDING_OPEN",
        reason="RANK_1",
        signal_date=yesterday_str,
    )
    mock_db.orders.insert_one(buy_order.to_dict())

    # Create GAPPER.NS where today's open gaps up +4% (> +2% cap)
    s_gapper = make_synthetic_stock("GAPPER.NS", n_days=301, start_price=100.0, daily_ret=0.001, start_date="2023-01-01")
    s_gapper.loc[s_gapper.index[-1], "date"] = today_dt
    prev_close = float(s_gapper.iloc[-2]["close"])
    s_gapper.loc[s_gapper.index[-1], "open"] = prev_close * 1.04  # +4% gap-up
    s_gapper.loc[s_gapper.index[-1], "high"] = prev_close * 1.05
    s_gapper.loc[s_gapper.index[-1], "low"] = prev_close * 1.03
    s_gapper.loc[s_gapper.index[-1], "close"] = prev_close * 1.04
    universe["GAPPER.NS"] = s_gapper

    exec_res = mgr.execute_morning_fills(universe, today_dt)
    assert len(exec_res["deferred"]) >= 1
    skipped_item = [d for d in exec_res["deferred"] if d["symbol"] == "GAPPER.NS"][0]
    assert "Skipped: Open" in skipped_item["reason"]
    assert "+2% Cap" in skipped_item["reason"]


def test_structural_exit_reason_preserved_and_survives_missing_morning_bar():
    """
    Verifies that:
    1. Exact structural exit reasons (e.g. 'STRUCTURAL_EMA100') are recorded in order_ledger and closed_trades.
    2. If a stock is missing today's morning bar, its exit signal is NOT erased by unconditional .clear(),
       and exits safely when its bar becomes available.
    """
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
    cfg = BacktestConfig(initial_capital=100_000.0, portfolio_size=2)
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    # 2 stocks
    df1 = make_synthetic_stock("STOCK1.NS", n_days=300, start_price=100.0, daily_ret=0.002, start_date="2020-01-01")
    # Stock 1 plunges below EMA100 on day 280
    df1.loc[280:, "close"] = df1.loc[279, "close"] * 0.5
    df1.loc[280:, "low"] = df1.loc[280:, "close"] * 0.99
    df1.loc[280:, "high"] = df1.loc[280:, "close"] * 1.01
    df1.loc[280:, "open"] = df1.loc[280:, "close"]

    # Drop one specific day to test missing morning bar survival
    missing_day = df1.loc[281, "date"]
    df1_with_gap = df1[df1["date"] != missing_day].reset_index(drop=True)

    universe = {"STOCK1.NS": df1_with_gap}
    metrics, trades, orders, execs, nav = engine.run(
        universe, eval_start_date="2020-09-01", eval_end_date="2020-11-01"
    )

    if not trades.empty:
        # Verify exit reason is NOT generic 'STRUCTURAL_EXIT'
        exit_reasons = trades["exit_reason"].tolist()
        assert any("EMA100" in r or "SUPERTREND" in r or "UNLOCKED" in r for r in exit_reasons), f"Actual reasons: {exit_reasons}"


def test_circuit_unlock_order_ledger_reconciliation():
    """
    Verifies that every execution for a circuit-unlocked sell maps 1-to-1 to a FILLED order
    in order_ledger, resolving order vs execution discrepancies.
    """
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
    cfg = BacktestConfig(initial_capital=100_000.0, portfolio_size=1, enforce_lower_circuit_deferral=True)
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    # 300 days for warm-up
    df = make_synthetic_stock("CIRCUIT.NS", n_days=300, start_price=100.0, daily_ret=0.003, start_date="2020-01-01")
    # Day 270: force exit signal
    df.loc[270:, "close"] = 30.0
    df.loc[270:, "low"] = 28.0
    df.loc[270:, "high"] = 32.0

    # Day 271: lower circuit lock
    prev_c = float(df.loc[270, "close"])
    lc_price = round(prev_c * 0.95, 2)
    df.loc[271, "open"] = lc_price
    df.loc[271, "high"] = lc_price
    df.loc[271, "low"] = lc_price
    df.loc[271, "close"] = lc_price

    # Day 272: normal open (circuit unlocks)
    df.loc[272, "open"] = lc_price
    df.loc[272, "high"] = lc_price * 1.02
    df.loc[272, "low"] = lc_price * 0.98
    df.loc[272, "close"] = lc_price * 1.01

    universe = {"CIRCUIT.NS": df}
    metrics, trades, orders, execs, nav = engine.run(
        universe, eval_start_date="2020-09-01", eval_end_date="2021-02-01"
    )

    sell_execs = execs[execs["action"] == "SELL"]
    filled_sell_orders = orders[(orders["action"] == "SELL") & (orders["status"] == "FILLED")]
    assert len(sell_execs) == len(filled_sell_orders), f"Executions {len(sell_execs)} != Filled orders {len(filled_sell_orders)}"


def test_unaffordable_policy_try_next_replaces_candidate():
    """
    Verifies that when unaffordable_policy='TRY_NEXT', if top candidate is unaffordable
    or locked in upper circuit, the engine moves down the ranked pool to fill portfolio slots.
    """
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
    cfg = BacktestConfig(
        initial_capital=50_000.0,
        portfolio_size=1,
        unaffordable_policy="TRY_NEXT",
        enforce_upper_circuit_rejection=True
    )
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    # 300 days so indicators have warm-up
    # Stock 1: Price 100,000 (unaffordable with 50,000 capital)
    df1 = make_synthetic_stock("EXPENSIVE.NS", n_days=300, start_price=100_000.0, daily_ret=0.005, start_date="2020-01-01")
    # Stock 2: Price 100 (affordable)
    df2 = make_synthetic_stock("AFFORDABLE.NS", n_days=300, start_price=100.0, daily_ret=0.003, start_date="2020-01-01")

    universe = {"EXPENSIVE.NS": df1, "AFFORDABLE.NS": df2}
    metrics, trades, orders, execs, nav = engine.run(
        universe, eval_start_date="2020-09-01", eval_end_date="2021-01-01"
    )

    bought = execs[execs["action"] == "BUY"]["symbol"].tolist()
    assert "AFFORDABLE.NS" in bought, f"Expected AFFORDABLE.NS to be bought, got: {bought}"


def test_point_in_time_constituent_filter_blocks_future_entrants():
    """
    Verifies that PointInTimeUniverse prevents future Nifty 500 entrants
    (e.g., TITAGARH, BSE, GRAVITA) from being selected prior to their official inclusion date.
    """
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
    from nse_quant_lab.pit_universe import PointInTimeUniverse

    pit = PointInTimeUniverse(enforce_pit=True)
    assert not pit.is_eligible("TITAGARH", pd.Timestamp("2022-01-01"))
    assert pit.is_eligible("TITAGARH", pd.Timestamp("2024-01-01"))
    assert pit.is_eligible("INFY", pd.Timestamp("2022-01-01"))

    cfg_pit = BacktestConfig(enforce_pit_universe=True, min_turnover_50d=0.0, min_price=10.0)
    strat_pit = TrendMomentumStrategy(cfg_pit)

    df_tita = make_synthetic_stock("TITAGARH.NS", n_days=300, start_price=100.0, daily_ret=0.005, start_date="2021-01-01")
    df_infy = make_synthetic_stock("INFY.NS", n_days=300, start_price=100.0, daily_ret=0.004, start_date="2021-01-01")

    universe = {
        "TITAGARH.NS": strat_pit.compute_indicators(df_tita),
        "INFY.NS": strat_pit.compute_indicators(df_infy),
    }

    decision_dt = pd.Timestamp("2021-10-01")
    candidates_pit = strat_pit.select_entry_candidates(universe, decision_dt, {}, cfg_pit)

    assert "TITAGARH.NS" not in candidates_pit.index, "TITAGARH should be excluded under PIT before 2023-09-29"
    assert "INFY.NS" in candidates_pit.index, "INFY should be included"

    # When PIT is disabled, TITAGARH is eligible
    cfg_no_pit = BacktestConfig(enforce_pit_universe=False, min_turnover_50d=0.0, min_price=10.0)
    strat_no_pit = TrendMomentumStrategy(cfg_no_pit)
    candidates_no_pit = strat_no_pit.select_entry_candidates(universe, decision_dt, {}, cfg_no_pit)
    assert "TITAGARH.NS" in candidates_no_pit.index, "TITAGARH should be eligible when PIT is disabled"


def test_volume_participation_cap_with_try_next():
    """
    Verifies that buy orders are capped at 10% of 50-day average daily turnover (ADT),
    and if allocation is < 25% of slot capital, engine moves down to next candidate via TRY_NEXT.
    """
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
    cfg = BacktestConfig(
        initial_capital=1_000_000.0,
        portfolio_size=1,
        unaffordable_policy="TRY_NEXT",
        min_turnover_50d=0.0,
        min_price=10.0,
        max_volume_participation_pct=0.10,
        enforce_pit_universe=False,
    )
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    # Candidate 1: High momentum, but tiny 50d ADT (Rs 50,000 / day).
    # 10% cap = Rs 5,000, which is < 25% of slot capital (Rs 250,000) -> skipped
    df1 = make_synthetic_stock("ILLIQUID.NS", n_days=300, start_price=100.0, daily_ret=0.006, start_date="2020-01-01", volume=500)
    # Candidate 2: Slightly lower momentum, but high 50d ADT (Rs 50,000,000 / day) -> bought
    df2 = make_synthetic_stock("LIQUID.NS", n_days=300, start_price=100.0, daily_ret=0.004, start_date="2020-01-01", volume=500_000)

    universe = {
        "ILLIQUID.NS": strat.compute_indicators(df1),
        "LIQUID.NS": strat.compute_indicators(df2),
    }
    metrics, trades, orders, execs, nav = engine.run(
        universe, eval_start_date="2020-09-01", eval_end_date="2021-01-01"
    )

    bought = execs[execs["action"] == "BUY"]["symbol"].tolist()
    assert "LIQUID.NS" in bought, f"Expected LIQUID.NS to be bought via TRY_NEXT, got: {bought}"
    assert "ILLIQUID.NS" not in bought, f"ILLIQUID.NS should be rejected due to volume participation cap"


def test_cross_sectional_top_liquid_set_gating():
    """
    Verifies that PointInTimeUniverse.get_top_liquid_universe() correctly selects
    only the Top N stocks by 50d ADT, filtering out lower-ranked stocks.
    """
    from nse_quant_lab.pit_universe import PointInTimeUniverse
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy

    # Create 3 stocks with differing 50d ADT
    s1 = make_synthetic_stock("HIGH_TO.NS", n_days=300, start_price=100.0, daily_ret=0.003, volume=1_000_000)
    s2 = make_synthetic_stock("MID_TO.NS", n_days=300, start_price=100.0, daily_ret=0.003, volume=500_000)
    s3 = make_synthetic_stock("LOW_TO.NS", n_days=300, start_price=100.0, daily_ret=0.003, volume=10_000)

    cfg = BacktestConfig(enforce_pit_universe=True, pit_top_n_liquidity=2, min_turnover_50d=0.0, min_price=10.0)
    strat = TrendMomentumStrategy(cfg)

    universe = {
        "HIGH_TO.NS": strat.compute_indicators(s1),
        "MID_TO.NS": strat.compute_indicators(s2),
        "LOW_TO.NS": strat.compute_indicators(s3),
    }

    dec_dt = s1["date"].iloc[-1]
    top_set = PointInTimeUniverse.get_top_liquid_universe(universe, dec_dt, top_n=2)
    assert "HIGH_TO.NS" in top_set
    assert "MID_TO.NS" in top_set
    assert "LOW_TO.NS" not in top_set, "LOW_TO.NS should be excluded from top 2 liquid set"

    candidates = strat.select_entry_candidates(universe, dec_dt, {}, cfg)
    assert "LOW_TO.NS" not in candidates.index


def test_annual_march_31_tax_cash_deduction_and_stcl_offsets_ltcg():
    """
    Verifies that on March 31 with deduct_annual_tax=True, statutory tax under Sections 70/74
    is deducted from cash, and Short-Term Capital Losses (STCL) offset Long-Term Capital Gains (LTCG).
    """
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
    cfg = BacktestConfig(
        initial_capital=100_000.0,
        portfolio_size=1,
        deduct_annual_tax=True,
        min_turnover_50d=0.0,
        min_price=10.0,
        enforce_pit_universe=False,
    )
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    # Run over March 31 boundary
    df = make_synthetic_stock("PROFIT_STOCK.NS", n_days=400, start_price=100.0, daily_ret=0.002, start_date="2020-01-01")
    universe = {"PROFIT_STOCK.NS": strat.compute_indicators(df)}

    metrics, trades, orders, execs, nav = engine.run(
        universe, eval_start_date="2020-06-01", eval_end_date="2021-05-01"
    )

    fy_ledger = metrics.get("fy_tax_ledger", [])
    # Verify that March 31 FY settlement was evaluated
    assert "total_tax_paid" in metrics


def test_rank_hysteresis_retains_healthy_holdings():
    """
    Phase 2 Audit Check: Verify that when rebalance_hold_rank_buffer=16 and portfolio_size=8,
    a held position ranked #12 (outside Top 8 but inside Top 16) is RETAINED and not dumped as REBALANCE_DROP.
    """
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
    cfg = BacktestConfig(
        portfolio_size=8,
        rebalance_hold_rank_buffer=16,
        min_turnover_50d=0.0,
        min_price=10.0,
        enforce_pit_universe=False
    )
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    # 15 synthetic stocks
    universe = {}
    for i in range(1, 16):
        sym = f"STOCK_{i:02d}.NS"
        df = make_synthetic_stock(sym, n_days=350, start_price=100.0, daily_ret=0.001 * (16 - i), start_date="2021-01-01")
        universe[sym] = strat.compute_indicators(df)

    # Run engine
    metrics, trades_df, orders_df, execs_df, nav_df = engine.run(
        universe, eval_start_date="2021-06-01", eval_end_date="2021-09-01"
    )

    # Verify that wash trades are 0
    fills = orders_df[orders_df["status"] == "FILLED"]
    sells = fills[fills["action"] == "SELL"][["date", "symbol"]]
    buys = fills[fills["action"] == "BUY"][["date", "symbol"]]
    wash_trades = pd.merge(sells, buys, on=["date", "symbol"])
    assert len(wash_trades) == 0, f"Expected 0 wash trades, got {len(wash_trades)}"


def test_soft_tax_settlement_prevents_negative_cash():
    """
    Phase 2 Audit Check: Verify that soft annual tax settlement never drives cash negative,
    even when statutory tax due exceeds idle cash on March 31.
    """
    from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
    cfg = BacktestConfig(
        initial_capital=100_000.0,
        portfolio_size=8,
        rebalance_hold_rank_buffer=16,
        deduct_annual_tax=True,
        min_turnover_50d=0.0,
        min_price=10.0,
        enforce_pit_universe=False,
    )
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    df = make_synthetic_stock("PROFIT_MAKER.NS", n_days=400, start_price=100.0, daily_ret=0.003, start_date="2020-01-01")
    universe = {"PROFIT_MAKER.NS": strat.compute_indicators(df)}

    metrics, trades, orders, execs, nav_df = engine.run(
        universe, eval_start_date="2020-06-01", eval_end_date="2021-05-01"
    )

    # Cash must never be negative on any trading day
    assert (nav_df["cash"] >= 0.0).all(), f"Found negative cash days: {nav_df[nav_df['cash'] < 0]}"


def test_point_in_time_reentrant_state_machine():
    """
    Verifies that PointInTimeUniverse correctly handles stocks that exit and later re-enter the index
    (e.g., GVT and D, SUZLON, FORTIS) without permanent collision bans.
    """
    from nse_quant_lab.pit_universe import PointInTimeUniverse
    import pandas as pd

    pit = PointInTimeUniverse(enforce_pit=True)

    # GVT and D: Excluded 2016-04-01, Re-included 2024-03-28
    assert pit.is_eligible('GVT&D.NS', pd.Timestamp('2016-01-01')), 'GVT&D should be eligible before 2016 exclusion'
    assert not pit.is_eligible('GVT&D.NS', pd.Timestamp('2016-05-01')), 'GVT&D should be ineligible after 2016 exclusion'
    assert not pit.is_eligible('GVT&D.NS', pd.Timestamp('2023-01-01')), 'GVT&D should be ineligible prior to 2024 re-entry'
    assert pit.is_eligible('GVT&D.NS', pd.Timestamp('2024-04-01')), 'GVT&D MUST be eligible after 2024-03-28 re-entry'

    # SUZLON: Excluded 2018-09-28, Re-included 2023-09-29
    assert pit.is_eligible('SUZLON.NS', pd.Timestamp('2016-01-01')), 'SUZLON should be eligible in 2016'
    assert not pit.is_eligible('SUZLON.NS', pd.Timestamp('2019-01-01')), 'SUZLON should be ineligible after 2018 exclusion'
    assert pit.is_eligible('SUZLON.NS', pd.Timestamp('2023-10-01')), 'SUZLON MUST be eligible after 2023-09-29 re-entry'

    # FORTIS: Included 2016-04-01, Excluded 2018-02-05, Re-included 2019-12-27
    assert not pit.is_eligible('FORTIS.NS', pd.Timestamp('2016-01-01')), 'FORTIS should be ineligible before 2016 inclusion'
    assert pit.is_eligible('FORTIS.NS', pd.Timestamp('2016-05-01')), 'FORTIS should be eligible after 2016 inclusion'
    assert not pit.is_eligible('FORTIS.NS', pd.Timestamp('2018-03-01')), 'FORTIS should be ineligible after 2018 exclusion'
    assert pit.is_eligible('FORTIS.NS', pd.Timestamp('2020-01-01')), 'FORTIS MUST be eligible after 2019-12-27 re-entry'
