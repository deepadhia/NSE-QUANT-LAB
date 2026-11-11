import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from nse_quant_lab.dashboard import build_snapshot
import run_dashboard
from test_paper import InMemoryCollection


class FakeDb:
    def __init__(self):
        self.strategies = InMemoryCollection()
        self.positions = InMemoryCollection()
        self.daily_nav = InMemoryCollection()


def _seed():
    db = FakeDb()
    db.strategies.docs += [
        {"strategy_id": "A", "mode": "paper", "name": "Strat A", "status": "ACTIVE", "initial_capital": 1_000_000.0, "cash": 400_000.0},
        {"strategy_id": "B", "mode": "paper", "name": "Strat B", "status": "ACTIVE", "initial_capital": 500_000.0, "cash": 500_000.0},
        {"strategy_id": "OLD", "mode": "paper", "name": "Old", "status": "ARCHIVED", "initial_capital": 100.0, "cash": 100.0},
        {"strategy_id": "R", "mode": "real", "name": "Real", "status": "ACTIVE", "initial_capital": 1.0, "cash": 1.0},
    ]
    db.positions.docs += [
        # stored market_value is deliberately stale: the page must re-derive shares * last_close
        {"strategy_id": "A", "mode": "paper", "symbol": "WIN", "shares": 100, "entry_price": 99.0, "entry_fill_price": 100.0,
         "entry_date": "2026-10-01", "entry_outlay": 10_010.0, "last_close": 120.0, "market_value": 1.0, "unrealized_pnl": 0.0},
        {"strategy_id": "A", "mode": "paper", "symbol": "LOSE", "shares": 50, "entry_price": 200.0,
         "entry_date": "2026-10-05", "last_close": 180.0},
        {"strategy_id": "A", "mode": "paper", "symbol": "NOPX", "shares": 10, "entry_price": 50.0, "entry_date": "2026-10-06"},
        {"strategy_id": "R", "mode": "real", "symbol": "ZZ", "shares": 1, "entry_price": 1.0, "last_close": 1.0},
    ]
    db.daily_nav.docs.append({"strategy_id": "A", "mode": "paper", "date": "2026-11-07", "nav": 1.0, "cash": 1.0})
    return db


def test_snapshot_values_are_rederived_and_hand_checkable():
    snap = build_snapshot(_seed(), "paper")
    a = next(s for s in snap["strategies"] if s["strategy_id"] == "A")
    win = next(p for p in a["positions"] if p["symbol"] == "WIN")
    lose = next(p for p in a["positions"] if p["symbol"] == "LOSE")

    assert win["market_value"] == 12_000.0                       # 100 * 120, not the stale stored 1.0
    assert win["entry_price"] == 100.0                           # entry_fill_price preferred over entry_price
    assert win["gain_pct"] == pytest.approx(20.0)
    assert win["pnl"] == pytest.approx(12_000.0 - 10_010.0)      # net of entry fees
    assert lose["entry_price"] == 200.0                          # falls back to entry_price
    assert lose["gain_pct"] == pytest.approx(-10.0)
    assert lose["pnl"] == pytest.approx(50 * 180.0 - 50 * 200.0)

    assert a["holdings_value"] == pytest.approx(12_000.0 + 9_000.0)
    assert a["total_value"] == pytest.approx(400_000.0 + 21_000.0)
    assert a["cash_pct"] == pytest.approx(400_000.0 / 421_000.0 * 100.0)
    assert a["return_pct"] == pytest.approx((421_000.0 / 1_000_000.0 - 1) * 100.0)
    assert a["last_nav_date"] == "2026-11-07"
    assert win["weight_pct"] == pytest.approx(12_000.0 / 421_000.0 * 100.0)
    assert [p["symbol"] for p in a["positions"]][0] == "WIN"     # best gain first, unpriced last
    assert a["positions"][-1]["symbol"] == "NOPX"


def test_missing_price_is_flagged_not_zeroed():
    a = next(s for s in build_snapshot(_seed(), "paper")["strategies"] if s["strategy_id"] == "A")
    nopx = next(p for p in a["positions"] if p["symbol"] == "NOPX")
    assert nopx["last_close"] is None and nopx["gain_pct"] is None and nopx["market_value"] is None
    assert a["unpriced_symbols"] == ["NOPX"]


def test_modes_are_isolated_and_archived_empty_hidden_and_totals_reconcile():
    paper = build_snapshot(_seed(), "paper")
    assert {s["strategy_id"] for s in paper["strategies"]} == {"A", "B"}   # no real-mode account, no empty archived one
    assert paper["totals"]["total_value"] == pytest.approx(421_000.0 + 500_000.0)
    assert paper["totals"]["cash"] == pytest.approx(900_000.0)
    assert paper["totals"]["open_positions"] == 3
    real = build_snapshot(_seed(), "real")
    assert [s["strategy_id"] for s in real["strategies"]] == ["R"]
    with pytest.raises(ValueError):
        build_snapshot(_seed(), "backtest")


def test_nan_and_garbage_never_become_numbers():
    db = FakeDb()
    db.strategies.docs.append({"strategy_id": "A", "mode": "paper", "status": "ACTIVE", "initial_capital": 100.0, "cash": float("nan")})
    db.positions.docs.append({"strategy_id": "A", "mode": "paper", "symbol": "X", "shares": 1, "entry_price": "abc", "last_close": float("nan")})
    snap = build_snapshot(db, "paper")
    json.dumps(snap, allow_nan=False)           # must be strictly serialisable
    assert snap["strategies"][0]["cash"] is None and snap["strategies"][0]["total_value"] is None


def test_auth_and_bind_rules():
    good = "Basic " + base64.b64encode(b"admin:pw").decode()
    bad = "Basic " + base64.b64encode(b"admin:nope").decode()
    assert run_dashboard.auth_ok(good, "admin", "pw")
    assert not run_dashboard.auth_ok(bad, "admin", "pw")
    assert not run_dashboard.auth_ok(None, "admin", "pw")
    assert not run_dashboard.auth_ok("Basic !!!notbase64", "admin", "pw")
    assert run_dashboard.auth_ok(None, "admin", "")          # open only when no password is configured
    assert run_dashboard.is_loopback("127.0.0.1") and run_dashboard.is_loopback("::1")
    assert not run_dashboard.is_loopback("0.0.0.0") and not run_dashboard.is_loopback("10.0.0.5")


def test_main_refuses_public_bind_without_password(monkeypatch):
    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert run_dashboard.main() == 2


@pytest.fixture()
def served():
    db = _seed()
    handler = run_dashboard.make_handler(lambda: db, "admin", "pw")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url, auth=True, method="GET"):
    req = urllib.request.Request(url, method=method)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:pw").decode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_end_to_end(served):
    assert _get(served + "/api/portfolio", auth=False)[0] == 401
    code, body = _get(served + "/api/portfolio?mode=paper")
    assert code == 200 and json.loads(body)["totals"]["open_positions"] == 3
    code, body = _get(served + "/")
    assert code == 200 and b"Portfolio tracker" in body
    assert _get(served + "/api/portfolio?mode=evil")[0] == 400
    assert _get(served + "/nope")[0] == 404
    assert _get(served + "/api/portfolio", method="POST")[0] == 405     # read-only


def test_db_failure_returns_503_not_a_guess():
    class Boom:
        @property
        def strategies(self):
            raise RuntimeError("mongo down")

    handler = run_dashboard.make_handler(lambda: Boom(), "admin", "")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, _ = _get(f"http://127.0.0.1:{srv.server_address[1]}/api/portfolio", auth=False)
        assert code == 503
    finally:
        srv.shutdown()
        srv.server_close()
