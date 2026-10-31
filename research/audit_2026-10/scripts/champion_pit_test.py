#!/usr/bin/env python3
"""
champion_pit_test.py -- Finance-Guru "Champion v1.1 High-TP DNA" rules replayed on our 20-year full-market panel.
Pre-registered in PROTOCOL_2026-10-31.md (Addendum 4). Rules copied from Finance-Guru (read, not executed); no threshold is tuned.

  python champion_pit_test.py            # full run -> results/champion_2026-10-31/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from nse_quant_lab.config import BacktestConfig  # noqa: E402
from nse_quant_lab.cost_model import slippage_fraction  # noqa: E402
from nse_quant_lab.execution import is_locked_down, is_locked_up  # noqa: E402
from nse_quant_lab.fees import calc_buy_fees_exact, calc_sell_fees_exact  # noqa: E402

PANEL = REPO / "data" / "external" / "nse_archive" / "clean_panel.parquet"
OUT = REPO / "research" / "audit_2026-10" / "results" / "champion_2026-10-31"
DATA_START = pd.Timestamp("2006-04-01")   # symbols already trading at the start of the data are not IPOs
IPO_MAX_AGE_DAYS = 24 * 30
SL_PCT, TP_PCT = -0.10, 0.25
NOTIONAL = 10_000.0                        # Rs per trade for the cost model / integer shares (small-account scale)
CFG = BacktestConfig()
AS_CODED_COST = 0.01                       # Finance-Guru's research convention: 1% round trip


# ----------------------------------------------------------------------------- exits (pure, unit-tested)
def simulate_exit(o, h, l, c, start: int, sl: float, tp: float, locked_check: bool = False) -> Tuple[int, float, str]:
    """Scan bars from `start`. SL first (gap-down fills at the open); SL and TP in the same bar -> SL.
    Returns (bar index, raw exit price, reason). With locked_check a lower-circuit-locked trigger bar defers the sale to the
    next bar's open (repeatedly). If never triggered: last bar close, reason OPEN_AT_END."""
    n = len(c)
    j = start
    while j < n:
        trig, px, why = False, 0.0, ""
        if o[j] <= sl:
            trig, px, why = True, o[j], "SL_GAP"
        elif l[j] <= sl:
            trig, px, why = True, sl, "SL"
        elif h[j] >= tp:
            trig, px, why = True, max(o[j], tp), "TP"
        if trig:
            if locked_check and why != "TP":
                k = j
                prev_c = c[k - 1] if k > 0 else o[k]
                while k < n and is_locked_down(o[k], h[k], l[k], c[k], prev_c):
                    prev_c = c[k]
                    k += 1
                if k >= n:
                    return n - 1, float(c[n - 1]), "OPEN_AT_END"
                if k != j:
                    return k, float(o[k]), why + "_DEFERRED"
            return j, float(px), why
        j += 1
    return n - 1, float(c[n - 1]), "OPEN_AT_END"


# ----------------------------------------------------------------------------- features
def load_nifty() -> pd.Series:
    from pymongo import MongoClient

    col = MongoClient("mongodb://localhost:27017/")["stock_scanner"]["benchmark_ohlcv"]
    b = pd.DataFrame(list(col.find({"symbol": "^NSEI"}, {"_id": 0, "date": 1, "close": 1})))
    b["date"] = pd.to_datetime(b["date"])
    return b.set_index("date")["close"].sort_index()


def features(g: pd.DataFrame, nifty: pd.Series) -> Optional[pd.DataFrame]:
    """Per-symbol Champion features for every day T (all use data up to T's close)."""
    if g["date"].iloc[0] <= DATA_START or len(g) < 60:
        return None
    g = g.sort_values("date").reset_index(drop=True)
    c, h, l, v = g["close"], g["high"], g["low"], g["volume"]
    out = pd.DataFrame({"date": g["date"], "series": g["series"].astype(str)})
    out["age"] = (g["date"] - g["date"].iloc[0]).dt.days
    out["n"] = np.arange(1, len(g) + 1)
    out["px20"] = c.rolling(20).mean()
    out["vol20"] = v.rolling(20).mean()
    out["to20"] = (c * v).rolling(20).mean()
    out["bh"] = h.shift(1).rolling(10).max()
    out["bl"] = l.shift(1).rolling(10).min()
    out["avb"] = v.shift(1).rolling(10).mean()
    out["depth"] = (out["bh"] - out["bl"]) / out["bh"] * 100
    nf = nifty.reindex(nifty.index.union(g["date"])).ffill().reindex(g["date"]).values
    stock_ret = c / c.shift(55) - 1
    nift_ret = pd.Series(nf, index=c.index) / pd.Series(nf, index=c.index).shift(55) - 1
    out["rs55"] = ((stock_ret - nift_ret) * 100).round(2)
    out["vs"] = v / out["avb"]
    out["ext"] = (c - out["bh"]) / out["bh"] * 100
    out["gate"] = ((out["n"] >= 50) & (out["px20"] >= 50) & (out["vol20"] >= 1e5) & (out["to20"] >= 5e6) & (c <= 1000)
                   & (out["series"] == "EQ") & (out["age"] <= IPO_MAX_AGE_DAYS))
    out["breakout"] = out["gate"] & (out["depth"] <= 18) & (out["avb"] > 0) & (c > out["bh"]) & out["vs"].between(1.0, 5.0) \
        & (out["ext"] <= 5.0) & out["rs55"].notna()
    # cost-model inputs (previous close only)
    adt = (c * v).rolling(50, min_periods=20).median()
    out["adt_prev"] = adt.shift(1)
    out["sig_prev"] = c.pct_change().rolling(20, min_periods=10).std().shift(1)
    return out


