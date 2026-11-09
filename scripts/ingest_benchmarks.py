"""
scripts/ingest_benchmarks.py -- Ingests Nifty 50 & Nifty 500 benchmark candles (Upstox primary, Yahoo fallback)
==============================================================================================================
Writes daily index OHLC into MongoDB 'benchmark_ohlcv' (^NSEI = Nifty 50, ^CRSLDX = Nifty 500). The Nifty 500 close
drives the strategies' regime gate, and every backtest/paper comparison uses these bars.

Source policy (every stored bar carries a `source` field):
  * `upstox`             -- the authenticated Upstox index candle (NSE_INDEX|Nifty 50 / Nifty 500) for the last
                            UPSTOX_LOOKBACK_DAYS days. Wins over Yahoo whenever both have the date.
  * `yahoo_provisional`  -- a Yahoo bar NEWER than Upstox's newest bar. Upstox publishes the same-day index candle
                            late, so the evening run must still use Yahoo for "today"; the next run replaces it with
                            the Upstox bar (the lookback window is re-upserted every run).
  * `yahoo`              -- Yahoo history older than the Upstox window, or any bar written while Upstox was unreachable.
  * A Yahoo-only bar INSIDE Upstox's range is NOT written (Upstox, the exchange-sourced feed, has no such session; this
    is how a phantom or stale Yahoo day would be caught). Special sessions such as Diwali Muhurat are real sessions and
    Upstox carries them, so they are kept even when Yahoo has dropped them.

Failure policy (fail loud, never guess):
  * Upstox unreachable / token rejected / empty answer, a Yahoo-vs-Upstox close disagreement above CLOSE_TOLERANCE, or a
    rejected bar -> logged at ERROR/CRITICAL and sent to Telegram. The run still exits 0 when the freshness check passes,
    because the scheduler HOLDS the 18:00 pipeline while this job is retrying and a source outage must not block it.
  * Freshness check: no bar for the latest completed NSE session on either benchmark (or no data at all) -> exit 1, so
    the scheduler retries and finally alerts. That is the "candle was not inserted" case.

Usage:
    python scripts/ingest_benchmarks.py            # live run (writes MongoDB)
    python scripts/ingest_benchmarks.py --dry-run  # fetch + merge + report, NO database or Telegram writes
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
from pymongo import MongoClient, UpdateOne

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:  # pragma: no cover - dotenv is a declared dependency; env may already be populated
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_benchmarks")

IST = timezone(timedelta(hours=5, minutes=30))

BENCHMARK_SYMBOLS = {
    "^NSEI": "NIFTY 50",
    "^CRSLDX": "NIFTY 500",
}
UPSTOX_INDEX_KEYS = {
    "^NSEI": "NSE_INDEX|Nifty 50",
    "^CRSLDX": "NSE_INDEX|Nifty 500",
}
UPSTOX_LOOKBACK_DAYS = 40          # calendar days re-fetched from Upstox on every run
CLOSE_TOLERANCE = 0.001            # 0.1 %: Yahoo and Upstox closes agreed to the paisa on every day inspected
SESSION_COMPLETE_AFTER = dtime(15, 45)  # IST: before this, today's daily bar is not final


# ----------------------------------------------------------------------------- sources
def fetch_yahoo_rows(ticker: str, start_date: str, end_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """Daily OHLCV rows from Yahoo Finance (the long-history layer). [] when Yahoo returns nothing."""
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance is required. Run: pip install yfinance")
        sys.exit(1)

    kwargs = {"start": start_date, "progress": False, "timeout": 15, "auto_adjust": False}
    if end_date:
        kwargs["end"] = end_date
    df = yf.download(ticker, **kwargs)
    if df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    def num(r, k, default=0.0):
        v = r[k] if k in r else default
        return float(v.iloc[0] if isinstance(v, pd.Series) else v)

    rows = []
    for dt_idx, r in df.iterrows():
        c = num(r, "Close")
        if pd.isna(c) or c <= 0:
            continue
        v = num(r, "Volume")
        rows.append({"date": dt_idx.strftime("%Y-%m-%d"), "open": round(num(r, "Open"), 2), "high": round(num(r, "High"), 2),
                     "low": round(num(r, "Low"), 2), "close": round(c, 2), "volume": 0.0 if pd.isna(v) else float(v)})
    return rows


def fetch_upstox_rows(ticker: str, from_date: str, to_date: str, token: str) -> List[Dict[str, Any]]:
    """Daily index candles from Upstox. Raises UpstoxAuthError (bad token/IP) or RuntimeError (persistent failure)."""
    from nse_quant_lab import upstox_feed as uf
    return uf.fetch_daily_candles(UPSTOX_INDEX_KEYS[ticker], from_date, to_date, token)


# ----------------------------------------------------------------------------- pure logic (unit-tested)
def _ohlc_violation(r: Dict[str, Any]) -> Optional[str]:
    o, h, l, c = r["open"], r["high"], r["low"], r["close"]
    if min(o, h, l, c) <= 0:
        return "non-positive price"
    eps = 1e-6 * c
    if h + eps < max(o, c) or l - eps > min(o, c) or h + eps < l:
        return f"OHLC inconsistent (o={o} h={h} l={l} c={c})"
    return None


def merge_bars(yahoo: List[Dict[str, Any]], upstox: List[Dict[str, Any]], upstox_ok: bool, window_start: str,
               tol: float = CLOSE_TOLERANCE) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Combine the two feeds per date following the module's source policy.

    Returns (rows_to_write, mismatches, rejected). `rows_to_write` carry a `source` field. `mismatches` are Yahoo-vs-Upstox
    close disagreements (Upstox is still written); `rejected` are bars deliberately NOT written, with the reason."""
    y = {r["date"]: r for r in yahoo}
    u = {r["date"]: r for r in upstox} if upstox_ok else {}
    u_max = max(u) if u else ""
    out: List[Dict[str, Any]] = []
    mismatches: List[str] = []
    rejected: List[str] = []

    for d in sorted(set(y) | set(u)):
        if d in u:
            row = dict(u[d])
            bad = _ohlc_violation(row)
            if bad:
                rejected.append(f"{d}: Upstox bar rejected ({bad})")
                continue
            if d in y:
                if y[d]["close"] > 0 and abs(row["close"] - y[d]["close"]) / row["close"] > tol:
                    mismatches.append(f"{d}: Upstox close {row['close']} vs Yahoo close {y[d]['close']}")
                row["volume"] = y[d].get("volume", 0.0) or 0.0   # Upstox reports volume 0 for indices
            row["source"] = "upstox"
            out.append(row)
            continue

        row = dict(y[d])
        in_window = d >= window_start
        if upstox_ok and in_window and u and d < u_max:
            rejected.append(f"{d}: Yahoo-only bar inside Upstox's range ({window_start}..{u_max}) not written (no such session at Upstox)")
            continue
        if in_window:
            bad = _ohlc_violation(row)
            if bad:
                rejected.append(f"{d}: Yahoo bar rejected ({bad})")
                continue
        row["source"] = "yahoo_provisional" if (upstox_ok and in_window) else "yahoo"
        out.append(row)
    return out, mismatches, rejected


