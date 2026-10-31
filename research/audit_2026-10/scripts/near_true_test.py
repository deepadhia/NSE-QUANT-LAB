"""Near-true 2016-2026 test: liquidity-PIT universe (top-200 by 50d turnover, past data only),
Upstox survivors vs Upstox + 41 validated ex-Nifty-200 stocks from yfinance. No index events.
Usage: python near_true_test.py prep | run"""
import sys, os, json, pickle
import numpy as np
import pandas as pd

SCR = r"C:/Users/DEEPJA~1/AppData/Local/Temp/claude/D--Personal-Projects-NSE-Quant-Lab/1dbda2cc-f19e-4d67-8ad9-d06d85c15be9/scratchpad/"
sys.path.insert(0, SCR + "wt_src/src")
sys.path.insert(0, SCR)
from variant_runner import Variant, make_cfg, stats, load_index  # same rules/config as the protocol
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.indicators import compute_indicators

START, END, WARM = "2016-01-01", "2026-10-23", "2014-01-01"
COLS = ["symbol", "date", "open", "high", "low", "close", "volume"]
OUT = SCR + "near_true/"


def clean(raw):
    raw = raw[(raw.date >= WARM) & (raw.date <= END) & (raw.open > 0) & (raw.high > 0) & (raw.low > 0) & (raw.close > 0) & (raw.volume >= 0)]
    return raw[(raw.high >= raw.low) & (raw.high >= raw.open) & (raw.high >= raw.close) & (raw.low <= raw.open) & (raw.low <= raw.close)]


def prep():
    from pymongo import MongoClient
    up = clean(pd.read_parquet(SCR + "ohlcv.parquet")[COLS])
    ext = pd.DataFrame(list(MongoClient("mongodb://localhost:27017/")["stock_scanner"]["external_daily_ohlcv"].find({}, {"_id": 0})))
    ext["date"] = pd.to_datetime(ext["date"])
    ext = clean(ext[COLS])
    assert not set(up.symbol) & set(ext.symbol)
    cfg = make_cfg(6)
    U = {}
    for src, frame in (("upstox", up), ("external", ext)):
        for s, g in frame.groupby("symbol"):
            ind = compute_indicators(g.sort_values("date").reset_index(drop=True), cfg)
            if not ind.empty:
                ind.attrs["source"] = src
                U[s] = ind
    n_ext = sum(1 for v in U.values() if v.attrs["source"] == "external")
    assert len(U) - n_ext >= 490 and n_ext >= 35, (len(U), n_ext)
    pickle.dump(U, open(SCR + "near_true_universe.pkl", "wb"))
    print("prepared", len(U), "symbols;", n_ext, "external")


_U, _IDX, _BULL = {}, None, None


def _init():
    global _U, _IDX
    _U = pickle.load(open(SCR + "near_true_universe.pkl", "rb"))
    _IDX = load_index()


def run_one(job):
    name, universe_kind, slots, slip, tax = job
    U = _U if universe_kind == "expanded" else {s: d for s, d in _U.items() if d.attrs["source"] == "upstox"}
    cfg = make_cfg(slots, slip, tax)
    strat = Variant(cfg, None, False, None)
    strat.pit_universe.events_by_sym = {}  # liquidity-only eligibility; index event data is unreliable after 2020
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(U, START, END)
    nav.to_csv(OUT + f"nav_{name}.csv", index=False)
    trades.to_csv(OUT + f"trades_{name}.csv", index=False)
    s = trades.net_pnl.sort_values(ascending=False)
    ext_syms = {k for k, v in _U.items() if v.attrs["source"] == "external"}
    ext_tr = trades[trades.symbol.isin(ext_syms)]
    dead = trades[trades.exit_reason == "DATA_ENDED_FORCED_EXIT"]
    return {"run": name, "universe": universe_kind, "slots": slots, "slip_bps": int(slip * 1e4), "post_tax": tax,
            **stats(nav, _IDX, START, END), "final_nav": round(float(nav.nav.iloc[-1])), "trades": len(trades),
            "top10_share_pct": round(s.head(10).sum() / s.sum() * 100, 1) if s.sum() > 0 else None,
            "external_stock_trades": len(ext_tr), "external_stock_pnl": round(float(ext_tr.net_pnl.sum())),
            "external_symbols_traded": sorted(ext_tr.symbol.unique().tolist()),
            "data_ended_exits": len(dead), "data_ended_proceeds_lost_if_zero": round(float((dead.exit_fill_price * dead.shares).sum()))}


