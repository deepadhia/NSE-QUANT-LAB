#!/usr/bin/env python3
"""
probe_upstox_quotes.py -- READ-ONLY check of the real Upstox quote payload at the 09:16 IST open.

Why: the stale-quote guard in PaperTradingManager (`_quote_is_stale`) compares the quote `timestamp` with the execution date, but the real
payload format was never seen live. Run this ONCE at about 09:16-09:20 IST on any trading day (no orders needed). It prints, per symbol, the raw
`ts`, how our parser reads it, whether that is TODAY (IST), open/LTP, bid/ask and spread, then a verdict:
  * PARSER OK       every quote's ts parsed and is today's date            -> the freshness guard works as designed
  * TS UNPARSEABLE  ts missing / not understood                            -> guard is blind (accepts + logs); send me the raw ts samples
  * STALE QUOTES    parsed ts is NOT today during the session              -> do not trust fills yet; investigate
It writes NOTHING (no Mongo, no files) and places no orders. It makes one batched, read-only HTTPS call to Upstox using the token from .env.

Usage:  python scripts/probe_upstox_quotes.py [--symbols RELIANCE,HDFCBANK,...] [--limit 20]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DEFAULT_SYMBOLS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "ITC", "LT", "AXISBANK", "BHARTIARTL",
                   "KOTAKBANK", "HINDUNILVR", "MARUTI", "SUNPHARMA", "BAJFINANCE", "TITAN", "ASIANPAINT", "NTPC", "ONGC", "WIPRO"]


def summarize(rows: List[Dict[str, Any]], today: dt.date) -> Dict[str, Any]:
    """rows: [{symbol, ts, open, ltp, bid, ask}]. Pure function (tested without network). Uses the SAME parser as the manager's guard."""
    from nse_quant_lab.paper.manager import PaperTradingManager

    out_rows, parsed, same_day, spreads = [], 0, 0, []
    for r in rows:
        d = PaperTradingManager._quote_session_date(r.get("ts"))
        if d is not None:
            parsed += 1
            same_day += int(d == today)
        spread_bps = None
        if r.get("bid") and r.get("ask") and (r["bid"] + r["ask"]) > 0:
            spread_bps = round((r["ask"] - r["bid"]) / ((r["ask"] + r["bid"]) / 2.0) * 1e4, 2)
            spreads.append(spread_bps)
        out_rows.append({**r, "parsed_date": d, "is_today": (d == today) if d is not None else None, "spread_bps": spread_bps})
    n = len(rows)
    if n == 0:
        verdict = "NO QUOTES RETURNED"
    elif parsed == 0:
        verdict = "TS UNPARSEABLE"
    elif parsed < n:
        verdict = "TS PARTLY UNPARSEABLE"
    elif same_day == n:
        verdict = "PARSER OK"
    else:
        verdict = "STALE QUOTES"
    return {"n": n, "parsed": parsed, "same_day": same_day, "median_spread_bps": (sorted(spreads)[len(spreads) // 2] if spreads else None),
            "verdict": verdict, "rows": out_rows}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Upstox quote payload probe (no writes, no orders).")
    ap.add_argument("--symbols", default="", help="comma-separated NSE symbols (default: 20 large caps)")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args(argv)

    from nse_quant_lab import upstox_feed as uf
    from nse_quant_lab.data_loader import auto_refresh_upstox_token

    now_ist = dt.datetime.now(IST)
    token = auto_refresh_upstox_token() or os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        print("No Upstox access token available (UPSTOX_ACCESS_TOKEN / auto refresh). Nothing probed.")
        return 1
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS
    syms = syms[: a.limit]
    arch = next((p for p in (os.path.join(PROJECT_ROOT, "data", "external", "nse_archive", "parquet"),
                             os.path.join(PROJECT_ROOT, "external", "nse_archive", "parquet")) if os.path.exists(p)), None)
    isins: Dict[str, str] = uf.symbol_isin_map(__import__("pathlib").Path(arch), [now_ist.year - 1, now_ist.year]) if arch else {}
    keys = {s: uf.instrument_key(isins[s]) for s in syms if s in isins}
    missing = [s for s in syms if s not in keys]
    if missing:
        print(f"No ISIN for: {missing} (skipped)")
    quotes = uf.fetch_quotes(list(keys.values()), token) if keys else {}
    rows = []
    for s, k in keys.items():
        q = quotes.get(k)
        if q:
            rows.append({"symbol": s, "ts": q.get("ts"), "open": q.get("open"), "ltp": q.get("ltp"), "bid": q.get("bid"), "ask": q.get("ask")})
        else:
            print(f"{s}: no quote returned for {k}")
    res = summarize(rows, now_ist.date())
    print(f"\nNow (IST): {now_ist.isoformat(timespec='seconds')}  | execution-date check uses IST date {now_ist.date()}")
    print(f"{'symbol':<12}{'raw ts':<34}{'parsed date':<13}{'today?':<8}{'open':>10}{'ltp':>10}{'spread bps':>12}")
    for r in res["rows"]:
        print(f"{r['symbol']:<12}{str(r['ts'])[:32]:<34}{str(r['parsed_date']):<13}{str(r['is_today']):<8}"
              f"{(r['open'] or 0):>10.2f}{(r['ltp'] or 0):>10.2f}{str(r['spread_bps']):>12}")
    print(f"\nquotes {res['n']} | ts parsed {res['parsed']} | dated today {res['same_day']} | median spread {res['median_spread_bps']} bps")
    print(f"VERDICT: {res['verdict']}")
    if now_ist.hour < 9 or (now_ist.hour == 9 and now_ist.minute < 15) or now_ist.weekday() >= 5:
        print("NOTE: run outside market hours / on a weekend: 'STALE QUOTES' is expected here. Re-run at 09:16-09:20 IST on a trading day.")
    return 0 if res["n"] else 1


if __name__ == "__main__":
    sys.exit(main())
