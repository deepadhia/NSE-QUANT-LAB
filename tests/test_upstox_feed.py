"""Upstox feed: all tests use mocked HTTP responses (the live API was NOT called)."""
import io
import json
import urllib.error

import pandas as pd
import pytest

from nse_quant_lab import upstox_feed as uf


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(payload):
    return lambda req, timeout=0: FakeResp(payload)


CANDLES = {"status": "success", "data": {"candles": [
    ["2026-11-02T00:00:00+05:30", 100.0, 105.0, 99.0, 104.0, 1000, 0],
    ["2026-10-30T00:00:00+05:30", 98.0, 101.0, 97.0, 100.0, 900, 0],
]}}


def test_parse_candles_sorts_and_types():
    rows = uf.parse_candles(CANDLES)
    assert [r["date"] for r in rows] == ["2026-10-30", "2026-11-02"]
    assert rows[1] == {"date": "2026-11-02", "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 1000.0}


def test_fetch_daily_candles_builds_v3_url_and_returns_rows():
    seen = {}

    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return FakeResp(CANDLES)

    rows = uf.fetch_daily_candles("NSE_EQ|INE002A01018", "2026-10-30", "2026-11-02", "TOK", opener, sleep=lambda s: None)
    assert seen["url"].endswith("/v3/historical-candle/NSE_EQ%7CINE002A01018/days/1/2026-11-02/2026-10-30")
    assert seen["auth"] == "Bearer TOK" and len(rows) == 2


def http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))


def test_429_backs_off_then_succeeds():
    calls = {"n": 0}

    def opener(req, timeout=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise http_error(429)
        return FakeResp(CANDLES)

    sleeps = []
    rows = uf.fetch_daily_candles("K", "a", "b", "T", opener, sleep=sleeps.append)
    assert len(rows) == 2 and calls["n"] == 3 and max(sleeps) >= 2.0


def test_auth_error_is_raised_not_swallowed():
    def opener(req, timeout=0):
        raise http_error(401)

    with pytest.raises(uf.UpstoxAuthError):
        uf.fetch_daily_candles("K", "a", "b", "T", opener, sleep=lambda s: None)


def test_404_returns_empty_and_persistent_failure_raises():
    assert uf.fetch_daily_candles("K", "a", "b", "T", lambda r, timeout=0: (_ for _ in ()).throw(http_error(404)), sleep=lambda s: None) == []

    def boom(req, timeout=0):
        raise http_error(500)

    with pytest.raises(RuntimeError):
        uf.fetch_daily_candles("K", "a", "b", "T", boom, sleep=lambda s: None, max_retries=2)


QUOTE = {"status": "success", "data": {"NSE_EQ:AAA": {
    "ohlc": {"open": 50.0, "high": 52.0, "low": 49.5, "close": 51.0}, "last_price": 50.4, "volume": 12345,
    "instrument_token": "NSE_EQ|INE000A01011", "upper_circuit_limit": 60.0, "lower_circuit_limit": 40.0,
    "timestamp": "2026-11-02T09:16:01+05:30",
    "depth": {"buy": [{"price": 50.35, "quantity": 10, "orders": 1}, {"price": 0, "quantity": 0, "orders": 0}],
              "sell": [{"price": 50.45, "quantity": 7, "orders": 1}]}}}}


def test_fetch_quotes_normalises_and_batches_500():
    q = uf.fetch_quotes(["NSE_EQ|INE000A01011"], "T", opener_returning(QUOTE))
    got = q["NSE_EQ|INE000A01011"]
    assert got["open"] == 50.0 and got["bid"] == 50.35 and got["ask"] == 50.45 and got["upper_circuit"] == 60.0
    urls = []

    def counting(req, timeout=0):
        urls.append(req.full_url)
        return FakeResp({"data": {}})

    uf.fetch_quotes([f"NSE_EQ|K{i}" for i in range(1201)], "T", counting)
    assert len(urls) == 3  # 500 + 500 + 201


def test_quote_provider_maps_symbol_to_isin_and_caches():
    calls = {"n": 0}

    def opener(req, timeout=0):
        calls["n"] += 1
        return FakeResp(QUOTE)

    p = uf.UpstoxQuoteProvider("T", {"AAA": "INE000A01011"}, opener)
    p.prefetch(["AAA"])
    assert p("AAA")["ask"] == 50.45 and p("AAA")["open"] == 50.0
    assert calls["n"] == 1 and p("UNKNOWN") is None


def test_select_targets_uses_buffer_history_series_and_isin(tmp_path):
    stats = pd.DataFrame({"symbol": ["A", "B", "C", "D", "E"], "adt": [5e8, 4e7, 1e7, 5e8, 5e8], "hist": [500, 300, 500, 100, 500],
                          "series": ["EQ", "EQ", "EQ", "EQ", "BE"]})
    t = uf.select_targets(stats, {"A": "INE001", "B": "INE002", "C": "INE003", "D": "INE004"})
    assert list(t["symbol"]) == ["A", "B"]          # C below buffer, D short history, E not EQ
    assert t.loc[0, "key"] == "NSE_EQ|INE001"


def test_symbol_isin_map_takes_latest_valid_isin(tmp_path):
    df = pd.DataFrame({"symbol": ["X", "X", "Y"], "isin": ["INE111", "INE222", "INF999"], "date": pd.to_datetime(["2025-01-01", "2025-06-01", "2025-06-01"])})
    df.to_parquet(tmp_path / "bhavcopy_2025.parquet")
    assert uf.symbol_isin_map(tmp_path, [2025, 2026]) == {"X": "INE222"}  # ETF-style INF ISIN ignored


def test_gap_flag():
    assert uf.gap_flag(100.0, 70.0) and uf.gap_flag(100.0, 140.0)
    assert not uf.gap_flag(100.0, 95.0) and not uf.gap_flag(None, 50.0) and not uf.gap_flag(100.0, 0.0)
