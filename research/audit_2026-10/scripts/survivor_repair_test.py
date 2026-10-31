"""10-year (2016-2026) test of the committed trend_momentum strategy on progressively repaired universes (same rules/costs):
  survivors : Upstox research data only (today's ~500 index members)
  yf        : + 43 yfinance/BSE/Upstox-ISIN ex-index stocks
  full      : + 90 Kaggle-bhavcopy delisted/ex-index names (to 2024-12)
  full2     : + still-listed names absent from the Upstox DB that were liquid (source='yfinance_gap', validated vs bhavcopy)
Eligibility = top-200 by 50d turnover on each date (past data only); index events disabled (unreliable after 2020).
Usage: python survivor_repair_test.py <WORK_DIR> prep|run|baseline
WORK_DIR needs ohlcv.parquet and src_snapshot/src.  Optional env DROP_ADJUST=1 is NOT supported here (see sensitivity script).
"""
import sys, os, json, pickle
import numpy as np
import pandas as pd

WORK = sys.argv[1].rstrip("/\\") + "/"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORK + "src_snapshot/src")
sys.path.insert(0, HERE)
from variant_runner import Variant, make_cfg, stats, load_index  # noqa: E402
from nse_quant_lab.engine import BacktestEngine  # noqa: E402
from nse_quant_lab.indicators import compute_indicators  # noqa: E402

START, END, WARM = "2016-01-01", "2026-10-23", "2014-01-01"
COLS = ["symbol", "date", "open", "high", "low", "close", "volume"]
OUT = WORK + os.environ.get("OUTDIR", "out2/")
UNIV = os.environ.get("UNIV", "universe2.pkl")
CA_FILE = r"D:/Personal Projects/NSE-Quant-Lab/data/external/niftyshop_ca/corporate_actions.csv"
KIND_OF = {"kaggle_bhavcopy": "kaggle", "yfinance_gap": "gap", "kaggle_bhavcopy_gap10": "gap10"}
KINDS = {"survivors": {"upstox"}, "yf": {"upstox", "yf"}, "full": {"upstox", "yf", "kaggle"},
         "full2": {"upstox", "yf", "kaggle", "gap"}, "full3": {"upstox", "yf", "kaggle", "gap", "gap10"}}   # full3: only for windows ending <= 2024-12-13


def clean(raw):
    raw = raw[(raw.date >= WARM) & (raw.date <= END) & (raw.open > 0) & (raw.high > 0) & (raw.low > 0) & (raw.close > 0) & (raw.volume >= 0)]
    return raw[(raw.high >= raw.low) & (raw.high >= raw.open) & (raw.high >= raw.close) & (raw.low <= raw.open) & (raw.low <= raw.close)]


def apply_ca(frame, comb, kind, log, tol=0.12):
    """Apply same-day split*bonus price factors from the third-party NSE corporate-action file, ONLY where the series
    still shows the unadjusted step at the ex-date (close ratio within tol of the factor). Rows before the ex-date are
    multiplied by the factor, volume divided. Order-insensitive (a later event scales both sides of an earlier ratio)."""
    out = []
    for sym, g in frame.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        ev = comb[comb.symbol == sym.replace(".NS", "")]
        for e in ev.itertuples():
            idx = g.index[g.date >= e.ex_date]
            if len(idx) == 0 or idx[0] == 0:
                continue
            i = int(idx[0])
            if (g.date[i] - e.ex_date).days > 3:
                continue
            ratio = g.close[i] / g.close[i - 1]
            # the observed step must be within tol of the factor AND closer to the factor than to "no step" (ratio 1);
            # without the second test a factor near 1 (e.g. 0.909) is "corroborated" by an already-adjusted series
            if abs(ratio / e.price_factor - 1) < tol and abs(ratio - e.price_factor) < abs(ratio - 1.0):
                for c in ("open", "high", "low", "close"):
                    g.loc[:i - 1, c] = g.loc[:i - 1, c] * e.price_factor
                g.loc[:i - 1, "volume"] = g.loc[:i - 1, "volume"] / e.price_factor
                log.append({"kind": kind, "symbol": sym, "ex_date": e.ex_date.strftime("%Y-%m-%d"), "factor": float(e.price_factor), "observed_ratio": round(float(ratio), 4)})
        out.append(g)
    return pd.concat(out, ignore_index=True) if out else frame


