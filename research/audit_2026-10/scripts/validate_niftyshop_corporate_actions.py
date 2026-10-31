import pandas as pd, numpy as np
CA = r"D:/Personal Projects/NSE-Quant-Lab/data/external/niftyshop_ca/corporate_actions.csv"
CSV = r"C:/Users/DeepJAdhia/Downloads/archive/Combined_Data_2016_2024.csv"
UP = r"C:/Users/DEEPJA~1/AppData/Local/Temp/claude/D--Personal-Projects-NSE-Quant-Lab/0897a504-3001-43fc-a23d-f068c139a6eb/scratchpad/work/ohlcv.parquet"
ca = pd.read_csv(CA); ca["ex_date"] = pd.to_datetime(ca.ex_date)
up = pd.read_parquet(UP, columns=["symbol", "date", "close"]); up["sym"] = up.symbol.str.replace(".NS", "", regex=False)
S = set(ca.symbol) & set(up.sym)
raw = pd.read_csv(CSV, usecols=["SYMBOL", "SERIES", "CLOSE", "TOTTRDQTY", "TIMESTAMP"], dtype={"SYMBOL": str, "SERIES": str})
raw = raw[raw.SYMBOL.isin(S) & raw.SERIES.isin(["EQ", "BE", "BZ"])]
raw["date"] = pd.to_datetime(raw.TIMESTAMP)
raw = raw.sort_values(["SYMBOL", "date", "TOTTRDQTY"]).drop_duplicates(["SYMBOL", "date"], keep="last")
obs = []   # observed steps in log(upstox/raw)
for s, g in raw.groupby("SYMBOL"):
    u = up[up.sym == s][["date", "close"]]
    m = g[["date", "CLOSE"]].merge(u, on="date").sort_values("date")
    if len(m) < 250: continue
    lr = np.log(m.close / m.CLOSE).to_numpy()
    d = np.diff(lr)
    for i in np.where(np.abs(d) > 0.03)[0]:
        obs.append({"symbol": s, "date": m.date.iloc[i + 1], "factor": float(np.exp(-d[i]))})   # price multiplier applied to earlier prices = 1/ratio change
obs = pd.DataFrame(obs)
print("symbols compared:", raw.SYMBOL.nunique(), "| observed adjustment steps (Upstox vs raw, 2016-24):", len(obs))
# recall: each observed step -> file entry within +-5 days with factor within 5%
hit = 0; rows = []
for r in obs.itertuples():
    c = ca[(ca.symbol == r.symbol) & (ca.ex_date.between(r.date - pd.Timedelta(days=5), r.date + pd.Timedelta(days=5))) & (ca.kind != "dividend")]
    ok = ((c.price_factor / r.factor - 1).abs() < 0.05).any() if len(c) else False
    hit += ok
    rows.append((r.symbol, r.date.date(), round(r.factor, 4), ok, c[["kind", "price_factor"]].values.tolist()))
print(f"RECALL: {hit}/{len(obs)} = {hit/len(obs):.1%} of observed adjustment steps have a matching file entry")
miss = [x for x in rows if not x[3]]
print("unmatched observed steps (first 25):"); [print("  ", x) for x in miss[:25]]
# precision: file entries with price_factor<0.97 in window for compared symbols -> is there an observed step?
fe = ca[(ca.symbol.isin(raw.SYMBOL.unique())) & (ca.price_factor < 0.97) & (ca.ex_date >= "2016-01-05") & (ca.ex_date <= "2024-12-01")]
p = 0; bad = []
for r in fe.itertuples():
    o = obs[(obs.symbol == r.symbol) & (obs.date.between(r.ex_date - pd.Timedelta(days=5), r.ex_date + pd.Timedelta(days=5)))]
    ok = ((o.factor / r.price_factor - 1).abs() < 0.05).any() if len(o) else False
    p += ok
    if not ok: bad.append((r.symbol, r.ex_date.date(), r.kind, round(r.price_factor, 4), o.factor.round(4).tolist()))
print(f"PRECISION: {p}/{len(fe)} = {p/len(fe):.1%} of file adjustments (factor<0.97) match an observed step")
print("by kind:", fe.assign(ok=[(x[0], x[1]) not in {(b[0], b[1]) for b in bad} for x in zip(fe.symbol, fe.ex_date.dt.date)]).groupby("kind").ok.agg(["sum", "count"]).to_dict("index"))
print("unmatched file entries (first 25):"); [print("  ", x) for x in bad[:25]]
