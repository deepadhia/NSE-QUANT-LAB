"""Select still-trading (at the bhavcopy end) equity symbols that are absent from the research DB but were liquid.

Same data-driven rule as ingest_bhavcopy_delisted.py (no strategy information): equity-ISIN symbols whose ISIN and
symbol are NOT in the DB, best full-market 50-day turnover rank <= 250 on some date, but whose last print is
>= 2024-12-01 (i.e. still listed at the file end, so 2025-26 prices must come from another source).
Writes a JSON with the candidates, their ISIN, first/last dates, best rank, and the bhavcopy daily closes for later
cross-validation of the second source (bhavcopy_closes parquet, unadjusted).

Usage: python select_still_listed.py --csv <Combined_Data_2016_2024.csv> --out <dir>
"""
import argparse, json, os
import pandas as pd
from pymongo import MongoClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mongo-uri", default="mongodb://localhost:27017/")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    use = ["SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TIMESTAMP", "ISIN"]
    df = pd.read_csv(a.csv, usecols=use, dtype={"SYMBOL": str, "SERIES": str, "ISIN": str})
    df["date"] = pd.to_datetime(df["TIMESTAMP"])
    m = df[df.SERIES.isin(["EQ", "BE", "BZ"])].copy()
    m = m.sort_values(["SYMBOL", "date", "TOTTRDQTY"]).drop_duplicates(["SYMBOL", "date"], keep="last")
    m = m.rename(columns={"SYMBOL": "symbol", "OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close",
                          "PREVCLOSE": "prevclose", "TOTTRDQTY": "volume"})
    m = m[m.date >= "2016-01-01"].reset_index(drop=True)

    db = MongoClient(a.mongo_uri, serverSelectionTimeoutMS=3000)["stock_scanner"]
    db_isin = {d["isin"] for d in db["symbols"].find({}, {"isin": 1, "_id": 0})}
    db_sym = {s.replace(".NS", "") for s in db["upstox_daily_ohlcv"].distinct("symbol")}
    db_sym |= {s.replace(".NS", "") for s in db["external_daily_ohlcv"].distinct("symbol")}

    m["to"] = m.close * m.volume
    to50 = m.pivot(index="date", columns="symbol", values="to").rolling(50, min_periods=30).mean()
    best_rank = to50.rank(axis=1, ascending=False).min()
    days_top300 = (to50.rank(axis=1, ascending=False) <= 300).sum()
    info = m.groupby("symbol").agg(first=("date", "min"), last=("date", "max"), isn=("ISIN", "last"))
    info["best_rank"] = best_rank
    info["days_top300"] = days_top300
    sel = info[(~info.isn.str.startswith("INF")) & (~info.isn.isin(db_isin)) & (~info.index.isin(db_sym))
               & (info.best_rank <= 250) & (info["last"] >= "2024-12-01")].copy()
    print("still-listed candidates:", len(sel))
    out = sel.reset_index()
    out["first"] = out["first"].dt.strftime("%Y-%m-%d")
    out["last"] = out["last"].dt.strftime("%Y-%m-%d")
    out.to_json(os.path.join(a.out, "still_listed_candidates.json"), orient="records", indent=1)
    keep = m[m.symbol.isin(sel.index)][["symbol", "date", "open", "high", "low", "close", "prevclose", "volume"]]
    keep.to_parquet(os.path.join(a.out, "bhavcopy_candidates.parquet"))
    print(out.sort_values("best_rank").head(15).to_string())
    print("bhavcopy rows kept", len(keep))


if __name__ == "__main__":
    main()
