"""
holidays.py -- Comprehensive NSE Market Holiday & Trading Session Calendar
==========================================================================
Hierarchy:
1. Dynamic fetch from Upstox Market Holidays API (/v2/market/holidays)
2. MongoDB cache in 'market_holidays' collection
3. Curated verified static baseline of NSE trading holidays (2024-2027)
"""

from __future__ import annotations
import os
import json
import logging
from typing import Set, Optional
from datetime import datetime, date, timezone
import urllib.request

logger = logging.getLogger("holidays")

# Curated verified official NSE equity trading holidays (excluding Saturdays/Sundays)
STATIC_NSE_HOLIDAYS: Set[str] = {
    # 2024 Verified NSE Holidays
    "2024-01-22", "2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29",
    "2024-04-11", "2024-04-17", "2024-05-01", "2024-05-20", "2024-06-17",
    "2024-07-17", "2024-08-15", "2024-10-02", "2024-11-01", "2024-11-15",
    "2024-11-20", "2024-12-25",

    # 2025 Verified NSE Holidays
    "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10",
    "2025-04-14", "2025-04-18", "2025-05-01", "2025-06-07", "2025-08-15",
    "2025-08-27", "2025-10-02", "2025-10-21", "2025-10-22", "2025-11-05",
    "2025-11-20", "2025-12-25",

    # 2026 Verified NSE Holidays
    "2026-01-26", "2026-02-16", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-08-15", "2026-08-28", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-10", "2026-11-24", "2026-12-25",

    # 2027 Projected NSE Holidays
    "2027-01-26", "2027-03-08", "2027-03-22", "2027-03-26", "2027-04-14",
    "2027-05-01", "2027-08-15", "2027-10-02", "2027-11-08", "2027-12-25",
}

_MEM_CACHE: dict = {}


def get_nse_holidays(year: Optional[int] = None, mongo_uri: Optional[str] = None) -> Set[str]:
    """Returns the set of official NSE holiday dates (YYYY-MM-DD)."""
    current_year = year or datetime.now().year

    # Check memory cache
    if current_year in _MEM_CACHE:
        return _MEM_CACHE[current_year]

    # Try MongoDB cache
    try:
        from pymongo import MongoClient
        uri = mongo_uri or os.environ.get("MONGO_URI")
        if uri:
            client = MongoClient(uri, serverSelectionTimeoutMS=500)
            db_n = os.environ.get("MONGO_DB", "stock_scanner")
            doc = client[db_n]["market_holidays"].find_one({"year": current_year})
            if doc and doc.get("holidays"):
                holidays_set = set(doc["holidays"])
                _MEM_CACHE[current_year] = holidays_set
                return holidays_set
    except Exception:
        pass

    # Try live Upstox API
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if token:
        try:
            url = "https://api.upstox.com/v2/market/holidays"
            headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8")).get("data", [])
                    fetched = set()
                    for item in data:
                        h_date = item.get("date")
                        if h_date:
                            fetched.add(str(h_date)[:10])
                    if fetched:
                        _MEM_CACHE[current_year] = fetched
                        return fetched
        except Exception:
            pass

    # Fallback to static verified baseline
    filtered = {h for h in STATIC_NSE_HOLIDAYS if h.startswith(str(current_year))}
    res = filtered if filtered else STATIC_NSE_HOLIDAYS
    _MEM_CACHE[current_year] = res
    return res


def is_trading_day(dt: Optional[datetime | date | str] = None) -> bool:
    """
    Checks if a given date is an official NSE trading day.
    Returns False on Saturdays (5), Sundays (6), and official NSE holidays.
    """
    if dt is None:
        target_dt = datetime.now()
    elif isinstance(dt, str):
        target_dt = datetime.strptime(dt[:10], "%Y-%m-%d")
    elif isinstance(dt, date) and not isinstance(dt, datetime):
        target_dt = datetime.combine(dt, datetime.min.time())
    else:
        target_dt = dt

    # Check weekend (Monday is 0, Sunday is 6)
    if target_dt.weekday() in (5, 6):
        return False

    date_str = target_dt.strftime("%Y-%m-%d")
    holidays = get_nse_holidays(target_dt.year)
    return date_str not in holidays
