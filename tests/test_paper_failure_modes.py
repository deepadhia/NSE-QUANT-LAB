"""
test_paper_failure_modes.py -- Falsification tests for the paper-trading manager (audit regression guards).

Each test targets a real-world failure mode, not the happy path:
  H1  crash mid-fill must never double-credit cash / double-fill / lose a position      (idempotent, resumable fills)
  H2  a data gap must not liquidate a healthy stock at a 5% haircut
  H3  recorded splits/bonuses change share counts exactly once; unrecorded jumps scream
  H5  a quote outage must retain the order, fail the run, and never silently expire the entry
  M2  signals for a date older than recorded activity are refused
  M5/M1 morning path is exercised WITHOUT the execution day's EOD bar (09:16 reality)
"""

import logging

import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.base_strategy import BaseStrategy
from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.paper.manager import PaperTradingManager
from test_backtest_paper_parity import ParityMockDb

SID = "stub_strategy"


class StubStrategy(BaseStrategy):
    name = "STUB"
    rebalance_frequency = "MONTHLY"

    def compute_indicators(self, df, config=None):
        return df

    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        return pd.DataFrame(columns=["symbol", "close", "turnover_50d", "rank"])

    def evaluate_daily_exits(self, universe, decision_date, open_positions, config):
        return {}

    def is_rebalance_decision_date(self, timeline, decision_date, config):
        return False


def _frame(last_date: str, n: int = 60, price: float = 100.0, with_exec_bar: bool = True) -> pd.DataFrame:
    dates = pd.bdate_range(end=last_date, periods=n)
    c = np.full(n, price)
    return pd.DataFrame({"date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": 1_000_000.0, "turnover": c * 1_000_000.0, "series": "EQ"})


def _make(db=None, capital=100000.0):
    cfg = PaperTradingConfig(strategy_id=SID, initial_capital=capital, portfolio_size=5, telegram_enabled=False,
                             unaffordable_policy="SKIP")
    fresh = db is None
    db = db or ParityMockDb()
    mgr = PaperTradingManager(StubStrategy(config=cfg), cfg, db)
    if fresh:                       # a restarted process reuses the existing DB documents
        mgr.initialize_portfolio()
    return mgr, db, cfg


def _position(db, sym="AAA", shares=100, price=100.0, entry_date="2026-01-02"):
    db.positions.insert_one({
        "strategy_id": SID, "mode": "paper", "symbol": sym, "shares": shares, "entry_price": price,
        "entry_fill_price": price, "entry_date": entry_date, "entry_outlay": shares * price + 20.0, "entry_fees": 20.0,
        "last_close": price, "market_value": shares * price, "unrealized_pnl": 0.0, "applied_actions": [],
    })


def _order(db, oid, sym, action, signal_date, reason="STRUCTURAL_X", shares=100, price=100.0, status="PENDING_OPEN"):
    db.orders.insert_one({
        "order_id": oid, "strategy_id": SID, "mode": "paper", "symbol": sym, "action": action, "shares": shares,
        "price": price, "order_type": "EXIT_AT_OPEN" if action == "SELL" else "ENTRY_AT_OPEN", "status": status,
        "reason": reason, "signal_date": signal_date, "deferral_days": 0,
    })


def _cash(db):
    return db.strategies.find_one({"strategy_id": SID, "mode": "paper"})["cash"]


