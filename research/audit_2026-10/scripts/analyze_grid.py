#!/usr/bin/env python3
"""Apply the pre-registered selection rule (PROTOCOL_2026-10-31.md) to grid_results.csv and print everything."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from alpha_lab import OUT  # noqa: E402

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
f = sys.argv[1] if len(sys.argv) > 1 else "grid_results.csv"
g = pd.read_csv(OUT / f)
if "error" in g.columns and g["error"].notna().any():
    print("ERRORS:\n", g[g.error.notna()][["kind", "uni", "regime", "slots", "error"]])
g = g[g.get("error").isna()] if "error" in g.columns else g
cols = ["kind", "uni", "regime", "slots", "trades", "FULL_cagr", "FULL_excess", "FULL_mdd", "FULL_t", "DEV_excess", "DEV_t",
        "VAL_excess", "LATE_excess", "LATE_cagr", "top10_share", "avg_hold_days", "forced_exits"]
print("N500 FULL cagr %.2f mdd %.1f | DEV %.2f | VAL %.2f | LATE %.2f" % (
    g.FULL_bench.iloc[0], g.FULL_bmdd.iloc[0], g.DEV_bench.iloc[0], g.VAL_bench.iloc[0], g.LATE_bench.iloc[0]))
print(g.sort_values(["kind", "uni", "regime", "slots"])[cols].round(2).to_string(index=False))
print("\nLATE excess > 0 in %d of %d configs; VAL excess > 0 in %d; DEV excess > 0 in %d" % (
    (g.LATE_excess > 0).sum(), len(g), (g.VAL_excess > 0).sum(), (g.DEV_excess > 0).sum()))
print("FULL CAGR distribution: min %.1f median %.1f max %.1f" % (g.FULL_cagr.min(), g.FULL_cagr.median(), g.FULL_cagr.max()))
fin = g[(g.DEV_t > 0) & (g.VAL_excess > 0) & (g.FULL_mdd >= g.FULL_bmdd - 15)]
print("\nFINALISTS (DEV t>0, VAL excess>0, maxDD within N500 DD -15pp... i.e. mdd >= bmdd-15):", len(fin))
print(fin.sort_values("FULL_mdd", ascending=False)[cols].round(2).to_string(index=False))
