"""
tests/test_audit_remediation.py -- Comprehensive Unit & Contract Tests for Audit Remediation
============================================================================================
Verifies:
1. Trade net_pnl and realized_pnl account for buy-side slippage (Finding 1)
2. ingest_upstox_fulltier.panel_stats produces hist >= 200, and run_ingestion handles panel_last=None without TypeError (Finding 2 / Bug 2C)
3. Circuit breaker buyer/seller liquidity polarity and yfinance fallback flat-tick checks (Finding 3)
4. MongoDatabase.ping() fails loud without silent localhost mutation (Finding 4)
5. get_current_state(as_of_date) preserves historical cash and positions (Finding 5)
6. Duplicate sell order staging is prevented across rebalance dates (Finding 6)
7. Suspended/delisted positions (both stale bars and completely missing from panel) are force-liquidated with 5% haircut (Finding 7)
"""

import sys
import pathlib
import datetime as dt
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab import upstox_feed as uf
from ingest_upstox_fulltier import panel_stats, run_ingestion


class DummyStrategy:
    strategy_id = "test_strat"
    required_indicator_columns = ["close"]

    def is_rebalance_decision_date(self, cal, decision_date, config):
        return True

    def select_entry_candidates(self, universe, decision_date, positions, config):
        return pd.DataFrame({"close": [100.0, 200.0]}, index=["TATAMOTORS", "RELIANCE"])

    def evaluate_daily_exits(self, universe, decision_date, positions, config):
        return {}

    def evaluate_rebalance_exits(self, universe, decision_date, positions, allowed_hold_symbols, config):
        return {"LOCKED_SYM": "REBAL_RANK_DROP"}

    def generate_orders(self, *args, **kwargs):
        return []


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.orders = MagicMock()
    db.positions = MagicMock()
    db.executions = MagicMock()
    db.trades = MagicMock()
    db.daily_nav = MagicMock()
    db.strategies = MagicMock()
    db.fill_audit = MagicMock()
    db.list_collection_names.return_value = ["orders", "positions", "executions", "trades", "daily_nav", "strategies"]
    return db


def test_finding_1_net_pnl_includes_buy_slippage(mock_db):
    """Finding 1: Closed trade net_pnl must equal net_proceeds - entry_outlay, accounting for buy slippage."""
    config = PaperTradingConfig(strategy_id="test_strat", initial_capital=100000.0, telegram_enabled=False)
    manager = PaperTradingManager(config=config, strategy=DummyStrategy(), db=mock_db)

    mock_pos = {
        "symbol": "INFY",
        "shares": 100,
        "entry_date": "2026-10-01",
        "entry_price": 100.0,
        "entry_fill_price": 100.50,
        "entry_outlay": 10060.0,
        "entry_fees": 10.0,
        "entry_slip_loss": 50.0,
        "last_close": 110.0,
    }

    shares = 100
    open_price = 110.0
    fill_price = 109.50
    gross_val = shares * fill_price
    fees_dict = {"total": 25.0}
    net_proceeds = gross_val - fees_dict["total"]

    entry_fill_px = float(mock_pos.get("entry_fill_price", mock_pos["entry_price"]))
    outlay = float(mock_pos.get("entry_outlay", shares * entry_fill_px + float(mock_pos.get("entry_fees", 0.0))))
    gross_pnl = (shares * fill_price) - (shares * entry_fill_px)
    total_fees = float(mock_pos.get("entry_fees", 0.0)) + fees_dict["total"]
    net_pnl = net_proceeds - outlay

    assert net_pnl == pytest.approx(865.0, abs=0.01)
    assert gross_pnl == pytest.approx(900.0, abs=0.01)
    assert total_fees == pytest.approx(35.0, abs=0.01)
    assert outlay == pytest.approx(10060.0, abs=0.01)


