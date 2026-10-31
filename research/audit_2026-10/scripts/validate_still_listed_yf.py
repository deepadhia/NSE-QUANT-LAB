"""Validate yfinance prices for the still-listed gap names against the (unadjusted) Kaggle bhavcopy BEFORE ingesting.

yfinance = split/bonus-adjusted, bhavcopy = raw. So log(yf_close / bhav_close) must be PIECEWISE CONSTANT: constant
between corporate actions, stepping only at split/bonus dates (and, within tolerance, ~0 for symbols with none).

PRE-STATED rules (written before looking at results):
  ACCEPT  if  (1) >=95% of overlap dates have |log-ratio - segment median| < 1.0%   (levels agree between steps)
          and (2) <= 4 step days (|d log-ratio| > 3%) in the overlap; each is reported with its ratio
          and (3) every yf row passes OHLC sanity (high>=low etc., all > 0) and overlap >= 250 dates
          and (4) yf covers beyond 2024-12-13 (it must supply the 2025-26 part, which the bhavcopy lacks).
  Otherwise the symbol is REJECTED (not ingested) with the reason listed. Nothing is repaired by hand.
Additional output: for accepted symbols, the yf last date (still trading vs data ended) and step table.
Usage: python validate_still_listed_yf.py <dir>   (needs bhavcopy_candidates.parquet, yf_still_listed.parquet)
"""
import json, os, sys
import numpy as np
import pandas as pd


def main():
    d = sys.argv[1]
    bh = pd.read_parquet(os.path.join(d, "bhavcopy_candidates.parquet"))
    yf = pd.read_parquet(os.path.join(d, "yf_still_listed.parquet"))
    yf["sym"] = yf.symbol.str.replace(".NS", "", regex=False)
    cands = json.load(open(os.path.join(d, "still_listed_candidates.json")))
    status = json.load(open(os.path.join(d, "yf_fetch_status.json")))
    rows = []
    for c in cands:
        s = c["symbol"]
        r = {"symbol": s, "yf_status": status[s]["status"], "yf_rows": status[s]["rows"], "yf_last": status[s]["last"],
             "best_rank": c["best_rank"]}
        if status[s]["status"] != "OK":
            r.update(outcome="REJECT", reason=f"yfinance {status[s]['status']}")
            rows.append(r); continue
        y = yf[yf.sym == s].copy()
        y["date"] = pd.to_datetime(y.date)
        ohlc_bad = int(((y[["open", "high", "low", "close"]] <= 0).any(axis=1) | (y.high < y.low) | (y.close > y.high * (1 + 1e-9))
                        | (y.close < y.low * (1 - 1e-9))).sum())
        b = bh[bh.symbol == s].copy()
        b["date"] = pd.to_datetime(b.date)
        m = b[["date", "close"]].merge(y[["date", "close"]], on="date", suffixes=("_b", "_y")).sort_values("date")
        r.update(overlap=len(m), ohlc_bad=ohlc_bad)
        if len(m) < 250:
            r.update(outcome="REJECT", reason=f"overlap {len(m)} < 250"); rows.append(r); continue
        lr = np.log(m.close_y / m.close_b).to_numpy()
        step_idx = np.where(np.abs(np.diff(lr)) > 0.03)[0] + 1
        seg_id = np.zeros(len(lr), int)
        for i in step_idx:
            seg_id[i:] += 1
        dev = np.zeros(len(lr))
        for k in np.unique(seg_id):
            ix = seg_id == k
            dev[ix] = lr[ix] - np.median(lr[ix])
        agree = float((np.abs(dev) < 0.01).mean())
        steps = [{"date": m.date.iloc[i].strftime("%Y-%m-%d"), "ratio_change": round(float(np.exp(lr[i] - lr[i - 1])), 4)} for i in step_idx]
        r.update(agree_pct=round(agree * 100, 1), n_steps=len(steps), steps=steps[:6], base_ratio=round(float(np.exp(np.median(lr))), 4))
        reasons = []
        if agree < 0.95: reasons.append(f"levels agree only {agree:.1%}")
        if len(steps) > 4: reasons.append(f"{len(steps)} step days")
        if ohlc_bad: reasons.append(f"{ohlc_bad} OHLC-invalid yf rows")
        if status[s]["last"] is None or status[s]["last"] <= "2024-12-13": reasons.append("yf does not extend past 2024-12-13")
        r.update(outcome="REJECT" if reasons else "ACCEPT", reason="; ".join(reasons))
        rows.append(r)
    df = pd.DataFrame(rows)
    df.to_json(os.path.join(d, "yf_validation.json"), orient="records", indent=1)
    print(df.outcome.value_counts().to_dict())
    print(df[df.outcome == "REJECT"].reason.str.replace(r"\d+", "N", regex=True).value_counts().to_string())
    print("accepted still-trading at 2026-10 (yf last >= 2026-10-01):",
          int(((df.outcome == "ACCEPT") & (df.yf_last >= "2026-10-01")).sum()))
    print("accepted with steps:", int(((df.outcome == "ACCEPT") & (df.n_steps > 0)).sum()))


if __name__ == "__main__":
    main()