# ------------------------------------------------------------------ H1: crash-safe, idempotent fills
@pytest.mark.parametrize("crash_point", ["_persist_cash", "_record_trade"])
def test_sell_fill_survives_crash_without_double_credit(crash_point, monkeypatch):
    mgr, db, _ = _make()
    _position(db)
    _order(db, f"ORD_EXIT_{SID}_2026-03-02_AAA", "AAA", "SELL", "2026-03-02")
    uni = {"AAA": _frame("2026-03-03")}                         # execution day bar present (replay-style)
    start_cash = _cash(db)

    original = getattr(mgr, crash_point)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(mgr, crash_point, boom)
    with pytest.raises(RuntimeError):
        mgr.execute_morning_fills(uni, pd.Timestamp("2026-03-03"))
    monkeypatch.setattr(mgr, crash_point, original)

    # retry (fresh manager, same DB): must finish the job exactly once
    mgr2, _, _ = _make(db)
    mgr2.execute_morning_fills(uni, pd.Timestamp("2026-03-03"))
    mgr2.execute_morning_fills(uni, pd.Timestamp("2026-03-03"))   # and a second retry must be a no-op

    execs = [e for e in db.executions.docs if e["action"] == "SELL"]
    assert len(execs) == 1
    assert _cash(db) == pytest.approx(start_cash + execs[0]["net_cash"], abs=0.01), "cash credited more or less than once"
    assert db.positions.find_one({"strategy_id": SID, "symbol": "AAA"}) is None
    assert len(db.trades.docs) == 1
    assert db.orders.find_one({"order_id": f"ORD_EXIT_{SID}_2026-03-02_AAA"})["status"] == "FILLED"


