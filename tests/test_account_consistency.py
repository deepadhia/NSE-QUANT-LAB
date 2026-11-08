"""Strategies-table consistency, account display names, quoted-depth capture in the fill audit, and the normaliser's no-behaviour-change rules."""
import sys
from pathlib import Path

import pandas as pd
import pytest

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.paper.manager import PaperTradingManager
from test_paper import InMemoryCollection
from test_paper_failure_modes import _make, ParityMockDb, StubStrategy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_alias_account_shows_its_own_id_as_the_display_name_and_live_accounts_are_unchanged():
    cfg = PaperTradingConfig(strategy_id="fwd_trend_momentum_pit_50k_15slots_under1000", initial_capital=50000.0, telegram_enabled=False)
    db = ParityMockDb()
    PaperTradingManager(StubStrategy(config=cfg), cfg, db).initialize_portfolio()
    doc = db.strategies.find_one({"strategy_id": cfg.strategy_id})
    assert doc["name"] == "Fwd Trend Momentum Pit 50K 15Slots Under1000"
    # an account whose id equals its strategy name keeps the plain name (the existing live accounts)
    s = StubStrategy(config=cfg); s.name = "live_account"
    cfg2 = PaperTradingConfig(strategy_id="live_account", initial_capital=100000.0, telegram_enabled=False)
    db2 = ParityMockDb()
    PaperTradingManager(s, cfg2, db2).initialize_portfolio()
    assert db2.strategies.find_one({"strategy_id": "live_account"})["name"] == "Live Account"


def test_normaliser_fills_only_missing_fields_with_the_values_already_in_effect():
    import normalize_strategy_docs as n
    live = {"strategy_id": "minervini_trend_template", "portfolio_size": 15, "status": "ACTIVE"}
    assert n.planned_changes(live) == {"strategy_class": "minervini_trend_template", "max_entry_price": None, "unaffordable_policy": "TRY_NEXT",
                                       "execution_approach": "APPROACH_3", "rebalance_hold_rank_buffer": 1500}
    complete = {"strategy_id": "trend_momentum_pit", "portfolio_size": 15, "strategy_class": "trend_momentum_pit", "max_entry_price": None,
                "unaffordable_policy": "TRY_NEXT", "execution_approach": "APPROACH_3", "rebalance_hold_rank_buffer": 1500}
    assert n.planned_changes(complete) == {}                                    # never touches a consistent document
    custom = dict(complete, unaffordable_policy="SKIP", execution_approach="BASELINE")
    assert n.planned_changes(custom) == {}                                      # never overwrites a deliberate value


def test_normaliser_corrects_the_hold_buffer_only_for_forward_test_accounts():
    import normalize_strategy_docs as n
    fwd10 = {"strategy_id": "fwd_x_10slots", "portfolio_size": 10, "strategy_class": "trend_momentum_pit", "max_entry_price": 1000.0,
             "unaffordable_policy": "TRY_NEXT", "execution_approach": "APPROACH_3", "rebalance_hold_rank_buffer": 1500}
    assert n.planned_changes(fwd10) == {"rebalance_hold_rank_buffer": 1000}
    live10 = dict(fwd10, strategy_id="some_live_10_slot_account")
    assert n.planned_changes(live10) == {}


def test_manage_strategy_init_uses_the_researched_hold_buffer_and_records_the_approach():
    src = (ROOT / "scripts" / "manage_strategy.py").read_text(encoding="utf-8")
    assert "h_buf = 100 * p_size if is_pit else 15" in src and '"execution_approach": "APPROACH_3"' in src


def test_quote_normaliser_keeps_best_level_quantities():
    from nse_quant_lab import upstox_feed as uf
    import test_upstox_feed as t
    got = uf.fetch_quotes(["NSE_EQ|INE000A01011"], "T", t.opener_returning(t.QUOTE))["NSE_EQ|INE000A01011"]
    assert got["bid_qty"] == 10 and got["ask_qty"] == 7


@pytest.mark.parametrize("shares,expected", [(5, False), (7, False), (8, True)])
def test_fill_audit_flags_orders_larger_than_the_quoted_quantity(shares, expected):
    mgr, db, _ = _make()
    coll = InMemoryCollection()
    db.get_collection = lambda name: coll
    mgr.quote_provider = lambda sym: {"bid": 50.35, "ask": 50.45, "ltp": 50.4, "open": 50.0, "bid_qty": 10.0, "ask_qty": 7.0}
    bar = {"adt_prev": 1e8, "sig_prev": 0.02, "series": "EQ"}
    mgr._audit_fill({"order_id": "O1"}, "AAA", "BUY", shares, bar, 50.45, 50.5, "2026-11-09")
    doc = coll.docs[0]
    assert doc["quoted_qty_at_price"] == 7.0 and doc["depth_shortfall"] is expected
