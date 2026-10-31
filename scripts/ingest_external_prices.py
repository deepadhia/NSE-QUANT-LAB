"""
ingest_external_prices.py -- Fill Survivorship Gaps from yfinance (with Validation)
==================================================================================
Subcommands:
  crosscheck  Compare yfinance against upstox_daily_ohlcv for N surviving stocks present in both.
              Read-only. Establishes whether yfinance is trustworthy enough to fill gaps.
  ingest      Fetch stocks that were index members but have no Upstox candles
              (pit_reconstitution_events.has_candles == False, or --symbols), validate each,
              and upsert ONLY validation-clean series into 'external_daily_ohlcv'.

Every run writes a JSON report to data/external/reports/. Failures, empty responses and
validation errors are listed per symbol; nothing is skipped silently.

Usage:
  python scripts/ingest_external_prices.py crosscheck --n 40
  python scripts/ingest_external_prices.py ingest --from-pit-gaps [--dry-run]
"""

import os
import sys
import json
import time
import hashlib
import argparse
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.external_prices import EXTERNAL_COLLECTION, fetch_yfinance, upsert_prices
from nse_quant_lab.price_validation import validate_ohlcv, compare_sources, drop_carried_forward_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("external_ingest")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPORT_DIR = os.path.join(PROJECT_ROOT, "data", "external", "reports")


def connect(mongo_uri: str) -> MongoDatabase:
    # Research data lives in the local DB; .env MONGO_URI may point at the production cluster.
    db = MongoDatabase(uri=mongo_uri)
    host = urlsplit(mongo_uri).hostname
    try:
        db.client.admin.command("ping")
    except Exception as exc:
        raise ConnectionError(f"MongoDB unreachable at host {host}: {type(exc).__name__}") from exc
    logger.info("Using MongoDB host %s, database %s", host, db.db_name)
    return db


def session_calendar(db: MongoDatabase) -> pd.DatetimeIndex:
    docs = db.db["benchmark_ohlcv"].find({"symbol": "^NSEI"}, {"_id": 0, "date": 1})
    cal = pd.DatetimeIndex(sorted(pd.to_datetime([d["date"] for d in docs])))
    if len(cal) < 1000:
        raise ValueError(f"Session calendar too short ({len(cal)} dates); check benchmark_ohlcv ^NSEI.")
    return cal


