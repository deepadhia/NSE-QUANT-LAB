"""
Tests for scripts/ingest_nse_bhavcopy.py
"""

import datetime as dt
import io
import os
import tempfile
import zipfile
import pytest

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.ingest_nse_bhavcopy import get_bhav_url, parse_bhavcopy_zip, UDIFF_START_DATE


def test_get_bhav_url_legacy():
    # Before 2024-07-08
    d = dt.date(2016, 1, 4)
    url = get_bhav_url(d)
    assert "https://nsearchives.nseindia.com/content/historical/EQUITIES/2016/JAN/cm04JAN2016bhav.csv.zip" == url


def test_get_bhav_url_udiff():
    # On or after 2024-07-08
    d = dt.date(2024, 7, 8)
    url = get_bhav_url(d)
    assert "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip" == url


def test_parse_legacy_bhavcopy_zip():
    # Create in-memory zip with legacy structure
    csv_content = (
        "SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE, TOTTRDQTY, TOTTRDVAL, TIMESTAMP, TOTALTRADES, ISIN\n"
        "RELIANCE, EQ, 2500.0, 2550.0, 2490.0, 2540.0, 2539.0, 2505.0, 1000000, 2540000000, 04-JAN-2024, 50000, INE002A01018\n"
        "JUNKDEBT, GS, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 50, 5000, 04-JAN-2024, 1, IN0000000000\n"
    )
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = tmp.name
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("cm04JAN2024bhav.csv", csv_content)

    try:
        records = parse_bhavcopy_zip(zip_path)
        assert len(records) == 1  # GS series filtered out
        r = records[0]
        assert r["symbol"] == "RELIANCE"
        assert r["series"] == "EQ"
        assert r["open"] == 2500.0
        assert r["close"] == 2540.0
        assert r["volume"] == 1000000
        assert r["turnover"] == 2540000000.0
        assert r["isin"] == "INE002A01018"
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)


def test_parse_udiff_bhavcopy_zip():
    # Create in-memory zip with UDiFF structure
    csv_content = (
        "TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN, TckrSymb, SctySrs, OpnPric, HghPric, LwPric, ClsPric, LastPric, PrvsClsgPric, TtlTradgVol, TtlTrdVal, TtlNbOfTxsExctd\n"
        "2024-07-08, 2024-07-08, CM, NSE, STK, 12345, INE002A01018, RELIANCE, EQ, 3000.0, 3050.0, 2990.0, 3040.0, 3039.0, 3005.0, 2000000, 6080000000, 80000\n"
        "2024-07-08, 2024-07-08, FO, NSE, FUT, 54321, INE002A01018, RELIANCE, FO, 3010.0, 3060.0, 3000.0, 3050.0, 3049.0, 3015.0, 50000, 150000000, 1000\n"
    )
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = tmp.name
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv", csv_content)

    try:
        records = parse_bhavcopy_zip(zip_path)
        assert len(records) == 1  # Sgmt == 'CM' only
        r = records[0]
        assert r["symbol"] == "RELIANCE"
        assert r["series"] == "EQ"
        assert r["open"] == 3000.0
        assert r["close"] == 3040.0
        assert r["volume"] == 2000000
        assert r["turnover"] == 6080000000.0
        assert r["isin"] == "INE002A01018"
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