def no_skill_baseline():
    """Equal-weight all eligible stocks monthly (open->open), gross. Dead stocks: (a) last price, (b) total loss."""
    U = pickle.load(open(SCR + "near_true_universe.pkl", "rb"))
    rows = {}
    for kind in ("survivors", "expanded"):
        sub = U if kind == "expanded" else {s: d for s, d in U.items() if d.attrs["source"] == "upstox"}
        panel = pd.concat([d[["symbol", "date", "open", "close", "turnover_50d"]] for d in sub.values()])
        op = panel.pivot(index="date", columns="symbol", values="open")
        cl = panel.pivot(index="date", columns="symbol", values="close")
        to = panel.pivot(index="date", columns="symbol", values="turnover_50d")
        last_date = {s: d["date"].max() for s, d in sub.items()}
        dates = cl.index
        mls = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max().tolist()
        ra, rb = [], []
        for i in range(len(mls) - 1):
            d = mls[i]
            ie, ix = dates.get_loc(d) + 1, dates.get_loc(mls[i + 1]) + 1
            if ix >= len(dates) or dates[ie] < pd.Timestamp(START):
                continue
            t = to.loc[d].dropna(); t = t[t > 0]
            n = min(200, max(1, int(len(t) * 0.45)))
            liq = t.sort_values(ascending=False).index[:n]
            elig = [s for s in liq if cl.loc[d, s] >= 10 and t[s] >= 1e7]
            o_in = op.loc[dates[ie], elig]
            o_out = op.loc[dates[ix], elig].fillna(cl.loc[:dates[ix], elig].ffill().iloc[-1])
            r = (o_out / o_in - 1).dropna()
            died = [s for s in r.index if last_date[s] < dates[ix]]
            ra.append(r.mean())
            rb.append(r.where(~r.index.isin(died), -1.0).mean())
        yrs = len(ra) / 12
        rows[kind] = {"months": len(ra), "cagr_last_price": round((np.prod(1 + np.array(ra)) ** (1 / yrs) - 1) * 100, 2),
                      "cagr_dead_to_zero": round((np.prod(1 + np.array(rb)) ** (1 / yrs) - 1) * 100, 2)}
    return rows


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if sys.argv[1] == "prep":
        prep(); sys.exit(0)
    from multiprocessing import Pool
    jobs = [("A_survivors_6", "survivors", 6, 0.002, False),
            ("B_expanded_6", "expanded", 6, 0.002, False),
            ("C_expanded_6_40bps", "expanded", 6, 0.004, False),
            ("D_expanded_6_posttax", "expanded", 6, 0.002, True),
            ("E_survivors_15", "survivors", 15, 0.002, False),
            ("F_expanded_15", "expanded", 15, 0.002, False)]
    with Pool(6, initializer=_init) as p:
        res = p.map(run_one, jobs)
    df = pd.DataFrame(res)
    df.to_csv(OUT + "results.csv", index=False)
    pd.set_option("display.width", 260)
    print(df.drop(columns=["external_symbols_traded"]).to_string())
    for r in res:
        print(r["run"], "external symbols traded:", r["external_symbols_traded"])
    base = no_skill_baseline()
    json.dump({"runs": res, "no_skill": base}, open(OUT + "summary.json", "w"), indent=2, default=str)
    print("NO-SKILL BASELINE:", json.dumps(base, indent=2))