def prep():
    from pymongo import MongoClient
    up = clean(pd.read_parquet(WORK + "ohlcv.parquet")[COLS])
    ext = pd.DataFrame(list(MongoClient("mongodb://localhost:27017/")["stock_scanner"]["external_daily_ohlcv"].find({}, {"_id": 0})))
    ext["date"] = pd.to_datetime(ext["date"])
    ext["kind"] = ext.source.map(KIND_OF).fillna("yf")
    ext = ext.drop_duplicates(["symbol", "date", "kind"])
    sets = {k: set(ext[ext.kind == k].symbol) for k in ("yf", "kaggle", "gap", "gap10")}
    sets["upstox"] = set(up.symbol)
    ks = list(sets)
    for i in range(len(ks)):
        for j in range(i + 1, len(ks)):
            assert not sets[ks[i]] & sets[ks[j]], (ks[i], ks[j], sorted(sets[ks[i]] & sets[ks[j]])[:5])
    cfg = make_cfg(6)
    U = {}
    comb, calog = None, []
    if os.environ.get("CA") == "1":
        ca = pd.read_csv(CA_FILE); ca["ex_date"] = pd.to_datetime(ca.ex_date)
        comb = ca[ca.kind.isin(["split", "bonus"])].groupby(["symbol", "ex_date"]).price_factor.prod().reset_index()
        comb = comb[comb.price_factor < 0.97]
    for kind, frame in (("upstox", up), ("yf", clean(ext[ext.kind == "yf"][COLS])), ("kaggle", clean(ext[ext.kind == "kaggle"][COLS])),
                        ("gap", clean(ext[ext.kind == "gap"][COLS])), ("gap10", clean(ext[ext.kind == "gap10"][COLS]))):
        if comb is not None:
            frame = apply_ca(frame, comb, kind, calog)
        for s, g in frame.groupby("symbol"):
            ind = compute_indicators(g.sort_values("date").reset_index(drop=True), cfg)
            if not ind.empty:
                ind.attrs["kind"] = kind
                U[s] = ind
    print("prepared", len(U), pd.Series([v.attrs["kind"] for v in U.values()]).value_counts().to_dict())
    pickle.dump(U, open(WORK + UNIV, "wb"))
    if comb is not None:
        json.dump(calog, open(OUT + "ca_applied.json", "w"), indent=1)
        print("CA factors applied:", len(calog), pd.Series([x["kind"] for x in calog]).value_counts().to_dict())
        for x in calog[:60]: print("  ", x)


_U, _IDX = {}, None


def _init():
    global _U, _IDX
    _U = pickle.load(open(WORK + UNIV, "rb"))
    _IDX = load_index()


# Pre-stated sensitivity exclusions (chosen BEFORE seeing the repaired-universe results):
WEAK_ADJ = {"SINTEX.NS", "DISHMAN.NS", "MANPASAND.NS", "COSMOFILMS.NS"}            # least-certain bonus/split decisions (Kaggle)
UNEXPLAINED = {s + ".NS" for s in ("ALLCARGO", "GVKPIL", "INDIAGLYCO", "KESORAMIND", "MIRZAINT", "NIITLTD", "PARAS", "SANOFI", "SHANKARA")}
EXCL = {"w": WEAK_ADJ, "u": UNEXPLAINED, "wu": WEAK_ADJ | UNEXPLAINED, "pel": {"PEL.NS"}, "inf": {"INFIBEAM.NS"}, "pelinf": {"PEL.NS", "INFIBEAM.NS"}}


