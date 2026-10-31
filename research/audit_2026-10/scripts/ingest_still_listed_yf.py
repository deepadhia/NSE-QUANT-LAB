"""Clean and ingest the validated yfinance still-listed gap names into local Mongo external_daily_ohlcv (source='yfinance_gap').
Reads <dir>/yf_validation.json (ACCEPT rows only), yf_still_listed.parquet, bhavcopy_candidates.parquet.  Dry run by default.

Cleaning (every change is recorded in <dir>/yf_ingest_report.json; nothing is silently repaired):
  1. SPIKE filter: drop a bar whose close moves >40% from the previous bar and >40% back the other way, with the
     two neighbours within 15% of each other (one-bar glitches, e.g. THYROCARE special-session bars with unadjusted prices).
  2. BHAVCOPY RECONCILE: in the overlap, a yf bar whose close differs from the bhavcopy close (x the segment's median
     yf/bhav ratio) by >5% is a bad bar: OHLC replaced by bhavcopy OHLC x that ratio, yf volume kept (e.g. GICHSGFIN 2019-10-16).
  3. FILL: weekday sessions in the Upstox calendar (2016+) that yf lacks but the bhavcopy has are inserted from the bhavcopy
     (OHLC x last known yf/bhav ratio, volume / ratio). Needed because a missing month-end bar force-drops a held stock.
  4. NOT adjusted: unexplained >30% one-day moves present in both sources or only in 2025-26 (corporate actions that Yahoo
     did not adjust). They are LISTED in the report; the sensitivity test excludes those symbols.
Usage: python ingest_still_listed_yf.py <dir> [--write]
"""
import json, os, sys
import numpy as np
import pandas as pd
from pymongo import MongoClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")))
from nse_quant_lab.external_prices import EXTERNAL_COLLECTION, upsert_prices  # noqa: E402
from nse_quant_lab.price_validation import validate_ohlcv  # noqa: E402

SOURCE = "yfinance_gap"


