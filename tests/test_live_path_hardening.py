"""
tests/test_live_path_hardening.py -- failure-mode tests for the 2026-11-07 live-path review (AUDIT doc rows S3-S8).

Each test encodes a way the forward paper path could silently trade on stale/partial data or lose a stop:
  S3 stale / holey Mongo data passing the coverage gate, swallowed Mongo errors defaulting everything to series EQ
  S4 a failed benchmark load being cached for the life of the process (regime stuck "bear")
  S5 a previous-session broker quote pricing today's fill
  S6 a held name falling below the ingestion liquidity tier and so never receiving bars (stop never evaluated)
  S7 audit of a past date reporting TODAY's positions
"""
import os
import sys
from pathlib import Path

import pandas as pd
import pymongo
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nse_quant_lab import upstox_feed as uf  # noqa: E402
from nse_quant_lab.config import PaperTradingConfig  # noqa: E402
from nse_quant_lab.data_loader import load_pit_universe  # noqa: E402
from nse_quant_lab.holidays import is_trading_day  # noqa: E402
from nse_quant_lab.paper.manager import PaperTradingManager  # noqa: E402
from nse_quant_lab.registry import get_strategy  # noqa: E402
from tests.test_corporate_actions import MockDatabase  # noqa: E402


# ---------------------------------------------------------------- S3: Mongo fallback loader
class _Col:
    def __init__(self, data=None, error=None):
        self.data, self.error = data or [], error

    def find(self, *a, **k):
        if self.error:
            raise self.error
        return iter(self.data)


def _install_mongo(monkeypatch, hist, full=None, symbols=None):
    orig_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: False if "clean_panel.parquet" in str(p) else orig_exists(p))

    class _Admin:
        def command(self, name):
            return {"ok": 1}

    class _Client:
        def __init__(self, *a, **k):
            self.admin = _Admin()

        def __getitem__(self, name):
            return {"upstox_daily_ohlcv": hist, "upstox_fulltier_daily": full or _Col(), "symbols": symbols or _Col()}

    monkeypatch.setattr(pymongo, "MongoClient", _Client)


def _docs(start, end, skip=()):
    out = []
    for d in pd.bdate_range(start, end):
        ds = d.strftime("%Y-%m-%d")
        if ds in skip:
            continue
        for sym in ("SYM1", "SYM2", "SYM3"):
            out.append({"symbol": sym, "date": ds, "open": 200.0, "high": 205.0, "low": 195.0, "close": 200.0, "volume": 1_000_000})
    return out


def _load():
    return load_pit_universe(end_date="2026-10-30", min_adt_ever=1.0e6, min_rows=50, include_live=True, live_coverage_gate=0.90)


def test_complete_recent_data_still_loads(monkeypatch):
    _install_mongo(monkeypatch, _Col(_docs("2026-07-01", "2026-10-30")))
    assert set(_load()) == {"SYM1", "SYM2", "SYM3"}


def test_data_more_than_14_days_stale_is_refused(monkeypatch):
    """Before the fix a last bar older than 14 days skipped the gate (the stale last day had 100% coverage by construction)."""
    _install_mongo(monkeypatch, _Col(_docs("2026-06-01", "2026-09-30")))
    with pytest.raises(ValueError, match="live data incomplete"):
        _load()


def test_missing_interior_session_is_refused(monkeypatch):
    hole = "2026-10-22"
    assert is_trading_day(pd.Timestamp(hole).date()), "precondition: the removed day must be a trading day in the calendar"
    _install_mongo(monkeypatch, _Col(_docs("2026-07-01", "2026-10-30", skip=(hole,))))
    with pytest.raises(ValueError, match=hole):
        _load()


def test_failed_history_query_raises_instead_of_building_a_partial_universe(monkeypatch):
    _install_mongo(monkeypatch, _Col(error=RuntimeError("boom")), full=_Col(_docs("2026-10-01", "2026-10-30")))
    with pytest.raises(ValueError, match="history query"):
        _load()


