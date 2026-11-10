"""Small-capital grid: Rs 30k / 40k / 50k x slot/price-cap variants x the three deployed strategies.

Question (user, 2026-11-08): what if a 15-stock portfolio is limited to share prices under Rs 1,000, at small capital?
DESCRIPTIVE research on a 10-year window (2016-10-01..2026-10-30) of the production strategy classes and production engine on the
NOMINAL-price execution panel (raw prices, integer shares, Rs 14.75 DP charge, dated Indian tax). NOT a forecast, NOT out-of-sample,
and the price cap / slot count are extra configurations tried on the same history (the tried set was already ~180).
The v1 capped rows used the filtered ranking for hold-buffer exits. This corrected v2 run sets max_entry_price on the engine config
so exits see the unfiltered ranking; v1 results are preserved separately and must not be read as corrected capped results.

A third research policy, unverified_share_action_policy="accept_factor", treats a price-factor jump with no matched official event as a split of that
ratio (alpha_semivol_pit stopped on PHILIPCARB 2018 otherwise). Strict mode (all 27 configs, see results_strict_all_incomplete.csv) stopped every run: the window holds 540 symbols whose bars end (delistings, mergers:
CAIRN 2017, MANGCHEFER 2025, ...) and 146 bonus/split events with fractional ratios (OIL 2017, ...); none can be resolved from primary evidence one by one.
So each config is run under two explicit RESEARCH BOUNDS for a held name whose bars end, with bonus fractions rounded down with no cash (the live manager's
behaviour): bound "optimistic" = sold at its last close, bound "pessimistic" = written off (proceeds 0). Reality for any one name lies between; treat the
pair as a range, never as a certified result.

Variants (cap applies to NEW entries only; held names are exempt, like every entry-only filter in this repo):
  S15_nocap   : 15 slots, no price cap (budget gating still skips names the slot cannot afford)
  S15_cap1000 : 15 slots, new entries need nominal price < Rs 1,000
  S10_cap1000 : 10 slots, same cap
Usage: python small_capital_grid.py     (resumable: finished v2 runs are skipped)
"""
import gc
import sys
import time
from pathlib import Path

import pandas as pd
import psutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUT = HERE / "small_capital_grid"
OUT.mkdir(exist_ok=True)
# Earlier cap runs filtered the strategy ranking before the engine could preserve the original hold-buffer rank.
# Keep those artifacts unchanged and write corrected research runs to a new series.
RESULTS = OUT / "results_entry_only_hold_rank_v2.csv"
RUNS = OUT / "runs_entry_only_hold_rank_v2"
RUNS.mkdir(exist_ok=True)
START, END = "2016-10-01", "2026-10-30"
TRIMMED = ROOT / "external" / "nse_archive" / "clean_panel_execution_v2_from2014.parquet"
FULL = ROOT / "external" / "nse_archive" / "clean_panel_execution_v2.parquet"
CAPITALS = [25_000, 30_000, 40_000, 50_000, 75_000, 100_000]
BOUNDS = {"optimistic": "last_close", "pessimistic": "write_off"}
VARIANTS = {"S15_nocap": (15, None), "S15_cap1000": (15, 1000.0), "S10_cap1000": (10, 1000.0)}


def rss_gb():
    return psutil.Process().memory_info().rss / 1e9


def build_trimmed_panel():
    if TRIMMED.exists():
        return
    import pyarrow.dataset as ds
    tbl = ds.dataset(str(FULL)).to_table(filter=ds.field("date") >= pd.Timestamp("2014-01-01"))
    tbl.to_pandas().to_parquet(TRIMMED)
    print(f"trimmed panel written: {len(tbl)} rows", flush=True)


def bench_series():
    from pymongo import MongoClient
    col = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)["stock_scanner"]["benchmark_ohlcv"]
    b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    b["date"] = pd.to_datetime(b["date"])
    return b.drop_duplicates("date", keep="last").sort_values("date").set_index("date")["close"]


def make_strategy(cls, cfg, bench, memo):
    class Variant(cls):
        def compute_indicators(self, df, config=None):          # indicators depend only on the frame: share them across runs
            key = id(df)
            if key not in memo:
                memo[key] = super().compute_indicators(df, config)
            return memo[key]

    return Variant(config=cfg, benchmark=bench)