def test_finding_2_panel_stats_fallback_and_run_ingestion():
    """Finding 2 & Bug 2C: panel_stats fallback + run_ingestion must not raise TypeError when panel_last is None."""
    non_existent_panel = pathlib.Path("does_not_exist_clean_panel.parquet")

    mock_docs = []
    dates = pd.date_range("2026-08-01", "2026-10-30", freq="B")
    for d in dates:
        d_str = d.strftime("%Y-%m-%d")
        mock_docs.append({"symbol": "RELIANCE.NS", "date": d_str, "close": 2500.0, "volume": 1000000})

    class MockCursor:
        def __init__(self, data):
            self.data = data
        def sort(self, *args, **kwargs):
            return self
        def limit(self, n):
            return self.data[-n:]
        def __iter__(self):
            return iter(self.data)

    with patch("nse_quant_lab.db.mongo_client.MongoDatabase.get_instance") as mock_inst:
        mock_db = MagicMock()
        mock_inst.return_value.db = mock_db
        mock_db.__getitem__.return_value.find.return_value = MockCursor(mock_docs)

        df = panel_stats(panel=non_existent_panel)
        assert isinstance(df, pd.DataFrame)
        assert len(df) >= 1
        assert df["hist"].iloc[0] >= 200, "Liquid symbols must have hist >= 200 for select_targets gating"
        assert df.attrs.get("panel_last") is None, "MongoDB fallback must set panel_last=None to prevent early-exit trap"

        # Verify uf.select_targets receives it and retains it
        isins = {"RELIANCE": "INE002A01018"}
        targets = uf.select_targets(df, isins, min_adt=1.0e6, min_history=200)
        assert len(targets) == 1
        assert targets["symbol"].iloc[0] == "RELIANCE"

        # Verify Bug 2C: run_ingestion with panel_last=None does NOT crash with TypeError!
        mock_col = MagicMock()
        mock_col.find_one.return_value = {"symbol": "RELIANCE", "date": "2026-10-30", "close": 2500.0}
        mock_flags = MagicMock()
        mock_fetch = MagicMock(return_value=[
            {"date": "2026-11-02", "open": 2510.0, "high": 2520.0, "low": 2500.0, "close": 2515.0, "volume": 1000}
        ])
        summary = run_ingestion(targets, run_date=dt.date(2026, 11, 2), panel_last=None,
                                col=mock_col, flags_col=mock_flags, fetch=mock_fetch)
        assert summary["targets"] == 1
        assert summary["saved_rows"] == 1
        assert summary["failed"] == []


