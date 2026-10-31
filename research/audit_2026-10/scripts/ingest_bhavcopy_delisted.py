"""Ingest delisted / merged / ex-index liquid stocks from the Kaggle NSE bhavcopy (2016-01 .. 2024-12) into
local Mongo `external_daily_ohlcv` (source='kaggle_bhavcopy'). Dry-run by default; --write to upsert.

Usage:
  python ingest_bhavcopy_delisted.py --csv <Combined_Data_2016_2024.csv> --report <report.json> [--write]

Why only ENDED names: still-listed names missing from Upstox need 2025-26 prices that the file lacks.
Selection (data-driven, no strategy information): equity-ISIN symbols whose ISIN and symbol are NOT in the DB,
that ranked <=250 by 50-day turnover (close*volume, full-market ranking from this file) on at least one date,
and whose last print is before 2024-12-01  +  the 20 named targets +  DHANI (same ISIN as IBVENTURES).
Bhavcopy is UNADJUSTED. Split/bonus decisions are explicit (ADJUST below), each with an evidence label.
Nothing is inferred as zero; reconstructed bars are flagged in the report.
"""
import argparse, json
import numpy as np
import pandas as pd
from pymongo import MongoClient, ReplaceOne

SOURCE = "kaggle_bhavcopy"
TARGETS = ["DHFL", "AMTEKAUTO", "COX&KINGS", "JPASSOCIAT", "RELCAPITAL", "RNAVAL", "SINTEX", "ALBK", "ANDHRABANK",
           "ORIENTBANK", "SYNDIBANK", "BHARATFIN", "GRUH", "GSKCONS", "CAIRN", "ABIRLANUVO", "IDFC", "TV18BRDCST",
           "TATAMTRDVR", "IBVENTURES", "DHANI"]
