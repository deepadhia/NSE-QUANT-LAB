"""Independent check: how much of the strategy's return is available from the
survivor-only universe itself (no stock selection skill)?

Monthly, open-to-open, gross of costs. Decision = last session of month (close data),
execution = next session open. Same eligibility as the strategy:
top-N by 50d turnover (capped at 45% of active pool), price >= 10, to50 >= 1 Cr.
"""
import sys
import numpy as np
import pandas as pd
from pymongo import MongoClient

sys.path.insert(0, r"D:/Personal Projects/NSE-Quant-Lab/src")
from nse_quant_lab.indicators import compute_indicators  # noqa: E402

SCR = r"C:/Users/DEEPJA~1/AppData/Local/Temp/claude/D--Personal-Projects-NSE-Quant-Lab/1dbda2cc-f19e-4d67-8ad9-d06d85c15be9/scratchpad/"
df = pd.read_parquet(SCR + "ohlcv.parquet")
df = df[df.date >= "2004-01-01"]

frames = []
for sym, g in df.groupby("symbol"):
    g = g.sort_values("date").reset_index(drop=True)
    ind = compute_indicators(g[["symbol", "date", "open", "high", "low", "close", "volume"]])
    if ind.empty:
        continue
    frames.append(ind[["symbol", "date", "open", "close", "ema100", "supertrend_dir", "mom_6m", "turnover_50d"]])
panel = pd.concat(frames)
opens = panel.pivot(index="date", columns="symbol", values="open")
closes = panel.pivot(index="date", columns="symbol", values="close")
ema = panel.pivot(index="date", columns="symbol", values="ema100")
std = panel.pivot(index="date", columns="symbol", values="supertrend_dir")
mom = panel.pivot(index="date", columns="symbol", values="mom_6m")
to50 = panel.pivot(index="date", columns="symbol", values="turnover_50d")

dates = closes.index
s = pd.Series(dates, index=dates)
month_last = s.groupby(dates.to_period("M")).max().tolist()

rng = np.random.default_rng(7)
N_RANDOM = 500
rows = []
for i in range(len(month_last) - 1):
    d, d_next = month_last[i], month_last[i + 1]
    if d < pd.Timestamp("2005-12-01"):
        continue
    ie, ix = dates.get_loc(d) + 1, dates.get_loc(d_next) + 1
    if ix >= len(dates):
        break
    e_dt, x_dt = dates[ie], dates[ix]
    t = to50.loc[d].dropna()
    t = t[t > 0]
    n_eff = min(200, max(1, int(len(t) * 0.45))) if len(t) >= 10 else len(t)
    liquid = t.sort_values(ascending=False).index[:n_eff]
    c = closes.loc[d, liquid]
    elig = liquid[(c >= 10) & (t[liquid] >= 1e7)]
    o_in, o_out = opens.loc[e_dt, elig], opens.loc[x_dt, elig]
    # forward-fill exit open from last known close if missing that day
    o_out = o_out.fillna(closes.loc[:x_dt, elig].ffill().iloc[-1])
    r = (o_out / o_in - 1).dropna()
    trend = [x for x in r.index if closes.loc[d, x] >= 1.03 * ema.loc[d, x] and std.loc[d, x] == 1]
    ranked = mom.loc[d, trend].dropna().sort_values(ascending=False)
    top6 = [x for x in ranked.index if x in r.index][:6]
    top20 = [x for x in ranked.index if x in r.index][:20]
    rand = [r[rng.choice(trend, size=min(6, len(trend)), replace=False)].mean() if len(trend) >= 6 else np.nan
            for _ in range(N_RANDOM)]
    rows.append({
        "exec": e_dt, "n_elig": len(r), "n_trend": len(trend),
        "ew_all": r.mean(), "ew_trend": r[trend].mean() if trend else 0.0,
        "mom_top6": r[top6].mean() if top6 else 0.0,
        "mom_top20": r[top20].mean() if top20 else 0.0,
        **{f"rand_{k}": v for k, v in enumerate(rand)},
    })

res = pd.DataFrame(rows).set_index("exec")
res.to_parquet(SCR + "survivor_monthly.parquet")

db = MongoClient("mongodb://localhost:27017/")["stock_scanner"]
b = pd.DataFrame(list(db["benchmark_ohlcv"].find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
b["date"] = pd.to_datetime(b.date)
b = b.set_index("date").close


def cagr(m, start):
    m = m[m.index >= start]
    yrs = (m.index[-1] - m.index[0]).days / 365.25 + 1 / 12
    return (np.prod(1 + m.fillna(0)) ** (1 / yrs) - 1) * 100


for start in ["2006-01-01", "2016-01-01", "2021-01-01"]:
    sub = res[res.index >= start]
    b0 = b[b.index >= start]
    yrs = (b0.index[-1] - b0.index[0]).days / 365.25
    print(f"\n== From {start} ({len(sub)} months) — GROSS, no costs/tax ==")
    print(f"Nifty500 price index CAGR : {((b0.iloc[-1] / b0.iloc[0]) ** (1 / yrs) - 1) * 100:6.2f}%")
    for col in ["ew_all", "ew_trend", "mom_top20", "mom_top6"]:
        print(f"{col:26s}: {cagr(res[col], start):6.2f}%")
    rc = [cagr(res[f"rand_{k}"], start) for k in range(N_RANDOM)]
    print(f"random 6 from trend set   : median {np.median(rc):6.2f}%  p5 {np.percentile(rc, 5):6.2f}%  p95 {np.percentile(rc, 95):6.2f}%")
    print(f"mom_top6 percentile among random: {(np.array(rc) < cagr(res['mom_top6'], start)).mean() * 100:.1f}")
print("\navg eligible:", res.n_elig.mean().round(1), " avg trend-qualified:", res.n_trend.mean().round(1))