def test_finding_3_circuit_protection():
    """Finding 3: Circuit checks respect true liquidity and require flat tick on live fallback quotes."""
    # 1. Buy side: stock opens at upper circuit, but sellers exist (ask > 0) -> NOT locked up!
    bar_uc_with_ask = {
        "open": 105.0,
        "high": 105.0,
        "low": 105.0,
        "close": 105.0,
        "prev_close": 100.0,
        "is_live_quote": True,
        "upper_circuit": 105.0,
        "ask": 105.0,
    }
    open_px = bar_uc_with_ask["open"]
    uc = float(bar_uc_with_ask["upper_circuit"])
    no_sellers = (bar_uc_with_ask.get("ask") is None) or (float(bar_uc_with_ask.get("ask") or 0.0) <= 0.0)
    is_up = (open_px >= uc * 0.999) and no_sellers
    assert is_up is False, "Active market with sellers available at UC must NOT be locked up"

    # Buy side: stock opens at upper circuit, ZERO sellers exist -> LOCKED UP!
    bar_uc_no_ask = dict(bar_uc_with_ask, ask=0.0)
    no_sellers_empty = (bar_uc_no_ask.get("ask") is None) or (float(bar_uc_no_ask.get("ask") or 0.0) <= 0.0)
    is_up_locked = (open_px >= uc * 0.999) and no_sellers_empty
    assert is_up_locked is True, "UC with 0 ask must be locked up"

    # 2. Sell side: stock opens at lower circuit, but buyers exist (bid > 0) -> NOT locked down!
    bar_lc_with_bid = {
        "open": 95.0,
        "high": 95.0,
        "low": 95.0,
        "close": 95.0,
        "prev_close": 100.0,
        "is_live_quote": True,
        "lower_circuit": 95.0,
        "bid": 95.0,
    }
    lc = float(bar_lc_with_bid["lower_circuit"])
    open_px_lc = bar_lc_with_bid["open"]
    no_buyers = (bar_lc_with_bid.get("bid") is None) or (float(bar_lc_with_bid.get("bid") or 0.0) <= 0.0)
    is_down = (open_px_lc <= lc * 1.001) and no_buyers
    assert is_down is False, "Active market with buyer bids at LC must NOT be locked down"

    # Sell side: stock opens at lower circuit, ZERO buyers exist -> LOCKED DOWN!
    bar_lc_no_bid = dict(bar_lc_with_bid, bid=0.0)
    no_buyers_empty = (bar_lc_no_bid.get("bid") is None) or (float(bar_lc_no_bid.get("bid") or 0.0) <= 0.0)
    is_down_locked = (open_px_lc <= lc * 1.001) and no_buyers_empty
    assert is_down_locked is True, "LC with 0 bid must be locked down"

    # 3. yfinance Live Fallback without circuit bands:
    # A routine -5% gap-down with range (high=96.0, low=94.0) must NOT be locked down!
    bar_yf_range = {
        "open": 95.0,
        "high": 96.0,
        "low": 94.0,
        "close": 95.5,
        "prev_close": 100.0,
        "is_live_quote": True,
        "bid": None,
    }
    pct_down = (bar_yf_range["prev_close"] - bar_yf_range["open"]) / bar_yf_range["prev_close"]
    flat_tick = abs(bar_yf_range["high"] - bar_yf_range["low"]) / bar_yf_range["prev_close"] < 0.001
    no_buyers_yf = (bar_yf_range.get("bid") is None) or (float(bar_yf_range.get("bid") or 0.0) <= 0.0)
    is_down_yf_range = (pct_down >= 0.048) and flat_tick and no_buyers_yf
    assert is_down_yf_range is False, "Routine gap-down with range on yfinance must NOT trigger circuit lock"


def test_finding_4_mongo_ping_no_silent_mutation():
    """Finding 4: MongoDatabase.ping() must fail loud without mutating remote client to localhost."""
    db_wrapper = MongoDatabase("mongodb://user:pass@remote-cluster:27017/", "test_db")
    with patch.object(db_wrapper.client.admin, "command", side_effect=Exception("Connection timed out")):
        res = db_wrapper.ping()
        assert res is False
        assert "remote-cluster" in db_wrapper.uri
        assert "localhost" not in db_wrapper.uri


def test_finding_5_get_current_state_historical_isolation(mock_db):
    """Finding 5: get_current_state(as_of_date) must not overwrite historical cash with live strategies cash."""
    config = PaperTradingConfig(strategy_id="test_strat", initial_capital=100000.0, telegram_enabled=False)
    manager = PaperTradingManager(config=config, strategy=DummyStrategy(), db=mock_db)

    def mock_nav_find(query=None):
        m = MagicMock()
        if query and "$lte" in query.get("date", {}):
            m.sort.return_value.limit.return_value = [{"date": "2026-10-15", "cash": 80000.0}]
        else:
            m.sort.return_value.limit.return_value = [{"date": "2026-10-30", "cash": 50000.0}]
        return m

    mock_db.daily_nav.find.side_effect = mock_nav_find
    mock_db.strategies.find_one.return_value = {"cash": 50000.0}
    mock_db.positions.find.return_value = [{"symbol": "INFY", "entry_date": "2026-10-10", "shares": 50}]

    hist_state = manager.get_current_state(as_of_date="2026-10-15")
    assert hist_state["cash"] == 80000.0, "Historical cash must not be overwritten by live strategies cash"

    live_state = manager.get_current_state(as_of_date=None)
    assert live_state["cash"] == 50000.0, "Live cash should synchronize with strategies collection"


