"""
test_corporate_events.py -- evidenced merger / delisting / cash-out events for held names.

Falsification targets: an event must be applied exactly once per account (retries, crash mid-way), must never be applied
when its terms are incomplete (no evidence, no explicit cash, unresolved fraction), must never touch a position opened
after the event, and missing data on its own must still never sell anything.
"""

import pandas as pd
import pytest

from nse_quant_lab.corporate_events import (CASH_OUT, CONVERSION, FRACTION_CASH, build_event, split_whole_and_fraction,
                                            validate_event)
from test_paper_failure_modes import SID, _cash, _frame, _make, _order, _position

DECISION = pd.Timestamp("2026-03-03")
EVIDENCE = "NSE circular NSE/CML/00001 dated 2026-03-01"


def _record(db, doc):
    db.get_collection("corporate_events").insert_one(doc)


def _cash_out(symbol="AAA", cps=12.5, eff="2026-03-02", evidence=EVIDENCE):
    return build_event(CASH_OUT, symbol, eff, evidence, cash_per_share=cps)


def _conversion(ratio=1.87, fraction=None, eff="2026-03-02", new="BBB"):
    return build_event(CONVERSION, "AAA", eff, EVIDENCE, new_symbol=new, ratio=ratio, fraction_cash_per_new_share=fraction)


def _run(mgr, uni=None):
    return mgr.generate_post_market_signals(uni if uni is not None else {"ZZZ": _frame("2026-03-03")}, DECISION)


def _ledger_cash(db, capital=100000.0):
    return capital + sum(e["net_cash"] for e in db.executions.docs)


def _assert_nav_cash_consistent(db):
    """The NAV record is built from the in-memory cash of the run: a double-counted credit shows up here even when the
    stored balance (protected by compare-and-swap) is right."""
    nav = db.daily_nav.find_one({"strategy_id": SID, "date": "2026-03-03"})
    assert nav["cash"] == pytest.approx(_cash(db), abs=0.01), f"NAV cash {nav['cash']} != stored cash {_cash(db)}"
    assert nav["nav"] == pytest.approx(nav["cash"] + nav["holdings_val"], abs=0.01)


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.update(evidence="n/a"), "evidence"),
    (lambda d: d.update(effective_date="2026-13-40"), "real date|YYYY"),
    (lambda d: d.update(type="MERGER"), "type"),
    (lambda d: d.update(event_id="wrong"), "event_id"),
    (lambda d: d.pop("cash_per_share"), "cash_per_share"),
    (lambda d: d.update(cash_per_share=-1.0), "cash_per_share"),
    (lambda d: d.update(cash_per_share=float("nan")), "cash_per_share"),
])
def test_invalid_cash_out_documents_are_rejected(mutate, msg):
    doc = _cash_out()
    mutate(doc)
    with pytest.raises(ValueError, match=msg):
        validate_event(doc)


def test_conversion_validation_rejects_self_conversion_and_bad_ratio():
    with pytest.raises(ValueError, match="new_symbol"):
        build_event(CONVERSION, "AAA", "2026-03-02", EVIDENCE, new_symbol="AAA", ratio=1.0)
    with pytest.raises(ValueError, match="ratio"):
        build_event(CONVERSION, "AAA", "2026-03-02", EVIDENCE, new_symbol="BBB", ratio=0)


def test_split_whole_and_fraction_is_float_safe():
    assert split_whole_and_fraction(100, 1.87) == (187, 0.0)
    whole, frac = split_whole_and_fraction(100, 1.875)
    assert whole == 187 and frac == pytest.approx(0.5)


