"""
price_validation.py -- Fail-Loud OHLCV Data Validation & Cross-Source Reconciliation
====================================================================================
Every check reports a severity:
- ERROR: data is unusable as-is (impossible prices, duplicates, NaNs). Ingestion must stop.
- WARN:  data is usable but suspicious (missing sessions, stale runs, large gaps that may be
         unadjusted corporate actions). Logged and kept in the report, never silently dropped.
"""

from __future__ import annotations
from typing import Any, Dict, Iterable, List, Optional, Tuple
import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class DataValidationError(ValueError):
    """Raised when a price series fails one or more ERROR-severity checks."""


def _issue(check: str, severity: str, count: int, dates: Iterable[Any] = (), detail: str = "") -> Dict[str, Any]:
    return {
        "check": check,
        "severity": severity,
        "count": int(count),
        "examples": [str(pd.Timestamp(d).date()) for d in list(dates)[:5]],
        "detail": detail,
    }


def validate_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    calendar: Optional[Iterable[pd.Timestamp]] = None,
    max_overnight_gap: float = 0.35,
    stale_run_length: int = 5,
) -> Dict[str, Any]:
    """
    Validates one symbol's daily OHLCV series and returns a report:
    {"symbol", "rows", "first_date", "last_date", "issues": [...], "has_errors": bool}.
    calendar: exchange session dates; missing sessions are checked only within the
    symbol's own first..last date range (suspensions show up here as WARN).
    """
    issues: List[Dict[str, Any]] = []
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        issues.append(_issue("required_columns", "ERROR", len(missing_cols), detail=f"missing {missing_cols}"))
        return {"symbol": symbol, "rows": len(df), "first_date": None, "last_date": None,
                "issues": issues, "has_errors": True}
    if df.empty:
        issues.append(_issue("non_empty", "ERROR", 0, detail="no rows"))
        return {"symbol": symbol, "rows": 0, "first_date": None, "last_date": None,
                "issues": issues, "has_errors": True}

    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    px = ["open", "high", "low", "close"]

    intraday = d["date"] != d["date"].dt.normalize()
    if intraday.any():
        issues.append(_issue("non_midnight_timestamp", "ERROR", intraday.sum(), d.loc[intraday, "date"],
                             detail="daily bars must be session dates; use normalize_session_dates first"))
    if d["date"].dt.normalize().duplicated(keep=False).any() and not d["date"].duplicated(keep=False).any():
        same_day = d["date"].dt.normalize().duplicated(keep=False)
        issues.append(_issue("duplicate_session_day", "ERROR", d.loc[same_day, "date"].dt.normalize().nunique(),
                             d.loc[same_day, "date"]))

    nan_mask = d[px + ["volume"]].isna().any(axis=1)
    if nan_mask.any():
        issues.append(_issue("nan_values", "ERROR", nan_mask.sum(), d.loc[nan_mask, "date"]))

    nonpos = (d[px] <= 0).any(axis=1)
    if nonpos.any():
        issues.append(_issue("non_positive_price", "ERROR", nonpos.sum(), d.loc[nonpos, "date"]))

    neg_vol = d["volume"] < 0
    if neg_vol.any():
        issues.append(_issue("negative_volume", "ERROR", neg_vol.sum(), d.loc[neg_vol, "date"]))

    bad_env = (
        (d["high"] < d["low"]) | (d["high"] < d["open"]) | (d["high"] < d["close"])
        | (d["low"] > d["open"]) | (d["low"] > d["close"])
    )
    if bad_env.any():
        issues.append(_issue("ohlc_envelope", "ERROR", bad_env.sum(), d.loc[bad_env, "date"]))

    dup = d["date"].duplicated(keep=False)
    if dup.any():
        issues.append(_issue("duplicate_dates", "ERROR", d.loc[dup, "date"].nunique(), d.loc[dup, "date"]))

    if not d["date"].is_monotonic_increasing:
        issues.append(_issue("unsorted_dates", "ERROR", 1, detail="dates not in ascending order"))

    d = d.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    first, last = d["date"].iloc[0], d["date"].iloc[-1]

    if calendar is not None:
        cal = pd.DatetimeIndex(pd.to_datetime(list(calendar)))
        in_range = cal[(cal >= first) & (cal <= last)]
        missing = in_range.difference(pd.DatetimeIndex(d["date"]))
        if len(missing):
            issues.append(_issue("missing_sessions", "WARN", len(missing), missing,
                                 detail=f"{len(missing)}/{len(in_range)} sessions absent"))
        extra = pd.DatetimeIndex(d["date"]).difference(cal)
        if len(extra):
            issues.append(_issue("non_session_dates", "WARN", len(extra), extra,
                                 detail="rows on dates that are not exchange sessions"))

    zero_vol = d["volume"] == 0
    if zero_vol.any():
        issues.append(_issue("zero_volume", "WARN", zero_vol.sum(), d.loc[zero_vol, "date"]))

    # Stale runs: identical close with zero volume for N+ consecutive rows (carried-forward prices)
    stale = (d["close"].diff() == 0) & zero_vol
    run_id = (~stale).cumsum()
    run_len = stale.groupby(run_id).transform("sum")
    stale_rows = stale & (run_len >= stale_run_length - 1)
    if stale_rows.any():
        issues.append(_issue("stale_price_run", "WARN", stale_rows.sum(), d.loc[stale_rows, "date"],
                             detail=f">= {stale_run_length} consecutive unchanged zero-volume closes"))

    carried = carried_forward_mask(d)
    if carried.any():
        issues.append(_issue("carried_forward_row", "WARN", carried.sum(), d.loc[carried, "date"],
                             detail="zero-volume row repeating the previous close (placeholder, not a trade)"))

    gap = (d["open"] / d["close"].shift(1) - 1.0).abs()
    big = gap > max_overnight_gap
    if big.any():
        issues.append(_issue("large_overnight_gap", "WARN", big.sum(), d.loc[big, "date"],
                             detail=f"|open/prev_close - 1| > {max_overnight_gap:.0%}; possible unadjusted corporate action"))

    return {
        "symbol": symbol,
        "rows": int(len(d)),
        "first_date": str(first.date()),
        "last_date": str(last.date()),
        "issues": issues,
        "has_errors": any(i["severity"] == "ERROR" for i in issues),
    }