RENAME = {"ABIRLANUVO": "ABNUVO", "IBVENTURES": "DHANI"}          # names used in pit_reconstitution_events
# (symbol, ex-date) -> (price ratio applied to ALL EARLIER prices, evidence)
ADJUST = {
    ("MINDTREE", "2016-03-09"): (0.5, "ASSUMED bonus 1:1: open ratio 0.479 (close 0.443 incl. same-day move), volume x2.5"),
    ("ESSELPACK", "2018-06-20"): (0.5, "ASSUMED bonus 1:1: open ratio 0.502 with price otherwise flat, volume x2.3"),
    ("PHILIPCARB", "2018-04-19"): (0.2, "ASSUMED split 1:5: close ratio 0.21, ISIN changes next day"),
    ("GRUH", "2018-06-06"): (0.5, "ASSUMED split 1:2: close ratio 0.523, volume x2.0"),
    ("SINTEX", "2017-05-25"): (0.25, "ASSUMED 4:1 split-like: close ratio 0.248, volume x46.8, ISIN unchanged"),
    ("DISHMAN", "2016-05-02"): (0.5, "ASSUMED bonus 1:1: close ratio 0.499, volume x1.7"),
    ("MANPASAND", "2017-09-27"): (0.5, "ASSUMED bonus 1:1: close ratio 0.482, volume x1.1"),
    ("VIVIMEDLAB", "2016-04-06"): (0.2, "ASSUMED split 1:5: close ratio 0.208, ISIN changes next day"),
    ("ARCOTECH", "2017-06-22"): (0.2, "ASSUMED split 1:5: close ratio 0.192, ISIN changes next day"),
    ("TIDEWATER", "2016-03-16"): (0.25, "ASSUMED split 1:4: close ratio 0.243, ISIN changes next day"),
    ("TIDEWATER", "2021-07-26"): (0.2, "ASSUMED split 1:5: close ratio 0.21, ISIN changes next day"),
    ("ATLASCYCLE", "2017-10-30"): (0.5, "ASSUMED split 1:2: close ratio 0.525, ISIN changes next day"),
    ("COSMOFILMS", "2022-06-16"): (2 / 3, "ASSUMED bonus 1:2: close ratio 0.658"),
}
# >30% one-day falls deliberately NOT adjusted (genuine crash, demerger or scheme: value not recoverable from price alone)
NOT_ADJUSTED = {
    ("DHFL", "2018-09-21"): "genuine crash (-42.6%, volume x28)",
    ("DHFL", "2018-09-25"): "genuine crash",
    ("8KMILES", "2016-10-10"): "genuine crash (-59%, fraud allegations); ISIN change 3 days later not a price event",
    ("ABIRLANUVO", "2016-01-20"): "likely demerger (-57%); consideration not in price data; left unadjusted (same convention as Upstox demerger gaps)",
    ("MAJESCO", "2020-12-23"): "scheme/delisting artefact (-98.8%); left unadjusted",
    ("SUNILHITEC", "2016-12-01"): "ratio 0.053 not a standard ratio; left unadjusted",
    ("FRL", "2016-05-11"): "-87%; scheme event; left unadjusted",
    ("BCG", "2022-03-15"): "ratio 0.63 ambiguous; left unadjusted",
    ("EQUITAS", "2018-10-26"): "-22.8% volume x40: demerger; left unadjusted",
}
MISSING_SESSION_RECON = "2020-07-31"   # full session absent from the file; close recoverable from next day's NSE PREVCLOSE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--mongo-uri", default="mongodb://localhost:27017/")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    use = ["SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TIMESTAMP", "ISIN"]
    df = pd.read_csv(a.csv, usecols=use, dtype={"SYMBOL": str, "SERIES": str, "ISIN": str})
    df["date"] = pd.to_datetime(df["TIMESTAMP"])
    m = df[df.SERIES.isin(["EQ", "BE", "BZ"])].copy()          # BL (block deal) and other series excluded: different prices
    m = m.sort_values(["SYMBOL", "date", "TOTTRDQTY"]).drop_duplicates(["SYMBOL", "date"], keep="last")
    m = m.rename(columns={"SYMBOL": "symbol", "OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close",
                          "PREVCLOSE": "prevclose", "TOTTRDQTY": "volume"})
    m = m[(m.date >= "2016-01-01")].reset_index(drop=True)

    db = MongoClient(a.mongo_uri, serverSelectionTimeoutMS=3000)["stock_scanner"]
    db_isin = {d["isin"] for d in db["symbols"].find({}, {"isin": 1, "_id": 0})}
    db_sym = {s.replace(".NS", "") for s in db["upstox_daily_ohlcv"].distinct("symbol")}
    db_sym |= {s.replace(".NS", "") for s in db["external_daily_ohlcv"].distinct("symbol")}

    # selection by full-market 50d turnover rank
    m["to"] = m.close * m.volume
    to50 = m.pivot(index="date", columns="symbol", values="to").rolling(50, min_periods=30).mean()
    best_rank = to50.rank(axis=1, ascending=False).min()
    info = m.groupby("symbol").agg(first=("date", "min"), last=("date", "max"), isn=("ISIN", "last"))
    info["best_rank"] = best_rank
    sel = info[(~info.isn.str.startswith("INF")) & (~info.isn.isin(db_isin)) & (~info.index.isin(db_sym))
               & (info.best_rank <= 250) & (info["last"] < "2024-12-01")]
    names = sorted(set(sel.index) | set(TARGETS))
    names = [s for s in names if s in info.index]
    print("selected", len(names), "symbols;", "targets missing from file:", [t for t in TARGETS if t not in info.index])

    out, decisions, recon, warnings = [], [], [], []
    for s in names:
        g = m[m.symbol == s].sort_values("date").reset_index(drop=True)
        g["ratio"] = g.close / g.close.shift(1)
        # explicit decisions
        f = np.ones(len(g))
        for (sy, d), (ratio, ev) in ADJUST.items():
            if sy != s:
                continue
            idx = g.index[g.date == pd.Timestamp(d)]
            if len(idx) != 1:
                warnings.append(f"{s} {d}: adjust date not found"); continue
            i = int(idx[0])
            obs = float(g.ratio.iloc[i])
            if not (abs(obs / ratio - 1) < 0.13):          # close ratio includes that day's genuine move
                warnings.append(f"{s} {d}: observed ratio {obs:.3f} not near {ratio:.3f}; NOT applied"); continue
            f[i] = ratio
            decisions.append({"symbol": s, "date": d, "action": "ADJUST", "ratio": ratio, "observed": round(obs, 4), "evidence": ev})
        cum = np.cumprod(f[::-1])[::-1]
        mult = np.concatenate([cum[1:], [1.0]])
        for c in ("open", "high", "low", "close"):
            g[c] = g[c] * mult
        g["volume"] = g.volume / mult
        # flag large unexplained one-day moves
        for _, r in g[(g.ratio < 0.70) & (g.ratio.notna())].iterrows():
            d = r.date.strftime("%Y-%m-%d")
            if (s, d) in NOT_ADJUSTED:
                decisions.append({"symbol": s, "date": d, "action": "LEAVE", "observed": round(float(r.ratio), 4), "evidence": NOT_ADJUSTED[(s, d)]})
            elif not any(x["symbol"] == s and x["date"] == d for x in decisions):
                warnings.append(f"UNREVIEWED drop {s} {d} ratio {r.ratio:.3f}")
        # reconstruct the one missing full session at a month end from NSE's next-day PREVCLOSE
        nxt = g[g.date > pd.Timestamp(MISSING_SESSION_RECON)].head(1)
        alive = (g.date.min() < pd.Timestamp(MISSING_SESSION_RECON)) and len(nxt) == 1 and \
                (nxt.date.iloc[0] - pd.Timestamp(MISSING_SESSION_RECON)).days <= 4 and (g.date == pd.Timestamp(MISSING_SESSION_RECON)).sum() == 0
        if alive:
            mult_at = float(mult[g.index[g.date > pd.Timestamp(MISSING_SESSION_RECON)][0] - 1])
            pc = float(nxt.prevclose.iloc[0]) * mult_at
            row = {"symbol": s, "date": pd.Timestamp(MISSING_SESSION_RECON), "open": pc, "high": pc, "low": pc, "close": pc,
                   "volume": 0.0}
            g = pd.concat([g, pd.DataFrame([row])], ignore_index=True).sort_values("date").reset_index(drop=True)
            recon.append({"symbol": s, "date": MISSING_SESSION_RECON, "close": round(pc, 4),
                          "note": "close from next-day NSE PREVCLOSE; open/high/low set = close; volume 0 (unknown)"})
        g["out_symbol"] = RENAME.get(s, s) + ".NS"
        out.append(g[["out_symbol", "date", "open", "high", "low", "close", "volume"]])
    allr = pd.concat(out, ignore_index=True)
    allr = allr.drop_duplicates(["out_symbol", "date"], keep="first")
    # validation
    bad = allr[(allr.high < allr.low) | (allr.close > allr.high + 1e-9) | (allr.close < allr.low - 1e-9) | (allr.close <= 0)]
    if len(bad):
        raise SystemExit(f"OHLC validation failed on {len(bad)} rows")
    allr["date_s"] = allr.date.dt.strftime("%Y-%m-%d")
    rep = {"selected_symbols": len(names), "rows": int(len(allr)), "decisions": decisions, "reconstructed_bars": recon,
           "warnings": warnings, "symbols": sorted(allr.out_symbol.unique().tolist()),
           "last_dates": {k: v for k, v in allr.groupby("out_symbol").date_s.max().items()}}
    json.dump(rep, open(a.report, "w"), indent=1, default=str)
    print("rows", len(allr), "| decisions", len(decisions), "| reconstructed", len(recon), "| warnings", len(warnings))
    for w in warnings[:40]:
        print("  WARN", w)
    if a.write:
        col = db["external_daily_ohlcv"]
        ops = [ReplaceOne({"symbol": r.out_symbol, "date": r.date_s, "source": SOURCE},
                          {"symbol": r.out_symbol, "date": r.date_s, "source": SOURCE, "open": float(r.open), "high": float(r.high),
                           "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}, upsert=True)
               for r in allr.itertuples()]
        for i in range(0, len(ops), 5000):
            col.bulk_write(ops[i:i + 5000], ordered=False)
        print("WROTE", len(ops), "rows; total docs source=", SOURCE, col.count_documents({"source": SOURCE}))
    else:
        print("dry-run: nothing written (use --write)")


if __name__ == "__main__":
    main()
