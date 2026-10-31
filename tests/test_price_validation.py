import pytest
import pandas as pd

from nse_quant_lab.data_loader import make_synthetic_stock
from nse_quant_lab.price_validation import (
    validate_ohlcv, raise_on_errors, compare_sources, DataValidationError, drop_carried_forward_rows,
    normalize_session_dates,
)


def _checks(report, severity=None):
    return {i["check"] for i in report["issues"] if severity is None or i["severity"] == severity}


def _clean(n=30):
    return make_synthetic_stock("TEST.NS", n_days=n, start_date="2024-01-01")


def test_clean_series_has_no_issues():
    df = _clean()
    rep = validate_ohlcv(df, "TEST.NS", calendar=df["date"])
    assert rep["issues"] == [] and not rep["has_errors"]
    raise_on_errors(rep)  # must not raise


@pytest.mark.parametrize("mutate,check", [
    (lambda d: d.assign(close=d["close"].where(d.index != 5, 0.0)), "non_positive_price"),
    (lambda d: d.assign(high=d["high"].where(d.index != 5, d["low"] * 0.5)), "ohlc_envelope"),
    (lambda d: d.assign(close=d["close"].where(d.index != 5, float("nan"))), "nan_values"),
    (lambda d: pd.concat([d, d.iloc[[3]]]).sort_values("date"), "duplicate_dates"),
    (lambda d: d.iloc[::-1], "unsorted_dates"),
    (lambda d: d.assign(volume=d["volume"].where(d.index != 5, -1.0)), "negative_volume"),
])
def test_impossible_data_is_an_error_and_raises(mutate, check):
    rep = validate_ohlcv(mutate(_clean()), "TEST.NS")
    assert check in _checks(rep, "ERROR")
    with pytest.raises(DataValidationError, match=check):
        raise_on_errors(rep)


def test_missing_columns_and_empty_frame_are_errors():
    assert "required_columns" in _checks(validate_ohlcv(_clean().drop(columns=["volume"]), "X"), "ERROR")
    assert "non_empty" in _checks(validate_ohlcv(_clean().iloc[0:0], "X"), "ERROR")


def test_missing_session_inside_range_is_warned_not_dropped():
    df = _clean()
    cal = df["date"].tolist()
    rep = validate_ohlcv(df.drop(index=[10, 11]), "TEST.NS", calendar=cal)
    miss = [i for i in rep["issues"] if i["check"] == "missing_sessions"][0]
    assert miss["severity"] == "WARN" and miss["count"] == 2
    assert miss["examples"] == [str(cal[10].date()), str(cal[11].date())]
    assert not rep["has_errors"]


def test_sessions_outside_listing_range_are_not_counted_missing():
    df = _clean()
    long_cal = pd.date_range("2023-06-01", periods=200, freq="B")
    rep = validate_ohlcv(df, "TEST.NS", calendar=long_cal.union(pd.DatetimeIndex(df["date"])))
    assert "missing_sessions" not in _checks(rep)


def test_stale_zero_volume_run_and_large_gap_warned():
    df = _clean().reset_index(drop=True)
    df.loc[10:15, ["open", "high", "low", "close"]] = df.loc[10, "close"]
    df.loc[10:15, "volume"] = 0.0
    df.loc[20:, ["open", "high", "low", "close"]] = df.loc[20:, ["open", "high", "low", "close"]] * 0.5
    rep = validate_ohlcv(df, "TEST.NS")
    warns = _checks(rep, "WARN")
    assert {"stale_price_run", "zero_volume", "large_overnight_gap"} <= warns
    gap = [i for i in rep["issues"] if i["check"] == "large_overnight_gap"][0]
    assert gap["examples"] == [str(df.loc[20, "date"].date())]


def test_single_carried_forward_placeholder_row_is_flagged_and_droppable():
    # Mirrors yfinance HEROMOTOCO 2025-03-18: previous close repeated with zero volume.
    df = _clean().reset_index(drop=True)
    prev_close = df.loc[9, "close"]
    df.loc[10, ["open", "high", "low", "close", "volume"]] = [prev_close, prev_close, prev_close, prev_close, 0.0]
    rep = validate_ohlcv(df, "TEST.NS")
    cf = [i for i in rep["issues"] if i["check"] == "carried_forward_row"][0]
    assert cf["severity"] == "WARN" and cf["count"] == 1
    assert "stale_price_run" not in _checks(rep)  # a single row is not a 5-row run

    clean, dropped = drop_carried_forward_rows(df)
    assert dropped == [str(df.loc[10, "date"].date())]
    assert len(clean) == len(df) - 1
    assert "carried_forward_row" not in _checks(validate_ohlcv(clean, "TEST.NS"))


def test_genuine_zero_volume_day_with_new_price_is_not_dropped():
    df = _clean().reset_index(drop=True)
    df.loc[10, "volume"] = 0.0  # price still moved, so it is not a placeholder
    _, dropped = drop_carried_forward_rows(df)
    assert dropped == []


def test_0915_stamped_bars_and_same_day_duplicates_are_errors_until_normalized():
    # Mirrors Upstox INE069I01010: 2015-12-31 00:00 and 2015-12-31 09:15 identical, later bars 09:15.
    df = _clean(10).reset_index(drop=True)
    dup = df.iloc[[4]].copy()
    dup["date"] = dup["date"] + pd.Timedelta(hours=9, minutes=15)
    later = df.iloc[5:].copy()
    later["date"] = later["date"] + pd.Timedelta(hours=9, minutes=15)
    raw = pd.concat([df.iloc[:5], dup, later]).reset_index(drop=True)
    rep = validate_ohlcv(raw, "TEST.NS")
    assert {"non_midnight_timestamp", "duplicate_session_day"} <= _checks(rep, "ERROR")

    fixed, dropped = normalize_session_dates(raw)
    assert dropped == 1 and len(fixed) == 10
    assert not validate_ohlcv(fixed, "TEST.NS")["has_errors"]


def test_normalize_refuses_conflicting_bars_on_same_day():
    df = _clean(5).reset_index(drop=True)
    other = df.iloc[[2]].copy()
    other["date"] = other["date"] + pd.Timedelta(hours=9, minutes=15)
    other["close"] = other["close"] * 1.01
    other["high"] = other["high"] * 1.02
    with pytest.raises(DataValidationError, match="conflicting bars"):
        normalize_session_dates(pd.concat([df, other]))


def test_compare_sources_identical_ok_and_detects_basis_and_bad_days():
    a = _clean(60)
    assert compare_sources(a, a.copy())["status"] == "OK"

    halved = a.assign(close=a["close"] * 0.5)  # different split basis: returns agree, levels don't
    rep = compare_sources(a, halved)
    assert rep["return_mismatch_days"] == 0 and rep["close_ratio_median"] == pytest.approx(2.0)
    assert rep["status"] == "MISMATCH"

    bad = a.copy()
    bad.loc[30, "close"] *= 1.10  # one bad print -> two mismatching returns
    rep = compare_sources(a, bad)
    assert rep["return_mismatch_days"] == 2 and rep["status"] == "MISMATCH"


def test_compare_sources_requires_overlap():
    a = _clean(30)
    b = make_synthetic_stock("TEST.NS", n_days=30, start_date="2025-01-01")
    assert compare_sources(a, b)["status"] == "INSUFFICIENT_OVERLAP"
