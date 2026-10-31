#!/usr/bin/env python3
"""No-skill baseline: equal-weight ALL point-in-time-eligible names of a universe spec, rebalanced monthly open->open,
gross of costs and tax, dead names marked at last close (and at zero as the adversarial case)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from alpha_lab import LAB, OUT, UNIVERSES  # noqa: E402


def main() -> None:
    u = pd.read_parquet(LAB, columns=["symbol", "date", "series", "open", "close", "adt50m"])
    dates = pd.DatetimeIndex(np.sort(u["date"].unique()))
    per = dates.to_period("M")
    last_d = pd.Series(dates, index=dates).groupby(per).max()
    first_d = pd.Series(dates, index=dates).groupby(per).min()
    first_set, last_set = set(first_d), set(last_d)
    last_seen = u.groupby("symbol")["date"].max()
    op = u[u["date"].isin(first_set)].pivot(index="date", columns="symbol", values="open")
    me = u[u["date"].isin(last_set)]
    months = list(last_d.index)
    res = {}
    for name, (series_ok, min_adt, min_px) in UNIVERSES.items():
        rets_last, rets_zero, counts, idx = [], [], [], []
        for i in range(len(months) - 2):
            d_dec = last_d[months[i]]
            d_in, d_out = first_d[months[i + 1]], first_d[months[i + 2]]
            if d_in < pd.Timestamp("2007-01-01"):
                continue
            r = me[me["date"] == d_dec]
            r = r[r["series"].isin(series_ok) & (r["adt50m"] >= min_adt) & (r["close"] >= min_px)]
            syms = [s for s in r["symbol"] if s in op.columns and not np.isnan(op.at[d_in, s])]
            if not syms:
                continue
            o_in = op.loc[d_in, syms].astype(float)
            o_out = op.loc[d_out, syms].astype(float)
            dead = o_out.isna()
            if dead.any():
                lastpx = {s: float(u.loc[(u.symbol == s) & (u.date <= d_out), "close"].iloc[-1]) for s in o_out.index[dead]}
                o_out = o_out.fillna(pd.Series(lastpx))
            ret = (o_out / o_in - 1).dropna()
            died = [s for s in ret.index if last_seen[s] < d_out]
            rets_last.append(ret.mean())
            rets_zero.append(ret.where(~ret.index.isin(died), -1.0).mean())
            counts.append(len(ret))
            idx.append(d_in)
        s_last = pd.Series(rets_last, index=idx)
        s_zero = pd.Series(rets_zero, index=idx)
        res[name] = {
            "avg_names": float(np.mean(counts)),
            "cagr_last_pct": float((np.prod(1 + s_last) ** (12 / len(s_last)) - 1) * 100),
            "cagr_dead_zero_pct": float((np.prod(1 + s_zero) ** (12 / len(s_zero)) - 1) * 100),
            "cagr_2016_on_pct": float((np.prod(1 + s_last[s_last.index >= '2016-01-01']) ** (12 / (s_last.index >= '2016-01-01').sum()) - 1) * 100),
            "cagr_2021_on_pct": float((np.prod(1 + s_last[s_last.index >= '2021-01-01']) ** (12 / (s_last.index >= '2021-01-01').sum()) - 1) * 100),
            "cagr_2007_2015_pct": float((np.prod(1 + s_last[s_last.index < '2016-01-01']) ** (12 / (s_last.index < '2016-01-01').sum()) - 1) * 100),
        }
        s_last.to_csv(OUT / f"basket_monthly_{name}.csv")
        print(name, json.dumps(res[name]))
    (OUT / "basket_baselines.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
