"""
check_upstox_quote_shape.py -- one-off, READ-ONLY check of the live Upstox full-market-quote payload shape.

Why: upstox_feed.fetch_quotes() and PaperTradingManager._quote_is_stale() were written against the published docs and never run
live. Live fills are refused (fail-closed) unless the quote carries a parseable `timestamp` from the execution session, and they
need depth (bid/ask) and circuit limits. This prints which of those fields the real payload has. It never prints the token.

Usage (from the repo root, with UPSTOX_ACCESS_TOKEN set in your environment/.env):
    python scripts/check_upstox_quote_shape.py            # RELIANCE
    python scripts/check_upstox_quote_shape.py INE009A01021   # any ISIN

Run it on a trading day, ideally 09:16-09:30 IST, so the timestamp can be compared with today's date. Outside market hours it still
shows whether the field exists (it will hold the last session's time).
"""
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

from nse_quant_lab import upstox_feed as uf
from nse_quant_lab.data_loader import auto_refresh_upstox_token
from nse_quant_lab.paper.manager import PaperTradingManager

IST = timezone(timedelta(hours=5, minutes=30))


def main() -> int:
    isin = sys.argv[1] if len(sys.argv) > 1 else "INE002A01018"
    key = uf.instrument_key(isin)
    token = auto_refresh_upstox_token()
    if not token:
        print("NO TOKEN: set UPSTOX_ACCESS_TOKEN (or enable UPSTOX_AUTO_REFRESH) and retry.")
        return 2
    url = uf.QUOTE_URL + "?instrument_key=" + urllib.parse.quote(key, safe="")
    try:
        payload = uf._http_json(url, token)
    except Exception as exc:  # report the class of failure, never the token
        print(f"REQUEST FAILED: {type(exc).__name__}: {exc}")
        return 2
    data = payload.get("data") or {}
    if not data:
        print("EMPTY data in response; top-level keys:", sorted(payload))
        return 2
    raw_key, raw = next(iter(data.items()))
    print("Response key:", raw_key, "| status:", payload.get("status"))
    print("Raw quote fields:", sorted(raw))
    depth = raw.get("depth") or {}
    print("depth.buy levels:", len(depth.get("buy", [])), "| depth.sell levels:", len(depth.get("sell", [])))
    print("circuit limits:", raw.get("lower_circuit_limit"), raw.get("upper_circuit_limit"))
    for field in ("timestamp", "last_trade_time"):
        print(f"{field}: {raw.get(field)!r}")

    normalised = uf.fetch_quotes([key], token)
    q = next(iter(normalised.values()), {})
    ts = q.get("ts")
    session = PaperTradingManager._quote_session_date(ts)
    today = datetime.now(IST).date()
    print("feed maps ts =", repr(ts), "-> parsed IST session date:", session, "| today (IST):", today)
    if ts in (None, ""):
        print("RESULT: FAIL - `timestamp` missing; every live fill would be refused (QUOTE_FRESHNESS_UNVERIFIED). "
              "If `last_trade_time` is present, decide deliberately whether to map it; do not guess.")
        return 1
    if session is None:
        print("RESULT: FAIL - timestamp present but unparseable by _quote_session_date; extend the parser with this exact format.")
        return 1
    print("RESULT: OK - timestamp present and parseable." + ("" if session == today else
          " NOTE: it is not today's session (expected outside market hours / before the first trade)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