# ------------------------------------------------------------------ CASH_OUT
def test_cash_out_closes_position_credits_cash_once_and_clears_the_missing_bar_alarm():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100, price=100.0)
    _order(db, f"ORD_EXIT_{SID}_2026-03-02_AAA", "AAA", "SELL", "2026-03-02")
    _record(db, _cash_out(cps=12.5))
    start = _cash(db)

    res = _run(mgr)
    assert res["missing_held_bars"] == []
    assert db.positions.find_one({"symbol": "AAA"}) is None
    assert _cash(db) == pytest.approx(start + 1250.0, abs=0.01)
    assert db.daily_nav.find_one({"strategy_id": SID, "date": "2026-03-03"})["cash"] == pytest.approx(start + 1250.0, abs=0.01)
    trade = db.trades.find_one({"symbol": "AAA"})
    assert trade["exit_reason"].startswith("EVENT_CASH_OUT") and trade["net_pnl"] == pytest.approx(1250.0 - (100 * 100.0 + 20.0), abs=0.01)
    assert db.orders.find_one({"order_id": f"ORD_EXIT_{SID}_2026-03-02_AAA"})["status"] == "CANCELLED"
    assert _cash(db) == pytest.approx(_ledger_cash(db), abs=0.01), "events must reconcile with the executions ledger"

    # re-running the same decision date (scheduler retry) is a no-op
    _run(mgr)
    assert len(db.executions.docs) == 1 and len(db.trades.docs) == 1
    assert _cash(db) == pytest.approx(start + 1250.0, abs=0.01)
    _assert_nav_cash_consistent(db)


def test_explicit_zero_cash_out_is_a_recorded_write_off_not_a_default():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100, price=100.0)
    _record(db, _cash_out(cps=0.0))
    start = _cash(db)
    _run(mgr)
    assert db.positions.find_one({"symbol": "AAA"}) is None
    assert _cash(db) == pytest.approx(start, abs=0.01)
    assert db.trades.find_one({"symbol": "AAA"})["net_pnl"] < 0


@pytest.mark.parametrize("breaker", [
    lambda d: d.pop("evidence"),                 # no evidence
    lambda d: d.pop("cash_per_share"),           # no explicit proceeds
])
def test_incomplete_event_is_not_applied_and_position_stays_flagged(breaker):
    mgr, db, _ = _make()
    _position(db, "AAA")
    doc = _cash_out()
    breaker(doc)
    _record(db, doc)
    start = _cash(db)
    res = _run(mgr)
    assert db.positions.find_one({"symbol": "AAA"}) is not None
    assert _cash(db) == pytest.approx(start, abs=0.01)
    assert res["missing_held_bars"] == ["AAA"], "an unapplied event must leave the missing-bar alarm on"


def test_future_dated_event_and_event_for_a_later_position_are_not_applied():
    mgr, db, _ = _make()
    _position(db, "AAA", entry_date="2026-03-02")        # opened ON the effective date: not affected
    _record(db, _cash_out(eff="2026-03-02"))
    start = _cash(db)
    assert _run(mgr)["missing_held_bars"] == ["AAA"]
    assert db.positions.find_one({"symbol": "AAA"}) is not None and _cash(db) == pytest.approx(start, abs=0.01)

    mgr2, db2, _ = _make()
    _position(db2, "AAA", entry_date="2026-01-02")
    _record(db2, _cash_out(eff="2026-03-10"))             # effective after the decision date
    _run(mgr2)
    assert db2.positions.find_one({"symbol": "AAA"}) is not None


def test_cash_out_crash_after_cash_is_resumed_exactly_once(monkeypatch):
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100)
    _record(db, _cash_out(cps=10.0))
    start = _cash(db)

    def boom(*a, **k):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(mgr, "_record_trade", boom)
    with pytest.raises(RuntimeError):
        _run(mgr)
    assert _cash(db) == pytest.approx(start + 1000.0, abs=0.01)       # cash was credited before the crash
    assert db.positions.find_one({"symbol": "AAA"}) is not None       # position still there: resumable

    mgr2, _, _ = _make(db)
    _run(mgr2)
    _run(mgr2)
    assert _cash(db) == pytest.approx(start + 1000.0, abs=0.01), "cash must be credited exactly once"
    assert len(db.trades.docs) == 1 and len(db.executions.docs) == 1
    assert db.positions.find_one({"symbol": "AAA"}) is None
    _assert_nav_cash_consistent(db)


def test_event_only_touches_accounts_holding_the_symbol_and_does_not_leak_between_accounts():
    mgr, db, _ = _make()
    _position(db, "CCC", shares=50)                                   # a different held name is untouched
    _record(db, _cash_out(symbol="AAA"))
    start = _cash(db)
    _run(mgr, {"CCC": _frame("2026-03-03")})
    assert db.positions.find_one({"symbol": "CCC"}) is not None and _cash(db) == pytest.approx(start, abs=0.01)


