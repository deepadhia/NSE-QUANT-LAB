#!/usr/bin/env python3
"""
Run Finance-Guru Main v3.2 through Finance-Guru's own PIT harness on OUR panel (read-only; nothing is written to any DB).
  python main_v32_on_our_data.py smoke 60      # time + sanity on 60 symbols
  python main_v32_on_our_data.py run [workers] # full run, signals cached, results printed + saved
Pre-registered in PROTOCOL_2026-10-31.md (Addendum 5). Note: importing Finance-Guru modules creates a log file under Finance-Guru/logs.
"""
import json
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
FG = Path(r"D:\Personal Projects\Finance-Guru")
PANEL = REPO / "data" / "external" / "nse_archive" / "clean_panel.parquet"
OUT = REPO / "research" / "audit_2026-10" / "results" / "main_v32_2026-10-31"
DATA_START = pd.Timestamp("2006-04-01")
IPO_MAX_AGE_DAYS = 24 * 30


def build_frames():
    p = pd.read_parquet(PANEL)
    p["series"] = p["series"].astype(str)
    frames = {}
    for sym, g in p.groupby("symbol", sort=False):
        g = g.sort_values("date")
        first = g["date"].iloc[0]
        if first <= DATA_START:
            continue
        g = g[(g["date"] - first).dt.days <= IPO_MAX_AGE_DAYS]
        g = g[g["series"].isin(["EQ", "BE"])]
        if len(g) < 80:
            continue
        f = g.assign(date=pd.to_datetime(g["date"]).dt.normalize()).drop_duplicates("date").set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
        frames[sym] = f
    return frames


def build_nifty():
    from pymongo import MongoClient
    col = MongoClient("mongodb://localhost:27017/")["stock_scanner"]["benchmark_ohlcv"]
    n = pd.DataFrame(list(col.find({"symbol": "^NSEI"}, {"_id": 0, "date": 1, "close": 1})))
    n["date"] = pd.to_datetime(n["date"]).dt.normalize()
    n = n.drop_duplicates("date").sort_values("date").set_index("date")[["close"]]
    n["50_dma"] = n["close"].rolling(50, min_periods=50).mean()
    return n


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    sys.path.insert(0, str(FG))
    os.chdir(FG)  # their modules use repo-relative paths for logs
    import utils.pit_backtest as pb
    from utils.alpha import alpha_metrics
    import scripts.pit_backtest_main as pm
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    frames, nifty = build_frames(), build_nifty()
    print(f"frames: {len(frames)} IPO symbols (<=24 months of bars each), nifty {nifty.index.min().date()} -> {nifty.index.max().date()} ({time.time()-t0:.0f}s)", flush=True)
    if mode == "smoke":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        syms = sorted(frames, key=lambda s: -len(frames[s]))[:n]
        t1 = time.time(); found = 0
        for s in syms:
            found += len(pb.signals_for_symbol(s, frames[s], nifty, 400))
        dt = time.time() - t1
        print(f"smoke: {n} symbols, {found} signals, {dt:.0f}s ({dt/n:.1f}s/symbol) -> full run single-core est {dt/n*len(frames)/60:.0f} min")
        return
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    cache = OUT / "signals.pkl"
    if cache.exists():
        sigs = pickle.load(open(cache, "rb"))
    else:
        t1 = time.time()
        sigs = pb.generate_signals_parallel(frames, nifty, window=400, start=None, workers=workers, progress=pm.progress)
        pickle.dump(sigs, open(cache, "wb"))
        print(f"generated {len(sigs)} signals in {time.time()-t1:.0f}s", flush=True)
    errs = [s for s in sigs if "error" in s]
    good = [s for s in sigs if "error" not in s]
    print(f"signals {len(good)} ({len(errs)} symbol errors) | per year:", pd.Series([s['signal_date'].year for s in good]).value_counts().sort_index().to_dict(), flush=True)
    closes, bench = pm.closes_wide(frames), pm.benchmark_series(frames)
    bars_cache, results, summary = {}, {}, {"signals": len(good), "errors": len(errs)}
    for label, gate in (("PRIMARY: no Nifty gate", False), ("VARIANT 2: Nifty > 50DMA gate", True)):
        trades = pb.simulate_portfolio(sigs, frames, bars_cache, slots=15, nifty_gate=gate, circuit_aware=True)
        stats, book, m = pm.evaluate(label, trades, frames, closes, bench, 15, 1.0)
        pm.print_result(label, stats, m)
        pm.by_year(trades, frames, 1.0)
        pd.DataFrame(trades).to_csv(OUT / f"trades_{'gate' if gate else 'nogate'}.csv", index=False)
        results[label] = (trades, stats, m)
        summary[label] = {"stats": stats, "metrics": {k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in (m or {}).items()}}
        ex = [t for t in trades if not (2021 <= t["entry_date"].year <= 2024)]
        summary[label]["ex_2021_2024"] = pb.trade_stats(ex, frames, cost_pct=1.0)
        for c in (0.5, 1.5, 2.5):
            s2, _, m2 = pm.evaluate("cost", trades, frames, closes, bench, 15, c)
            summary[label][f"cost_{c}"] = {"avg": s2["avg"], "pf": s2["pf"], "excess_matched_ann": (m2 or {}).get("excess_vs_matched_ann_pct"), "t": (m2 or {}).get("t_excess_matched")}
            print(f"  cost {c}%: avg {s2['avg']:+.2f}% PF {s2['pf']:.2f} excess vs matched {(m2 or {}).get('excess_vs_matched_ann_pct', float('nan')):+.0f}%/yr t={(m2 or {}).get('t_excess_matched', float('nan')):+.2f}", flush=True)
    from scripts.pit_random_control import random_control
    print("\nRANDOM-ENTRY CONTROL (100 books, same exits) for PRIMARY:", flush=True)
    random_control(results["PRIMARY: no Nifty gate"], frames, closes, bench, bars_cache, 15, 1.0, 100)
    (OUT / "main_summary.json").write_text(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