def load_upstox(db: MongoDatabase, symbol: str, start: str, end: str) -> pd.DataFrame:
    docs = db.ohlcv.find({"symbol": symbol, "date": {"$gte": start, "$lte": end}},
                         {"_id": 0, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
    df = pd.DataFrame(list(docs))
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
    return df


def write_report(kind: str, payload: dict) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(REPORT_DIR, f"{kind}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return path


def frame_sha256(df: pd.DataFrame) -> str:
    cols = ["date", "open", "high", "low", "close", "volume"]
    body = df[cols].assign(date=df["date"].dt.strftime("%Y-%m-%d")).to_csv(index=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def cmd_crosscheck(args) -> int:
    db = connect(args.mongo_uri)
    syms = sorted(s for s in db.ohlcv.distinct("symbol") if s.endswith(".NS"))
    step = max(1, len(syms) // args.n)
    sample = syms[::step][: args.n]
    results = []
    for i, sym in enumerate(sample, 1):
        up = load_upstox(db, sym, args.start, args.end)
        yf_df, status = fetch_yfinance(sym, args.start, args.end)
        row = {"symbol": sym, "yf_status": status, "upstox_rows": len(up), "yf_rows": len(yf_df)}
        if status == "OK" and not up.empty:
            row.update(compare_sources(up, yf_df, tolerance=args.tolerance))
            vol = up[["date", "close", "volume"]].merge(yf_df[["date", "close", "volume"]], on="date", suffixes=("_u", "_y"))
            to_u, to_y = vol["close_u"] * vol["volume_u"], vol["close_y"] * vol["volume_y"]
            row["turnover_ratio_median"] = round(float((to_y / to_u.replace(0, pd.NA)).dropna().median()), 4) if len(vol) else None
        else:
            row["status"] = "NO_COMPARISON"
        logger.info("[%d/%d] %s -> %s", i, len(sample), sym, row.get("status"))
        results.append(row)
        time.sleep(args.sleep)
    df = pd.DataFrame(results)
    summary = {
        "compared": int((df["status"].isin(["OK", "MISMATCH"])).sum()),
        "ok": int((df["status"] == "OK").sum()),
        "mismatch": int((df["status"] == "MISMATCH").sum()),
        "no_comparison": int((~df["status"].isin(["OK", "MISMATCH"])).sum()),
        "median_return_mismatch_pct": float(df.get("return_mismatch_pct", pd.Series(dtype=float)).median()),
    }
    path = write_report("crosscheck_yfinance_vs_upstox", {
        "window": [args.start, args.end], "tolerance": args.tolerance, "summary": summary, "results": results})
    print(json.dumps(summary, indent=2))
    print("report:", path)
    return 0


def cmd_ingest(args) -> int:
    db = connect(args.mongo_uri)
    cal = session_calendar(db)
    if args.symbols:
        syms = sorted(set(args.symbols))
    else:
        syms = sorted(db.db["pit_reconstitution_events"].distinct("symbol", {"has_candles": False}))
    if not syms:
        raise ValueError("No symbols to ingest.")
    col = db.db[EXTERNAL_COLLECTION]
    results = []
    for i, sym in enumerate(syms, 1):
        df, status = fetch_yfinance(sym, args.start, args.end)
        row = {"symbol": sym, "fetch_status": status, "rows": len(df)}
        if status == "OK":
            df, dropped = drop_carried_forward_rows(df)
            row["rows"] = len(df)
            row["dropped_placeholder_dates"] = dropped
            rep = validate_ohlcv(df, sym, calendar=cal)
            row["validation"] = rep
            row["sha256"] = frame_sha256(df)
            if rep["has_errors"]:
                row["outcome"] = "REJECTED_VALIDATION_ERROR"
            elif args.dry_run:
                row["outcome"] = "VALID_DRY_RUN"
            else:
                row["write"] = upsert_prices(col, df)
                row["outcome"] = "WRITTEN"
        else:
            row["outcome"] = "NO_DATA" if status == "EMPTY" else "FETCH_FAILED"
        logger.info("[%d/%d] %s -> %s (%d rows)", i, len(syms), sym, row["outcome"], len(df))
        results.append(row)
        time.sleep(args.sleep)
    outcomes = pd.Series([r["outcome"] for r in results]).value_counts().to_dict()
    path = write_report("ingest_yfinance", {
        "window": [args.start, args.end], "dry_run": args.dry_run, "collection": EXTERNAL_COLLECTION,
        "outcomes": outcomes, "results": results})
    print(json.dumps(outcomes, indent=2))
    for r in results:
        if r["outcome"] not in ("WRITTEN", "VALID_DRY_RUN"):
            print(f"  GAP {r['symbol']}: {r['outcome']} ({r['fetch_status']})")
    print("report:", path)
    return 1 if outcomes.get("FETCH_FAILED") else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("crosscheck", "ingest"):
        s = sub.add_parser(name)
        s.add_argument("--start", default="2015-01-01")
        s.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
        s.add_argument("--sleep", type=float, default=0.5, help="Seconds between symbols")
        s.add_argument("--mongo-uri", default="mongodb://localhost:27017/", help="Research DB (default: local)")
    sub.choices["crosscheck"].add_argument("--n", type=int, default=40)
    sub.choices["crosscheck"].add_argument("--tolerance", type=float, default=0.005)
    sub.choices["ingest"].add_argument("--from-pit-gaps", action="store_true", help="Default symbol source")
    sub.choices["ingest"].add_argument("--symbols", nargs="*", help="Explicit symbols, e.g. JETAIRWAYS.NS")
    sub.choices["ingest"].add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return cmd_crosscheck(args) if args.cmd == "crosscheck" else cmd_ingest(args)


if __name__ == "__main__":
    sys.exit(main())
