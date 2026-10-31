"""Pre-registered A/B for the hold-rule consistency fix (see PROTOCOL_HOLDRULE.md).
Usage: python ab_holdrule.py prep | run"""
import sys, pickle
import numpy as np
import pandas as pd

SCR = r"C:/Users/DEEPJA~1/AppData/Local/Temp/claude/D--Personal-Projects-NSE-Quant-Lab/1dbda2cc-f19e-4d67-8ad9-d06d85c15be9/scratchpad/"
sys.path.insert(0, SCR + "wt_src/src")
sys.path.insert(0, SCR)
from variant_runner import Variant, make_cfg, stats, load_index
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.indicators import compute_indicators

WINDOWS = {"W1_2006_2015": ("2006-01-01", "2015-12-31"), "W2_2016_2026": ("2016-01-01", "2026-10-23")}
OUT = SCR + "holdrule/"
PKL = SCR + "holdrule_universe.pkl"
COLS = ["symbol", "date", "open", "high", "low", "close", "volume"]


class HoldFix(Variant):
    """Held stocks stay rankable while close >= EMA100; new entries keep the full filter."""

    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        new = super().select_entry_candidates(universe, decision_date, current_holdings, config)
        rows = []
        for sym in sorted(current_holdings or {}):
            if sym in new.index:
                continue
            df = universe.get(sym)
            if df is None or df.empty:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            r = sub.iloc[0]
            vals = [float(r[c]) for c in ("close", "ema100", "mom_6m", "turnover_50d")]
            if any(np.isnan(v) for v in vals) or vals[0] < vals[1]:
                continue
            rows.append({"symbol": sym, "close": vals[0], "ema100": vals[1], "supertrend_dir": int(r["supertrend_dir"]),
                         "mom_6m": vals[2], "turnover_50d": vals[3]})
        if not rows:
            return new
        cdf = pd.concat([new.reset_index(drop=True).drop(columns=["rank"], errors="ignore"), pd.DataFrame(rows)], ignore_index=True)
        cdf = cdf.sort_values(by=["mom_6m", "turnover_50d", "symbol"], ascending=[False, False, True]).reset_index(drop=True)
        cdf["rank"] = np.arange(1, len(cdf) + 1)
        return cdf.set_index("symbol", drop=False)


def clean(raw, start):
    raw = raw[(raw.date >= start) & (raw.date <= "2026-10-23") & (raw.open > 0) & (raw.high > 0) & (raw.low > 0) & (raw.close > 0) & (raw.volume >= 0)]
    return raw[(raw.high >= raw.low) & (raw.high >= raw.open) & (raw.high >= raw.close) & (raw.low <= raw.open) & (raw.low <= raw.close)]


def prep():
    from pymongo import MongoClient
    up = clean(pd.read_parquet(SCR + "ohlcv.parquet")[COLS], "2004-01-01")
    ext = pd.DataFrame(list(MongoClient("mongodb://localhost:27017/")["stock_scanner"]["external_daily_ohlcv"].find({}, {"_id": 0})))
    ext["date"] = pd.to_datetime(ext["date"])
    ext = clean(ext[COLS], "2014-01-01")
    cfg = make_cfg(15)
    U = {}
    for frame in (up, ext):
        for s, g in frame.groupby("symbol"):
            ind = compute_indicators(g.sort_values("date").reset_index(drop=True), cfg)
            if not ind.empty:
                U[s] = ind
    assert len(U) >= 535, len(U)
    pickle.dump(U, open(PKL, "wb"))
    print("prepared", len(U))


_U, _IDX = None, None


def _init():
    global _U, _IDX
    _U = pickle.load(open(PKL, "rb"))
    _IDX = load_index()


def run_one(job):
    arm, window, tax = job
    start, end = WINDOWS[window]
    cfg = make_cfg(15, 0.002, tax)
    S = HoldFix if arm == "holdfix" else Variant
    strat = S(cfg, None, False, None)
    strat.pit_universe.events_by_sym = {}
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(_U, start, end)
    name = f"{window}_{arm}_{'posttax' if tax else 'pretax'}"
    nav.to_csv(OUT + f"nav_{name}.csv", index=False)
    trades.to_csv(OUT + f"trades_{name}.csv", index=False)
    return {"window": window, "arm": arm, "post_tax": tax, **stats(nav, _IDX, start, end), "trades": len(trades),
            "holds_365d_plus": int((trades.hold_days >= 365).sum()), "median_hold_days": float(trades.hold_days.median()),
            "rebalance_drops": int((trades.exit_reason == "REBALANCE_DROP").sum()),
            "fees_slip": round(m["total_friction"]), "tax": round(m.get("total_tax_paid", 0))}


if __name__ == "__main__":
    import os
    os.makedirs(OUT, exist_ok=True)
    if sys.argv[1] == "prep":
        prep(); sys.exit(0)
    from multiprocessing import Pool
    jobs = [(a, w, t) for w in WINDOWS for a in ("baseline", "holdfix") for t in (False, True)]
    with Pool(8, initializer=_init) as p:
        res = p.map(run_one, jobs)
    df = pd.DataFrame(res)
    df.to_csv(OUT + "results.csv", index=False)
    pd.set_option("display.width", 260)
    print(df.to_string(index=False))
    verdict = {}
    for w in WINDOWS:
        b = df[(df.window == w) & (df.arm == "baseline") & df.post_tax].iloc[0]
        h = df[(df.window == w) & (df.arm == "holdfix") & df.post_tax].iloc[0]
        verdict[w] = {"post_tax_cagr_better": bool(h.cagr > b.cagr), "fewer_trades": bool(h.trades < b.trades)}
    print("VERDICT:", verdict, "-> KEEP" if all(all(v.values()) for v in verdict.values()) else "-> REJECT")