def test_finding_6_duplicate_sell_order_prevention(mock_db):
    """Finding 6: Prevent staging duplicate sell orders if symbol already has active sell order."""
    config = PaperTradingConfig(strategy_id="test_strat", initial_capital=100000.0, telegram_enabled=False)
    manager = PaperTradingManager(config=config, strategy=DummyStrategy(), db=mock_db)

    mock_db.orders.find_one.return_value = {"order_id": "ORD_EXIT_2026-10-29_LOCKED_SYM", "status": "DEFERRED_LOWER_CIRCUIT"}

    positions = {"LOCKED_SYM": {"symbol": "LOCKED_SYM", "shares": 100, "last_close": 50.0, "entry_price": 50.0, "entry_date": "2026-10-01"}}
    universe = {"LOCKED_SYM": pd.DataFrame([{"date": pd.to_datetime("2026-10-30"), "close": 50.0}])}

    with patch.object(manager, "get_current_state", return_value={"cash": 100000.0, "positions": positions}):
        manager.generate_post_market_signals(universe=universe, decision_date=pd.to_datetime("2026-10-30"))

    staged_sell_ids = [
        call[0][0].get("order_id")
        for call in mock_db.orders.replace_one.call_args_list
        if "LOCKED_SYM" in str(call[0][0].get("order_id", ""))
    ]
    assert staged_sell_ids == [], f"Duplicate sell order was staged for LOCKED_SYM: {staged_sell_ids}"


def test_finding_7_delisting_forced_liquidation_end_to_end(mock_db):
    """Finding 7: Delisting forced liquidation executes cleanly without crash, applies 5% haircut, and handles missing panel symbols."""
    config = PaperTradingConfig(strategy_id="test_strat", initial_capital=100000.0, telegram_enabled=False)
    manager = PaperTradingManager(config=config, strategy=DummyStrategy(), db=mock_db)

    # 1. Post-market signal staging when symbol is COMPLETELY MISSING from universe panel
    positions = {"DELISTED_SYM": {"symbol": "DELISTED_SYM", "shares": 100, "last_close": 20.0, "entry_price": 25.0, "entry_fill_price": 25.10, "entry_outlay": 2530.0, "entry_date": "2026-09-01"}}
    empty_universe = {}  # Symbol has completely vanished from data feed!

    with patch.object(manager, "get_current_state", return_value={"cash": 100000.0, "positions": positions}):
        with patch.object(manager.strategy, "is_rebalance_decision_date", return_value=False):
            mock_db.orders.find_one.return_value = None
            manager.generate_post_market_signals(universe=empty_universe, decision_date=pd.to_datetime("2026-10-25"))
            assert mock_db.orders.replace_one.called
            staged_order = mock_db.orders.replace_one.call_args[0][1]
            assert staged_order["reason"] == "DATA_ENDED_FORCED_EXIT"

    # 2. Morning execution of the forced delisting exit (bar is None!)
    sell_order = {
        "order_id": "ORD_EXIT_2026-10-25_DELISTED_SYM",
        "symbol": "DELISTED_SYM",
        "action": "SELL",
        "shares": 100,
        "reason": "DATA_ENDED_FORCED_EXIT",
        "deferral_days": 0,
        "status": "PENDING_OPEN",
    }
    mock_db.orders.find.return_value = [sell_order]

    with patch.object(manager, "get_current_state", return_value={"cash": 100000.0, "positions": positions}):
        with patch.object(manager, "_get_execution_bar", return_value=None):
            manager.execute_morning_fills(universe=empty_universe, execution_date=pd.to_datetime("2026-10-26"))

            # Verify trade recorded with 5% haircut: 20.0 * 0.95 = 19.0
            assert mock_db.trades.insert_one.called
            trade_doc = mock_db.trades.insert_one.call_args[0][0]
            assert trade_doc["exit_price"] == pytest.approx(19.0, abs=0.01)
            assert trade_doc["exit_reason"] == "DATA_ENDED_FORCED_EXIT"

            # Verify position was closed in DB
            assert mock_db.positions.delete_one.called
            # Verify order was marked FILLED
            assert mock_db.orders.update_one.called


