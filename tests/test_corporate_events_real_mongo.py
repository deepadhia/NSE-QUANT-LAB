"""
Corporate events against a REAL MongoDB (uniquely named temporary database on localhost, dropped afterwards). The in-memory
doubles do not enforce the unique indexes, upsert semantics or cursor behaviour the event path relies on. Skipped when no
MongoDB is reachable on localhost. The URI is explicit on purpose: this test must never use MONGO_URI from the environment.
"""
import uuid

import pandas as pd
import pymongo
import pytest

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.corporate_events import CASH_OUT, CONVERSION, build_event
from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.paper.manager import PaperTradingManager
from test_paper_failure_modes import StubStrategy, _frame

URI = "mongodb://localhost:27017/"
EVIDENCE = "NSE circular NSE/CML/00001 dated 2026-03-01"
DECISION = pd.Timestamp("2026-03-03")


@pytest.fixture()
def mdb():
    client = pymongo.MongoClient(URI, serverSelectionTimeoutMS=1500)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip("no local MongoDB available")
    name = f"nse_lab_events_test_{uuid.uuid4().hex[:8]}"
    db = MongoDatabase(URI, name)
    yield db
    client.drop_database(name)


def _mgr(db, sid):
    cfg = PaperTradingConfig(strategy_id=sid, initial_capital=100000.0, portfolio_size=5, telegram_enabled=False,
                             mongo_uri=URI, db_name=db.db_name)
    m = PaperTradingManager(StubStrategy(config=cfg), cfg, db)
    m.initialize_portfolio()                      # creates the real unique indexes
    return m


def _hold(db, sid, sym, shares=100, price=100.0, entry="2026-01-02"):
    db.positions.insert_one({"strategy_id": sid, "mode": "paper", "symbol": sym, "shares": shares, "entry_price": price,
                             "entry_fill_price": price, "entry_date": entry, "entry_outlay": shares * price + 20.0,
                             "entry_fees": 20.0, "last_close": price, "market_value": shares * price,
                             "unrealized_pnl": 0.0, "applied_actions": []})


def _cash(db, sid):
    return db.strategies.find_one({"strategy_id": sid, "mode": "paper"})["cash"]


def test_two_accounts_holding_the_same_symbol_each_get_the_event_once_on_real_mongo(mdb):
    a, b = _mgr(mdb, "acct_a"), _mgr(mdb, "acct_b")
    _hold(mdb, "acct_a", "AAA", 100)
    _hold(mdb, "acct_b", "AAA", 40)
    mdb.get_collection("corporate_events").insert_one(build_event(CASH_OUT, "AAA", "2026-03-02", EVIDENCE, cash_per_share=10.0))
    start_a, start_b = _cash(mdb, "acct_a"), _cash(mdb, "acct_b")
    uni = {"ZZZ": _frame("2026-03-03")}
    for m in (a, b, a, b):                                           # second pass = scheduler retry
        m.generate_post_market_signals(uni, DECISION)
    assert _cash(mdb, "acct_a") == pytest.approx(start_a + 1000.0, abs=0.01)
    assert _cash(mdb, "acct_b") == pytest.approx(start_b + 400.0, abs=0.01)
    assert mdb.executions.count_documents({}) == 2 and mdb.trades.count_documents({}) == 2
    assert mdb.positions.count_documents({"symbol": "AAA"}) == 0


def test_conversion_on_real_mongo_respects_unique_position_index_and_is_idempotent(mdb):
    m = _mgr(mdb, "acct_c")
    _hold(mdb, "acct_c", "AAA", 100)
    mdb.get_collection("corporate_events").insert_one(
        build_event(CONVERSION, "AAA", "2026-03-02", EVIDENCE, new_symbol="BBB", ratio=1.875, fraction_cash_per_new_share=90.0))
    start = _cash(mdb, "acct_c")
    uni = {"BBB": _frame("2026-03-03", price=53.5)}
    for _ in range(3):
        m.generate_post_market_signals(uni, DECISION)
    docs = list(mdb.positions.find({"strategy_id": "acct_c"}))
    assert [d["symbol"] for d in docs] == ["BBB"] and docs[0]["shares"] == 187 and docs[0]["entry_date"] == "2026-01-02"
    assert _cash(mdb, "acct_c") == pytest.approx(start + 45.0, abs=0.01)
    assert mdb.executions.count_documents({"strategy_id": "acct_c"}) == 1
    ledger = 100000.0 + sum(e["net_cash"] for e in mdb.executions.find({"strategy_id": "acct_c"}))
    assert _cash(mdb, "acct_c") == pytest.approx(ledger, abs=0.01)


def test_crash_resume_on_real_mongo_credits_cash_once(mdb, monkeypatch):
    m = _mgr(mdb, "acct_d")
    _hold(mdb, "acct_d", "AAA", 100)
    mdb.get_collection("corporate_events").insert_one(build_event(CASH_OUT, "AAA", "2026-03-02", EVIDENCE, cash_per_share=10.0))
    start = _cash(mdb, "acct_d")
    monkeypatch.setattr(m, "_record_trade", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated crash")))
    with pytest.raises(RuntimeError):
        m.generate_post_market_signals({"ZZZ": _frame("2026-03-03")}, DECISION)
    m2 = _mgr(mdb, "acct_d")
    for _ in range(2):
        m2.generate_post_market_signals({"ZZZ": _frame("2026-03-03")}, DECISION)
    assert _cash(mdb, "acct_d") == pytest.approx(start + 1000.0, abs=0.01)
    assert mdb.trades.count_documents({"strategy_id": "acct_d"}) == 1 and mdb.positions.count_documents({"strategy_id": "acct_d"}) == 0
    nav = mdb.daily_nav.find_one({"strategy_id": "acct_d", "date": "2026-03-03"})
    assert nav["cash"] == pytest.approx(_cash(mdb, "acct_d"), abs=0.01)