def run_one(job):
    name, universe, slots, slip, tax = job[:5]
    excl = EXCL[job[5]] if len(job) > 5 and job[5] else set()
    start, end = job[6] if len(job) > 6 else (START, END)
    U = {s: d for s, d in _U.items() if d.attrs["kind"] in KINDS[universe] and s not in excl}
    cfg = make_cfg(slots, slip, tax)
    strat = Variant(cfg, None, False, None)
    strat.pit_universe.events_by_sym = {}
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(U, start, end)
    nav.to_csv(OUT + f"nav_{name}.csv", index=False)
    trades.to_csv(OUT + f"trades_{name}.csv", index=False)
    s = trades.net_pnl.sort_values(ascending=False)
    kind_of = {k: v.attrs["kind"] for k, v in _U.items()}
    tk = trades.symbol.map(kind_of)
    dead = trades[trades.exit_reason == "DATA_ENDED_FORCED_EXIT"]
    return {"run": name, "universe": universe, "slots": slots, "slip_bps": int(slip * 1e4), "post_tax": tax,
            "window": f"{start}..{end}", **stats(nav, _IDX, start, end), "final_nav": round(float(nav.nav.iloc[-1])), "trades": len(trades),
            "top10_share_pct": round(s.head(10).sum() / s.sum() * 100, 1) if s.sum() > 0 else None,
            "pnl_by_kind": {k: round(float(trades[tk == k].net_pnl.sum())) for k in ("upstox", "yf", "kaggle", "gap", "gap10")},
            "trades_by_kind": {k: int((tk == k).sum()) for k in ("upstox", "yf", "kaggle", "gap", "gap10")},
            "data_ended_exits": len(dead), "data_ended_symbols": sorted(dead.symbol.unique().tolist())}


def no_skill_baseline(universe, start=START, end=END, tag=""):
    """Equal-weight all eligible stocks monthly (open->open), gross. Dead stocks: (a) last price, (b) total loss.
    Eligibility mirrors the strategy filter: top-200 / 45% of reporting names by 50d turnover, close>=10, turnover>=1e7."""
    U = pickle.load(open(WORK + UNIV, "rb"))
    sub = {s: d for s, d in U.items() if d.attrs["kind"] in KINDS[universe]}
    panel = pd.concat([d[["symbol", "date", "open", "close", "turnover_50d"]] for d in sub.values()])
    op = panel.pivot(index="date", columns="symbol", values="open")
    cl = panel.pivot(index="date", columns="symbol", values="close")
    to = panel.pivot(index="date", columns="symbol", values="turnover_50d")
    last_date = {s: d["date"].max() for s, d in sub.items()}
    dates = cl.index
    mls = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max().tolist()
    ra, rb, lab = [], [], []
    for i in range(len(mls) - 1):
        d = mls[i]
        ie, ix = dates.get_loc(d) + 1, dates.get_loc(mls[i + 1]) + 1
        if ix >= len(dates) or dates[ie] < pd.Timestamp(start) or dates[ix] > pd.Timestamp(end):
            continue
        t = to.loc[d].dropna(); t = t[t > 0]
        n = min(200, max(1, int(len(t) * 0.45)))
        liq = t.sort_values(ascending=False).index[:n]
        elig = [s for s in liq if cl.loc[d, s] >= 10 and t[s] >= 1e7]
        o_in = op.loc[dates[ie], elig]
        o_out = op.loc[dates[ix], elig].fillna(cl.loc[:dates[ix], elig].ffill().iloc[-1])
        r = (o_out / o_in - 1).dropna()
        died = [s for s in r.index if last_date[s] < dates[ix]]
        ra.append(r.mean()); lab.append(str((d.to_period("M") + 1)))
        rb.append(r.where(~r.index.isin(died), -1.0).mean())
    yrs = len(ra) / 12
    f = lambda x: round((np.prod(1 + np.array(x)) ** (1 / yrs) - 1) * 100, 2)
    pd.Series(ra, index=lab).to_csv(OUT + f"baseline_monthly_{universe}{tag}.csv")
    return {"universe": universe, "window": f"{start}..{end}", "months": len(ra), "cagr_last_price": f(ra), "cagr_dead_to_zero": f(rb)}


