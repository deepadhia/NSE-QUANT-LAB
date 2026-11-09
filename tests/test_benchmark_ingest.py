"""Failure-mode tests for scripts/ingest_benchmarks.py (Upstox primary, Yahoo fallback, freshness + cross-check)."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ingest_benchmarks as ib  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
TRADING = {"2026-11-05", "2026-11-06", "2026-11-08", "2026-11-09", "2026-11-10"}   # 11-08 = Diwali Muhurat (Sunday)


def is_trading(d):
    return d.isoformat() in TRADING


def bar(d, o, h, l, c, v=0.0):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


class FakeCol:
    def __init__(self):
        self.docs = {}

    def create_index(self, *a, **k):
        pass

    def bulk_write(self, ops, ordered=False):
        for op in ops:
            f, u = op._filter, op._doc["$set"]
            self.docs[(f["symbol"], f["date"])] = dict(u)
        return type("R", (), {"upserted_count": len(ops), "modified_count": 0})()


YAHOO = {   # Yahoo has NO Muhurat 11-08 bar (observed live) and a same-day 11-09 bar
    "^NSEI": [bar("2026-11-05", 24378.05, 24579.9, 24296.3, 24536.6, 534600.0), bar("2026-11-06", 24538.2, 25030.35, 24388.05, 25030.35, 491000.0),
              bar("2026-11-09", 25076.4, 25084.05, 24838.75, 25044.65)],
    "^CRSLDX": [bar("2026-11-05", 23134.0, 23270.65, 23059.65, 23224.7, 1.0), bar("2026-11-06", 23162.0, 23460.65, 23004.35, 23456.95, 1.0),
                bar("2026-11-09", 23543.1, 23567.0, 23388.2, 23567.0)],
}
UPSTOX = {  # Upstox has the Muhurat bar but not yet the same-day 11-09 bar (observed live at 19:30 IST)
    "^NSEI": [bar("2026-11-05", 24378.05, 24579.9, 24296.3, 24536.6), bar("2026-11-06", 24538.2, 25030.35, 24388.05, 25030.35),
              bar("2026-11-08", 25070.45, 25131.85, 24985.3, 24986.4)],
    "^CRSLDX": [bar("2026-11-05", 23134.0, 23270.65, 23059.65, 23224.7), bar("2026-11-06", 23162.0, 23460.65, 23004.35, 23456.95),
                bar("2026-11-08", 23540.7, 23595.3, 23478.9, 23489.2)],
}
EVENING = datetime(2026, 11, 9, 17, 55, tzinfo=IST)


def run(yahoo=None, upstox=None, token="tok", now=EVENING, col=None, notify=None, **kw):
    col = FakeCol() if col is None else col
    yahoo = YAHOO if yahoo is None else yahoo
    upstox = UPSTOX if upstox is None else upstox
    ytk = lambda t, s, e=None: yahoo[t] if not isinstance(yahoo[t], Exception) else (_ for _ in ()).throw(yahoo[t])
    utk = lambda t, f, to, tok: upstox[t] if not isinstance(upstox[t], Exception) else (_ for _ in ()).throw(upstox[t])
    res = ib.ingest_benchmarks(col=col, yahoo_fetch=ytk, upstox_fetch=utk, upstox_token=token, now_ist=now,
                               trading_day_fn=is_trading, notify=notify, **kw)
    return res, col


def test_happy_path_keeps_muhurat_bar_and_marks_same_day_bar_provisional():
    res, col = run()
    assert res["ok"] and not res["problems"] and not res["degraded"]
    assert col.docs[("^NSEI", "2026-11-08")]["source"] == "upstox"            # Yahoo dropped it; Upstox keeps the real session
    assert col.docs[("^NSEI", "2026-11-09")]["source"] == "yahoo_provisional"  # same-day bar only Yahoo has
    assert col.docs[("^CRSLDX", "2026-11-06")]["source"] == "upstox"
    assert col.docs[("^CRSLDX", "2026-11-06")]["volume"] == 1.0                # Yahoo volume kept (Upstox reports 0 for indices)


def test_missing_todays_candle_fails_the_run_so_the_scheduler_retries():
    yahoo = {k: [b for b in v if b["date"] != "2026-11-09"] for k, v in YAHOO.items()}     # Yahoo lags: no 11-09 bar
    res, _ = run(yahoo=yahoo)
    assert not res["ok"]
    assert all("2026-11-09" in p and "missing" in p for p in res["problems"]) and len(res["problems"]) == 2


def test_upstox_outage_still_writes_yahoo_but_alerts_loudly_and_does_not_block():
    sent = []
    res, col = run(upstox={k: RuntimeError("boom") for k in UPSTOX}, notify=sent.append)
    assert res["ok"]                                                           # must not hold the 18:00 pipeline
    assert len(res["degraded"]) == 2 and "Upstox fetch failed" in res["degraded"][0]
    assert col.docs[("^NSEI", "2026-11-09")]["source"] == "yahoo"              # not 'provisional': Upstox could not confirm anything
    assert sent and "degraded" in sent[0]


def test_no_token_is_degraded_not_silent():
    res, col = run(token=None)
    assert res["ok"] and any("no Upstox token" in d for d in res["degraded"])
    assert all(d["source"] == "yahoo" for d in col.docs.values())


def test_empty_upstox_answer_is_treated_as_failure_not_as_no_data():
    res, _ = run(upstox={k: [] for k in UPSTOX})
    assert res["ok"] and any("no candles" in d for d in res["degraded"])


def test_close_disagreement_is_flagged_and_upstox_value_wins():
    up = {k: [dict(b) for b in v] for k, v in UPSTOX.items()}
    up["^CRSLDX"][1]["close"] = 23100.0                                        # 11-06: Yahoo says 23456.95
    sent = []
    res, col = run(upstox=up, notify=sent.append)
    assert any("SOURCE MISMATCH" in d and "2026-11-06" in d for d in res["degraded"])
    assert col.docs[("^CRSLDX", "2026-11-06")]["close"] == 23100.0
    assert sent


def test_yahoo_phantom_bar_inside_upstox_range_is_not_written():
    yahoo = {k: list(v) for k, v in YAHOO.items()}
    yahoo["^NSEI"].append(bar("2026-11-07", 25000.0, 25010.0, 24990.0, 25000.0))   # Saturday: Upstox range ends 11-08, no such session
    res, col = run(yahoo=yahoo)
    assert ("^NSEI", "2026-11-07") not in col.docs
    assert any("2026-11-07" in d and "not written" in d for d in res["degraded"])


def test_inconsistent_upstox_bar_is_rejected():
    up = {k: [dict(b) for b in v] for k, v in UPSTOX.items()}
    up["^NSEI"][2]["high"] = 24000.0                                           # high below close
    res, col = run(upstox=up)
    assert ("^NSEI", "2026-11-08") not in col.docs
    assert any("rejected" in d for d in res["degraded"])


def test_no_data_from_any_source_fails():
    res, col = run(yahoo={k: [] for k in YAHOO}, upstox={k: [] for k in UPSTOX})
    assert not res["ok"] and not col.docs
    assert all("no usable bars" in p for p in res["problems"])


def test_rerun_is_idempotent_and_next_day_replaces_provisional_with_upstox():
    res, col = run()
    snapshot = {k: {x: y for x, y in v.items() if x != "updated_at"} for k, v in col.docs.items()}
    run(col=col)
    assert {k: {x: y for x, y in v.items() if x != "updated_at"} for k, v in col.docs.items()} == snapshot
    up = {k: v + [bar("2026-11-09", 25076.4, 25084.05, 24838.75, 25044.65)] for k, v in UPSTOX.items()}
    up["^CRSLDX"][-1] = bar("2026-11-08", 23540.7, 23595.3, 23478.9, 23489.2)
    up["^CRSLDX"].append(bar("2026-11-09", 23543.1, 23567.0, 23388.2, 23567.0))
    run(upstox=up, col=col, now=datetime(2026, 11, 10, 9, 0, tzinfo=IST))
    assert col.docs[("^NSEI", "2026-11-09")]["source"] == "upstox"


def test_expected_session_handles_pre_close_runs_and_holidays():
    assert ib.expected_session(datetime(2026, 11, 9, 17, 55, tzinfo=IST), is_trading) == date(2026, 11, 9)
    assert ib.expected_session(datetime(2026, 11, 9, 9, 0, tzinfo=IST), is_trading) == date(2026, 11, 8)   # before final: previous session (Muhurat)
    assert ib.expected_session(datetime(2026, 11, 7, 18, 0, tzinfo=IST), is_trading) == date(2026, 11, 6)   # Saturday
    with pytest.raises(RuntimeError):
        ib.expected_session(EVENING, lambda d: False)


def test_unusable_holiday_calendar_fails_loud_instead_of_guessing():
    def broken(_):
        raise ValueError("calendar down")
    col = FakeCol()
    res = ib.ingest_benchmarks(col=col, yahoo_fetch=lambda t, s, e=None: YAHOO[t], upstox_fetch=lambda t, f, to, tok: UPSTOX[t],
                               upstox_token="tok", now_ist=EVENING, trading_day_fn=broken)
    assert not res["ok"] and any("latest NSE session" in p for p in res["problems"])


def test_dry_run_writes_nothing_and_sends_nothing():
    sent = []
    res, col = run(upstox={k: RuntimeError("x") for k in UPSTOX}, notify=sent.append, dry_run=True)
    assert not col.docs and not sent and res["ok"]
