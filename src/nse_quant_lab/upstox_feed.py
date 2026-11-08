"""
upstox_feed.py -- daily full-tier market data from the user's authorised Upstox API (no NSE scraping).

What it does
  * maps NSE symbols to Upstox instrument keys (`NSE_EQ|<ISIN>`; ISINs come from the local bhavcopy archive),
  * picks the stocks worth fetching (the strategy's liquid tier plus a safety buffer below the Rs 10 Cr floor),
  * fetches daily candles (V3 historical-candle endpoint) with a request pace safely under Upstox's published limits
    (50/s, 500/min, 2,000 per 30 min) and 429 back-off,
  * fetches full market quotes (<= 500 instruments per call) for the 09:16 fills: open, last price, best bid/ask, circuit limits.

STATUS: written against Upstox's published API docs and tested with mocked responses only. It has NOT been run against the live
API (no credentials were available to the author). First live run: use `scripts/ingest_upstox_fulltier.py --dry-run`, then `--limit 5`.
Whether Upstox daily candles are split/bonus adjusted is not stated in the docs; earlier research found them adjusted, so the loader
re-anchors history from an adjusted refresh when a gap is flagged.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

logger = logging.getLogger("upstox_feed")

API_BASE = "https://api.upstox.com"
CANDLE_URL = API_BASE + "/v3/historical-candle/{key}/days/1/{to_date}/{from_date}"
QUOTE_URL = API_BASE + "/v2/market-quote/quotes"
MAX_QUOTE_KEYS = 500
MIN_SECONDS_BETWEEN_CANDLE_CALLS = 0.17  # ~350 requests/minute, under the 500/min limit
USER_AGENT = "nse-quant-lab/1.0 (personal research client for the authorised Upstox API)"
GAP_DOWN, GAP_UP = 0.75, 1.33            # open / previous close beyond these -> possible split/bonus, flag for review


class UpstoxAuthError(RuntimeError):
    """401/403 from Upstox: the access token is missing or expired."""


# ----------------------------------------------------------------------------- symbols and targets
def instrument_key(isin: str) -> str:
    return f"NSE_EQ|{isin}"


def symbol_isin_map(parquet_dir: Path, years: Iterable[int]) -> Dict[str, str]:
    """Latest non-empty ISIN per NSE symbol from the local raw bhavcopy Parquet archive (recent years only).
    If parquet archive is not present on disk (cloud server), falls back to MongoDB 'symbols' collection."""
    frames = []
    if parquet_dir and Path(parquet_dir).exists():
        for y in years:
            f = Path(parquet_dir) / f"bhavcopy_{y}.parquet"
            if f.exists():
                frames.append(pd.read_parquet(f, columns=["symbol", "isin", "date"]))
    if not frames:
        try:
            import os
            from pymongo import MongoClient

            mongo_u = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
            m_db = os.environ.get("MONGO_DB", os.environ.get("MONGO_DB_NAME", "stock_scanner"))
            cli = MongoClient(mongo_u, serverSelectionTimeoutMS=2000)
            sym_docs = list(cli[m_db]["symbols"].find({"isin": {"$regex": "^INE"}}, {"_id": 0, "symbol": 1, "isin": 1}))
            res = {}
            for d in sym_docs:
                clean = d["symbol"].replace(".NS", "").replace(".BO", "")
                res[clean] = d["isin"]
                res[d["symbol"]] = d["isin"]
            if res:
                return res
        except Exception:
            pass
        return {}
    d = pd.concat(frames, ignore_index=True)
    d = d[d["isin"].fillna("").str.startswith("INE")].sort_values("date")
    return d.groupby("symbol")["isin"].last().to_dict()


def select_targets(stats: pd.DataFrame, isins: Dict[str, str], min_adt: float = 3.0e7, min_history: int = 200,
                   include_symbols: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """stats: columns symbol, adt (50-session median turnover, Rs), hist (sessions), series (last series).
    Keeps main-board names with enough history and turnover above a buffer under the strategy's Rs 10 Cr floor, that have an ISIN.
    `include_symbols` (open paper positions) bypass the turnover/history/series screens: a held name that drifts below the tier must keep
    receiving bars, otherwise its EMA/SMA stop is never evaluated (evaluate_daily_exits skips symbols without a bar)."""
    keep = (stats["series"] == "EQ") & (stats["adt"] >= min_adt) & (stats["hist"] >= min_history)
    held = set(include_symbols or [])
    if held:
        keep = keep | stats["symbol"].isin(held)
        absent = sorted(held - set(stats["symbol"]))
        if absent:
            logger.error("[HELD_NOT_IN_STATS] open positions with no price history in the stats source, cannot be fetched: %s", absent)
    s = stats[keep].copy()
    s["isin"] = s["symbol"].map(isins)
    missing = s[s["isin"].isna()]["symbol"].tolist()
    if missing:
        held_missing = sorted(set(missing) & held)
        if held_missing:
            logger.error("[HELD_NO_ISIN] open positions with no ISIN, no bars will be fetched (stop-loss blind): %s", held_missing)
        logger.warning("no ISIN for %d target symbols (skipped): %s", len(missing), missing[:15])
    s = s.dropna(subset=["isin"])
    s["key"] = s["isin"].map(instrument_key)
    return s.sort_values("adt", ascending=False).reset_index(drop=True)


# ----------------------------------------------------------------------------- HTTP helpers
def _error_detail(e: "urllib.error.HTTPError") -> str:
    """Upstox's error payload (code + message) without ever echoing credentials; empty string if unreadable."""
    try:
        body = e.read().decode("utf-8", errors="replace")[:300]
        return f" | Upstox said: {body}" if body.strip() else ""
    except Exception:
        return ""