def excess_vs_basket(name, universe):
    """Monthly strategy return minus the equal-weight same-universe basket (approx. alignment: nav month vs open-to-open month)."""
    nav = pd.read_csv(OUT + f"nav_{name}.csv"); nav = nav.set_index(pd.to_datetime(nav["date"])).nav
    sr = nav.resample("ME").last().pct_change().dropna(); sr.index = sr.index.to_period("M").astype(str)
    br = pd.read_csv(OUT + f"baseline_monthly_{universe}.csv", index_col=0).iloc[:, 0]
    ex = (sr - br.reindex(sr.index)).dropna()
    return {"run": name, "vs_basket": universe, "months": len(ex), "mean_excess_pm_pct": round(ex.mean() * 100, 3),
            "t": round(ex.mean() / ex.std() * np.sqrt(len(ex)), 2)}


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if sys.argv[2] == "prep":
        prep(); sys.exit(0)
    if sys.argv[2] == "baseline":
        res = [no_skill_baseline(u) for u in ("survivors", "yf", "full", "full2")]
        json.dump(res, open(OUT + "no_skill.json", "w"), indent=1)
        print(pd.DataFrame(res).to_string()); sys.exit(0)
    from multiprocessing import Pool
    if sys.argv[2] == "sens":
        jobs = [(f"S{n}_{k}", "full2", n, .002, False, k) for n in (15, 6) for k in ("w", "u", "wu")]
        with Pool(6, initializer=_init) as p:
            res = p.map(run_one, jobs)
        df = pd.DataFrame(res); df.to_csv(OUT + "sens_results.csv", index=False)
        pd.set_option("display.width", 300)
        print(df.drop(columns=["pnl_by_kind", "trades_by_kind", "data_ended_symbols"]).to_string()); sys.exit(0)
    WINDOWS = {"W7a": ("2018-01-01", "2024-12-13"), "W7b": ("2019-01-01", "2026-10-23"), "W5": ("2021-10-01", "2026-10-23")}
    if sys.argv[2] == "windows":
        jobs = [(f"{w}_{n}{'_40bps' if sl == .004 else ''}", "full2", n, sl, False, None, win)
                for w, win in WINDOWS.items() for n in (15, 6) for sl in (.002, .004)]
        with Pool(6, initializer=_init) as p:
            res = p.map(run_one, jobs)
        df = pd.DataFrame(res); df.to_csv(OUT + "window_results.csv", index=False)
        bl = [no_skill_baseline("full2", a, b, w) for w, (a, b) in WINDOWS.items()] +              [no_skill_baseline("survivors", a, b, w) for w, (a, b) in WINDOWS.items()]
        pd.set_option("display.width", 300)
        print(df.drop(columns=["pnl_by_kind", "trades_by_kind", "data_ended_symbols", "vol", "info_ratio"]).to_string())
        print(pd.DataFrame(bl).to_string()); sys.exit(0)
    if sys.argv[2] == "w3":
        win = ("2018-01-01", "2024-12-13")
        jobs = [("H15", "full3", 15, .002, False, None, win), ("H6", "full3", 6, .002, False, None, win),
                ("H15_40bps", "full3", 15, .004, False, None, win), ("H6_40bps", "full3", 6, .004, False, None, win),
                ("H15_expel", "full3", 15, .002, False, "pel", win), ("H6_expel", "full3", 6, .002, False, "pel", win),
                ("H15_exinf", "full3", 15, .002, False, "inf", win), ("H6_exinf", "full3", 6, .002, False, "inf", win)]
        import time; t0 = time.time()
        with Pool(8, initializer=_init) as p:
            res = p.map(run_one, jobs)
        df = pd.DataFrame(res); df.to_csv(OUT + "w3_results.csv", index=False)
        pd.set_option("display.width", 300)
        print(df.drop(columns=["pnl_by_kind", "trades_by_kind", "data_ended_symbols", "vol", "info_ratio"]).to_string())
        for r in res[:2]:
            print(r["run"], r["pnl_by_kind"], r["trades_by_kind"])
        print(pd.DataFrame([no_skill_baseline("full3", *win, "w3")]).to_string()); print("elapsed s", round(time.time() - t0)); sys.exit(0)
    if sys.argv[2] == "w8":
        win = ("2018-01-01", "2026-10-23")      # full2 only: gap10 names end 2024-12-13 so they cannot be used past it
        jobs = [(f"E{n}{'_40bps' if sl == .004 else ''}", "full2", n, sl, False, None, win) for n in (15, 6) for sl in (.002, .004)]
        jobs += [("E15_tax", "full2", 15, .002, True, None, win), ("E6_tax", "full2", 6, .002, True, None, win)]
        import time; t0 = time.time()
        with Pool(6, initializer=_init) as p:
            res = p.map(run_one, jobs)
        df = pd.DataFrame(res); df.to_csv(OUT + "w8_results.csv", index=False)
        pd.set_option("display.width", 300)
        print(df.drop(columns=["pnl_by_kind", "trades_by_kind", "data_ended_symbols", "vol", "info_ratio"]).to_string())
        print(pd.DataFrame([no_skill_baseline(u, *win, "w8") for u in ("full2", "survivors")]).to_string()); print("elapsed s", round(time.time() - t0)); sys.exit(0)
    if sys.argv[2] == "ca":
        w7, w8 = ("2018-01-01", "2024-12-13"), ("2018-01-01", "2026-10-23")
        jobs = [(f"C7_{n}{'_40bps' if sl == .004 else ''}", "full3", n, sl, False, None, w7) for n in (15, 6) for sl in (.002, .004)]
        jobs += [(f"C8_{n}{'_40bps' if sl == .004 else ''}", "full2", n, sl, False, None, w8) for n in (15, 6) for sl in (.002, .004)]
        jobs += [("C8_15_tax", "full2", 15, .002, True, None, w8), ("C8_6_tax", "full2", 6, .002, True, None, w8)]
        import time; t0 = time.time()
        with Pool(8, initializer=_init) as p:
            res = p.map(run_one, jobs)
        df = pd.DataFrame(res); df.to_csv(OUT + "ca_results.csv", index=False)
        pd.set_option("display.width", 300)
        print(df.drop(columns=["pnl_by_kind", "trades_by_kind", "data_ended_symbols", "vol", "info_ratio"]).to_string()); print("elapsed s", round(time.time() - t0)); sys.exit(0)
    if sys.argv[2] == "excess":
        res = [excess_vs_basket(n, "full2") for n in ("G15", "G6", "G15_tax", "G6_tax")]
        print(pd.DataFrame(res).to_string()); sys.exit(0)
    jobs = [("F15", "full", 15, .002, False), ("F6", "full", 6, .002, False),
            ("G15", "full2", 15, .002, False), ("G6", "full2", 6, .002, False),
            ("G15_40bps", "full2", 15, .004, False), ("G6_40bps", "full2", 6, .004, False),
            ("G15_tax", "full2", 15, .002, True), ("G6_tax", "full2", 6, .002, True)]
    with Pool(6, initializer=_init) as p:
        res = p.map(run_one, jobs)
    df = pd.DataFrame(res)
    df.to_csv(OUT + "results.csv", index=False)
    json.dump(res, open(OUT + "results.json", "w"), indent=1, default=str)
    pd.set_option("display.width", 300)
    print(df.drop(columns=["pnl_by_kind", "trades_by_kind", "data_ended_symbols"]).to_string())
    for r in res:
        print(r["run"], "pnl_by_kind", r["pnl_by_kind"], "trades_by_kind", r["trades_by_kind"], "| data-ended:", r["data_ended_symbols"])
