"""
tests/test_split_single_application.py -- a split/bonus must change an open position's share count EXACTLY ONCE.

Two writers can touch shares: scripts/ingest_upstox_fulltier.py (sees the price discontinuity at 17:45) and
PaperTradingManager._apply_corporate_actions (applies a recorded `corporate_actions` document at 18:00 / 09:16).
Before the fix both ran: 1:2 split -> 4x shares and NAV overstated by 100% of the position (AGENTS.md invariant 6).
"""
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ingest_upstox_fulltier as ing  # noqa: E402

from nse_quant_lab.config import PaperTradingConfig  # noqa: E402
from nse_quant_lab.paper.manager import PaperTradingManager  # noqa: E402
from nse_quant_lab.registry import get_strategy  # noqa: E402
from tests.test_corporate_actions import MockDatabase  # noqa: E402
from tests.test_ingest_upstox_fulltier import FakeCol, FakeMany, bar  # noqa: E402

TARGETS = pd.DataFrame({"symbol": ["BBB"], "key": ["K2"], "last_close": [200.0]})
PANEL_LAST, RUN = dt.date(2026, 10, 30), dt.date(2026, 11, 2)


def _fetch(k, f, t):   # BBB 2:1 split on 2026-11-02: adjusted previous close 100, new bar opens at 100
    return [bar("2026-10-30", 100, 100), bar("2026-11-02", 100, 101)] if f <= "2026-10-30" else [bar("2026-11-02", 100, 101)]


class _CA(FakeMany):
    pass


def _position():
    return {"_id": 1, "strategy_id": "alpha_semivol_pit", "symbol": "BBB", "mode": "paper", "shares": 10, "entry_price": 200.0,
            "entry_fill_price": 202.0, "last_close": 200.0, "entry_date": "2026-10-01", "applied_actions": []}


def _ingest(pos, ca_docs, col=None):
    orders = FakeMany([{"_id": 9, "symbol": "BBB", "mode": "paper", "action": "BUY", "shares": 0, "price": 200.0, "status": "PENDING_OPEN"}])
    ing.run_ingestion(TARGETS, RUN, PANEL_LAST, col or FakeCol(), FakeCol(), _fetch, now=lambda: "t",
                      positions_col=pos, orders_col=orders, corporate_actions_col=_CA(ca_docs))
    return orders


def _manager_with(position):
    db = MockDatabase()
    cfg = PaperTradingConfig(strategy_id="alpha_semivol_pit", initial_capital=100000.0)
    mgr = PaperTradingManager(strategy=get_strategy("alpha_semivol_pit", cfg), config=cfg, db=db)
    doc = {k: v for k, v in position.items() if k != "_id"}
    db.positions.insert_one(doc)
    return db, mgr


def test_recorded_action_is_applied_once_by_the_manager_not_by_ingestion():
    pos = FakeMany([_position()])
    orders = _ingest(pos, [{"_id": 5, "symbol": "BBB", "ex_date": "2026-11-02", "ratio": 2.0}])
    p = pos.docs[0]
    assert p["shares"] == 10 and "split_adjusted_factor" not in p            # ingestion left the share count alone
    assert orders.docs[0]["price"] == pytest.approx(100.0)                    # pending order prices still follow the new price basis

    db, mgr = _manager_with(p)
    db.corporate_actions.insert_one({"symbol": "BBB", "ex_date": "2026-11-02", "ratio": 2.0, "source": "NSE_OFFICIAL"})
    held = {"BBB": db.positions.find_one({"symbol": "BBB"})}
    mgr._apply_corporate_actions(held, as_of_date="2026-11-02")
    mgr._apply_corporate_actions(held, as_of_date="2026-11-03")              # re-run / next day: still once
    assert db.positions.find_one({"symbol": "BBB"})["shares"] == 20           # NOT 40


def test_unrecorded_discontinuity_is_rescaled_by_ingestion_and_a_later_document_is_not_applied_again():
    pos = FakeMany([_position()])
    _ingest(pos, [])                                                          # nothing recorded yet
    p = pos.docs[0]
    assert p["shares"] == 20 and p["entry_price"] == pytest.approx(100.0)
    assert p["applied_actions"] == ["BBB:2026-11-02:ingest"]

    db, mgr = _manager_with(p)
    db.corporate_actions.insert_one({"symbol": "BBB", "ex_date": "2026-11-02", "ratio": 2.0, "source": "MANUAL"})   # recorded afterwards
    mgr._apply_corporate_actions({"BBB": db.positions.find_one({"symbol": "BBB"})}, as_of_date="2026-11-03")
    assert db.positions.find_one({"symbol": "BBB"})["shares"] == 20           # NOT 40


def test_unrelated_recorded_action_does_not_suppress_ingestion_rescale():
    pos = FakeMany([_position()])
    _ingest(pos, [{"_id": 5, "symbol": "BBB", "ex_date": "2026-11-02", "ratio": 1.2}])     # ratio far from the 2.0 price gap
    assert pos.docs[0]["shares"] == 20


def test_recorded_action_outside_the_gap_window_is_ignored():
    assert not ing.recorded_action_matches(_CA([{"symbol": "BBB", "ex_date": "2026-09-01", "ratio": 2.0}]), "BBB", "2026-10-30", "2026-11-02", 2.0)
    assert not ing.recorded_action_matches(_CA([{"symbol": "ZZZ", "ex_date": "2026-11-02", "ratio": 2.0}]), "BBB", "2026-10-30", "2026-11-02", 2.0)
    assert not ing.recorded_action_matches(None, "BBB", "2026-10-30", "2026-11-02", 2.0)
    assert ing.recorded_action_matches(_CA([{"symbol": "BBB", "ex_date": "2026-11-02", "ratio": "2.0"}]), "BBB", "2026-10-30", "2026-11-02", 2.0)


def test_old_behaviour_would_have_quadrupled_shares(monkeypatch):
    """Sensitivity check: with the recorded-action check disabled (the pre-fix behaviour) the same flow yields 4x shares."""
    monkeypatch.setattr(ing, "recorded_action_matches", lambda *a, **k: False)
    pos = FakeMany([_position()])
    _ingest(pos, [{"_id": 5, "symbol": "BBB", "ex_date": "2026-11-02", "ratio": 2.0}])
    p = pos.docs[0]
    assert p["shares"] == 20
    # recorded doc present and (in the old code) no marker keyed to it -> strip our marker to mimic the old code path exactly
    p["applied_actions"] = []
    db, mgr = _manager_with(p)
    db.corporate_actions.insert_one({"symbol": "BBB", "ex_date": "2026-11-02", "ratio": 2.0, "source": "NSE_OFFICIAL"})
    mgr._apply_corporate_actions({"BBB": db.positions.find_one({"symbol": "BBB"})}, as_of_date="2026-11-02")
    assert db.positions.find_one({"symbol": "BBB"})["shares"] == 40
