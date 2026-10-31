import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ingest_upstox_fulltier as ing  # noqa: E402
from nse_quant_lab import upstox_feed as uf  # noqa: E402


class FakeCol:
    def __init__(self):
        self.docs = {}

    def find_one(self, q, sort=None):
        rows = [d for d in self.docs.values() if d["symbol"] == q["symbol"]]
        return max(rows, key=lambda d: d["date"]) if rows else None

    def update_one(self, q, upd, upsert=False):
        self.docs[(q["symbol"], q["date"])] = {**self.docs.get((q["symbol"], q["date"]), {}), **upd["$set"]}

    def insert_one(self, doc):
        self.docs[("flag", len(self.docs))] = doc


def bar(date, o, c, v=1000.0):
    return {"date": date, "open": o, "high": max(o, c), "low": min(o, c), "close": c, "volume": v}


TARGETS = pd.DataFrame({"symbol": ["AAA", "BBB", "CCC"], "key": ["K1", "K2", "K3"], "last_close": [100.0, 200.0, 50.0]})
PANEL_LAST, RUN = dt.date(2026, 10, 30), dt.date(2026, 11, 2)


def test_stores_new_bars_and_the_anchor_bar_idempotently():
    data = {"K1": [bar("2026-10-30", 99, 100), bar("2026-11-02", 101, 103)], "K2": [bar("2026-11-02", 201, 205)], "K3": [bar("2026-11-02", 50, 51)]}
    col, flags = FakeCol(), FakeCol()
    s = ing.run_ingestion(TARGETS, RUN, PANEL_LAST, col, flags, lambda k, f, t: data[k], now=lambda: "t")
    assert s["failed"] == [] and s["flagged"] == []
    assert ("AAA", "2026-10-30") in col.docs and ("AAA", "2026-11-02") in col.docs   # overlap anchor + new bar
    n = len(col.docs)
    ing.run_ingestion(TARGETS, RUN, PANEL_LAST, col, flags, lambda k, f, t: data[k], now=lambda: "t")
    assert len(col.docs) == n                                                        # re-run changes nothing


def test_gap_is_flagged_and_adjusted_history_refreshed():
    calls = []

    def fetch(k, f, t):
        calls.append((k, f))
        if k == "K2":
            return [bar("2026-11-02", 100, 101)] if f >= "2026-10-30" else [bar("2026-01-02", 100, 100), bar("2026-11-02", 100, 101)]
        return [bar("2026-11-02", 100 if k == "K1" else 50, 101 if k == "K1" else 51)]

    col, flags = FakeCol(), FakeCol()
    s = ing.run_ingestion(TARGETS, RUN, PANEL_LAST, col, flags, fetch, now=lambda: "t")   # BBB prev close 200 -> open 100 = 2:1 split
    assert s["flagged"] == ["BBB"]
    assert any(d.get("source") == "adjusted_refresh" for d in col.docs.values())
    assert any(c[0] == "K2" and c[1] < "2026-10-01" for c in calls)                       # a ~400-day refresh was requested


def test_failures_are_reported_and_auth_error_aborts():
    def fetch(k, f, t):
        if k == "K1":
            return []
        if k == "K2":
            raise RuntimeError("boom")
        return [bar("2026-11-02", 50, 51)]

    col, flags = FakeCol(), FakeCol()
    s = ing.run_ingestion(TARGETS, RUN, PANEL_LAST, col, flags, fetch, now=lambda: "t")
    assert s["failed"] == ["AAA", "BBB"] and ("CCC", "2026-11-02") in col.docs

    def auth(k, f, t):
        raise uf.UpstoxAuthError("expired")

    with pytest.raises(uf.UpstoxAuthError):
        ing.run_ingestion(TARGETS, RUN, PANEL_LAST, FakeCol(), FakeCol(), auth, now=lambda: "t")


class FakeMany:
    def __init__(self, docs):
        self.docs = docs

    def find(self, q):
        out = []
        for d in self.docs:
            ok = all((d.get(k) in v["$in"]) if isinstance(v, dict) else d.get(k) == v for k, v in q.items())
            if ok:
                out.append(d)
        return out

    def update_one(self, q, upd):
        for d in self.docs:
            if d["_id"] == q["_id"]:
                d.update(upd["$set"])


def test_split_rescales_open_position_and_pending_sell_but_not_other_symbols():
    pos = FakeMany([{"_id": 1, "symbol": "BBB", "mode": "paper", "shares": 10, "entry_price": 200.0, "entry_fill_price": 202.0, "last_close": 200.0},
                    {"_id": 2, "symbol": "AAA", "mode": "paper", "shares": 5, "entry_price": 100.0, "entry_fill_price": 100.5, "last_close": 100.0}])
    orders = FakeMany([{"_id": 9, "symbol": "BBB", "mode": "paper", "action": "SELL", "shares": 10, "price": 200.0, "status": "PENDING_OPEN"},
                       {"_id": 8, "symbol": "BBB", "mode": "paper", "action": "BUY", "shares": 0, "price": 200.0, "status": "FILLED"}])

    def fetch(k, f, t):   # BBB: 2:1 split. Adjusted history shows the previous close (2026-10-30) at 100; the new bar opens at 100
        if k == "K2":
            return [bar("2026-10-30", 100, 100), bar("2026-11-02", 100, 101)] if f <= "2026-10-30" else [bar("2026-11-02", 100, 101)]
        return [bar("2026-11-02", 100 if k == "K1" else 50, 101 if k == "K1" else 51)]

    ing.run_ingestion(TARGETS, RUN, PANEL_LAST, FakeCol(), FakeCol(), fetch, now=lambda: "t", positions_col=pos, orders_col=orders)
    b, a = pos.docs[0], pos.docs[1]
    assert b["shares"] == 20 and b["entry_price"] == pytest.approx(100.0) and b["split_adjusted_factor"] == pytest.approx(2.0)
    assert a["shares"] == 5 and "split_adjusted_factor" not in a
    assert orders.docs[0]["shares"] == 20 and orders.docs[0]["price"] == pytest.approx(100.0)   # pending sell follows the split
    assert orders.docs[1]["shares"] == 0 and orders.docs[1]["price"] == 200.0                  # filled order untouched