def test_failed_symbols_lookup_raises_instead_of_defaulting_every_name_to_eq(monkeypatch):
    _install_mongo(monkeypatch, _Col(_docs("2026-07-01", "2026-10-30")), symbols=_Col(error=RuntimeError("no role")))
    with pytest.raises(ValueError, match="symbols lookup"):
        _load()


# ---------------------------------------------------------------- S4: regime benchmark failure must not be cached
@pytest.mark.parametrize("sid", ["trend_momentum_pit", "alpha_semivol_pit", "minervini_trend_template"])
def test_benchmark_load_failure_is_not_cached(monkeypatch, sid):
    attempts = []

    class _Boom:
        def __init__(self, *a, **k):
            attempts.append(1)
            raise RuntimeError("mongo down")

    monkeypatch.setattr(pymongo, "MongoClient", _Boom)
    strat = get_strategy(sid, PaperTradingConfig(strategy_id=sid))
    assert strat._bench().empty
    n1 = len(attempts)
    assert strat._benchmark is None                       # NOT cached as an empty Series
    assert strat._bench().empty and len(attempts) > n1    # the next call retries


# ---------------------------------------------------------------- S5: quote freshness
def _mgr_and_universe():
    db = MockDatabase()
    cfg = PaperTradingConfig(strategy_id="trend_momentum_pit", initial_capital=100000.0)
    mgr = PaperTradingManager(strategy=get_strategy("trend_momentum_pit", cfg), config=cfg, db=db)
    dates = pd.bdate_range("2026-08-01", "2026-11-05")
    df = pd.DataFrame({"date": dates, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6,
                       "turnover": 1e8, "series": "EQ"})
    return mgr, {"AAA": df}


def _quote(ts):
    q = {"open": 101.0, "high": 102.0, "low": 100.5, "ltp": 101.5, "bid": 101.4, "ask": 101.6}
    if ts is not None:
        q["ts"] = ts
    return q


EXEC = pd.Timestamp("2026-11-06")


def test_quote_session_date_parsing():
    f = PaperTradingManager._quote_session_date
    assert str(f("2026-11-06T09:15:03+05:30")) == "2026-11-06"
    assert str(f("2026-11-05T09:15:03+05:30")) == "2026-11-05"
    assert str(f("2026-11-06T09:15:03")) == "2026-11-06"                      # naive -> IST
    assert str(f(int(pd.Timestamp("2026-11-06 03:50:00", tz="UTC").timestamp() * 1000))) == "2026-11-06"   # epoch ms
    assert f(None) is None and f("") is None and f("not a time") is None


def test_previous_session_quote_is_not_used_for_todays_fill():
    mgr, uni = _mgr_and_universe()
    mgr.quote_provider = lambda s: _quote("2026-11-05T15:29:59+05:30")
    mgr_bar = mgr._get_execution_bar("AAA", EXEC, uni)
    assert mgr_bar is None                                                      # no fill, no yfinance fallback: order retained as NO_QUOTE


def test_same_session_quote_is_used():
    mgr, uni = _mgr_and_universe()
    mgr.quote_provider = lambda s: _quote("2026-11-06T09:15:10+05:30")
    bar = mgr._get_execution_bar("AAA", EXEC, uni)
    assert bar is not None and bar["is_live_quote"] and bar["open"] == 101.0


def test_quote_without_timestamp_is_accepted_but_logged(caplog):
    mgr, uni = _mgr_and_universe()
    mgr.quote_provider = lambda s: _quote(None)
    with caplog.at_level("WARNING"):
        bar = mgr._get_execution_bar("AAA", EXEC, uni)
    assert bar is not None and bar["is_live_quote"]
    assert any("QUOTE_FRESHNESS_UNVERIFIED" in r.message for r in caplog.records)