def main():
    d = sys.argv[1]
    write = "--write" in sys.argv
    val = pd.read_json(os.path.join(d, "yf_validation.json"))
    acc = sorted(val[val.outcome == "ACCEPT"].symbol)
    yf = pd.read_parquet(os.path.join(d, "yf_still_listed.parquet"))
    yf["sym"] = yf.symbol.str.replace(".NS", "", regex=False)
    yf["date"] = pd.to_datetime(yf.date)
    bh = pd.read_parquet(os.path.join(d, "bhavcopy_candidates.parquet"))
    bh["date"] = pd.to_datetime(bh.date)
    db = MongoClient("mongodb://localhost:27017/")["stock_scanner"]
    cal = pd.DatetimeIndex(sorted(pd.to_datetime([x["date"] for x in db["benchmark_ohlcv"].find({"symbol": "^NSEI"}, {"_id": 0, "date": 1})])))

    out, rep = [], {"spikes_dropped": [], "bars_patched": [], "big_moves_left_unadjusted": [], "validation_errors": [],
                    "missing_month_end_sessions": {}, "missing_sessions_total": {}}
    for s in acc:
        g = yf[yf.sym == s].sort_values("date").reset_index(drop=True)
        c = g.close.to_numpy()
        drop = []
        for i in range(1, len(g) - 1):
            a, b = c[i] / c[i - 1] - 1, c[i + 1] / c[i] - 1
            if abs(a) > 0.40 and abs(b) > 0.40 and a * b < 0 and abs(c[i + 1] / c[i - 1] - 1) < 0.15:
                drop.append(i)
        for i in drop:
            rep["spikes_dropped"].append({"symbol": s, "date": g.date[i].strftime("%Y-%m-%d"), "close": float(c[i])})
        g = g.drop(index=drop).reset_index(drop=True)
        # reconcile with the bhavcopy
        b = bh[bh.symbol == s].set_index("date")
        m = g[["date", "close"]].merge(b[["close", "open", "high", "low"]].reset_index(), on="date", suffixes=("_y", "_b"))
        lr = np.log(m.close_y / m.close_b).to_numpy()
        seg = np.zeros(len(lr), int)
        for i in np.where(np.abs(np.diff(lr)) > 0.03)[0] + 1:
            seg[i:] += 1
        med = np.zeros(len(lr))
        for k in np.unique(seg):
            med[seg == k] = np.median(lr[seg == k])
        bad = set()
        # (a) short segments (<=2 bars) bracketed by two segments whose ratios agree within 1% are bar errors, not corporate
        #     actions (a real split changes the ratio permanently, so before/after ratios would differ)
        for k in np.unique(seg):
            ix = np.where(seg == k)[0]
            if len(ix) <= 2 and k > 0 and k < seg.max():
                before = np.median(lr[seg == k - 1]); after = np.median(lr[seg == k + 1])
                if abs(before - after) < 0.01 and np.all(np.abs(lr[ix] - before) > 0.02):
                    for i in ix:
                        med[i] = before
                        bad.add(int(i))
        # (b) a 5%+ disagreement inside a longer segment, isolated (neighbours agree with the segment median)
        for i in np.where(np.abs(lr - med) > 0.05)[0]:
            nb = [j for j in (i - 1, i + 1) if 0 <= j < len(lr)]
            if all(abs(lr[j] - med[j]) < 0.01 for j in nb):
                bad.add(int(i))
        for i in sorted(bad):
            if True:
                dt = m.date[i]
                k = g.index[g.date == dt][0]
                r = float(np.exp(med[i]))
                old = float(g.close[k])
                for col in ("open", "high", "low", "close"):
                    g.loc[k, col] = float(m[col + "_b"][i] if col + "_b" in m else b.loc[dt, col]) * r
                rep["bars_patched"].append({"symbol": s, "date": dt.strftime("%Y-%m-%d"), "old_close": old, "new_close": float(g.close[k])})
        # fill sessions the bhavcopy has and yf lacks, scaled by the last known yf/bhav ratio
        have = set(g.date)
        fills = []
        if len(m):
            for dt in b.index:
                if dt in have or dt < g.date.min() or dt > g.date.max() or dt < pd.Timestamp("2016-01-01") or dt not in cal or dt.weekday() >= 5:
                    continue
                j = np.searchsorted(m.date.to_numpy(), np.datetime64(dt)) - 1
                if j < 0:
                    continue
                r = float(np.exp(med[j]))
                row = b.loc[dt]
                fills.append({"symbol": g.symbol.iloc[0], "date": dt, "open": row.open * r, "high": row.high * r, "low": row.low * r,
                              "close": row.close * r, "volume": row.volume / r})
        if fills:
            g = pd.concat([g, pd.DataFrame(fills)], ignore_index=True).sort_values("date").reset_index(drop=True)
            rep.setdefault("filled_from_bhavcopy", {})[s] = [x["date"].strftime("%Y-%m-%d") for x in fills]
        g["ret"] = g.close.pct_change()
        for _, r in g[(g.ret < -0.30) | (g.ret > 0.60)].iterrows():
            if r.date >= pd.Timestamp("2016-01-01"):
                rep["big_moves_left_unadjusted"].append({"symbol": s, "date": r.date.strftime("%Y-%m-%d"), "ret": round(float(r.ret), 3)})
        g = g[["symbol", "date", "open", "high", "low", "close", "volume"]].assign(source=SOURCE)
        v = validate_ohlcv(g, s + ".NS", calendar=cal)
        if v["has_errors"]:   # recorded, symbol kept: the test harness drops invalid bars (e.g. a single OHLC-envelope violation)
            rep["validation_errors"].append({"symbol": s, "issues": [i for i in v["issues"] if i.get("severity") == "ERROR"]})
        sess = cal[(cal >= g.date.min()) & (cal <= g.date.max())]
        miss = sess.difference(pd.DatetimeIndex(g.date))
        rep["missing_sessions_total"][s] = int(len(miss))
        mes = pd.Series(cal, index=cal).groupby(cal.to_period("M")).max()
        me_miss = [x.strftime("%Y-%m-%d") for x in mes if x in miss and x >= pd.Timestamp("2016-01-01") and x.weekday() < 5]
        if me_miss:
            rep["missing_month_end_sessions"][s] = me_miss
        out.append(g)
    allr = pd.concat(out, ignore_index=True)
    rep.update(symbols=len(out), rows=int(len(allr)), symbols_with_val_errors=len(rep["validation_errors"]))
    json.dump(rep, open(os.path.join(d, "yf_ingest_report.json"), "w"), indent=1, default=str)
    print("symbols", len(out), "rows", len(allr), "| spikes dropped", len(rep["spikes_dropped"]), "| bars patched", len(rep["bars_patched"]),
          "| big moves left", len(rep["big_moves_left_unadjusted"]), "| validation-error symbols", len(rep["validation_errors"]), "| symbols w/ fills", len(rep.get("filled_from_bhavcopy", {})),
          "| bars filled", sum(len(v) for v in rep.get("filled_from_bhavcopy", {}).values()),
          "| symbols with missing month-end sessions", len(rep["missing_month_end_sessions"]),
          "| total missing sessions", sum(rep["missing_sessions_total"].values()))
    print("spikes:", rep["spikes_dropped"]); print("patched:", rep["bars_patched"])
    print("missing month-ends (first 15):", dict(list(rep["missing_month_end_sessions"].items())[:15]))
    for e in rep["validation_errors"][:10]:
        print("VALERR", e)
    if write:
        allr["symbol"] = allr.symbol.where(allr.symbol.str.endswith(".NS"), allr.symbol + ".NS")
        res = upsert_prices(db[EXTERNAL_COLLECTION], allr)
        print("WROTE", res, "total docs source=", SOURCE, db[EXTERNAL_COLLECTION].count_documents({"source": SOURCE}))
    else:
        print("dry-run: nothing written")


if __name__ == "__main__":
    main()
