"""
normalize_strategy_docs.py -- make the `strategies` documents consistent WITHOUT changing behaviour.

Default is a DRY RUN that only prints what it would do. `--apply` writes.
Rules (never overwrite an existing value unless listed under CORRECTIONS):
  FILL MISSING with the value the scripts already assume when the field is absent, so behaviour is unchanged:
      strategy_class -> the account id | max_entry_price -> None (no cap) | unaffordable_policy -> "TRY_NEXT"
      execution_approach -> "APPROACH_3" | rebalance_hold_rank_buffer -> 100 x slots for PIT strategies (1500 for 15 slots), 15 otherwise
  CORRECTIONS (explicit, listed in the dry run):
      forward-test accounts (id starts with `fwd_`): rebalance_hold_rank_buffer = 100 x portfolio_size, matching the research configuration.
ARCHIVED accounts are skipped. Only the `strategies` collection is touched.

Usage:  python scripts/normalize_strategy_docs.py            (dry run)
        python scripts/normalize_strategy_docs.py --apply
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.registry import strategy_class_of

PIT_CLASSES = ("trend_momentum_pit", "minervini_trend_template", "alpha_semivol_pit")


def planned_changes(doc: dict) -> dict:
    """Field -> new value for one strategy document (empty dict = already consistent)."""
    out = {}
    sid = doc["strategy_id"]
    cls = strategy_class_of(doc)
    slots = int(doc.get("portfolio_size") or (15 if cls in PIT_CLASSES else 6))
    defaults = {
        "strategy_class": sid,
        "max_entry_price": None,
        "unaffordable_policy": "TRY_NEXT",
        "execution_approach": "APPROACH_3",
        "rebalance_hold_rank_buffer": 100 * slots if cls in PIT_CLASSES else 15,
    }
    for k, v in defaults.items():
        if k not in doc:
            out[k] = v
    if sid.startswith("fwd_") and doc.get("rebalance_hold_rank_buffer") != 100 * slots:
        out["rebalance_hold_rank_buffer"] = 100 * slots
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    a = ap.parse_args()
    cfg = PaperTradingConfig(strategy_id="normalize")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    total = 0
    for doc in db.strategies.find({}):
        if doc.get("status") == "ARCHIVED":
            print(f"skip   {doc['strategy_id']} (ARCHIVED)")
            continue
        ch = planned_changes(doc)
        if not ch:
            print(f"ok     {doc['strategy_id']}: already consistent")
            continue
        total += 1
        before = {k: doc.get(k, "<missing>") for k in ch}
        print(f"{'APPLY' if a.apply else 'WOULD'} {doc['strategy_id']}: " + ", ".join(f"{k}: {before[k]} -> {v}" for k, v in ch.items()))
        if a.apply:
            db.strategies.update_one({"_id": doc["_id"]}, {"$set": {**ch, "updated_at": datetime.now(timezone.utc).isoformat()}})
    print(f"\n{total} document(s) {'updated' if a.apply else 'would change (dry run; re-run with --apply)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
