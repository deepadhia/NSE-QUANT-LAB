"""
report_fill_audit.py -- READ-ONLY fill-quality report from the `fill_audit` collection (one record per paper fill, written at fill time).

What a paper fill audit CAN tell you
  * how the modelled fill price compares with the live quote at that moment (bid/ask spread, last price, the quote's own open);
  * how much of each slot the whole-share rounding left uninvested;
  * how those differ by price bucket and by account (e.g. capped vs uncapped, 10 vs 15 slots).
What it CANNOT tell you
  * what a real order would have received (queue position, partial fills, price improvement). Paper fills are assumed at the quoted
    ask (buys) / bid (sells) plus the model's slippage. Only real fills can validate that.

Per fill (all in basis points, positive = worse for us):
  slip_vs_exec  : fill vs the executable quote side used (ask for BUY, bid for SELL) = the model's extra slippage
  vs_quote_open : fill vs the quote's own opening print
  vs_ltp        : fill vs the last traded price
  half_spread   : half the quoted bid/ask spread
  utilisation   : gross value / (initial capital / slots), i.e. how much of the slot was actually invested
  bigger_than_quoted_qty_% : share of fills whose size exceeded the quantity displayed at the quoted price (a real order would walk the book;
                  recorded only for fills made after the depth-quantity capture was deployed)

Usage:   python scripts/report_fill_audit.py [--since YYYY-MM-DD] [--strategy ID]
"""
import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.db.mongo_client import MongoDatabase

BUCKETS = [(0, 100, "< Rs 100"), (100, 500, "Rs 100-500"), (500, 1000, "Rs 500-1000"), (1000, 1e12, ">= Rs 1000")]


def build_frame(db, since, strategy):
    q = {"date": {"$gte": since}} if since else {}
    if strategy:
        q["strategy_id"] = strategy
    docs = list(db.db["fill_audit"].find(q, {"_id": 0}))
    strat = {s["strategy_id"]: s for s in db.strategies.find({}, {"strategy_id": 1, "initial_capital": 1, "portfolio_size": 1})}
    rows = []
    for d in docs:
        qt = d.get("quote") or {}
        sign = 1 if d.get("side") == "BUY" else -1
        fill = float(d["fill_price"])
        exec_px = float(d.get("open_price") or 0.0)
        ltp, qopen = float(qt.get("ltp") or 0.0), float(qt.get("open") or 0.0)
        bid, ask = qt.get("bid"), qt.get("ask")
        s = strat.get(d["strategy_id"], {})
        slot = float(s.get("initial_capital") or 0.0) / max(int(s.get("portfolio_size") or 1), 1)
        gross = fill * float(d["shares"])
        rows.append({
            "strategy": d["strategy_id"], "date": d["date"], "symbol": d["symbol"], "side": d["side"], "shares": int(d["shares"]),
            "fill": fill, "gross": gross, "has_quote": bool(qt),
            "slip_vs_exec": (fill / exec_px - 1) * 1e4 * sign if exec_px > 0 else np.nan,
            "vs_quote_open": (fill / qopen - 1) * 1e4 * sign if qopen > 0 else np.nan,
            "vs_ltp": (fill / ltp - 1) * 1e4 * sign if ltp > 0 else np.nan,
            "half_spread": (ask - bid) / (ask + bid) * 1e4 if (ask and bid and ask > 0 and bid > 0) else np.nan,
            "utilisation": gross / slot if (slot > 0 and d["side"] == "BUY") else np.nan,
            "depth_shortfall": (1.0 if d.get("depth_shortfall") else 0.0) if "depth_shortfall" in d else np.nan,
        })
    return pd.DataFrame(rows)


def summarise(df):
    def agg(g):
        return pd.Series({
            "fills": len(g), "with_quote": int(g.has_quote.sum()),
            "slip_vs_exec_bps": g.slip_vs_exec.mean(), "vs_quote_open_bps": g.vs_quote_open.mean(), "vs_ltp_bps": g.vs_ltp.mean(),
            "half_spread_bps": g.half_spread.mean(), "utilisation_%": g.utilisation.mean() * 100,
            "worse_than_ltp_%": (g.vs_ltp > 0).mean() * 100 if g.vs_ltp.notna().any() else np.nan,
            "bigger_than_quoted_qty_%": g.depth_shortfall.mean() * 100 if g.depth_shortfall.notna().any() else np.nan,
        })
    return df.groupby("strategy").apply(agg).round(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--strategy", default=None)
    a = ap.parse_args()
    cfg = PaperTradingConfig(strategy_id="report")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    df = build_frame(db, a.since, a.strategy)
    if df.empty:
        print("No fill_audit records for that selection yet (fills appear after the 09:16 run).")
        return 0
    pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)
    print(f"{len(df)} fill records, {df.date.min()} .. {df.date.max()}  (positive bps = worse for us)\n")
    print("PER ACCOUNT"); print(summarise(df).to_string())
    buys = df[df.side == "BUY"].copy()
    if len(buys):
        buys["bucket"] = pd.cut(buys.fill, [b[0] for b in BUCKETS] + [BUCKETS[-1][1]], labels=[b[2] for b in BUCKETS], right=False)
        print("\nBUYS BY PRICE BUCKET (all accounts)")
        print(buys.groupby("bucket", observed=True).agg(fills=("fill", "size"), slip_bps=("slip_vs_exec", "mean"), half_spread_bps=("half_spread", "mean"),
              utilisation_pct=("utilisation", lambda x: x.mean() * 100)).round(1).to_string())
    print("\nREADING THIS: slip_vs_exec is the model's extra cost on top of the quoted ask/bid. half_spread is what crossing the spread alone costs; if slip_vs_exec"
          " is well above it, the paper fills are conservative. utilisation < 100% is money left idle by whole-share rounding. Paper results cannot confirm real fills.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
