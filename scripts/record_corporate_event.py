#!/usr/bin/env python3
"""
record_corporate_event.py -- record an evidenced merger / delisting / cash-out for a held name.

Use this when the daily pipeline keeps reporting `MISSING_HELD_BAR` for a name that will NOT come back (merged,
delisted, long suspension resolved by a cash-out). Missing data alone never sells or writes down a position; a recorded,
evidenced event is the only way. The event is stored in MongoDB (`corporate_events`) and applied exactly once per
account by the next `run_daily_pipeline.py` run (the scheduler retries do this automatically).

PREVIEW is the default (nothing is written). Add --apply to store the event.

IMPORTANT on --effective-date:
  The effective date MUST be the trading cessation / ex-date on NSE when market bars stopped appearing,
  NOT the retroactive NCLT scheme 'Appointed Date'. A symbol still trading on or after the effective date
  will be rejected loudly by the manager's contradicted-event guard.

Examples
  # Delisted / cash-out at Rs 12.50 net per share (use --cash-per-share 0 ONLY for an evidenced total write-off):
  python scripts/record_corporate_event.py --type CASH_OUT --symbol XYZ --effective-date 2026-11-20 \
      --cash-per-share 12.5 --evidence "NSE circular NSE/CML/12345 dated 2026-11-10"

  # Merger: 187 new shares of NEWCO per 100 old shares of OLDCO; fractions paid at Rs 90 per new share
  # (state --fraction-cash-per-new-share explicitly, 0 if the scheme pays nothing; it is required if a fraction arises):
  python scripts/record_corporate_event.py --type CONVERSION --symbol OLDCO --new-symbol NEWCO --ratio 1.87 \
      --effective-date 2026-12-30 --fraction-cash-per-new-share 90 --evidence "Scheme order NCLT/... ; NSE circular ..."

  python scripts/record_corporate_event.py --list
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.corporate_events import (CASH_OUT, CONVERSION, COLLECTION, EVENT_TYPES, build_event,
                                            split_whole_and_fraction, validate_event)
from nse_quant_lab.db.mongo_client import MongoDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("record_corporate_event")

_COMPARED = ("symbol", "type", "effective_date", "cash_per_share", "new_symbol", "ratio", "fraction_cash_per_new_share")


def preview(db, doc: dict) -> int:
    """Print what the event would do to every account holding the symbol. Returns the number of affected positions."""
    holders = list(db.positions.find({"symbol": doc["symbol"]}))
    print("=" * 78)
    print(f"EVENT {doc['event_id']}")
    print(f"  evidence: {doc['evidence']}")
    affected = 0
    if not holders:
        print("  No account currently holds this symbol: nothing would change.")
    for p in holders:
        sid, mode, shares = p.get("strategy_id"), p.get("mode"), int(p.get("shares", 0))
        entry = str(p.get("entry_date", ""))[:10]
        if doc["effective_date"] <= entry:
            print(f"  [{sid}/{mode}] {shares} sh entered {entry} on/after the effective date: NOT affected.")
            continue
        affected += 1
        if doc["type"] == CASH_OUT:
            print(f"  [{sid}/{mode}] {shares} sh -> cash Rs {shares * float(doc['cash_per_share']):,.2f}; position closed, "
                  f"trade recorded (EVENT_CASH_OUT).")
        else:
            whole, frac = split_whole_and_fraction(shares, float(doc["ratio"]))
            fcs = doc.get("fraction_cash_per_new_share")
            problem = ""
            if frac > 0 and fcs is None:
                problem = "  ** BLOCKED: fraction arises and --fraction-cash-per-new-share was not given **"
            elif whole < 1:
                problem = "  ** BLOCKED: no whole share results; record a CASH_OUT instead **"
            in_lieu = round(frac * float(fcs or 0.0), 2)
            print(f"  [{sid}/{mode}] {shares} {doc['symbol']} -> {whole} {doc['new_symbol']} (fraction {frac:.4f}, cash in lieu "
                  f"Rs {in_lieu:,.2f}); cost basis and entry date carried over.{problem}")
    print("=" * 78)
    return affected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list recorded events and exit")
    ap.add_argument("--type", choices=EVENT_TYPES)
    ap.add_argument("--symbol")
    ap.add_argument("--effective-date", help="YYYY-MM-DD trading cessation/ex-date on NSE when bars stop appearing (NOT retroactive NCLT 'Appointed Date'; applied once pipeline reaches it)")
    ap.add_argument("--evidence", help="circular number / URL / filing reference (required, >= 10 chars)")
    ap.add_argument("--cash-per-share", type=float, help="CASH_OUT: NET cash per share (0 = evidenced write-off)")
    ap.add_argument("--new-symbol", help="CONVERSION: the symbol the shares become")
    ap.add_argument("--ratio", type=float, help="CONVERSION: new shares per 1 old share (100 old -> 187 new = 1.87)")
    ap.add_argument("--fraction-cash-per-new-share", type=float, help="CONVERSION: cash per NEW share for a fractional entitlement (0 if none)")
    ap.add_argument("--apply", action="store_true", help="store the event (default: preview only)")
    args = ap.parse_args()

    cfg = PaperTradingConfig(strategy_id="system")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    if not db.ping():
        logger.error("Cannot connect to MongoDB at %s.", cfg.mongo_uri)
        return 1
    coll = db.get_collection(COLLECTION)

    if args.list:
        for e in coll.find().sort("effective_date", -1).limit(100):
            print({k: v for k, v in e.items() if k != "_id"})
        return 0

    if not (args.type and args.symbol and args.effective_date and args.evidence):
        ap.error("--type, --symbol, --effective-date and --evidence are required")
    try:
        doc = build_event(args.type, args.symbol, args.effective_date, args.evidence,
                          cash_per_share=args.cash_per_share, new_symbol=args.new_symbol, ratio=args.ratio,
                          fraction_cash_per_new_share=args.fraction_cash_per_new_share)
        validate_event(doc)
    except ValueError as exc:
        logger.error("Invalid event: %s", exc)
        return 2

    existing = coll.find_one({"event_id": doc["event_id"]})
    if existing and any(existing.get(k) != doc.get(k) for k in _COMPARED):
        logger.error("An event with id %s already exists with DIFFERENT terms; refusing to overwrite. Existing: %s",
                     doc["event_id"], {k: existing.get(k) for k in _COMPARED})
        return 3

    preview(db, doc)
    if existing:
        print("Identical event already recorded; nothing to do.")
        return 0
    if not args.apply:
        print("PREVIEW ONLY. Re-run with --apply to record it.")
        return 0

    try:
        coll.create_index([("event_id", 1)], unique=True)
    except Exception as exc:                                    # index is a safety net, not a precondition
        logger.warning("could not create unique index on %s.event_id: %s", COLLECTION, exc)
    coll.insert_one(doc)
    print(f"RECORDED {doc['event_id']}.")
    print("It is applied once per account by the next daily pipeline run (scheduler retry, or "
          "`python scripts/run_daily_pipeline.py --strategy <id>`). A strategy that is PAUSED/ARCHIVED applies it when reactivated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
