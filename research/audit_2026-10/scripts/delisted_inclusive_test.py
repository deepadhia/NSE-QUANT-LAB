"""10-year (2016-2026) test of the committed trend_momentum strategy on three universes, same rules/costs:
  survivors : Upstox research data only (today's ~500 index members)
  yf        : survivors + 43 yfinance/BSE/Upstox-ISIN ex-index stocks (external_daily_ohlcv, non-kaggle sources)
  full      : yf + 90 delisted/merged/ex-index stocks from the Kaggle bhavcopy (source=kaggle_bhavcopy, to 2024-12)
Eligibility = top-200 by 50d turnover on each date (past data only); index events disabled (unreliable after 2020).
Usage: python delisted_inclusive_test.py <WORK_DIR> prep|run     (WORK_DIR needs ohlcv.parquet and src_snapshot/src)
"""
import sys, os, json, pickle
import numpy as np
import pandas as pd

WORK = sys.argv[1].rstrip("/\\") + "/"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORK + "src_snapshot/src")
sys.path.insert(0, HERE)
from variant_runner import Variant, make_cfg, stats, load_index  # noqa: E402  (same config as the protocol)
from nse_quant_lab.engine import BacktestEngine  # noqa: E402
from nse_quant_lab.indicators import compute_indicators  # noqa: E402

START, END, WARM = "2016-01-01", "2026-10-23", "2014-01-01"
COLS = ["symbol", "date", "open", "high", "low", "close", "volume"]
OUT = WORK + "out/"


def clean(raw):
    raw = raw[(raw.date >= WARM) & (raw.date <= END) & (raw.open > 0) & (raw.high > 0) & (raw.low > 0) & (raw.close > 0) & (raw.volume >= 0)]
    return raw[(raw.high >= raw.low) & (raw.high >= raw.open) & (raw.high >= raw.close) & (raw.low <= raw.open) & (raw.low <= raw.close)]


def prep():
    from pymongo import MongoClient
    up = clean(pd.read_parquet(WORK + "ohlcv.parquet")[COLS])
    ext = pd.DataFrame(list(MongoClient("mongodb://localhost:27017/")["stock_scanner"]["external_daily_ohlcv"].find({}, {"_id": 0})))
    ext["date"] = pd.to_datetime(ext["date"])
    ext["kind"] = np.where(ext.source == "kaggle_bhavcopy", "kaggle", "yf")
    ext = ext.drop_duplicates(["symbol", "date", "kind"])
    assert not set(up.symbol) & set(ext.symbol), set(up.symbol) & set(ext.symbol)
    cfg = make_cfg(6)
    U = {}
    for kind, frame in (("upstox", up), ("yf", clean(ext[ext.kind == "yf"][COLS])), ("kaggle", clean(ext[ext.kind == "kaggle"][COLS]))):
        for s, g in frame.groupby("symbol"):
            ind = compute_indicators(g.sort_values("date").reset_index(drop=True), cfg)
            if not ind.empty:
                ind.attrs["kind"] = kind
                U[s] = ind
    cnt = pd.Series([v.attrs["kind"] for v in U.values()]).value_counts().to_dict()
    print("prepared", len(U), cnt)
    pickle.dump(U, open(WORK + "universe.pkl", "wb"))


_U, _IDX = {}, None


def _init():
    global _U, _IDX
    _U = pickle.load(open(WORK + "universe.pkl", "rb"))
    _IDX = load_index()


KINDS = {"survivors": {"upstox"}, "yf": {"upstox", "yf"}, "full": {"upstox", "yf", "kaggle"}}


def run_one(job):
    name, universe, slots, slip, tax = job
    U = {s: d for s, d in _U.items() if d.attrs["kind"] in KINDS[universe]}
    cfg = make_cfg(slots, slip, tax)
    strat = Variant(cfg, None, False, None)
    strat.pit_universe.events_by_sym = {}
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(U, START, END)
    nav.to_csv(OUT + f"nav_{name}.csv", index=False)
    trades.to_csv(OUT + f"trades_{name}.csv", index=False)
    s = trades.net_pnl.sort_values(ascending=False)
    kag = {k for k, v in _U.items() if v.attrs["kind"] == "kaggle"}
    kt = trades[trades.symbol.isin(kag)]
    dead = trades[trades.exit_reason == "DATA_ENDED_FORCED_EXIT"]
    return {"run": name, "universe": universe, "slots": slots, "slip_bps": int(slip * 1e4), "post_tax": tax,
            **stats(nav, _IDX, START, END), "final_nav": round(float(nav.nav.iloc[-1])), "trades": len(trades),
            "top10_share_pct": round(s.head(10).sum() / s.sum() * 100, 1) if s.sum() > 0 else None,
            "kaggle_trades": len(kt), "kaggle_pnl": round(float(kt.net_pnl.sum())),
            "kaggle_symbols": sorted(kt.symbol.unique().tolist()),
            "data_ended_exits": len(dead), "data_ended_symbols": sorted(dead.symbol.unique().tolist())}


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if sys.argv[2] == "prep":
        prep(); sys.exit(0)
    from multiprocessing import Pool
    jobs = [(f"{u[0].upper()}{n}{'_40bps' if sl == .004 else ''}{'_tax' if t else ''}", u, n, sl, t)
            for u in ("survivors", "yf", "full") for n in (6, 15) for sl, t in ((.002, False),)]
    jobs += [("F15_40bps", "full", 15, .004, False), ("F15_tax", "full", 15, .002, True),
             ("F6_40bps", "full", 6, .004, False), ("F6_tax", "full", 6, .002, True)]
    with Pool(6, initializer=_init) as p:
        res = p.map(run_one, jobs)
    df = pd.DataFrame(res)
    df.to_csv(OUT + "results.csv", index=False)
    json.dump(res, open(OUT + "results.json", "w"), indent=1, default=str)
    pd.set_option("display.width", 280)
    print(df.drop(columns=["kaggle_symbols", "data_ended_symbols"]).to_string())
    for r in res:
        if r["universe"] == "full":
            print(r["run"], "kaggle symbols traded:", r["kaggle_symbols"], "| data-ended exits:", r["data_ended_symbols"])