# ----------------------------------------------------------------------------- trade simulation
def trade(g: pd.DataFrame, f: pd.DataFrame, i: int, mode: str, delist_cutoff: pd.Timestamp, haircut: float = 0.0,
          stop_mult: float = 1.0) -> Optional[dict]:
    o, h, l, c = (g[k].values.astype(float) for k in ("open", "high", "low", "close"))
    n = len(c)
    if i + 1 >= n:
        return None
    if mode == "AS_CODED":
        entry = c[i]
        sl, tp = entry * (1 + SL_PCT), entry * (1 + TP_PCT)
        j, px, why = simulate_exit(o, h, l, c, i + 1, sl, tp)
        gross = px / entry - 1
        if why == "OPEN_AT_END" and g["date"].iloc[-1] < delist_cutoff:
            gross = c[-1] * (1 - haircut) / entry - 1
            why = "DELIST"
        return {"entry_date": g["date"].iloc[i], "exit_date": g["date"].iloc[j], "ret": gross - AS_CODED_COST, "reason": why,
                "hold": (g["date"].iloc[j] - g["date"].iloc[i]).days}
    # REALISTIC: next open, +2% cap, upper-circuit skip
    e = i + 1
    if o[e] > c[i] * 1.02 or is_locked_up(o[e], h[e], l[e], c[e], c[i]) or o[e] > NOTIONAL:
        return None
    sh = int(NOTIONAL // o[e])
    if sh < 1:
        return None
    adt, sg = float(f["adt_prev"].iloc[e]), float(f["sig_prev"].iloc[e])
    fill = o[e] * (1 + slippage_fraction(adt, o[e], sh * o[e], sg))
    date_e = g["date"].iloc[e]
    buy_val = sh * fill
    outlay = buy_val + calc_buy_fees_exact(buy_val, date_e, CFG)["total"]
    sl, tp = fill * (1 + SL_PCT), fill * (1 + TP_PCT)
    j, px, why = simulate_exit(o, h, l, c, e, sl, tp, locked_check=True)
    side_mult = stop_mult if why.startswith("SL") else 1.0
    if why == "OPEN_AT_END" and g["date"].iloc[-1] < delist_cutoff:
        px, why, slip = c[-1] * (1 - haircut), "DELIST", 0.0
    else:
        slip = slippage_fraction(float(f["adt_prev"].iloc[j]), px, sh * px, float(f["sig_prev"].iloc[j]), side_mult)
    sell_val = sh * px * (1 - slip)
    net = sell_val - calc_sell_fees_exact(sell_val, g["date"].iloc[j], CFG)["total"]
    return {"entry_date": date_e, "exit_date": g["date"].iloc[j], "ret": net / outlay - 1, "reason": why,
            "hold": (g["date"].iloc[j] - date_e).days}


def run_signals(universe: Dict[str, Tuple[pd.DataFrame, pd.DataFrame]], rs_min: Optional[float], mode: str,
                cutoff: pd.Timestamp, **kw) -> pd.DataFrame:
    rows = []
    for sym, (g, f) in universe.items():
        mask = f["breakout"].values.copy()
        if rs_min is not None:
            mask &= (f["rs55"].values >= rs_min)
        busy_until = -1
        for i in np.flatnonzero(mask):
            if i <= busy_until:
                continue  # one position per symbol at a time
            t = trade(g, f, int(i), mode, cutoff, **kw)
            if t is None:
                continue
            t.update(symbol=sym, rs55=float(f["rs55"].iloc[i]), depth=float(f["depth"].iloc[i]), vs=float(f["vs"].iloc[i]))
            rows.append(t)
            busy_until = int(g.index[g["date"] == t["exit_date"]][0]) if t["reason"] != "OPEN_AT_END" else len(g)
    return pd.DataFrame(rows)


def control_trades(universe, mode, cutoff, per_year: Dict[int, int], seed: int = 0, **kw) -> pd.DataFrame:
    """Random-entry control: same IPO-age/liquidity/EQ gates, NO base/RS/volume/breakout condition, same exits and fills."""
    rng = np.random.default_rng(seed)
    pool = []
    for sym, (g, f) in universe.items():
        idx = np.flatnonzero((f["gate"].values) & (f["n"].values >= 60))
        pool.extend((sym, int(i), int(f["date"].iloc[i].year)) for i in idx)
    by_year: Dict[int, list] = {}
    for p in pool:
        by_year.setdefault(p[2], []).append(p)
    rows = []
    for y, k in per_year.items():
        cand = by_year.get(y, [])
        if not cand:
            continue
        for ix in rng.choice(len(cand), size=min(len(cand), k * 25), replace=False):
            sym, i, _ = cand[int(ix)]
            g, f = universe[sym]
            t = trade(g, f, i, mode, cutoff, **kw)
            if t is not None:
                t["year"] = y
                rows.append(t)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- stats
def summarize(r: pd.Series) -> dict:
    if len(r) == 0:
        return {"n": 0}
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    top10 = r.nlargest(10).sum() / pos if pos > 0 else np.nan
    return {"n": int(len(r)), "win_rate": float((r > 0).mean() * 100), "mean_pct": float(r.mean() * 100),
            "median_pct": float(r.median() * 100), "pf": float(pos / neg) if neg > 0 else np.inf,
            "top10_share_of_profit_pct": float(top10 * 100) if top10 == top10 else None}


def bootstrap_p(sig: pd.DataFrame, ctl: pd.DataFrame, draws: int = 2000, seed: int = 1) -> dict:
    """P(random control with the same yearly trade counts has mean >= signal mean)."""
    rng = np.random.default_rng(seed)
    sig = sig.assign(year=pd.to_datetime(sig["entry_date"]).dt.year)
    counts = sig.groupby("year").size()
    groups = {y: ctl[ctl["year"] == y]["ret"].values for y in counts.index if (ctl["year"] == y).any()}
    means = []
    for _ in range(draws):
        tot, nn = 0.0, 0
        for y, k in counts.items():
            if y in groups:
                s = rng.choice(groups[y], size=k, replace=True)
                tot += s.sum()
                nn += k
        means.append(tot / nn if nn else np.nan)
    means = np.array(means)
    sm = sig["ret"].mean()
    return {"signal_mean_pct": float(sm * 100), "control_mean_pct": float(np.nanmean(means) * 100),
            "control_p05_p95_pct": [float(np.nanpercentile(means, 5) * 100), float(np.nanpercentile(means, 95) * 100)],
            "p_value_control_ge_signal": float(np.mean(means >= sm))}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    panel = pd.read_parquet(PANEL)
    panel["series"] = panel["series"].astype(str)
    last = panel["date"].max()
    cutoff = last - pd.Timedelta(days=10)
    nifty = load_nifty()
    universe = {}
    for sym, g in panel.groupby("symbol", sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        f = features(g, nifty)
        if f is not None:
            universe[sym] = (g, f)
    del panel
    n_ipo_days = sum(int(f["gate"].sum()) for _, f in universe.values())
    print(f"IPO universe: {len(universe)} symbols, {n_ipo_days} gated stock-days", flush=True)
    summary = {"ipo_symbols": len(universe), "gated_days": n_ipo_days, "runs": {}}
    sets = {"breakout_only (RS gate off)": None, "v1.0-like (RS>=10)": 10.0, "HIGH-TP DNA (RS>=40)": 40.0}
    for mode in ("AS_CODED", "REALISTIC"):
        for name, rs_min in sets.items():
            t = run_signals(universe, rs_min, mode, cutoff)
            key = f"{mode} | {name}"
            res = {"stats": summarize(t["ret"]) if len(t) else {"n": 0}}
            if len(t):
                t.to_csv(OUT / f"trades_{mode}_{name.split()[0]}.csv", index=False)
                t["year"] = pd.to_datetime(t["entry_date"]).dt.year
                res["tp_rate_pct"] = float((t["reason"] == "TP").mean() * 100)
                res["sl_rate_pct"] = float(t["reason"].str.startswith("SL").mean() * 100)
                res["per_year_n"] = t.groupby("year").size().to_dict()
                res["per_year_mean_pct"] = (t.groupby("year")["ret"].mean() * 100).round(2).to_dict()
                res["ex_2021_2024"] = summarize(t[~t["year"].between(2021, 2024)]["ret"])
                if rs_min == 40.0 or rs_min is None:
                    ctl = control_trades(universe, mode, cutoff, t.groupby("year").size().to_dict())
                    res["control_stats"] = summarize(ctl["ret"]) if len(ctl) else {"n": 0}
                    if len(ctl):
                        res["vs_control"] = bootstrap_p(t, ctl)
            summary["runs"][key] = res
            print(key, json.dumps(res["stats"]), flush=True)
    (OUT / "champion_summary.json").write_text(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
