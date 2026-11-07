#!/usr/bin/env python3
"""
minervini_rs_pool_check.py -- does the Minervini relative-strength percentile pool differ between backtest and live, and does it matter?

Background: MinerviniStrategy ranks 1-year return as a cross-sectional PERCENTILE (entry needs >= 70) over every symbol that has a bar on the decision
date. Backtest: that is the whole clean panel (3,402 names incl. illiquid). Live: bars after the panel's end exist only for the ingestion tier
(EQ, 50-session median turnover >= Rs 3 Cr, >= 200 sessions; scripts/ingest_upstox_fulltier.py / upstox_feed.select_targets), so the live pool is
that tier. This script measures, at every month-end, how the production strategy's entry list changes when the pool is restricted to the tier.

Method (pre-declared, read-only, no tuning): production MinerviniStrategy (use_regime=False so only the POOL differs, not the regime), indicators
exactly as the engine computes them; for each month-end 2016-01..2026-09 compute select_entry_candidates on (a) the full panel and (b) the
tier-restricted pool; compare the top-15 lists (what would be bought into 15 empty slots) and the number of qualifying names.
Outputs: results/minervini_rs_pool/{monthly.csv,summary.txt}. Does NOT change any rule.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from nse_quant_lab.config import BacktestConfig  # noqa: E402
from nse_quant_lab.data_loader import load_pit_universe  # noqa: E402
from nse_quant_lab.strategies.minervini import MinerviniStrategy  # noqa: E402

OUT = REPO / "research" / "audit_2026-10" / "results" / "minervini_rs_pool"
END = "2026-10-30"
TIER_ADT, TIER_HIST = 3.0e7, 200      # == ingestion tier (select_targets defaults)


def tier_pool(ind: dict, d: pd.Timestamp) -> dict:
    keep = {}
    for sym, df in ind.items():
        k = int(np.searchsorted(df["date"].values, np.datetime64(d)))
        if k >= len(df) or df["date"].values[k] != np.datetime64(d):
            continue
        r = df.iloc[k]
        if str(r["series"]) == "EQ" and float(r["adt50m"]) >= TIER_ADT and int(r["hist_n"]) >= TIER_HIST:
            keep[sym] = df
    return keep


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = BacktestConfig(initial_capital=100_000.0, portfolio_size=15)
    strat = MinerviniStrategy(config=cfg, use_regime=False)
    uni = load_pit_universe(END, include_live=False)
    ind = {s: strat.compute_indicators(df, cfg) for s, df in uni.items()}
    ind = {s: d for s, d in ind.items() if not d.empty}
    timeline = sorted({d for df in ind.values() for d in df["date"]})
    s = pd.Series(timeline)
    month_ends = s.groupby(s.dt.to_period("M")).max()
    month_ends = [d for d in month_ends if pd.Timestamp("2016-01-01") <= d <= pd.Timestamp("2026-09-30")]
    rows = []
    for d in month_ends:
        full = strat.select_entry_candidates(ind, d, {}, cfg)
        pool = tier_pool(ind, d)
        tier = strat.select_entry_candidates(pool, d, {}, cfg)
        a, b = list(full.head(15).index), list(tier.head(15).index)
        rows.append({"date": d.date(), "pool_full": sum(1 for df in ind.values() if d in set()) or None, "pool_tier": len(pool),
                     "qualifiers_full": len(full), "qualifiers_tier": len(tier), "top15_full": " ".join(a), "top15_tier": " ".join(b),
                     "overlap": len(set(a) & set(b)), "n_compared": min(len(a), len(b)) if (a and b) else 0,
                     "top5_same": len(set(a[:5]) & set(b[:5]))})
    df = pd.DataFrame(rows).drop(columns=["pool_full"])
    df.to_csv(OUT / "monthly.csv", index=False)
    ok = df[(df.qualifiers_full > 0) | (df.qualifiers_tier > 0)]
    both = ok[(ok.qualifiers_full >= 15) & (ok.qualifiers_tier >= 15)]
    lines = [
        f"month-ends compared: {len(df)}; with any qualifier: {len(ok)}; with >=15 qualifiers in both pools: {len(both)}",
        f"median qualifiers  full={ok.qualifiers_full.median():.0f}  tier={ok.qualifiers_tier.median():.0f}",
        f"mean top-15 overlap (months with >=15 qualifiers in both): {both.overlap.mean():.1f} of 15 ({both.overlap.mean() / 15 * 100:.0f}%)",
        f"mean top-5 overlap: {both.top5_same.mean():.1f} of 5",
        f"months where the lists share <= 10 of 15: {(both.overlap <= 10).sum()} of {len(both)}",
        f"months where the tier pool yields fewer qualifiers than full: {(ok.qualifiers_tier < ok.qualifiers_full).sum()} of {len(ok)}",
    ]
    (OUT / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
