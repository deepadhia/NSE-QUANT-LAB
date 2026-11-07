#!/usr/bin/env python3
"""
approach_ablation_real_engine.py -- execution-approach ablation through the REAL BacktestEngine (no re-implemented loop).

Why: research/audit_2026-10/scripts/ab_execution_approaches.py (22.18% for Approach 3) used a copy of the engine loop and did not reproduce
(real engine: 20.50%). This script runs the production strategy + production engine, varying only `execution_approach`, and, for two ablations,
patches the TEXT of src/nse_quant_lab/engine.py in memory (no file is written to src/):
  noslot      : remove the equal-slot cash rule (`slot_cash_share = cash`)
  noslot_any  : additionally schedule the T+2 catch-up whenever a slot is empty (not only after a skipped order)
Usage:  python approach_ablation_real_engine.py OUT_DIR [baseline a1a a1b a3 a3_noslot a3_noslot_any ...]
Pre-declared: one run per variant, same config as minervini_20y.py, 2007-01-01..2026-10-30, post-tax, tiered costs. Results are path-dependent:
differences of ~1 pp between variants are within noise (the a3 vs a3_noslot gap is mostly the CUPID position size) and must not be used to tune.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from pymongo import MongoClient

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from nse_quant_lab.config import BacktestConfig  # noqa: E402
from nse_quant_lab.data_loader import load_pit_universe  # noqa: E402
from nse_quant_lab.engine import BacktestEngine  # noqa: E402
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy  # noqa: E402

START, END = "2007-01-01", "2026-10-30"
SLOT = 'slot_cash_share = cash / float(open_slots_remain) if approach in ("APPROACH_1B", "APPROACH_3") else cash'
ANY = "                    and had_rebal_skips\n"
VARIANTS = {  # name -> (execution_approach, patches)
    "baseline": ("BASELINE", []), "a1a": ("APPROACH_1A", []), "a1b": ("APPROACH_1B", []), "a3": ("APPROACH_3", []),
    "a3_noslot": ("APPROACH_3", [(SLOT, "slot_cash_share = cash")]),
    "a3_noslot_any": ("APPROACH_3", [(SLOT, "slot_cash_share = cash"), (ANY, "")]),
}


def engine_class(patches, tag):
    if not patches:
        return BacktestEngine
    src = (REPO / "src" / "nse_quant_lab" / "engine.py").read_text(encoding="utf-8")
    for old, new in patches:
        if old not in src:
            raise RuntimeError(f"patch target not found in engine.py (engine changed?): {old[:60]!r}")
        src = src.replace(old, new)
    path = Path(sys.argv[1]) / f"_engine_{tag}.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"nse_quant_lab._engine_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.BacktestEngine


def main():
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    names = sys.argv[2:] or list(VARIANTS)
    col = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)["stock_scanner"]["benchmark_ohlcv"]
    b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    b["date"] = pd.to_datetime(b["date"])
    bench = b.drop_duplicates("date", keep="last").sort_values("date").set_index("date")["close"]
    uni = load_pit_universe(END, include_live=False)
    for name in names:
        approach, patches = VARIANTS[name]
        cfg = BacktestConfig(
            initial_capital=100_000.0, portfolio_size=15, max_open_positions=15, rebalance_hold_rank_buffer=1500, rebalance_frequency="MONTHLY",
            min_turnover_50d=0.0, min_price=0.0, slippage_pct=0.002, unaffordable_policy="TRY_NEXT", enforce_pit_universe=False,
            pit_top_n_liquidity=0, brokerage_per_order=0.0, dp_charge_sell=14.75, max_volume_participation_pct=0.10, deduct_annual_tax=True,
            tax_model="india_fy", execution_approach=approach)
        t0 = time.time()
        metrics, trades, _o, _e, nav = engine_class(patches, name)(TrendMomentumPITStrategy(config=cfg, benchmark=bench), cfg).run(
            uni, eval_start_date=START, eval_end_date=END)
        nav["date"] = pd.to_datetime(nav["date"]); s = nav.set_index("date")["nav"]
        yrs = (s.index[-1] - s.index[0]).days / 365.25
        row = {"variant": name, "approach": approach, "trades": len(trades), "final_nav": round(float(s.iloc[-1])),
               "cagr": round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 2), "mdd": round(float((s / s.cummax() - 1).min()) * 100, 1),
               "secs": round(time.time() - t0)}
        s.to_csv(out / f"nav_{name}.csv"); trades.to_csv(out / f"trades_{name}.csv", index=False)
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
