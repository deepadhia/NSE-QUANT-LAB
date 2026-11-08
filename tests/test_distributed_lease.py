"""Cross-host portfolio lease: exercised against a REAL MongoDB (temporary database, dropped afterwards) because the arbitration relies on
server-side upsert/_id semantics that a hand-written fake would only assume. Skipped when no MongoDB is reachable on localhost.
"""
import threading
import uuid
from datetime import timedelta

import pandas as pd
import pymongo
import pytest

from nse_quant_lab.paper.lease import LeaseBusy, LeaseLost, PortfolioLease, supports_leases, COLLECTION
from test_paper_failure_modes import _make, _order, _frame


@pytest.fixture(scope="module")
def coll():
    client = pymongo.MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=1500)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip("no local MongoDB available for lease tests")
    name = f"nse_lab_lease_test_{uuid.uuid4().hex[:8]}"
    yield client[name][COLLECTION]
    client.drop_database(name)


@pytest.fixture(autouse=True)
def _clean(coll):
    coll.delete_many({})       # leases from one test (the stub strategy id is shared) must not leak into the next
    yield


def _key():
    return f"strat_{uuid.uuid4().hex[:8]}|paper"


def test_real_pymongo_collection_can_arbitrate_and_bare_doubles_cannot(coll):
    assert supports_leases(coll)
    class Bare:  # the shape of the in-memory test doubles
        def find_one(self, *a): ...
    assert not supports_leases(Bare()) and not supports_leases(None)


def test_second_host_is_refused_while_the_first_holds_the_lease(coll):
    key = _key()
    a, b = PortfolioLease(coll, key, owner="hostA"), PortfolioLease(coll, key, owner="hostB")
    a.acquire()
    with pytest.raises(LeaseBusy, match="ACCOUNT_BUSY"):
        b.acquire(timeout=0.4, poll=0.1)
    a.assert_held()


def test_release_hands_over_immediately_and_the_fencing_token_only_increases(coll):
    key = _key()
    a, b = PortfolioLease(coll, key, owner="hostA"), PortfolioLease(coll, key, owner="hostB")
    a.acquire(); t1 = a.token
    a.release()
    b.acquire(); t2 = b.token
    assert t2 > t1
    with pytest.raises(LeaseLost):
        a.assert_held()                      # a released and never re-acquired


def test_expired_lease_is_taken_over_and_the_old_owner_is_fenced_out(coll):
    key = _key()
    a, b = PortfolioLease(coll, key, owner="hostA"), PortfolioLease(coll, key, owner="hostB")
    a.acquire()
    coll.update_one({"_id": key}, {"$set": {"expires_at": a._now() - timedelta(seconds=1)}})   # hostA crashed / was paused past its ttl
    b.acquire()
    assert b.token > a.token
    with pytest.raises(LeaseLost, match="LEASE_LOST"):
        a.assert_held()                      # the paused worker wakes up: it must not write
    b.assert_held()


def test_exactly_one_of_many_racing_hosts_wins(coll):
    key = _key()
    leases = [PortfolioLease(coll, key, owner=f"host{i}") for i in range(10)]
    wins, barrier = [], threading.Barrier(len(leases))

    def go(lease):
        barrier.wait()
        try:
            lease.acquire(timeout=0.0)
            wins.append(lease.owner)
        except LeaseBusy:
            pass

    threads = [threading.Thread(target=go, args=(l,)) for l in leases]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(wins) == 1, f"expected a single owner, got {wins}"


def test_cash_and_execution_writes_are_fenced_when_the_lease_is_lost(coll):
    mgr, db, _ = _make()
    key = f"{mgr.config.strategy_id}|paper"
    mine = PortfolioLease(coll, key, owner="hostA"); mine.acquire()
    mgr._lease = mine
    coll.update_one({"_id": key}, {"$set": {"owner": "hostB", "token": mine.token + 1}})       # another host took over
    db.executions.insert_one({'strategy_id': mgr.config.strategy_id, 'mode': 'paper', 'exec_id': 'EXEC_F', 'net_cash': -1000})
    before = db.strategies.find_one({'strategy_id': mgr.config.strategy_id, 'mode': 'paper'})['cash']
    with pytest.raises(LeaseLost):
        mgr._persist_cash(before - 1000, '2026-03-03', 'EXEC_F')
    assert db.strategies.find_one({'strategy_id': mgr.config.strategy_id, 'mode': 'paper'})['cash'] == before


def test_morning_execution_on_a_second_host_is_refused_not_double_run(coll, monkeypatch):
    mgr, db, _ = _make()
    key = f"{mgr.config.strategy_id}|paper"
    other = PortfolioLease(coll, key, owner="other-host"); other.acquire()
    real_get = db.get_collection
    monkeypatch.setattr(db, "get_collection", lambda name: coll if name == COLLECTION else real_get(name), raising=False)
    _order(db, f"ORD_EXIT_{mgr.config.strategy_id}_2026-03-02_AAA", "AAA", "SELL", "2026-03-02")
    with pytest.raises(LeaseBusy):
        mgr.execute_morning_fills({"AAA": _frame("2026-03-03")}, pd.Timestamp("2026-03-03"))
    assert db.executions.docs == []          # nothing was filled by the refused host


def test_decorator_acquires_and_releases_the_lease_around_an_operation(coll, monkeypatch):
    mgr, db, _ = _make()
    key = f"{mgr.config.strategy_id}|paper"
    real_get = db.get_collection
    monkeypatch.setattr(db, "get_collection", lambda name: coll if name == COLLECTION else real_get(name), raising=False)
    mgr.execute_morning_fills({}, pd.Timestamp("2026-03-03"))
    doc = coll.find_one({"_id": key})
    assert doc is not None and doc["token"] >= 1 and doc["expires_at"] <= PortfolioLease(coll, key)._now()   # released = expired now
    assert mgr._lease is None and mgr._lease_depth == 0