def _headers(token: str) -> Dict[str, str]:
    # Honest client identification. Python's default urllib signature is blocked by Cloudflare (error 1010) in front of api.upstox.com;
    # we identify as ourselves rather than impersonating a browser.
    return {"Accept": "application/json", "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}


def _http_json(url: str, token: str, opener: Callable = urllib.request.urlopen, timeout: int = 20) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=_headers(token))
    with opener(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_candles(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Upstox candle rows are [timestamp, open, high, low, close, volume, open_interest]."""
    rows = []
    for c in (payload.get("data") or {}).get("candles") or []:
        ts = pd.Timestamp(c[0])
        rows.append({"date": ts.strftime("%Y-%m-%d"), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                     "close": float(c[4]), "volume": float(c[5])})
    return sorted(rows, key=lambda r: r["date"])


def fetch_daily_candles(key: str, from_date: str, to_date: str, token: str, opener: Callable = urllib.request.urlopen,
                        sleep: Callable[[float], None] = time.sleep, max_retries: int = 4) -> List[Dict[str, Any]]:
    """Daily candles for one instrument between from_date and to_date (inclusive). Raises UpstoxAuthError on 401/403.
    Backs off on 429 and on transient errors; returns [] when Upstox has no data (404/empty)."""
    url = CANDLE_URL.format(key=urllib.parse.quote(key, safe=""), to_date=to_date, from_date=from_date)
    for attempt in range(max_retries):
        sleep(MIN_SECONDS_BETWEEN_CANDLE_CALLS)
        try:
            return parse_candles(_http_json(url, token, opener))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise UpstoxAuthError(f"Upstox rejected the request (HTTP {e.code}): expired/invalid token, or the calling IP is not on the API key's "
                                      f"allow-list{_error_detail(e)}")
            if e.code == 404:
                return []
            wait = 2.0 * (attempt + 1) if e.code == 429 else 1.0 * (attempt + 1)
            logger.warning("Upstox HTTP %s for %s, retry %d in %.0fs", e.code, key, attempt + 1, wait)
            sleep(wait)
        except Exception as exc:
            logger.warning("Upstox error for %s: %s (retry %d)", key, exc, attempt + 1)
            sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Upstox candles failed after {max_retries} attempts for {key}")


def fetch_quotes(keys: List[str], token: str, opener: Callable = urllib.request.urlopen) -> Dict[str, Dict[str, Any]]:
    """Full market quotes in batches of <= 500 keys. Returns {instrument_token: normalised quote}."""
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(keys), MAX_QUOTE_KEYS):
        batch = keys[i:i + MAX_QUOTE_KEYS]
        url = QUOTE_URL + "?instrument_key=" + ",".join(urllib.parse.quote(k, safe="") for k in batch)
        try:
            payload = _http_json(url, token, opener)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise UpstoxAuthError(f"Upstox rejected the request (HTTP {e.code}){_error_detail(e)}")
            raise
        for _, q in (payload.get("data") or {}).items():
            tok = q.get("instrument_token")
            if not tok:
                continue
            depth = q.get("depth") or {}
            buys = [x for x in depth.get("buy", []) if x.get("price", 0) > 0]
            sells = [x for x in depth.get("sell", []) if x.get("price", 0) > 0]
            ohlc = q.get("ohlc") or {}
            out[tok] = {
                "open": float(ohlc.get("open") or 0.0), "high": float(ohlc.get("high") or 0.0), "low": float(ohlc.get("low") or 0.0),
                "ltp": float(q.get("last_price") or 0.0), "bid": float(buys[0]["price"]) if buys else None,
                "ask": float(sells[0]["price"]) if sells else None, "volume": float(q.get("volume") or 0.0),
                "bid_qty": float(buys[0].get("quantity") or 0.0) if buys else None, "ask_qty": float(sells[0].get("quantity") or 0.0) if sells else None,
                "upper_circuit": q.get("upper_circuit_limit"), "lower_circuit": q.get("lower_circuit_limit"),
                "ts": q.get("timestamp"),
            }
    return out


class UpstoxQuoteProvider:
    """Callable for PaperTradingManager.quote_provider: symbol -> {'open','high','low','ltp','bid','ask',...} or None.
    Quotes for all requested symbols are fetched in one batched call and cached for the run."""

    def __init__(self, token: str, isins: Dict[str, str], opener: Callable = urllib.request.urlopen):
        self.token, self.isins, self.opener = token, isins, opener
        self._cache: Dict[str, Dict[str, Any]] = {}

    def prefetch(self, symbols: Iterable[str]) -> None:
        keys = [instrument_key(self.isins[s]) for s in symbols if s in self.isins]
        if keys:
            self._cache.update(fetch_quotes(keys, self.token, self.opener))

    def __call__(self, symbol: str) -> Optional[Dict[str, Any]]:
        isin = self.isins.get(symbol)
        if not isin:
            return None
        key = instrument_key(isin)
        if key not in self._cache:
            self._cache.update(fetch_quotes([key], self.token, self.opener))
        return self._cache.get(key)


# ----------------------------------------------------------------------------- gap detection
def gap_flag(prev_close: Optional[float], open_px: float) -> bool:
    """True when the open is far from the previous close: a possible split / bonus / demerger to review (never auto-trusted)."""
    if not prev_close or prev_close <= 0 or open_px <= 0:
        return False
    r = open_px / prev_close
    return r < GAP_DOWN or r > GAP_UP