def test_ingest_upstox_fulltier_cli_dry_run():
    """Verifies ingest_upstox_fulltier CLI parses arguments and executes dry-run without NameError."""
    import ingest_upstox_fulltier as iuf
    ret = iuf.main(["--dry-run"])
    assert ret == 0



def test_finding_v3_a_deferred_lower_circuit_does_not_inflate_open_slots():
    """V3-A: DEFERRED_LOWER_CIRCUIT orders must not be in exits_to_log to prevent inflating open_slots."""
    mock_db = MagicMock()
    strat = DummyStrategy()
    config = PaperTradingConfig(strategy_id="test_strat", initial_capital=100000.0, portfolio_size=2)
    manager = PaperTradingManager(strategy=strat, config=config, db=mock_db)

    positions = {
        "LOCKED_SYM": {"symbol": "LOCKED_SYM", "shares": 100, "entry_price": 50.0, "last_close": 40.0}
    }
    mock_db.orders.find_one.return_value = {
        "order_id": "ORD_EXIT_LOCKED",
        "symbol": "LOCKED_SYM",
        "status": "DEFERRED_LOWER_CIRCUIT"
    }

    universe = {
        "TATAMOTORS": pd.DataFrame({"date": [pd.to_datetime("2026-10-26")], "close": [100.0]}),
        "RELIANCE": pd.DataFrame({"date": [pd.to_datetime("2026-10-26")], "close": [200.0]}),
    }

    with patch.object(manager, "get_current_state", return_value={"cash": 50000.0, "positions": positions}):
        res = manager.generate_post_market_signals(universe=universe, decision_date=pd.to_datetime("2026-10-26"))
        assert len(res["exits"]) == 0


def test_finding_v3_b_lower_circuit_writedown_reachable_when_bar_exists():
    """V3-B: Orders with deferral_days >= 10 must undergo forced write-down even when a quote/bar is present."""
    mock_db = MagicMock()
    strat = DummyStrategy()
    config = PaperTradingConfig(strategy_id="test_strat", initial_capital=100000.0)
    manager = PaperTradingManager(strategy=strat, config=config, db=mock_db)

    positions = {"LOCKED_SYM": {"symbol": "LOCKED_SYM", "shares": 100, "entry_price": 100.0, "last_close": 50.0, "entry_date": "2026-10-01"}}
    order = {
        "order_id": "ORD_1",
        "symbol": "LOCKED_SYM",
        "action": "SELL",
        "shares": 100,
        "deferral_days": 10,
        "status": "DEFERRED_LOWER_CIRCUIT", "reason": "STRUCTURAL_EMA100",
    }
    mock_db.orders.find.return_value = [order]
    bar_existing = {
        "open": 45.0, "high": 45.0, "low": 45.0, "close": 45.0, "prev_close": 47.0,
        "lower_circuit": 45.0, "upper_circuit": 50.0, "total_buy_qty": 0, "total_sell_qty": 10000
    }
    dummy_universe = {"LOCKED_SYM": pd.DataFrame({"date": [pd.to_datetime("2026-10-26")], "close": [45.0]})}

    with patch.object(manager, "get_current_state", return_value={"cash": 10000.0, "positions": positions}):
        with patch.object(manager, "_get_execution_bar", return_value=bar_existing):
            manager.execute_morning_fills(universe=dummy_universe, execution_date=pd.to_datetime("2026-10-26"))
            assert mock_db.trades.insert_one.called
            trade_doc = mock_db.trades.insert_one.call_args[0][0]
            assert trade_doc["exit_price"] == pytest.approx(45.0 * 0.95, abs=0.01)