def normalize_session_dates(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    """
    Truncates timestamps to session dates (some feeds stamp daily bars 09:15) and drops exact
    duplicate bars for the same day. Raises DataValidationError if two different bars share a day.
    Returns (frame, exact_duplicates_dropped).
    """
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    cols = ["date", "open", "high", "low", "close", "volume"]
    exact = d.duplicated(subset=cols)
    d = d.loc[~exact]
    clash = d["date"].duplicated(keep=False)
    if clash.any():
        days = sorted({str(x.date()) for x in d.loc[clash, "date"]})
        raise DataValidationError(f"conflicting bars on the same session day: {days[:5]} ({len(days)} days)")
    return d.sort_values("date").reset_index(drop=True), int(exact.sum())


def carried_forward_mask(df: pd.DataFrame) -> pd.Series:
    """Rows with zero volume whose open/high/low/close all equal the previous row's close."""
    prev = df["close"].shift(1)
    flat = (df["open"] == prev) & (df["high"] == prev) & (df["low"] == prev) & (df["close"] == prev)
    return (df["volume"] == 0) & flat


def drop_carried_forward_rows(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """
    Removes placeholder rows (see carried_forward_mask) so they read as missing sessions,
    not as fake 0% days. Returns (clean_frame, dropped_dates) so callers can log every drop.
    """
    d = df.sort_values("date").reset_index(drop=True)
    mask = carried_forward_mask(d)
    dropped = [str(pd.Timestamp(x).date()) for x in d.loc[mask, "date"]]
    return d.loc[~mask].reset_index(drop=True), dropped


def raise_on_errors(report: Dict[str, Any]) -> None:
    """Raises DataValidationError listing every ERROR-severity check in the report."""
    errors = [i for i in report["issues"] if i["severity"] == "ERROR"]
    if errors:
        summary = "; ".join(f"{e['check']} x{e['count']} {e['examples']}" for e in errors)
        raise DataValidationError(f"{report['symbol']}: {summary}")


def compare_sources(
    a: pd.DataFrame,
    b: pd.DataFrame,
    tolerance: float = 0.005,
    min_overlap: int = 20,
) -> Dict[str, Any]:
    """
    Reconciles two daily series for the same instrument on overlapping dates.
    Compares close-to-close returns (insensitive to a constant adjustment basis) and the
    close-price ratio (detects a different split/bonus basis or a different instrument).
    """
    x = a[["date", "close", "volume"]].copy()
    y = b[["date", "close", "volume"]].copy()
    x["date"], y["date"] = pd.to_datetime(x["date"]), pd.to_datetime(y["date"])
    m = x.merge(y, on="date", suffixes=("_a", "_b")).sort_values("date").reset_index(drop=True)
    out: Dict[str, Any] = {"overlap_days": int(len(m))}
    if len(m) < min_overlap:
        out["status"] = "INSUFFICIENT_OVERLAP"
        return out

    ra, rb = m["close_a"].pct_change(), m["close_b"].pct_change()
    diff = (ra - rb).abs().dropna()
    ratio = m["close_a"] / m["close_b"]
    bad = diff > tolerance
    worst = diff.sort_values(ascending=False).head(5)
    out.update({
        "return_mismatch_days": int(bad.sum()),
        "return_mismatch_pct": round(float(bad.mean() * 100), 3),
        "max_return_diff": round(float(diff.max()), 6),
        "close_ratio_median": round(float(ratio.median()), 6),
        "close_ratio_min": round(float(ratio.min()), 6),
        "close_ratio_max": round(float(ratio.max()), 6),
        "worst_dates": [str(m.loc[i, "date"].date()) for i in worst.index],
        "status": "OK" if bad.mean() <= 0.01 and np.isclose(ratio.median(), 1.0, atol=0.01) else "MISMATCH",
    })
    return out
