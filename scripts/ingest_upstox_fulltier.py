#!/usr/bin/env python3
"""
ingest_upstox_fulltier.py -- daily candles for the strategy's FULL liquid tier from the user's authorised Upstox API.

Run after the close (about 15:35 IST). Writes to Mongo `upstox_fulltier_daily` (idempotent upsert on symbol+date). The PIT loader joins these
bars onto the bhavcopy-based clean panel for every session after the panel's last date.
  python scripts/ingest_upstox_fulltier.py --dry-run          # no network: shows targets, instrument keys and the date window
  python scripts/ingest_upstox_fulltier.py --limit 5          # first live check on 5 stocks
  python scripts/ingest_upstox_fulltier.py                    # full tier for the latest trading day
Exit codes: 0 ok, 2 auth/credentials problem, 3 coverage below the gate (so cron/monitoring notices).
STATUS: tested with mocks only; never run against the live API by the author (no credentials available).
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from nse_quant_lab import upstox_feed as uf  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingest_upstox_fulltier")

ARCH = REPO / "data" / "external" / "nse_archive"
PANEL = ARCH / "clean_panel.parquet"
COLLECTION = "upstox_fulltier_daily"
COVERAGE_GATE = 0.90
ADJUSTED_REFRESH_DAYS = 400


def panel_stats(panel: Path = PANEL) -> pd.DataFrame:
    """Per-symbol 50-session median turnover, history length, last series and last close as of the panel's final date."""
    p = pd.read_parquet(panel, columns=["symbol", "date", "series", "close", "turnover"])
    p["series"] = p["series"].astype(str)
    last = p["date"].max()
    cut = p[p["date"] >= last - pd.Timedelta(days=130)]
    rows = []
    hist = p.groupby("symbol").size()
    for s, g in cut.groupby("symbol", sort=False):
        g = g.sort_values("date")
        if g["date"].iloc[-1] != last or len(g) < 50:
            continue
        rows.append((s, float(g["turnover"].tail(50).median()), int(hist[s]), g["series"].iloc[-1], float(g["close"].iloc[-1])))
    out = pd.DataFrame(rows, columns=["symbol", "adt", "hist", "series", "last_close"])
    out.attrs["panel_last"] = last.date()
    return out


def adjust_for_split(positions_col, orders_col, symbol: str, factor: float, now: str) -> Dict[str, int]:
    """A split/bonus multiplies the share count by `factor` (old price / new price) and divides prices by it; market value and cash
    cost are unchanged. Rescales open paper positions and pending orders so NAV, stops and sells stay correct. Demergers (no share-count
    change) are not touched: Upstox does not adjust them, so their factor stays near 1 and never reaches this function."""
    n_pos = n_ord = 0
    for p in list(positions_col.find({"symbol": symbol, "mode": "paper"})):
        positions_col.update_one({"_id": p["_id"]}, {"$set": {
            "shares": int(round(p["shares"] * factor)), "entry_price": p["entry_price"] / factor,
            "entry_fill_price": p.get("entry_fill_price", p["entry_price"]) / factor, "last_close": p.get("last_close", 0.0) / factor,
            "split_adjusted_factor": factor, "updated_at": now}})
        n_pos += 1
    for o in list(orders_col.find({"symbol": symbol, "mode": "paper", "status": {"$in": ["PENDING_OPEN", "DEFERRED_LOWER_CIRCUIT"]}})):
        upd = {"price": o.get("price", 0.0) / factor}
        if o.get("action") == "SELL":
            upd["shares"] = int(round(o["shares"] * factor))
        orders_col.update_one({"_id": o["_id"]}, {"$set": upd})
        n_ord += 1
    return {"positions": n_pos, "orders": n_ord}