# ------------------------------------------------------------------ CONVERSION (merger)
def test_conversion_carries_cost_basis_entry_date_and_share_count():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100, price=100.0, entry_date="2026-01-02")
    _order(db, f"ORD_EXIT_{SID}_2026-03-02_AAA", "AAA", "SELL", "2026-03-02")
    _record(db, _conversion(ratio=1.87))
    start = _cash(db)

    res = _run(mgr, {"BBB": _frame("2026-03-03", price=53.5)})
    assert res["missing_held_bars"] == []
    assert db.positions.find_one({"symbol": "AAA"}) is None
    new = db.positions.find_one({"symbol": "BBB"})
    assert new["shares"] == 187 and new["entry_date"] == "2026-01-02"
    assert new["shares"] * new["entry_price"] == pytest.approx(100 * 100.0, rel=1e-6), "total cost basis must be preserved"
    assert new["entry_outlay"] == pytest.approx(100 * 100.0 + 20.0)
    assert "EVENT:AAA:CONVERSION:2026-03-02" in new["applied_actions"]
    assert _cash(db) == pytest.approx(start, abs=0.01) and not db.executions.docs
    assert db.orders.find_one({"order_id": f"ORD_EXIT_{SID}_2026-03-02_AAA"})["status"] == "CANCELLED"

    _run(mgr, {"BBB": _frame("2026-03-03", price=53.5)})      # retry: still exactly one converted position
    assert db.positions.find_one({"symbol": "BBB"})["shares"] == 187
    assert len([p for p in db.positions.docs if p["symbol"] in ("AAA", "BBB")]) == 1


def test_conversion_with_unresolved_fraction_is_blocked_until_the_policy_is_stated():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100)
    _record(db, _conversion(ratio=1.875, fraction=None))              # 187.5 new shares, no fraction policy
    start = _cash(db)
    res = _run(mgr, {"BBB": _frame("2026-03-03")})
    assert db.positions.find_one({"symbol": "AAA"}) is not None and db.positions.find_one({"symbol": "BBB"}) is None
    assert _cash(db) == pytest.approx(start, abs=0.01) and res["missing_held_bars"] == ["AAA"]


def test_conversion_pays_cash_in_lieu_of_fractions_exactly_once():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100)
    _record(db, _conversion(ratio=1.875, fraction=90.0))
    start = _cash(db)
    _run(mgr, {"BBB": _frame("2026-03-03")})
    _run(mgr, {"BBB": _frame("2026-03-03")})
    new_pos = db.positions.find_one({"symbol": "BBB"})
    assert new_pos["shares"] == 187
    # 100 shares outlay was 10020.0; 187/187.5 pro-rated outlay is 9993.28
    assert new_pos["entry_outlay"] == pytest.approx(10020.0 * (187 / 187.5), abs=0.01)
    assert _cash(db) == pytest.approx(start + 0.5 * 90.0, abs=0.01)
    assert len(db.executions.docs) == 1
    _assert_nav_cash_consistent(db)
    assert _cash(db) == pytest.approx(_ledger_cash(db), abs=0.01)


def test_conversion_refuses_to_merge_into_an_already_held_new_symbol():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100)
    _position(db, "BBB", shares=40)
    _record(db, _conversion(ratio=2.0))
    res = _run(mgr, {"BBB": _frame("2026-03-03")})
    assert db.positions.find_one({"symbol": "AAA"})["shares"] == 100
    assert db.positions.find_one({"symbol": "BBB"})["shares"] == 40
    assert res["missing_held_bars"] == ["AAA"]


def test_conversion_crash_after_cash_before_delete_is_resumed_without_double_credit(monkeypatch):
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100)
    _record(db, _conversion(ratio=1.875, fraction=90.0))
    start = _cash(db)

    def boom(*a, **k):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(mgr, "_cancel_pending_orders", boom)           # after cash + new position, before old delete
    with pytest.raises(RuntimeError):
        _run(mgr, {"BBB": _frame("2026-03-03")})
    assert db.positions.find_one({"symbol": "AAA"}) is not None and db.positions.find_one({"symbol": "BBB"}) is not None

    mgr2, _, _ = _make(db)
    _run(mgr2, {"BBB": _frame("2026-03-03")})
    assert db.positions.find_one({"symbol": "AAA"}) is None
    assert db.positions.find_one({"symbol": "BBB"})["shares"] == 187, "resume must not convert twice"
    assert _cash(db) == pytest.approx(start + 45.0, abs=0.01)
    assert len(db.executions.docs) == 1
    _assert_nav_cash_consistent(db)


