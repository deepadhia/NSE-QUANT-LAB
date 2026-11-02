import logging
import pytest
import pandas as pd
import pymongo

from nse_quant_lab.data_loader import load_clean_universe, make_synthetic_stock


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, *args, **kwargs):
        return iter(self._docs)


class _FakeAdmin:
    def __init__(self, ping_error):
        self._ping_error = ping_error

    def command(self, name):
        if self._ping_error is not None:
            raise self._ping_error
        return {"ok": 1}


def _fake_client_factory(docs=None, ping_error=None):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.admin = _FakeAdmin(ping_error)

        def __getitem__(self, db_name):
            return {"upstox_daily_ohlcv": _FakeCollection(docs or []), "symbols": _FakeCollection([])}

    return _FakeClient


@pytest.fixture
def sample_parquet(tmp_path):
    df = pd.concat([
        make_synthetic_stock("SAMPLEA.NS", n_days=20, start_date="2024-01-01"),
        make_synthetic_stock("SAMPLEB.NS", n_days=20, start_date="2024-01-01"),
    ])
    path = tmp_path / "sample.parquet"
    df.to_parquet(path)
    return str(path)


def test_mongo_unreachable_raises_instead_of_loading_sample(monkeypatch, sample_parquet):
    """Regression: an unreachable DB used to silently return the 48-symbol sample universe."""
    monkeypatch.setattr(pymongo, "MongoClient", _fake_client_factory(ping_error=pymongo.errors.ServerSelectionTimeoutError("down")))
    with pytest.raises(ConnectionError, match="ServerSelectionTimeoutError"):
        load_clean_universe("2024-01-01", "2024-02-01", mongo_uri="mongodb://user:secret@db:27017/", parquet_path=sample_parquet)


def test_error_message_does_not_leak_mongo_uri_credentials(monkeypatch, sample_parquet):
    monkeypatch.setattr(pymongo, "MongoClient", _fake_client_factory(ping_error=RuntimeError("boom")))
    with pytest.raises(ConnectionError) as exc_info:
        load_clean_universe("2024-01-01", "2024-02-01", mongo_uri="mongodb://user:secret@db:27017/", parquet_path=sample_parquet)
    assert "secret" not in str(exc_info.value)


def test_empty_query_result_raises_instead_of_loading_sample(monkeypatch, sample_parquet):
    """Missing data must not be treated as 'use the sample instead'."""
    monkeypatch.setattr(pymongo, "MongoClient", _fake_client_factory(docs=[]))
    with pytest.raises(ConnectionError, match="no candles returned"):
        load_clean_universe("2024-01-01", "2024-02-01", parquet_path=sample_parquet)


def test_sample_fallback_only_with_explicit_opt_in_and_logged(monkeypatch, sample_parquet, caplog):
    monkeypatch.setattr(pymongo, "MongoClient", _fake_client_factory(ping_error=RuntimeError("down")))
    with caplog.at_level(logging.WARNING, logger="data_loader"):
        universe = load_clean_universe("2024-01-01", "2024-02-01", parquet_path=sample_parquet, allow_sample_fallback=True)
    assert set(universe) == {"SAMPLEA.NS", "SAMPLEB.NS"}
    assert "SAMPLE DATA MODE" in caplog.text


def test_mongo_success_returns_db_universe(monkeypatch, sample_parquet):
    docs = [
        {"symbol": "REAL.NS", "date": "2024-01-02", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000.0},
        {"symbol": "REAL.NS", "date": "2024-01-01", "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1000.0},
    ]
    monkeypatch.setattr(pymongo, "MongoClient", _fake_client_factory(docs=docs))
    universe = load_clean_universe("2024-01-01", "2024-02-01", parquet_path=sample_parquet)
    assert list(universe) == ["REAL.NS"]
    assert universe["REAL.NS"]["date"].is_monotonic_increasing

def test_load_pit_universe_explicit_panel_path_missing_raises_file_not_found():
    from nse_quant_lab.data_loader import load_pit_universe
    with pytest.raises(FileNotFoundError, match="clean panel not found"):
        load_pit_universe(panel_path="nonexistent_panel_file.parquet")


def test_load_pit_universe_falls_back_to_mongo_when_parquet_absent(monkeypatch):
    import os
    from nse_quant_lab.data_loader import load_pit_universe

    # Ensure parquet is reported as missing
    orig_exists = os.path.exists
    def fake_exists(path):
        if "clean_panel.parquet" in str(path):
            return False
        return orig_exists(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)

    # Generate 60 daily bars for 2 stocks
    dates = pd.date_range("2026-01-01", periods=60, freq="B").strftime("%Y-%m-%d").tolist()
    docs = []
    for d in dates:
        docs.append({"symbol": "SBIN.NS", "date": d, "open": 500.0, "high": 510.0, "low": 495.0, "close": 505.0, "volume": 100_000.0})
        docs.append({"symbol": "INFY.NS", "date": d, "open": 1500.0, "high": 1520.0, "low": 1490.0, "close": 1510.0, "volume": 50_000.0})

    class _FakeCollectionPIT:
        def __init__(self, data):
            self.data = data
        def find(self, *args, **kwargs):
            return iter(self.data)

    class _FakeClientPIT:
        def __init__(self, *args, **kwargs):
            self.admin = _FakeAdmin(None)
        def __getitem__(self, name):
            return {
                "upstox_daily_ohlcv": _FakeCollectionPIT(docs),
                "upstox_fulltier_daily": _FakeCollectionPIT([]),
                "symbols": _FakeCollectionPIT([]),
            }

    monkeypatch.setattr(pymongo, "MongoClient", _FakeClientPIT)

    universe = load_pit_universe(end_date="2026-10-30", min_adt_ever=1.0e6, min_rows=50)
    assert "SBIN" in universe
    assert "INFY" in universe
    assert len(universe["SBIN"]) == 60
    assert universe["SBIN"]["series"].iloc[0] == "EQ"
    assert "turnover" in universe["SBIN"].columns
    assert universe["SBIN"]["turnover"].iloc[0] == pytest.approx(505.0 * 100_000.0)


def test_load_pit_universe_empty_mongo_raises_value_error(monkeypatch):
    import os
    from nse_quant_lab.data_loader import load_pit_universe

    orig_exists = os.path.exists
    def fake_exists(path):
        if "clean_panel.parquet" in str(path):
            return False
        return orig_exists(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)

    class _FakeCollectionEmpty:
        def find(self, *args, **kwargs):
            return iter([])

    class _FakeClientEmpty:
        def __init__(self, *args, **kwargs):
            pass
        def __getitem__(self, name):
            return {
                "upstox_daily_ohlcv": _FakeCollectionEmpty(),
                "upstox_fulltier_daily": _FakeCollectionEmpty(),
            }

    monkeypatch.setattr(pymongo, "MongoClient", _FakeClientEmpty)

    with pytest.raises(ValueError, match="returned no valid market data"):
        load_pit_universe(end_date="2026-10-30")