def run_ingestion(targets: pd.DataFrame, run_date: dt.date, panel_last: dt.date, col, flags_col, fetch: Callable,
                  now: Callable[[], str] = lambda: dt.datetime.utcnow().isoformat(), positions_col=None, orders_col=None) -> Dict[str, object]:
    """Fetch and store bars for every target. `fetch(key, from_date, to_date)` returns candle rows; `col`/`flags_col` expose
    find_one / update_one / insert_one. Returns a summary dict (saved, failed symbols, flagged gaps)."""
    saved, failed, flagged = 0, [], []
    for r in targets.itertuples():
        last_doc = col.find_one({"symbol": r.symbol}, sort=[("date", -1)])
        last_stored = dt.date.fromisoformat(last_doc["date"]) if last_doc else panel_last
        start = max(panel_last, last_stored)           # panel_last itself is re-fetched once: it anchors the adjustment join
        try:
            rows = fetch(r.key, start.isoformat(), run_date.isoformat())
        except uf.UpstoxAuthError:
            raise
        except Exception as exc:
            log.error("fetch failed for %s: %s", r.symbol, exc)
            failed.append(r.symbol)
            continue
        if not rows:
            failed.append(r.symbol)
            continue
        new_rows = [x for x in rows if x["date"] > panel_last.isoformat()]
        prev_close = float(last_doc["close"]) if last_doc else float(r.last_close)
        if new_rows and uf.gap_flag(prev_close, new_rows[0]["open"]):
            flagged.append(r.symbol)
            flags_col.insert_one({"symbol": r.symbol, "date": new_rows[0]["date"], "prev_close": prev_close,
                                  "open": new_rows[0]["open"], "ratio": new_rows[0]["open"] / prev_close, "ts": now(),
                                  "note": "possible split/bonus/demerger; adjusted history refreshed; review before trusting signals"})
            try:
                start_ref = (run_date - dt.timedelta(days=ADJUSTED_REFRESH_DAYS)).isoformat()
                refreshed = fetch(r.key, start_ref, run_date.isoformat())
                for x in refreshed:
                    col.update_one({"symbol": r.symbol, "date": x["date"]},
                                   {"$set": {**x, "symbol": r.symbol, "source": "adjusted_refresh", "updated_at": now()}}, upsert=True)
                prev_date = last_doc["date"] if last_doc else panel_last.isoformat()
                ref_prev = next((x["close"] for x in refreshed if x["date"] == prev_date), None)
                if ref_prev and positions_col is not None and orders_col is not None:
                    factor = prev_close / ref_prev
                    if abs(factor - 1.0) > 0.05:
                        done = adjust_for_split(positions_col, orders_col, r.symbol, factor, now())
                        log.warning("%s: split factor %.4f applied to %d open positions and %d pending orders", r.symbol, factor, done["positions"], done["orders"])
            except Exception as exc:
                log.error("adjusted refresh failed for %s: %s", r.symbol, exc)
        for x in rows:
            col.update_one({"symbol": r.symbol, "date": x["date"]},
                           {"$set": {**x, "symbol": r.symbol, "source": "upstox", "updated_at": now()}}, upsert=True)
        saved += len([x for x in rows if x["date"] >= panel_last.isoformat()])
    return {"targets": len(targets), "saved_rows": saved, "failed": failed, "flagged": flagged}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None, help="run date YYYY-MM-DD (default: latest NSE trading day)")
    ap.add_argument("--dry-run", action="store_true", help="no network, no database writes")
    ap.add_argument("--anchors", action="store_true", help="fetch ONLY the panel's last date (anchor bars: verifies the feed against the bhavcopy and "
                                                              "lets the loader re-anchor adjusted history); use when no newer session exists yet")
    ap.add_argument("--limit", type=int, default=0, help="only the N most liquid targets (first live check)")
    ap.add_argument("--symbols", default="", help="comma-separated symbols to fetch instead of the full tier")
    ap.add_argument("--min-adt", type=float, default=3.0e7, help="target buffer: median turnover in Rs (strategy floor is Rs 10 Cr)")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:27017/")
    ap.add_argument("--db", default="stock_scanner")
    a = ap.parse_args(argv)

    from nse_quant_lab.holidays import is_trading_day

    d = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now().date()
    while not a.date and not is_trading_day(d):
        d -= dt.timedelta(days=1)
    if not is_trading_day(d):
        log.info("%s is not an NSE trading day: nothing to do.", d)
        return 0
    stats = panel_stats()
    panel_last = stats.attrs["panel_last"]
    if a.anchors:
        d = panel_last
    elif d <= panel_last:
        log.info("run date %s is not after the panel's last date %s: nothing to fetch (use --anchors to fetch the anchor bars).", d, panel_last)
        return 0
    isins = uf.symbol_isin_map(ARCH / "parquet", [panel_last.year - 1, panel_last.year])
    targets = uf.select_targets(stats, isins, min_adt=a.min_adt)
    if a.symbols:
        want = {s.strip() for s in a.symbols.split(",") if s.strip()}
        targets = targets[targets["symbol"].isin(want)]
    if a.limit:
        targets = targets.head(a.limit)
    log.info("run date %s | panel ends %s | %d targets (buffer >= Rs %.0f Cr)", d, panel_last, len(targets), a.min_adt / 1e7)
    if a.dry_run:
        print(targets[["symbol", "adt", "key"]].head(12).to_string(index=False))
        print(f"... {len(targets)} instruments; would request candles {panel_last} -> {d} for each (about {len(targets) * 0.17 / 60:.1f} min at the safe pace). No network call made.")
        return 0

    from nse_quant_lab.data_loader import auto_refresh_upstox_token
    from pymongo import MongoClient

    token = auto_refresh_upstox_token()
    if not token:
        log.error("no Upstox access token available (set UPSTOX_ACCESS_TOKEN, or the refresh-token variables).")
        return 2
    db = MongoClient(a.mongo_uri, serverSelectionTimeoutMS=5000)[a.db]
    col, flags = db[COLLECTION], db["corporate_action_flags"]
    col.create_index([("symbol", 1), ("date", 1)], unique=True)
    try:
        summary = run_ingestion(targets, d, panel_last, col, flags, lambda k, f, t: uf.fetch_daily_candles(k, f, t, token),
                                positions_col=db["positions"], orders_col=db["orders"])
    except uf.UpstoxAuthError as exc:
        log.error("%s", exc)
        return 2
    if a.anchors:  # compare Upstox's anchor-day close with the bhavcopy panel's close: verifies API shape and price basis
        diffs = []
        for r in targets.itertuples():
            doc = col.find_one({"symbol": r.symbol, "date": panel_last.isoformat()})
            if doc and r.last_close > 0:
                diffs.append(abs(float(doc["close"]) / r.last_close - 1.0))
        if diffs:
            ds = pd.Series(diffs)
            log.info("ANCHOR CHECK %s: %d symbols | median |close diff| %.2f%% | max %.1f%% | >0.5%%: %d | >5%% (would re-scale history): %d",
                     panel_last, len(ds), ds.median() * 100, ds.max() * 100, int((ds > 0.005).sum()), int((ds > 0.05).sum()))
        else:
            log.error("ANCHOR CHECK: no anchor bars were stored")
    covered = 1.0 - len(summary["failed"]) / max(summary["targets"], 1)
    log.info("saved %d bars | failed %d | flagged gaps %d | coverage %.1f%%", summary["saved_rows"], len(summary["failed"]), len(summary["flagged"]), covered * 100)
    if summary["flagged"]:
        log.warning("possible corporate actions (review): %s", summary["flagged"])
    if covered < COVERAGE_GATE:
        log.error("COVERAGE %.1f%% is below the %.0f%% gate: signals must not be generated from this data.", covered * 100, COVERAGE_GATE * 100)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