# ------------------------------------------------------------------ missing data alone is still never an action
def test_missing_data_without_an_event_still_sells_and_writes_down_nothing():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100, price=100.0)
    start = _cash(db)
    res = _run(mgr)
    assert res["missing_held_bars"] == ["AAA"]
    assert db.positions.find_one({"symbol": "AAA"})["shares"] == 100
    assert _cash(db) == pytest.approx(start, abs=0.01) and not db.trades.docs and not db.executions.docs


# ------------------------------------------------------------------ an event must never liquidate a live, priced position
@pytest.mark.parametrize("doc", [_cash_out(eff="2026-03-02"), _conversion(eff="2026-03-02")])
def test_event_is_refused_when_the_symbol_still_trades_after_its_effective_date(doc):
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100, price=100.0)
    _record(db, doc)
    start = _cash(db)
    res = _run(mgr, {"AAA": _frame("2026-03-03"), "BBB": _frame("2026-03-03")})      # AAA has a 2026-03-03 bar
    assert db.positions.find_one({"symbol": "AAA"})["shares"] == 100
    assert db.positions.find_one({"symbol": "BBB"}) is None
    assert _cash(db) == pytest.approx(start, abs=0.01) and not db.trades.docs and not db.executions.docs
    assert res["missing_held_bars"] == []


def test_event_is_applied_when_the_symbol_stopped_trading_before_its_effective_date():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100)
    _record(db, _cash_out(cps=10.0, eff="2026-03-02"))
    start = _cash(db)
    _run(mgr, {"AAA": _frame("2026-02-27"), "ZZZ": _frame("2026-03-03")})            # last AAA bar is before the effective date
    assert db.positions.find_one({"symbol": "AAA"}) is None
    assert _cash(db) == pytest.approx(start + 1000.0, abs=0.01)


# ------------------------------------------------------------------ FRACTION_CASH (settles a pending fractional entitlement)
def _entitlement(db, sym="AAA", ex="2026-01-20", fraction=2 / 3, status="PENDING_CASH_SETTLEMENT"):
    db.get_collection("fractional_entitlements").insert_one({
        "entitlement_id": f"{SID}:paper:{sym}:{ex}:1.6667", "strategy_id": SID, "mode": "paper", "symbol": sym, "ex_date": ex,
        "ratio": 5 / 3, "shares_before": 37, "fraction_shares": fraction, "est_value_rs": 199.0, "status": status})


def _fraction_cash(cpf=334.86, eff="2026-03-02", ex="2026-01-20", sym="AAA"):
    return build_event(FRACTION_CASH, sym, eff, EVIDENCE, entitlement_ex_date=ex, cash_per_fraction_share=cpf)


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.pop("entitlement_ex_date"), "entitlement_ex_date"),
    (lambda d: d.update(entitlement_ex_date="2026-13-01"), "entitlement_ex_date"),
    (lambda d: d.update(entitlement_ex_date="2026-04-01"), "after the payment"),
    (lambda d: d.pop("cash_per_fraction_share"), "cash_per_fraction_share"),
    (lambda d: d.update(cash_per_fraction_share=-1.0), "cash_per_fraction_share"),
    (lambda d: d.update(cash_per_fraction_share=float("nan")), "cash_per_fraction_share"),
    (lambda d: d.update(evidence="n/a"), "evidence"),
])
def test_invalid_fraction_cash_documents_are_rejected(mutate, msg):
    doc = _fraction_cash()
    mutate(doc)
    with pytest.raises(ValueError, match=msg):
        validate_event(doc)


