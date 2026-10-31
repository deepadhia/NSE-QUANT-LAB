"""Ingest the 10 liquid names yfinance could not supply (AMIORG, BARBEQUE, GLS, INFIBEAM, ISEC, ITDCEM, PEL, ROLTA, SEQUENT,
SMLISUZU) from the Kaggle bhavcopy (2016-01 .. 2024-12-13) into local Mongo external_daily_ohlcv, source='kaggle_bhavcopy_gap10'.
These names still trade after 2024-12-13, so they are only valid for windows that END on/before 2024-12-13.
Bhavcopy is raw: explicit adjustment decisions below (evidence from price/volume/ISIN around each event).
Dry run by default; --write to upsert.  Usage: python ingest_unresolved_bhavcopy.py --csv <file> [--write]
"""
import argparse
import numpy as np
import pandas as pd
from pymongo import MongoClient, ReplaceOne

SOURCE = "kaggle_bhavcopy_gap10"
NAMES = ["AMIORG", "BARBEQUE", "GLS", "INFIBEAM", "ISEC", "ITDCEM", "PEL", "ROLTA", "SEQUENT", "SMLISUZU"]
ADJUST = {   # (symbol, ex-date) -> (ratio applied to ALL EARLIER prices, evidence)
    ("INFIBEAM", "2017-08-31"): (0.1, "10:1 split: open ratio 0.1004, volume x26, ISIN changes next day"),
    ("INFIBEAM", "2021-03-18"): (0.5, "ASSUMED 2:1 split/bonus: open ratio 0.484, price flat afterwards, ISIN unchanged"),
    ("INFIBEAM", "2022-03-14"): (0.5, "ASSUMED 2:1 split/bonus: open ratio 0.480, price flat afterwards, ISIN unchanged"),
}
LEFT = {   # large falls deliberately NOT adjusted
    ("INFIBEAM", "2018-09-21"): "genuine crash (intraday low 140.95 from 235, volume x5)",
    ("INFIBEAM", "2018-09-28"): "genuine crash (intraday low 53.5 from 180, volume x42)",
    ("PEL", "2022-08-30"): "ratio 0.55 not a standard split, volume x5, ISIN unchanged: demerger-type; value of spin-off shares not in price data; left unadjusted",
    ("ROLTA", "2018-09-19"): "-20% fall, no corporate-action signature",
}
MISSING_SESSION = pd.Timestamp("2020-07-31")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--mongo-uri", default="mongodb://localhost:27017/")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    use = ["SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TIMESTAMP"]
    df = pd.read_csv(a.csv, usecols=use, dtype={"SYMBOL": str, "SERIES": str})
    df = df[df.SYMBOL.isin(NAMES) & df.SERIES.isin(["EQ", "BE", "BZ"])].copy()
    df["date"] = pd.to_datetime(df.TIMESTAMP)
    df = df.sort_values(["SYMBOL", "date", "TOTTRDQTY"]).drop_duplicates(["SYMBOL", "date"], keep="last")
    df = df.rename(columns={"OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close", "PREVCLOSE": "prevclose", "TOTTRDQTY": "volume"})
    out = []
    for s in NAMES:
        g = df[df.SYMBOL == s].sort_values("date").reset_index(drop=True)
        assert len(g), s
        g["raw_close"] = g.close.copy()
        for (sy, d), (ratio, ev) in ADJUST.items():
            if sy != s:
                continue
            before = g.date < pd.Timestamp(d)
            obs = float(g.open[g.date == pd.Timestamp(d)].iloc[0] / g.close[g.date < pd.Timestamp(d)].iloc[-1])
            assert abs(obs / ratio - 1) < 0.1, (s, d, obs, ratio)     # fail loudly if the event is not what the decision assumed
            for c in ("open", "high", "low", "close"):
                g.loc[before, c] = g.loc[before, c] * ratio
            g.loc[before, "volume"] = g.loc[before, "volume"] / ratio
        r = g.close / g.close.shift(1)
        for _, row in g[(r < 0.7) & r.notna()].iterrows():     # any remaining >30% fall must be a reviewed decision
            k = (s, row.date.strftime("%Y-%m-%d"))
            if k in LEFT or k in {(x, y) for (x, y) in ADJUST}:
                continue
            raise SystemExit(f"UNREVIEWED drop {k}")
        nxt = g[g.date > MISSING_SESSION].head(1)
        if g.date.min() < MISSING_SESSION and len(nxt) == 1 and (g.date == MISSING_SESSION).sum() == 0 and (nxt.date.iloc[0] - MISSING_SESSION).days <= 4:
            # next day's raw PREVCLOSE, scaled to the adjusted basis via the already-adjusted/raw ratio of the next close
            nrow = g[g.date == nxt.date.iloc[0]].iloc[0]
            scale = float(nrow.close / nrow.raw_close)
            pc = float(nxt.prevclose.iloc[0]) * scale
            g = pd.concat([g, pd.DataFrame([{"SYMBOL": s, "date": MISSING_SESSION, "open": pc, "high": pc, "low": pc, "close": pc, "volume": 0.0}])],
                          ignore_index=True).sort_values("date").reset_index(drop=True)
            print(f"  {s}: reconstructed {MISSING_SESSION.date()} close {pc:.2f} (volume 0)")
        g["symbol"] = s + ".NS"
        out.append(g[["symbol", "date", "open", "high", "low", "close", "volume"]])
    allr = pd.concat(out, ignore_index=True)
    bad = allr[(allr.high < allr.low) | (allr.close > allr.high + 1e-9) | (allr.close < allr.low - 1e-9) | (allr.close <= 0)]
    assert not len(bad), bad.head()
    print(allr.groupby("symbol").agg(rows=("date", "size"), first=("date", "min"), last=("date", "max")).to_string())
    if a.write:
        col = MongoClient(a.mongo_uri)["stock_scanner"]["external_daily_ohlcv"]
        allr["d"] = allr.date.dt.strftime("%Y-%m-%d")
        ops = [ReplaceOne({"symbol": r.symbol, "date": r.d, "source": SOURCE},
                          {"symbol": r.symbol, "date": r.d, "source": SOURCE, "open": float(r.open), "high": float(r.high), "low": float(r.low),
                           "close": float(r.close), "volume": float(r.volume)}, upsert=True) for r in allr.itertuples()]
        for i in range(0, len(ops), 5000):
            col.bulk_write(ops[i:i + 5000], ordered=False)
        print("WROTE", len(ops), "docs; total source docs:", col.count_documents({"source": SOURCE}))
    else:
        print("dry-run: nothing written")


if __name__ == "__main__":
    main()