def expected_session(now_ist: datetime, is_trading_day: Callable[[date], bool]) -> date:
    """Latest NSE session whose daily bar must exist now: today once the session is final, else the previous trading day."""
    d = now_ist.date()
    if now_ist.time() < SESSION_COMPLETE_AFTER:
        d -= timedelta(days=1)
    for _ in range(14):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    raise RuntimeError("no NSE trading day found in the last 14 days: holiday calendar unusable")


# ----------------------------------------------------------------------------- orchestration
def ingest_benchmarks(
    start_date: str = "2018-01-01",
    end_date: Optional[str] = None,
    mongo_uri: str = "mongodb://localhost:27017/",
    db_name: str = "stock_scanner",
    collection_name: str = "benchmark_ohlcv",
    *,
    col=None,
    yahoo_fetch: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    upstox_fetch: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    upstox_token: Optional[str] = None,
    now_ist: Optional[datetime] = None,
    trading_day_fn: Optional[Callable[[date], bool]] = None,
    notify: Optional[Callable[[str], Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fetch, merge and store both benchmarks. Returns {"ok": bool, "problems": [...], "degraded": [...]}.

    `problems` make the run fail (exit 1, scheduler retries); `degraded` are loud-but-non-blocking (alerted, exit 0)."""
    yahoo_fetch = yahoo_fetch or fetch_yahoo_rows
    upstox_fetch = upstox_fetch or fetch_upstox_rows
    now_ist = now_ist or datetime.now(IST)
    if trading_day_fn is None:
        from nse_quant_lab.holidays import is_trading_day as trading_day_fn
    problems: List[str] = []
    degraded: List[str] = []

    if col is None and not dry_run:
        col = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)[db_name][collection_name]
        col.create_index([("symbol", 1), ("date", 1)], unique=True)
        col.create_index([("date", 1)])

    try:
        want = expected_session(now_ist, trading_day_fn)
    except Exception as exc:
        want = None
        problems.append(f"cannot determine the latest NSE session ({exc})")
    window_start = (now_ist.date() - timedelta(days=UPSTOX_LOOKBACK_DAYS)).isoformat()
    to_date = now_ist.date().isoformat()
    now_utc = datetime.now(timezone.utc).isoformat()

    for ticker, display_name in BENCHMARK_SYMBOLS.items():
        logger.info("Benchmark %s (%s): fetching Yahoo history from %s ...", display_name, ticker, start_date)
        try:
            yahoo = yahoo_fetch(ticker, start_date, end_date)
        except Exception as exc:
            yahoo = []
            degraded.append(f"{ticker}: Yahoo fetch failed ({type(exc).__name__}: {exc})")
            logger.error("Yahoo fetch failed for %s: %s", ticker, exc, exc_info=True)

        upstox, upstox_ok = [], False
        if not upstox_token:
            degraded.append(f"{ticker}: no Upstox token available; stored Yahoo bars only (source=yahoo)")
        else:
            try:
                upstox = upstox_fetch(ticker, window_start, to_date, upstox_token)
                upstox_ok = bool(upstox)
                if not upstox:
                    degraded.append(f"{ticker}: Upstox returned no candles for {window_start}..{to_date}; stored Yahoo bars only")
            except Exception as exc:
                degraded.append(f"{ticker}: Upstox fetch failed ({type(exc).__name__}: {exc}); stored Yahoo bars only")
                logger.error("Upstox fetch failed for %s: %s", ticker, exc, exc_info=True)

        rows, mismatches, rejected = merge_bars(yahoo, upstox, upstox_ok, window_start)
        for m in mismatches:
            degraded.append(f"{ticker}: SOURCE MISMATCH {m} (Upstox value stored)")
        for r in rejected:
            degraded.append(f"{ticker}: {r}")

        if not rows:
            problems.append(f"{ticker}: no usable bars from any source")
            continue
        newest = max(r["date"] for r in rows)
        if want is not None and newest < want.isoformat():
            problems.append(f"{ticker}: newest bar is {newest} but the latest completed NSE session is {want}: today's candle is missing")

        by_source: Dict[str, int] = {}
        for r in rows:
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        logger.info("%s: %d bars merged %s, newest %s, expected session %s", ticker, len(rows), by_source, newest, want)

        if dry_run:
            for r in rows[-4:]:
                logger.info("[dry-run] %s %s", ticker, r)
            continue

        ops = [UpdateOne({"symbol": ticker, "date": r["date"]},
                         {"$set": {**r, "symbol": ticker, "name": display_name, "updated_at": now_utc}}, upsert=True) for r in rows]
        res = col.bulk_write(ops, ordered=False)
        logger.info("Synced %s: %d rows processed (upserted: %d, modified: %d).", ticker, len(ops), res.upserted_count, res.modified_count)

    for msg in degraded:
        logger.error("[BENCHMARK DEGRADED] %s", msg)
    for msg in problems:
        logger.error("[BENCHMARK FAILED] %s", msg)
    if (degraded or problems) and notify and not dry_run:
        try:
            notify("<b>Benchmark ingestion " + ("FAILED" if problems else "degraded") + "</b>\n" + "\n".join(f"- {m}" for m in problems + degraded))
        except Exception as exc:
            logger.error("Could not send the benchmark alert: %s", exc)
    return {"ok": not problems, "problems": problems, "degraded": degraded}


def _telegram_notify(text: str) -> Any:
    from nse_quant_lab.notifications.telegram import TelegramNotifier
    return TelegramNotifier().send_message(text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="fetch, merge and report only: no database or Telegram writes")
    a = ap.parse_args(argv)

    token = None
    try:
        from nse_quant_lab.data_loader import auto_refresh_upstox_token
        token = auto_refresh_upstox_token() or os.environ.get("UPSTOX_ACCESS_TOKEN")
    except Exception as exc:
        logger.error("Upstox token lookup failed: %s", exc)
    if token and "your_upstox" in token:
        token = None

    result = ingest_benchmarks(
        mongo_uri=os.environ.get("MONGO_URI", "mongodb://localhost:27017/"),
        db_name=os.environ.get("MONGO_DB", "stock_scanner"),
        upstox_token=token, notify=_telegram_notify, dry_run=a.dry_run,
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
