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
