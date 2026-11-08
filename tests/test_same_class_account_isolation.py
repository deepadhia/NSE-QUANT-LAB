"""Several accounts of ONE strategy class (different slot counts / price caps) sharing one database must neither mix orders nor distort each other.

Strongest property checked: each account's executions, positions and cash when all accounts run TOGETHER in one shared database are
IDENTICAL to what the same account produces when it runs ALONE in its own database. Any cross-account leakage (orders, cash, caps, state in a
shared class/instance) breaks that equality. Synthetic fixtures only.
"""
import pandas as pd
import pytest

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.registry import get_strategy
from test_backtest_paper_parity import ParityMockDb
from test_order_id_isolation import market  # noqa: F401  (pytest fixture)

ACCOUNTS = {
    "fwd_x_100k_2slots_nocap": dict(portfolio_size=2, max_entry_price=None),
    "fwd_x_100k_2slots_under450": dict(portfolio_size=2, max_entry_price=450.0),
    "fwd_x_100k_3slots_nocap": dict(portfolio_size=3, max_entry_price=None),
}


def _mgr(account, bench, db):
    kw = ACCOUNTS[account]
    cfg = PaperTradingConfig(strategy_id=account, initial_capital=100000.0, portfolio_size=kw["portfolio_size"], rebalance_hold_rank_buffer=100 * kw["portfolio_size"],
                             max_entry_price=kw["max_entry_price"], telegram_enabled=False, min_turnover_50d=0.0, min_price=0.0, enforce_pit_universe=False)
    mgr = PaperTradingManager(get_strategy("trend_momentum_pit", cfg, benchmark=bench), cfg, db)   # class resolved like strategy_class_of does
    mgr.initialize_portfolio()
    return mgr


def _replay(accounts, dates, bench, stocks, db):
    mgrs = {a: _mgr(a, bench, db) for a in accounts}
    for d in dates[226:]:
        uni = {s: df[df["date"] <= d].copy() for s, df in stocks.items()}
        for a, m in mgrs.items():
            m.execute_morning_fills(uni, d)
            m.generate_post_market_signals(uni, d)


def _fingerprint(db, account):
    ex = [(e["date"] if "date" in e else e.get("session_date"), e["symbol"], e["action"], e["shares"], round(e["net_cash"], 2))
          for e in db.executions.docs if e["strategy_id"] == account]
    pos = sorted((p["symbol"], p["shares"]) for p in db.positions.docs if p["strategy_id"] == account)
    cash = db.strategies.find_one({"strategy_id": account, "mode": "paper"})["cash"]
    return ex, pos, round(cash, 2)


def test_accounts_of_one_class_are_identical_alone_and_together(market):  # noqa: F811
    dates, bench, stocks = market
    shared = ParityMockDb()
    _replay(list(ACCOUNTS), dates, bench, stocks, shared)
    alone = {}
    for a in ACCOUNTS:
        db = ParityMockDb()
        _replay([a], dates, bench, stocks, db)
        alone[a] = _fingerprint(db, a)
    for a in ACCOUNTS:
        together = _fingerprint(shared, a)
        assert together == alone[a], f"{a}: running with the other accounts changed its executions/positions/cash"
    assert any(alone[a][0] for a in ACCOUNTS), "no executions happened in the replay; the test would be vacuous"


def test_no_order_or_execution_is_owned_by_two_accounts_and_the_cap_only_affects_its_own_account(market):  # noqa: F811
    dates, bench, stocks = market
    db = ParityMockDb()
    _replay(list(ACCOUNTS), dates, bench, stocks, db)
    ids = [o["order_id"] for o in db.orders.docs]
    assert len(ids) == len(set(ids))
    for o in db.orders.docs:
        assert o["strategy_id"] in ACCOUNTS and o["strategy_id"] in o["order_id"]
    ex_ids = [(e["strategy_id"], e["exec_id"]) for e in db.executions.docs]
    assert len(ex_ids) == len(set(ex_ids))
    capped_buys = [e for e in db.executions.docs if e["strategy_id"] == "fwd_x_100k_2slots_under450" and e["action"] == "BUY"]
    assert all(e["raw_price"] < 450.0 * 1.021 for e in capped_buys)       # cap holds for its account (fills may gap up <= 2%)
    uncapped = [e for e in db.executions.docs if e["strategy_id"] == "fwd_x_100k_2slots_nocap" and e["action"] == "BUY"]
    assert any(e["raw_price"] >= 450.0 * 1.021 for e in uncapped), "the cap leaked into an account that has no cap, or the fixture never exceeds it"