# ---------------------------------------------------------------- S6: held names stay in the ingestion tier
def test_held_symbol_below_the_tier_still_gets_bars_but_unheld_does_not():
    stats = pd.DataFrame({"symbol": ["LIQ", "HELD", "OTHER"], "adt": [5e8, 1e6, 1e6], "hist": [300, 100, 300], "series": ["EQ", "BE", "EQ"]})
    isins = {"LIQ": "INE001", "HELD": "INE002", "OTHER": "INE003"}
    assert set(uf.select_targets(stats, isins)["symbol"]) == {"LIQ"}
    assert set(uf.select_targets(stats, isins, include_symbols=["HELD"])["symbol"]) == {"LIQ", "HELD"}


def test_held_symbol_without_isin_is_reported_loudly(caplog):
    stats = pd.DataFrame({"symbol": ["LIQ", "HELD"], "adt": [5e8, 1e6], "hist": [300, 300], "series": ["EQ", "EQ"]})
    with caplog.at_level("ERROR"):
        t = uf.select_targets(stats, {"LIQ": "INE001"}, include_symbols=["HELD"])
    assert set(t["symbol"]) == {"LIQ"}
    assert any("HELD_NO_ISIN" in r.message for r in caplog.records)


# ---------------------------------------------------------------- S7: audit of a past date
class _Coll:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, q=None, *a, **k):
        q = q or {}
        out = [d for d in self.docs if all((d.get(k2) == v) for k2, v in q.items() if not isinstance(v, dict))]
        return iter(out)

    def find_one(self, q=None, sort=None, *a, **k):
        rows = list(self.find(q))
        if sort:
            rows.sort(key=lambda d: d[sort[0][0]], reverse=sort[0][1] < 0)
        return rows[0] if rows else None

    def replace_one(self, q, doc, upsert=False):
        self.docs = [d for d in self.docs if not all(d.get(k) == v for k, v in q.items())] + [doc]


class _AuditDB:
    def __init__(self):
        self.daily_nav = _Coll([
            {"strategy_id": "s", "mode": "paper", "date": "2026-11-05", "nav": 100000.0, "cash": 50000.0, "holdings_val": 50000.0, "open_positions": 3},
            {"strategy_id": "s", "mode": "paper", "date": "2026-11-06", "nav": 101000.0, "cash": 1000.0, "holdings_val": 100000.0, "open_positions": 1}])
        self.positions = _Coll([{"strategy_id": "s", "mode": "paper", "symbol": "NOW", "shares": 10, "entry_price": 100.0, "last_close": 110.0}])
        self.fill_audit = _Coll()
        self.daily_portfolio_audit = _Coll()


def test_audit_of_a_past_date_does_not_report_todays_positions():
    from scripts.audit_daily_portfolio import audit_strategy_for_date
    db = _AuditDB()
    past = audit_strategy_for_date(db, "s", "2026-11-05")
    assert past["positions"] == [] and past["positions_snapshot"] == "UNAVAILABLE_HISTORICAL" and past["open_positions_count"] == 3
    latest = audit_strategy_for_date(db, "s", "2026-11-06")
    assert latest["positions_snapshot"] == "CURRENT" and [p["symbol"] for p in latest["positions"]] == ["NOW"]


def test_backfill_does_not_overwrite_an_existing_audit_with_a_degraded_one():
    from scripts.audit_daily_portfolio import audit_strategy_for_date
    db = _AuditDB()
    db.daily_portfolio_audit.docs.append({"strategy_id": "s", "mode": "paper", "date": "2026-11-05", "positions": [{"symbol": "GOOD"}]})
    assert audit_strategy_for_date(db, "s", "2026-11-05") is None
    assert db.daily_portfolio_audit.docs[0]["positions"] == [{"symbol": "GOOD"}]


# ---------------------------------------------------------------- S8: yfinance is not a fill source unless explicitly enabled
def _fake_yfinance(monkeypatch, open_value):
    class _Info:
        open = open_value
        day_high = open_value
        day_low = open_value
        last_price = open_value

    class _Ticker:
        def __init__(self, sym):
            self.fast_info = _Info()

    class _Mod:
        Ticker = _Ticker

    monkeypatch.setitem(sys.modules, "yfinance", _Mod)


