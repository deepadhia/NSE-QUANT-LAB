"""Fetch yfinance daily OHLCV (split/bonus-adjusted, not dividend-adjusted) for the still-listed gap names.
Reads still_listed_candidates.json (select_still_listed.py), writes yf_still_listed.parquet + yf_fetch_status.json into
<dir>. Nothing is written to Mongo here; validation against the bhavcopy comes first.
Usage: python fetch_still_listed_yf.py <dir> [--start 2014-01-01] [--end 2026-10-23] [--sleep 0.4]
"""
import argparse, json, os, sys, time, logging
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")))
from nse_quant_lab.external_prices import fetch_yfinance  # noqa: E402
from nse_quant_lab.price_validation import drop_carried_forward_rows  # noqa: E402

logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--sleep", type=float, default=0.4)
    a = ap.parse_args()
    cands = json.load(open(os.path.join(a.dir, "still_listed_candidates.json")))
    frames, status = [], {}
    for i, c in enumerate(cands, 1):
        sym = c["symbol"] + ".NS"
        df, st = fetch_yfinance(sym, a.start, a.end)
        dropped = []
        if st == "OK":
            df, dropped = drop_carried_forward_rows(df)
            frames.append(df)
        status[c["symbol"]] = {"status": st, "rows": len(df), "placeholders_dropped": len(dropped),
                               "first": None if df.empty else df.date.min().strftime("%Y-%m-%d"),
                               "last": None if df.empty else df.date.max().strftime("%Y-%m-%d")}
        print(f"[{i}/{len(cands)}] {sym} {st} {len(df)}", flush=True)
        time.sleep(a.sleep)
    if frames:
        pd.concat(frames, ignore_index=True).to_parquet(os.path.join(a.dir, "yf_still_listed.parquet"))
    json.dump(status, open(os.path.join(a.dir, "yf_fetch_status.json"), "w"), indent=1)
    print(pd.Series([v["status"] for v in status.values()]).value_counts().to_dict())


if __name__ == "__main__":
    main()