def run_one(name, cls, capital, variant, uni, bench, memo, bound="optimistic"):
    from nse_quant_lab.config import BacktestConfig
    from nse_quant_lab.engine import BacktestEngine
    slots, cap = VARIANTS[variant]
    cfg = BacktestConfig(
        initial_capital=float(capital), portfolio_size=slots, max_open_positions=slots, rebalance_hold_rank_buffer=100 * slots,
        rebalance_frequency="MONTHLY", min_turnover_50d=0.0, min_price=0.0, slippage_pct=0.002, unaffordable_policy="TRY_NEXT",
        enforce_pit_universe=False, pit_top_n_liquidity=0, brokerage_per_order=0.0, dp_charge_sell=14.75,
        max_volume_participation_pct=0.10, deduct_annual_tax=True, tax_model="india_fy", execution_approach="APPROACH_3",
        max_entry_price=cap,
        missing_held_bar_policy=BOUNDS[bound], fractional_entitlement_policy="floor_no_cash",
        unverified_share_action_policy="accept_factor")
    t0 = time.time()
    row = {"strategy": name, "capital": capital, "variant": variant, "bound": bound, "slots": slots, "price_cap": cap}
    try:
        metrics, trades, _, execs, nav = BacktestEngine(make_strategy(cls, cfg, bench, memo), cfg).run(uni, START, END)
    except ValueError as exc:
        return {**row, "status": "INCOMPLETE", "reason": str(exc)[:160], "seconds": round(time.time() - t0)}
    nav["date"] = pd.to_datetime(nav["date"])
    s = nav.set_index("date")["nav"]
    tag = f"{name}_{capital}_{variant}_{bound}"          # full ledgers are kept so the results can be audited independently
    trades.drop(columns=[c for c in ("tax_lots",) if c in trades.columns]).to_csv(RUNS / f"{tag}_trades.csv", index=False)
    execs.to_csv(RUNS / f"{tag}_execs.csv", index=False)
    nav.to_csv(RUNS / f"{tag}_nav.csv", index=False)
    gains, losses = trades.net_pnl[trades.net_pnl > 0].sum(), -trades.net_pnl[trades.net_pnl < 0].sum()
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    row.update({
        "status": "OK", "final_nav": round(float(s.iloc[-1])), "cagr_pct": round(float(metrics["cagr_pct"]), 2),
        "max_dd_pct": round(float(metrics["max_drawdown_pct"]), 1), "sharpe": round(float(metrics["sharpe_ratio"]), 2),
        "trades": int(len(trades)), "win_pct": round(float((trades.net_pnl > 0).mean() * 100), 1) if len(trades) else 0.0,
        "pf": round(float(gains / losses), 2) if losses > 0 else None,
        "avg_hold_days": round(float(trades.hold_days.mean()), 1) if len(trades) else 0.0,
        "total_fees": round(float(trades.total_fees.sum())), "fees_pct_of_start": round(float(trades.total_fees.sum()) / capital * 100, 1),
        "tax_assessed": round(float(metrics.get("total_tax_assessed", 0.0))),
        "avg_cash_pct": round(float((nav.cash / nav.nav).mean() * 100), 1), "avg_positions": round(float(nav.open_positions.mean()), 1),
        "skipped_orders": int(metrics.get("skipped_orders", 0)), "years": round(yrs, 2), "seconds": round(time.time() - t0)})
    return row


COLUMNS = ["strategy", "capital", "variant", "bound", "slots", "price_cap", "status", "reason", "final_nav", "cagr_pct", "max_dd_pct", "sharpe",
           "trades", "win_pct", "pf", "avg_hold_days", "total_fees", "fees_pct_of_start", "tax_assessed", "avg_cash_pct", "avg_positions",
           "skipped_orders", "years", "seconds"]


def append(row):
    df = pd.DataFrame([row]).reindex(columns=COLUMNS)       # fixed columns: a stopped run must not shift fields
    df.to_csv(RESULTS, mode="a", header=not RESULTS.exists(), index=False)


def main():
    from nse_quant_lab.data_loader import load_pit_universe
    from nse_quant_lab.strategies.alpha_semivol_pit import AlphaSemivolPITStrategy
    from nse_quant_lab.strategies.minervini import MinerviniStrategy
    from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy
    strategies = {"trend_momentum_pit": TrendMomentumPITStrategy, "alpha_semivol_pit": AlphaSemivolPITStrategy, "minervini": MinerviniStrategy}
    build_trimmed_panel()
    t0 = time.time()
    uni = load_pit_universe(END, panel_path=str(TRIMMED), include_live=False)
    bench = bench_series()
    bs = bench[(bench.index >= START) & (bench.index <= END)]
    print(f"universe {len(uni)} symbols in {time.time() - t0:.0f}s, rss {rss_gb():.1f} GB; benchmark CAGR {((bs.iloc[-1] / bs.iloc[0]) ** (365.25 / (bs.index[-1] - bs.index[0]).days) - 1) * 100:.2f}%", flush=True)
    done = set()
    if RESULTS.exists():
        d = pd.read_csv(RESULTS)
        done = set(zip(d.strategy, d.capital, d.variant, d.bound))
    # Order: the conservative (pessimistic) bound for EVERY config first, then the optimistic bound, so the useful picture arrives earliest.
    # The shared-indicator repeat check already passed (trend_momentum_pit: IDENTICAL), so it is not repeated per strategy.
    # Calibration: the same harness at the ORIGINAL Rs 1 lakh capital must land near the registered 10-year figure for the deployed
    # strategy (R11 Y10 = 23.41% CAGR on the old adjusted basis); a big gap would mean the harness, not the strategy, is off.
    for bound in ("pessimistic", "optimistic"):
        if ("trend_momentum_pit", 100_000, "S15_nocap", bound) not in done:
            row = run_one("trend_momentum_pit", strategies["trend_momentum_pit"], 100_000, "S15_nocap", uni, bench, {}, bound)
            append(row); print("CALIBRATION", {k: row.get(k) for k in ("capital", "bound", "status", "cagr_pct", "max_dd_pct", "trades")}, flush=True)
            gc.collect()
    for bound in ("pessimistic", "optimistic"):
        for name, cls in strategies.items():
            memo = {}
            for capital in CAPITALS:
                for variant in VARIANTS:
                    if (name, capital, variant, bound) in done:
                        continue
                    row = run_one(name, cls, capital, variant, uni, bench, memo, bound)
                    append(row)
                    print({k: row.get(k) for k in ("strategy", "capital", "variant", "bound", "status", "cagr_pct", "max_dd_pct", "trades", "final_nav", "reason", "seconds")}, f"rss {rss_gb():.1f}GB", flush=True)
            memo.clear()
            gc.collect()
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
