#!/usr/bin/env python3
"""
Build the cleaned, split/bonus-adjusted daily panel used by the 2026-10-31 research.

Input : data/external/nse_archive/parquet/bhavcopy_*.parquet  (raw NSE bhavcopy, 2006 -> 2026-10-30)
        data/external/nse_archive/lists/symbolchange.csv      (ticker renames)
        data/external/nse_archive/all_corporate_actions.parquet (split / bonus factors)
Output: data/external/nse_archive/clean_panel.parquet         (one row per canonical symbol/date)
        research/audit_2026-10/results/clean_panel_events.csv (every adjustment applied, auditable)

Decisions (all logged, all documented in PROTOCOL_2026-10-31.md):
  1. Rename chains resolved date-aware to the latest ticker.
  2. One row per (symbol, date); series priority EQ > BE > BZ > SM; the chosen series is KEPT as a column
     so the strategy can restrict tradable series point-in-time.
  3. ETFs/MF units (ISIN 'INF...') and rights entitlements ('-RE', '-RE1', ...) are dropped: not stocks.
  4. Official split/bonus factors from the CA file applied backwards (prices x f, volume / f).
  5. RESIDUAL jumps (|close/prev| < 0.62 or > 1.6, gap <= 10 days) with no official event are treated as
     unadjusted corporate actions only when r <= 0.25 or r >= 4 AND r is within 10% of a clean split factor.
     Everything else is left alone (genuine crashes such as DHFL / YESBANK stay in the data).
Hindsight note: back-adjustment uses knowledge of splits that were public before their ex-date. That is how a
real holder experiences the event (share count changes, value does not) so it is not look-ahead for P&L.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
ARCH = REPO / "data" / "external" / "nse_archive"
OUT = ARCH / ("clean_panel_FAST_DEBUG.parquet" if os.environ.get("FAST") else "clean_panel.parquet")
EVENTS = REPO / "research" / "audit_2026-10" / "results" / "clean_panel_events.csv"

_down = {1 / k for k in range(2, 21)} | {k / (k + 1) for k in range(1, 11)}
CLEAN_FACTORS = np.array(sorted(_down | {1 / f for f in _down}))


def load_raw() -> pd.DataFrame:
    cols = ["symbol", "series", "open", "high", "low", "close", "volume", "turnover", "isin", "date"]
    frames = [pd.read_parquet(f, columns=cols) for f in sorted(glob.glob(str(ARCH / "parquet" / "bhavcopy_*.parquet")))]
    raw = pd.concat(frames, ignore_index=True)
    raw["date"] = pd.to_datetime(raw["date"])
    return raw


def load_changes() -> dict:
    ch = defaultdict(list)
    with open(ARCH / "lists" / "symbolchange.csv", encoding="latin-1") as f:
        for row in csv.reader(f):
            if len(row) >= 4 and row[1].strip() and row[1].strip() != "SM_KEY_SYMBOL":
                try:
                    when = pd.Timestamp(dt.datetime.strptime(row[3].strip(), "%d-%b-%Y"))
                except ValueError:
                    continue
                ch[row[1].strip()].append((when, row[2].strip()))
    for k in ch:
        ch[k].sort()
    return ch


def canonicalise(raw: pd.DataFrame, ch: dict) -> pd.Series:
    canon = raw["symbol"].copy()
    for old in ch:
        m = raw["symbol"] == old
        if not m.any():
            continue
        d = raw.loc[m, "date"]
        res = pd.Series(old, index=d.index)
        for _ in range(8):
            nxt, changed = res.copy(), False
            for sym in res.unique():
                for w, n in ch.get(sym, []):
                    sel = (res == sym) & (d < w)
                    if sel.any():
                        nxt[sel] = n
                        changed = True
            res = nxt
            if not changed:
                break
        canon.loc[m] = res
    return canon


def nearest_clean(r: float) -> float:
    return float(CLEAN_FACTORS[np.argmin(np.abs(np.log(CLEAN_FACTORS) - np.log(r)))])


def main() -> None:
    cache = ARCH / "raw_canon_cache.parquet"
    newest_src = max([f.stat().st_mtime for f in (ARCH / "parquet").glob("bhavcopy_*.parquet")] + [(ARCH / "lists" / "symbolchange.csv").stat().st_mtime])
    if cache.exists() and cache.stat().st_mtime >= newest_src and not os.environ.get("FAST"):
        raw = pd.read_parquet(cache)
    else:
        raw = load_raw()
        raw["canon"] = raw["symbol"] if os.environ.get("FAST") else canonicalise(raw, load_changes())
        if not os.environ.get("FAST"):
            raw.to_parquet(cache)
    print("raw rows", len(raw))

    pri = {"EQ": 0, "BE": 1, "BZ": 2, "SM": 3}
    raw["p"] = raw["series"].map(pri)
    raw = raw.sort_values(["canon", "date", "p", "volume"], ascending=[True, True, True, False])
    raw = raw.drop_duplicates(["canon", "date"])
    ok = (raw.open > 0) & (raw.high > 0) & (raw.low > 0) & (raw.close > 0) & (raw.high >= raw.low) & (raw.volume >= 0)
    ok &= (raw.high >= raw.close - 1e-9) & (raw.high >= raw.open - 1e-9) & (raw.low <= raw.close + 1e-9) & (raw.low <= raw.open + 1e-9)
    print("dropped invalid OHLC rows:", int((~ok).sum()))
    raw = raw[ok]

    isin_last = raw[raw["isin"].fillna("") != ""].groupby("canon")["isin"].last()
    inf_syms = set(isin_last[isin_last.str.startswith("INF")].index)
    re_syms = {s for s in raw.canon.unique() if "-RE" in s or s.endswith("-RE")}
    print("dropping ETF/MF symbols:", len(inf_syms), "rights-entitlement symbols:", len(re_syms))
    raw = raw[~raw.canon.isin(inf_syms | re_syms)]

    ca = pd.read_parquet(ARCH / "all_corporate_actions.parquet")
    ca["ex_date"] = pd.to_datetime(ca["ex_date"])
    ca = ca[ca.kind.isin(["split", "bonus"]) & (ca.price_factor < 0.99)]
    ca = ca.groupby(["symbol", "ex_date"]).price_factor.prod().reset_index()
    ca_by = {s: g for s, g in ca.groupby("symbol")}

    events, out = [], []
    for sym, g in raw.groupby("canon", sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        n = len(g)
        d = g["date"].values
        cum = np.ones(n)
        for e in (ca_by[sym].itertuples() if sym in ca_by else []):
            k = int(np.searchsorted(d, np.datetime64(e.ex_date), side="left"))
            if 0 < k < n:
                cum[:k] *= e.price_factor
                events.append((sym, e.ex_date.date(), "official", e.price_factor, "", ""))
        c = g["close"].values * cum
        vol = g["volume"].values / cum
        gapd = np.diff(d).astype("timedelta64[D]").astype(int)
        r = np.full(n, np.nan)
        r[1:] = c[1:] / c[:-1]
        cand = np.where((gapd <= 10) & ((r[1:] < 0.62) | (r[1:] > 1.6)))[0] + 1
        for k in cand:
            rk = r[k]
            before = vol[max(0, k - 21):k - 1]
            after = vol[k + 1:k + 21]
            f, rule = None, None
            fc = nearest_clean(rk)
            # Only extreme one-day moves that sit on a clean split/consolidation factor are treated as
            # unadjusted corporate actions. A volume-persistence rule (tried first) mislabelled genuine crashes
            # (YESBANK 2020-03-06, SUZLON 2008-10-24) and erratic illiquid prints (BIRLACOT, VISESHINFO) so it
            # was dropped; milder unexplained jumps are left in the data and disclosed.
            if (rk <= 0.25 or rk >= 4.0) and abs(np.log(rk / fc)) < np.log(1.10):
                f, rule = fc, "B_extreme_clean"
            if f is not None:
                cum[:k] *= f
                c[:k] *= f
                vol[:k] /= f
                events.append((sym, pd.Timestamp(d[k]).date(), rule, f, round(float(rk), 4), ""))
                # re-derive r for later candidates
                r[k] = c[k] / c[k - 1]
        g2 = pd.DataFrame({
            "symbol": sym, "date": g["date"], "series": g["series"],
            "open": g["open"].values * cum, "high": g["high"].values * cum,
            "low": g["low"].values * cum, "close": g["close"].values * cum,
            "volume": g["volume"].values / cum, "turnover": g["turnover"].values,
        })
        out.append(g2)
    panel = pd.concat(out, ignore_index=True)
    for col in ("open", "high", "low", "close"):
        panel[col] = panel[col].astype("float64")
    panel["volume"] = panel["volume"].astype("float64")
    panel["series"] = panel["series"].astype("category")
    panel.to_parquet(OUT)
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(events, columns=["symbol", "date", "source", "factor", "observed_ratio", "note"]).to_csv(EVENTS, index=False)
    ev = pd.DataFrame(events, columns=["symbol", "date", "source", "factor", "observed_ratio", "note"])
    print("panel rows", len(panel), "symbols", panel.symbol.nunique())
    print(ev.source.value_counts().to_string())


if __name__ == "__main__":
    main()