def test_fraction_cash_credits_the_ledger_entitlement_once_even_though_the_stock_is_no_longer_held():
    mgr, db, _ = _make()                                    # note: NO AAA position exists (sold long ago)
    _entitlement(db)
    _record(db, _fraction_cash(cpf=334.86))
    start = _cash(db)

    _run(mgr)
    expected = round(2 / 3 * 334.86, 2)
    assert _cash(db) == pytest.approx(start + expected, abs=0.01)
    ent = db.get_collection("fractional_entitlements").find_one({"symbol": "AAA"})
    assert ent["status"] == "SETTLED" and ent["settled_cash_rs"] == pytest.approx(expected, abs=0.01)
    assert ent["settled_by_event"] == f"AAA:FRACTION_CASH:2026-03-02"
    assert _cash(db) == pytest.approx(_ledger_cash(db), abs=0.01), "must reconcile with the executions ledger"
    _assert_nav_cash_consistent(db)

    _run(mgr)                                               # scheduler retry: no second credit
    assert len(db.executions.docs) == 1
    assert _cash(db) == pytest.approx(start + expected, abs=0.01)


def test_fraction_cash_resumes_after_a_crash_between_cash_credit_and_ledger_mark():
    mgr, db, _ = _make()
    _entitlement(db)
    _record(db, _fraction_cash(cpf=334.86))
    start = _cash(db)
    _run(mgr)
    db.get_collection("fractional_entitlements").update_one({"symbol": "AAA"}, {"$set": {"status": "PENDING_CASH_SETTLEMENT"}})  # "crash"
    _run(mgr)
    assert _cash(db) == pytest.approx(start + round(2 / 3 * 334.86, 2), abs=0.01), "cash must not be credited twice"
    assert len(db.executions.docs) == 1
    assert db.get_collection("fractional_entitlements").find_one({"symbol": "AAA"})["status"] == "SETTLED"


def test_fraction_cash_is_not_applied_before_its_payment_date_or_to_a_different_ex_date():
    mgr, db, _ = _make()
    _entitlement(db, ex="2026-01-20")
    _record(db, _fraction_cash(eff="2026-03-10"))                     # paid after the decision date 2026-03-03
    _record(db, _fraction_cash(eff="2026-03-02", ex="2026-02-01"))    # references an ex-date with no entitlement
    start = _cash(db)
    _run(mgr)
    assert _cash(db) == pytest.approx(start, abs=0.01)
    assert db.get_collection("fractional_entitlements").find_one({"symbol": "AAA"})["status"] == "PENDING_CASH_SETTLEMENT"
    assert not db.executions.docs


def test_explicit_zero_fraction_payout_settles_the_entitlement_without_cash():
    mgr, db, _ = _make()
    _entitlement(db)
    _record(db, _fraction_cash(cpf=0.0))
    start = _cash(db)
    _run(mgr)
    assert _cash(db) == pytest.approx(start, abs=0.01)
    ent = db.get_collection("fractional_entitlements").find_one({"symbol": "AAA"})
    assert ent["status"] == "SETTLED" and ent["settled_cash_rs"] == 0.0


def test_fraction_cash_never_touches_a_held_position_or_other_accounts_entitlements():
    mgr, db, _ = _make()
    _position(db, "AAA", shares=100, price=100.0)
    _entitlement(db)
    other = db.get_collection("fractional_entitlements")
    other.insert_one({"entitlement_id": "other_acct:paper:AAA:2026-01-20:1.6667", "strategy_id": "other_acct", "mode": "paper",
                      "symbol": "AAA", "ex_date": "2026-01-20", "fraction_shares": 0.5, "status": "PENDING_CASH_SETTLEMENT"})
    _record(db, _fraction_cash())
    _run(mgr)
    assert db.positions.find_one({"symbol": "AAA"})["shares"] == 100          # not converted/cashed out by the event
    assert other.find_one({"strategy_id": "other_acct"})["status"] == "PENDING_CASH_SETTLEMENT"


def test_recorder_preview_lists_every_accounts_pending_entitlement_and_changes_nothing(capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("record_corporate_event", Path(__file__).resolve().parent.parent / "scripts" / "record_corporate_event.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mgr, db, _ = _make()
    _entitlement(db)
    _entitlement(db, ex="2026-02-01")                                  # different ex-date: must not be listed
    doc = _fraction_cash(cpf=300.0)
    assert mod.preview_fraction(db, doc) == 1
    out = capsys.readouterr().out
    assert "fraction 0.6667 sh -> cash Rs 200.00" in out and "2026-02-01" not in out
    assert all(e["status"] == "PENDING_CASH_SETTLEMENT" for e in db.get_collection("fractional_entitlements").docs)