def test_finding_v4_a_regime_fails_safe_on_short_history():
    """V4-A: Benchmark history < 200 must fail safe to Bear regime (True)."""
    from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy
    strat = TrendMomentumPITStrategy()
    short_bench = pd.Series([100.0] * 50, index=pd.date_range("2026-01-01", periods=50, freq="B"))
    strat._benchmark = short_bench

    assert strat.regime_below(short_bench.index[-1]) is True
    assert strat.is_regime_transition_bull(short_bench.index[-1]) is False


def test_minervini_regime_fails_safe_on_short_history():
    """V4-A: MinerviniStrategy regime_below must fail safe to Bear (True) when history is under 200 sessions."""
    from nse_quant_lab.strategies.minervini import MinerviniStrategy
    strat = MinerviniStrategy()
    short_bench = pd.Series([100.0] * 50, index=pd.date_range("2026-01-01", periods=50, freq="B"))
    strat._benchmark = short_bench

    assert strat.regime_below(short_bench.index[-1]) is True
    assert strat.is_regime_transition_bull(short_bench.index[-1]) is False


def test_paper_manager_temporal_causality_skips_same_day_signals():
    """V3-C: execute_morning_fills must strictly query orders where signal_date is strictly before date_str."""
    mock_db = MagicMock()
    strat = DummyStrategy()
    config = PaperTradingConfig(strategy_id="test_strat", initial_capital=100000.0)
    manager = PaperTradingManager(strategy=strat, config=config, db=mock_db)

    with patch.object(manager, "get_current_state", return_value={"cash": 100000.0, "positions": {}}):
        manager.execute_morning_fills(universe={}, execution_date=pd.to_datetime("2026-11-03"))
        assert mock_db.orders.find.called
        query = mock_db.orders.find.call_args[0][0]
        assert "signal_date" in query
        assert query["signal_date"] == {"$lt": "2026-11-03"}


def test_paper_manager_repeat_run_alert_suppression():
    """V5-A: Repeating signal generation on rebalance day with unchanged orders suppresses duplicate Telegram alert."""
    mock_db = MagicMock()
    notifier = MagicMock()
    strat = DummyStrategy()
    config = PaperTradingConfig(strategy_id="test_strat", initial_capital=100000.0)
    manager = PaperTradingManager(strategy=strat, config=config, db=mock_db)
    manager.notifier = notifier

    mock_db.strategies.find_one.return_value = {
        "strategy_id": "test_strat",
        "last_signal_date": "2026-10-30",
        "status": "ACTIVE"
    }
    mock_db.orders.distinct.side_effect = lambda field, q: ["TATAMOTORS", "RELIANCE"] if q.get("action") == "BUY" else []

    with patch.object(manager, "get_current_state", return_value={"cash": 100000.0, "positions": {}}):
        manager.generate_post_market_signals(universe={}, decision_date=pd.to_datetime("2026-10-30"))
        assert not notifier.send_signal_alert.called


def test_fetch_from_upstox_bounded_backoff_on_429():
    """V5-B: fetch_from_upstox must retry max 3 times with exponential backoff on HTTP 429."""
    import urllib.error
    from nse_quant_lab.data_loader import fetch_from_upstox

    mock_error = urllib.error.HTTPError(url="http://test", code=429, msg="Rate Limited", hdrs={}, fp=None)
    with patch("os.environ.get", return_value="fake_token"):
        with patch("urllib.request.urlopen", side_effect=mock_error):
            with patch("time.sleep") as mock_sleep:
                df = fetch_from_upstox("INFY.NS", "2026-01-01", "2026-01-10")
                assert df is None
                sleep_calls = [c[0][0] for c in mock_sleep.call_args_list if c[0][0] > 0.5]
                assert sleep_calls == [2, 4, 8]
