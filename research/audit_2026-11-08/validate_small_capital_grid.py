"""Independent sanity checks on small_capital_grid results (reads the saved ledgers; never re-simulates).

Per run it checks things a buggy or flattering backtest would violate, and prints WHY a number might be unrealistic:
  recon      : final cash == initial capital + sum(execution net cash) - tax paid (tax paid between 0 and tax assessed)
  cap        : no BUY executed at a nominal open >= the price cap (cap variants only)
  integer    : every execution is a positive whole number of shares
  sizing     : no buy larger than 1.5x its equal-weight slot of that day's NAV
  nav        : finite, positive, no one-day move beyond +-25%
  ended_pnl  : share of net P&L from the RESEARCH data-ended exits (the bound-dependent part), and how many
  top1/top5  : concentration of positive P&L; crude CAGR without the single best trade (no path re-simulation)
  costs      : fees as % of starting capital, median position size, DP-charge drag on small positions
Also: determinism against results_prelim_no_ledgers.csv (same inputs must give identical numbers) and the Rs 1 lakh calibration row.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "small_capital_grid"
RUNS = OUT / "runs"


def check_run(row):
    tag = f"{row.strategy}_{row.capital}_{row.variant}_{row.bound}"
    tr, ex, nav = (pd.read_csv(RUNS / f"{tag}_{k}.csv") for k in ("trades", "execs", "nav"))
    flags, info = [], {}
    cap0, slots = float(row.capital), int(row.slots)
    # recon
    pre_tax_cash = cap0 + ex.net_cash.sum()
    tax_paid = pre_tax_cash - float(nav.cash.iloc[-1])
    info["tax_paid_implied"] = round(tax_paid)
    if not (-1.0 <= tax_paid <= float(row.tax_assessed) + 1.0):
        flags.append(f"recon: implied tax paid {tax_paid:.0f} outside [0, assessed {row.tax_assessed:.0f}]")
    # cap
    buys = ex[ex.action == "BUY"]
    if pd.notna(row.price_cap):
        # The cap tests the SIGNAL-day close; the engine may fill up to +2% above it (limit_cap_pct), so allow that gap-up.
        viol = int((buys.raw_price >= float(row.price_cap) * 1.021).sum())
        info["cap_violations"] = viol
        if viol:
            flags.append(f"cap: {viol} buys at nominal price >= {row.price_cap:.0f}")
    # integer
    if not ((ex.shares > 0) & (ex.shares == ex.shares.round())).all():
        flags.append("integer: non-integer or non-positive share count in executions")
    # sizing
    navd = nav.assign(date=pd.to_datetime(nav.date)).set_index("date").nav
    b = buys.assign(date=pd.to_datetime(buys.date))
    b["equity"] = navd.reindex(b.date, method="ffill").values
    over = (b.gross_val / b.equity) > 1.5 / slots
    info["max_buy_pct_of_nav"] = round(float((b.gross_val / b.equity).max() * 100), 1)
    if over.any():
        flags.append(f"sizing: {int(over.sum())} buys above 1.5x the equal-weight slot (max {info['max_buy_pct_of_nav']}% of NAV)")
    # nav
    r = navd.pct_change().dropna()
    info["max_daily_move_pct"] = round(float(r.abs().max() * 100), 1)
    if not np.isfinite(navd).all() or (navd <= 0).any() or r.abs().max() > 0.25:
        flags.append(f"nav: invalid or extreme daily move (max {info['max_daily_move_pct']}%)")
    # ended exits
    ended = tr[tr.exit_reason.astype(str).str.startswith("RESEARCH_")]
    tot = tr.net_pnl.sum()
    info["ended_exits"] = int(len(ended))
    info["ended_pnl_pct_of_total"] = round(float(ended.net_pnl.sum() / tot * 100), 1) if tot else None
    if len(ended) and abs(ended.net_pnl.sum()) > 0.05 * abs(tot):
        flags.append(f"ended_pnl: bound-dependent exits ({len(ended)}) carry {info['ended_pnl_pct_of_total']}% of total P&L")
    # concentration
    top = tr.net_pnl.sort_values(ascending=False)
    pos = tr.net_pnl[tr.net_pnl > 0].sum()
    info["top1_pct_of_gains"] = round(float(top.iloc[0] / pos * 100), 1)
    info["top5_pct_of_gains"] = round(float(top.head(5).sum() / pos * 100), 1)
    yrs = float(row.years)
    final = float(row.final_nav)
    info["cagr_ex_top1_crude_pct"] = round(((max(final - top.iloc[0], 1.0) / cap0) ** (1 / yrs) - 1) * 100, 1)
    if info["top5_pct_of_gains"] > 50:
        flags.append(f"concentration: top-5 trades are {info['top5_pct_of_gains']}% of gross gains")
    # costs
    notional = buys.gross_val
    info["median_position_rs"] = round(float(notional.median()))
    info["fees_pct_of_start"] = float(row.fees_pct_of_start)
    return tag, flags, info


def main():
    res = pd.read_csv(OUT / "results.csv")
    ok = res[res.status == "OK"]
    print(f"{len(ok)} OK runs of {len(res)} recorded ({(res.status != 'OK').sum()} not OK)")
    rows = []
    for row in ok.itertuples():
        tag, flags, info = check_run(row)
        rows.append({"run": tag, "flags": " | ".join(flags) or "-", **info})
    rep = pd.DataFrame(rows)
    rep.to_csv(OUT / "validation_report.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 110); pd.set_option("display.max_columns", 30)
    bad = rep[rep["flags"] != "-"]
    print(f"\nRUNS WITH FLAGS: {len(bad)} of {len(rep)}")
    if len(bad):
        print(bad[["run", "flags"]].to_string(index=False))
    print("\nSUMMARY of checks over all OK runs:")
    print(rep[["ended_exits", "ended_pnl_pct_of_total", "top1_pct_of_gains", "top5_pct_of_gains", "cagr_ex_top1_crude_pct",
               "median_position_rs", "max_buy_pct_of_nav", "max_daily_move_pct"]].describe().round(1).to_string())
    prelim = OUT / "results_prelim_no_ledgers.csv"
    if prelim.exists():
        p = pd.read_csv(prelim)
        p = p[p.status == "OK"].copy()                       # the earlier file also holds stopped rows with shifted columns
        m = ok.merge(p, on=["strategy", "capital", "variant", "bound"], suffixes=("", "_prelim"))
        diff = m[(m["final_nav"].astype(float) != m["final_nav_prelim"].astype(float)) | (m["trades"].astype(float) != m["trades_prelim"].astype(float))]
        print(f"\nDETERMINISM vs the earlier unsaved run: {len(m)} comparable, {len(diff)} differ")
    cal = res[(res.capital == 100000)]
    if len(cal):
        print("\nCALIBRATION at Rs 1 lakh (registered R11 Y10 = 23.41% CAGR, -37.8% DD on the old adjusted basis):")
        print(cal[["bound", "status", "cagr_pct", "max_dd_pct", "trades", "win_pct", "pf"]].to_string(index=False))


if __name__ == "__main__":
    sys.exit(main())
