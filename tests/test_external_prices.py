import pandas as pd

from nse_quant_lab.external_prices import fetch_yfinance, normalize_yfinance, upsert_prices


def _yf_frame(dates, tz="Asia/Kolkata"):
    idx = pd.DatetimeIndex(pd.to_datetime(dates)).tz_localize(tz)
    n = len(idx)
    return pd.DataFrame({
        "Open": [100.0 + i for i in range(n)], "High": [102.0 + i for i in range(n)],
        "Low": [99.0 + i for i in range(n)], "Close": [101.0 + i for i in range(n)],
        "Adj Close": [100.5 + i for i in range(n)], "Volume": [1000 * (i + 1) for i in range(n)],
        "Dividends": 0.0, "Stock Splits": 0.0,
    }, index=idx)


def test_normalize_uses_raw_close_and_tz_naive_ist_session_dates():
    df = normalize_yfinance(_yf_frame(["2019-04-16", "2019-04-15"]), "JETAIRWAYS.NS")
    assert list(df.columns) == ["symbol", "date", "open", "high", "low", "close", "volume", "source"]
    assert df["date"].dt.tz is None
    assert [str(d.date()) for d in df["date"]] == ["2019-04-15", "2019-04-16"]  # sorted
    assert df["close"].tolist() == [102.0, 101.0]  # 'Close', not 'Adj Close'
    assert (df["source"] == "yfinance").all()


def test_utc_midnight_index_maps_to_correct_ist_date():
    # 2019-04-15 18:30 UTC == 2019-04-16 00:00 IST
    idx = pd.DatetimeIndex(["2019-04-15 18:30"]).tz_localize("UTC")
    hist = _yf_frame(["2019-04-16"]).set_axis(idx)
    assert str(normalize_yfinance(hist, "X.NS")["date"].iloc[0].date()) == "2019-04-16"


def test_fetch_reports_ok_empty_and_error_status_and_inclusive_end():
    seen = {}

    def ok(sym, start, end_excl):
        seen["end"] = end_excl
        return _yf_frame(["2020-01-01"])

    df, status = fetch_yfinance("A.NS", "2020-01-01", "2020-01-31", history_fn=ok)
    assert status == "OK" and len(df) == 1 and seen["end"] == "2020-02-01"

    df, status = fetch_yfinance("A.NS", "2020-01-01", "2020-01-31", history_fn=lambda *a: pd.DataFrame())
    assert status == "EMPTY" and df.empty

    def boom(*a):
        raise ConnectionError("network down")

    df, status = fetch_yfinance("A.NS", "2020-01-01", "2020-01-31", history_fn=boom)
    assert status.startswith("ERROR: ConnectionError") and df.empty


class _FakeCollection:
    def __init__(self):
        self.rows, self.indexes = {}, []

    def create_index(self, keys, unique=False):
        self.indexes.append((tuple(keys), unique))

    def bulk_write(self, ops, ordered=True):
        upserted = matched = 0
        for op in ops:
            key = tuple(sorted(op._filter.items()))
            if key in self.rows:
                matched += 1
            else:
                upserted += 1
            self.rows[key] = op._doc["$set"]

        class R:
            pass
        r = R()
        r.upserted_count, r.modified_count, r.matched_count = upserted, 0, matched
        return r


def test_upsert_is_idempotent_and_indexed():
    col = _FakeCollection()
    df = normalize_yfinance(_yf_frame(["2020-01-01", "2020-01-02"]), "A.NS")
    assert upsert_prices(col, df)["upserted"] == 2
    second = upsert_prices(col, df)
    assert second["upserted"] == 0 and second["matched"] == 2
    assert len(col.rows) == 2
    assert col.indexes[0][1] is True
    assert {r["date"] for r in col.rows.values()} == {"2020-01-01", "2020-01-02"}