def test_yfinance_quote_is_not_used_by_default(monkeypatch):
    _fake_yfinance(monkeypatch, 101.0)                       # a perfectly plausible number we cannot prove is today's
    mgr, uni = _mgr_and_universe()
    assert getattr(mgr.config, "allow_yfinance_fallback") is False
    assert mgr._get_execution_bar("AAA", EXEC, uni) is None


def test_yfinance_quote_can_be_enabled_deliberately(monkeypatch, caplog):
    _fake_yfinance(monkeypatch, 101.0)
    mgr, uni = _mgr_and_universe()
    mgr.config.allow_yfinance_fallback = True
    with caplog.at_level("WARNING"):
        bar = mgr._get_execution_bar("AAA", EXEC, uni)
    assert bar is not None and bar["open"] == 101.0
    assert any("YFINANCE_FALLBACK_ENABLED" in r.message for r in caplog.records)


def test_allow_yfinance_fallback_env_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_YFINANCE_FALLBACK", "true")
    assert PaperTradingConfig(strategy_id="x").allow_yfinance_fallback is True
    monkeypatch.delenv("ALLOW_YFINANCE_FALLBACK")
    assert PaperTradingConfig(strategy_id="x").allow_yfinance_fallback is False


# ---------------------------------------------------------------- S9: an outage-expired BUY flags the catch-up scan instead of leaving slots empty
def test_stale_expired_buy_flags_catchup_for_the_next_post_market_scan():
    from tests.test_paper_failure_modes import SID, _frame, _make, _order
    mgr, db, _ = _make()
    oid = f"ORD_BUY_{SID}_2026-03-02_BBB"
    _order(db, oid, "BBB", "BUY", "2026-03-02", reason="RANK_1", shares=0)
    mgr.execute_morning_fills({"BBB": _frame("2026-03-04")}, pd.Timestamp("2026-03-05"))     # daemon was down through 03-03/03-04
    assert db.orders.find_one({"order_id": oid})["status"] == "SKIPPED_EXPIRED"
    assert db.strategies.find_one({"strategy_id": SID, "mode": "paper"})["catchup_eligible"] is True


def test_no_catchup_flag_when_nothing_expired_or_skipped():
    from tests.test_paper_failure_modes import SID, _frame, _make
    mgr, db, _ = _make()
    mgr.execute_morning_fills({"BBB": _frame("2026-03-04")}, pd.Timestamp("2026-03-05"))
    assert not db.strategies.find_one({"strategy_id": SID, "mode": "paper"}).get("catchup_eligible", False)


# ---------------------------------------------------------------- probe script summary (pure logic; the script itself is run by the user at 09:16)
def test_probe_verdicts():
    import datetime as dt
    from scripts.probe_upstox_quotes import summarize
    today = dt.date(2026, 11, 9)
    ok = [{"symbol": "A", "ts": "2026-11-09T09:15:02+05:30", "bid": 99.9, "ask": 100.1}, {"symbol": "B", "ts": "2026-11-09T09:15:30+05:30"}]
    assert summarize(ok, today)["verdict"] == "PARSER OK"
    assert summarize(ok, today)["rows"][0]["spread_bps"] == pytest.approx(20.0, abs=0.1)
    assert summarize([{"symbol": "A", "ts": "2026-11-06T15:29:59+05:30"}], today)["verdict"] == "STALE QUOTES"
    assert summarize([{"symbol": "A", "ts": None}, {"symbol": "B", "ts": "garbage"}], today)["verdict"] == "TS UNPARSEABLE"
    assert summarize(ok + [{"symbol": "C", "ts": None}], today)["verdict"] == "TS PARTLY UNPARSEABLE"
    assert summarize([], today)["verdict"] == "NO QUOTES RETURNED"