def test_buy_fill_survives_crash_without_free_shares(monkeypatch):
    mgr, db, _ = _make()
    _order(db, f"ORD_BUY_{SID}_2026-03-02_BBB", "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0)
    uni = {"BBB": _frame("2026-03-03")}
    start_cash = _cash(db)

    monkeypatch.setattr(mgr, "_audit_fill", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated crash")))
    with pytest.raises(RuntimeError):
        mgr.execute_morning_fills(uni, pd.Timestamp("2026-03-03"))

    mgr2, _, _ = _make(db)
    mgr2.execute_morning_fills(uni, pd.Timestamp("2026-03-03"))

    execs = [e for e in db.executions.docs if e["action"] == "BUY"]
    assert len(execs) == 1
    pos = db.positions.find_one({"strategy_id": SID, "symbol": "BBB"})
    assert pos is not None and pos["shares"] == execs[0]["shares"]
    assert _cash(db) == pytest.approx(start_cash + execs[0]["net_cash"], abs=0.01)   # net_cash is negative
    assert _cash(db) >= 0
    assert db.orders.find_one({"order_id": f"ORD_BUY_{SID}_2026-03-02_BBB"})["status"] == "FILLED"


def test_exec_ids_are_deterministic_per_order():
    mgr, db, _ = _make()
    _order(db, f"ORD_BUY_{SID}_2026-03-02_BBB", "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0)
    mgr.execute_morning_fills({"BBB": _frame("2026-03-03")}, pd.Timestamp("2026-03-03"))
    assert [e["exec_id"] for e in db.executions.docs] == [f"EXEC_ORD_BUY_{SID}_2026-03-02_BBB"]


# ------------------------------------------------------------------ H2: data gap is not a delisting
def test_data_gap_forced_exit_cancelled_when_stock_trades_normally():
    mgr, db, _ = _make()
    _position(db)
    _order(db, f"ORD_EXIT_{SID}_2026-03-02_AAA", "AAA", "SELL", "2026-03-02", reason="DATA_ENDED_FORCED_EXIT")
    mgr.execute_morning_fills({"AAA": _frame("2026-03-03")}, pd.Timestamp("2026-03-03"))
    assert db.positions.find_one({"strategy_id": SID, "symbol": "AAA"}) is not None, "healthy stock was liquidated"
    assert db.executions.docs == []
    assert db.orders.find_one({"order_id": f"ORD_EXIT_{SID}_2026-03-02_AAA"})["cancel_reason"] == "DATA_RECOVERED"


# ------------------------------------------------------------------ H3: corporate actions
def test_recorded_split_applied_exactly_once():
    mgr, db, _ = _make()
    _position(db, shares=100, price=200.0, entry_date="2026-01-02")
    db.get_collection("corporate_actions").insert_one({"symbol": "AAA", "ex_date": "2026-03-02", "ratio": 2.0})
    uni = {"AAA": _frame("2026-03-03", price=100.0)}
    for _ in range(2):
        mgr.generate_post_market_signals(uni, pd.Timestamp("2026-03-03"))
    pos = db.positions.find_one({"strategy_id": SID, "symbol": "AAA"})
    assert pos["shares"] == 200 and pos["entry_price"] == pytest.approx(100.0)
    assert len(pos["applied_actions"]) == 1


def test_unrecorded_price_discontinuity_is_loud(caplog):
    mgr, db, _ = _make()
    _position(db, shares=100, price=200.0)           # stored last_close 200 ...
    uni = {"AAA": _frame("2026-03-03", price=100.0)}  # ... bar says 100 with no corporate action recorded
    with caplog.at_level(logging.CRITICAL, logger="nse_quant_lab.paper.manager"):
        mgr.generate_post_market_signals(uni, pd.Timestamp("2026-03-03"))
    assert any("POSSIBLE_CORPORATE_ACTION" in r.message for r in caplog.records)


# ------------------------------------------------------------------ H5: quote outage
def test_quote_outage_retains_buy_and_reports_no_quote(monkeypatch):
    mgr, db, _ = _make()
    oid = f"ORD_BUY_{SID}_2026-03-02_BBB"
    _order(db, oid, "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0)
    uni = {"BBB": _frame("2026-03-02")}                       # no execution-day row, no quote provider
    monkeypatch.setattr(mgr, "_get_execution_bar", lambda *a, **k: None)   # Upstox + yfinance both down
    res = mgr.execute_morning_fills(uni, pd.Timestamp("2026-03-03"))
    assert res["no_quote"], "outage not reported -> run would exit 0"
    assert db.orders.find_one({"order_id": oid})["status"] == "PENDING_OPEN", "entry silently expired"
    assert db.executions.docs == []


def test_retained_buy_is_not_filled_on_a_stale_signal(monkeypatch):
    mgr, db, _ = _make()
    oid = f"ORD_BUY_{SID}_2026-03-02_BBB"
    _order(db, oid, "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0)
    # two sessions later the last completed session (03-03) is newer than the signal date (03-02)
    uni = {"BBB": _frame("2026-03-04")}
    mgr.execute_morning_fills(uni, pd.Timestamp("2026-03-05"))
    o = db.orders.find_one({"order_id": oid})
    assert o["status"] == "SKIPPED_EXPIRED" and o["cancel_reason"] == "STALE_SIGNAL"
    assert db.executions.docs == []


def test_executable_ask_is_used_without_todays_bar():
    """09:16 reality: the universe has NO bar for the execution day; the injected quote supplies the open."""
    mgr, db, _ = _make()
    oid = f"ORD_BUY_{SID}_2026-03-02_BBB"
    _order(db, oid, "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0)
    mgr.quote_provider = lambda sym: {"open": 100.5, "high": 100.8, "low": 100.2, "ltp": 100.6, "ask": 100.7, "bid": 100.5, "ts": "2026-03-03T09:16:00+05:30"}
    mgr.execute_morning_fills({"BBB": _frame("2026-03-02")}, pd.Timestamp("2026-03-03"))
    ex = db.executions.docs[0]
    assert ex["raw_price"] == 100.7, "fill must use the executable ask, never an unavailable open"


def test_gap_above_cap_skips_entry_and_never_fills():
    mgr, db, _ = _make()
    oid = f"ORD_BUY_{SID}_2026-03-02_BBB"
    _order(db, oid, "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0, price=100.0)
    mgr.quote_provider = lambda sym: {"open": 103.0, "high": 103.5, "low": 102.5, "ltp": 103.0, "ask": 103.0, "bid": 102.9, "ts": "2026-03-03T09:16:00+05:30"}   # +3% > +2% cap
    mgr.execute_morning_fills({"BBB": _frame("2026-03-02")}, pd.Timestamp("2026-03-03"))
    assert db.orders.find_one({"order_id": oid})["status"] == "SKIPPED_GAP_LIMIT_EXCEEDED"
    assert db.executions.docs == [] and _cash(db) == 100000.0


def test_cash_never_negative_when_allocation_exceeds_cash():
    mgr, db, _ = _make(capital=1500.0)               # cannot afford a 5-slot allocation of an expensive stock
    oid = f"ORD_BUY_{SID}_2026-03-02_BBB"
    _order(db, oid, "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0, price=1000.0)
    mgr.quote_provider = lambda sym: {"open": 1000.0, "high": 1001.0, "low": 999.0, "ltp": 1000.0, "ask": 1000.0, "bid": 999.9, "ts": "2026-03-03T09:16:00+05:30"}
    mgr.execute_morning_fills({"BBB": _frame("2026-03-02", price=1000.0)}, pd.Timestamp("2026-03-03"))
    assert _cash(db) >= 0


# ------------------------------------------------------------------ M2: back-dated run
def test_backdated_signal_run_is_refused():
    mgr, db, _ = _make()
    _position(db)
    db.strategies.update_one({"strategy_id": SID, "mode": "paper"}, {"$set": {"last_execution_date": "2026-03-10"}})
    nav_before = len(db.daily_nav.docs)
    res = mgr.generate_post_market_signals({"AAA": _frame("2026-03-03")}, pd.Timestamp("2026-03-03"))
    assert res["status"] == "REFUSED_STALE_DECISION_DATE"
    assert len(db.daily_nav.docs) == nav_before
    assert not [o for o in db.orders.docs if o["status"] == "PENDING_OPEN"]


def test_split_rebases_stored_mark_so_sizing_and_alerts_stay_correct(caplog):
    """Regression: after applying a split the stored last_close must move to the new price basis, otherwise
    equity for sizing is overstated (oversized buys) and a false POSSIBLE_CORPORATE_ACTION alert fires."""
    mgr, db, _ = _make()
    _position(db, shares=100, price=200.0, entry_date="2026-01-02")
    db.get_collection("corporate_actions").insert_one({"symbol": "AAA", "ex_date": "2026-03-03", "ratio": 2.0, "action_type": "split"})
    _order(db, f"ORD_BUY_{SID}_2026-03-02_BBB", "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0)
    mgr.quote_provider = lambda s: {"open": 100.0, "high": 100.0, "low": 100.0, "ltp": 100.0, "ask": 100.0, "bid": 100.0, "ts": "2026-03-03T09:16:00+05:30"}
    uni = {"AAA": _frame("2026-03-02", price=200.0), "BBB": _frame("2026-03-02", price=100.0)}
    mgr.execute_morning_fills(uni, pd.Timestamp("2026-03-03"))
    pos = db.positions.find_one({"strategy_id": SID, "symbol": "AAA"})
    assert pos["shares"] == 200 and pos["last_close"] == pytest.approx(100.0)
    buy = [e for e in db.executions.docs if e["action"] == "BUY"][0]
    # true equity ~ cash 100000 + 200 x 100 = 120000 -> 5-slot target 24000 -> at most 240 shares at 100
    assert buy["shares"] <= 240

    with caplog.at_level(logging.CRITICAL, logger="nse_quant_lab.paper.manager"):
        mgr.generate_post_market_signals({"AAA": _frame("2026-03-03", price=100.0), "BBB": _frame("2026-03-03", price=100.0)}, pd.Timestamp("2026-03-03"))
    assert not any("POSSIBLE_CORPORATE_ACTION" in r.message for r in caplog.records), "false alert after a recorded split"


def test_missing_held_bar_does_not_block_healthy_position_exits():
    """Falsification test: When symbol A lacks an EOD bar, symbol A is retained at last close without
    sales, but healthy symbol B's technical exit is NOT blocked. Rebalance buys are blocked until data is restored."""
    class ExitStrategy(StubStrategy):
        def evaluate_daily_exits(self, universe, decision_date, open_positions, config):
            exits = {}
            if "HEALTHY_SYM" in open_positions:
                exits["HEALTHY_SYM"] = "TECHNICAL_STOP"
            return exits

        def is_rebalance_decision_date(self, timeline, decision_date, config):
            return True

        def select_entry_candidates(self, universe, decision_date, current_holdings, config):
            return pd.DataFrame([{"symbol": "NEW_CANDIDATE", "close": 50.0, "turnover_50d": 1e8, "rank": 1}])

    cfg = PaperTradingConfig(strategy_id=SID, initial_capital=100000.0, portfolio_size=5, telegram_enabled=False)
    db = ParityMockDb()
    strat = ExitStrategy(config=cfg)
    mgr = PaperTradingManager(strat, cfg, db)
    mgr.initialize_portfolio()

    _position(db, sym="HEALTHY_SYM", shares=100, price=100.0)
    _position(db, sym="SUSPENDED_SYM", shares=50, price=200.0)

    # Universe only contains HEALTHY_SYM and NEW_CANDIDATE; SUSPENDED_SYM is missing!
    uni = {
        "HEALTHY_SYM": _frame("2026-03-03", price=90.0),
        "NEW_CANDIDATE": _frame("2026-03-03", price=50.0),
    }

    res = mgr.generate_post_market_signals(uni, pd.Timestamp("2026-03-03"))

    # 1. Suspended symbol reported in missing_held_bars
    assert "SUSPENDED_SYM" in res.get("missing_held_bars", [])

    # 2. Healthy symbol successfully staged its technical exit order
    exit_order = db.orders.find_one({"strategy_id": SID, "symbol": "HEALTHY_SYM", "action": "SELL"})
    assert exit_order is not None
    assert exit_order["reason"] == "TECHNICAL_STOP"
    assert exit_order["status"] == "PENDING_OPEN"

    # 3. Suspended symbol has NO staged exit order (retained, never liquidated fictitiously)
    suspended_order = db.orders.find_one({"strategy_id": SID, "symbol": "SUSPENDED_SYM"})
    assert suspended_order is None

    # 4. New BUY orders are blocked because held positions are unpriced
    buy_orders = [o for o in db.orders.docs if o["action"] == "BUY"]
    assert buy_orders == [], f"Expected 0 buy orders when data is missing, got: {buy_orders}"



def _run_pipeline_with_manager_result(result):
    """Drive scripts/run_daily_pipeline.run_strategy_pipeline with a manager that returns `result`."""
    import importlib.util
    import os
    from unittest.mock import MagicMock, patch

    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_daily_pipeline.py")
    spec = importlib.util.spec_from_file_location("run_daily_pipeline_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake_mgr = MagicMock()
    fake_mgr.generate_post_market_signals.return_value = result
    with patch.object(mod, "PaperTradingManager", return_value=fake_mgr), patch.object(mod, "get_strategy", return_value=MagicMock()):
        return mod.run_strategy_pipeline({"strategy_id": "trend_momentum_pit", "mode": "paper"}, {}, pd.Timestamp("2026-03-03"), MagicMock())


def test_pipeline_reports_stale_decision_date_refusal_as_a_problem():
    """A refused back-dated run is a failure: it must produce a problem (-> exit 4), not a clean skip."""
    problems = _run_pipeline_with_manager_result(
        {"status": "REFUSED_STALE_DECISION_DATE", "exits": [], "buys": [], "message": "older than recorded activity"})
    assert problems and "REFUSED_STALE_DECISION_DATE" in problems[0]


def test_pipeline_reports_missing_held_bar_as_a_problem_and_paused_as_clean():
    problems = _run_pipeline_with_manager_result({"exits": [], "buys": [], "missing_held_bars": ["SUSPENDED_SYM"]})
    assert problems and "MISSING_HELD_BAR" in problems[0] and "SUSPENDED_SYM" in problems[0]
    assert _run_pipeline_with_manager_result({"status": "PAUSED", "exits": [], "buys": []}) == []
    assert _run_pipeline_with_manager_result({"exits": [], "buys": [], "missing_held_bars": []}) == []
