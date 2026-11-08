"""Recompute registered headline numbers under the 2026-11-08 metric definitions.

Definitions (src/nse_quant_lab/metrics.py): CAGR = calendar years from the ACTUAL initial capital; Sharpe = arithmetic mean daily
return * 252 minus risk-free, over daily vol * sqrt(252); max drawdown includes initial capital.

Two parts, never mixed in the output:
  NAV   : metrics recomputed from a SAVED NAV curve (no simulation). The saved curves start from the first end-of-day NAV, so the
          initial-capital row is prepended (Rs 100,000) exactly as the new metric requires.
  RESIM : the production strategy class re-simulated by the CURRENT engine on the cached ADJUSTED-basis universe
          (results/ab_regime_catchup/universe_cache.pkl). This is the legacy research basis that the registered runs used. It is NOT the
          corrected nominal-price basis (that replay is blocked: FRACTIONAL_ENTITLEMENT_UNRESOLVED, see corrected_history/validation.json).
          It differs from the registered runs only by engine/metric changes (final partial-FY tax, calendar CAGR, Sharpe convention).
Usage: python recompute_register_metrics.py nav | resim [R11 R12 R13]
Outputs are written to register_recompute/ and never overwrite an existing file.
"""
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUT = HERE / "register_recompute"
OUT.mkdir(exist_ok=True)
CAP = 100_000.0
RF = 0.06

NAV_RUNS = {
    "R11 (audit baseline replay)": HERE / "baseline" / "daily_nav.csv",
    "R20 A0 baseline": ROOT / "research/audit_2026-10/results/ab_rs55_filter/nav_A0_baseline.csv",
    "R21 A1 rs55>0": ROOT / "research/audit_2026-10/results/ab_rs55_filter/nav_A1_rs55_gt0.csv",
    "R22 A4 rs55>=20": ROOT / "research/audit_2026-10/results/ab_rs55_filter/nav_A4_rs55_ge20.csv",
    "R23 A5 rs55>=40": ROOT / "research/audit_2026-10/results/ab_rs55_filter/nav_A5_rs55_ge40.csv",
}


def new_definition_metrics(nav: pd.Series) -> dict:
    nav = nav.astype(float).sort_index()
    start = nav.index[0]  # engine convention: calendar years from the first NAV date, growth measured from initial capital
    full = pd.concat([pd.Series([CAP], index=[start - pd.Timedelta(days=1)]), nav])
    years = (nav.index[-1] - start).days / 365.25
    cagr = ((nav.iloc[-1] / CAP) ** (1 / years) - 1) * 100
    first_eod = ((nav.iloc[-1] / nav.iloc[0]) ** (1 / ((nav.index[-1] - nav.index[0]).days / 365.25)) - 1) * 100
    r = nav.pct_change().dropna()
    vol = r.std() * np.sqrt(252)
    sharpe_rf = (r.mean() * 252 - RF) / vol
    sharpe_0 = (r.mean() * 252) / vol
    dd = ((full / full.cummax().clip(lower=CAP)) - 1).min() * 100
    return {"final_nav": round(float(nav.iloc[-1])), "cagr_new_pct": round(cagr, 2), "cagr_first_eod_old_pct": round(first_eod, 2),
            "sharpe_rf6": round(float(sharpe_rf), 2), "sharpe_rf0": round(float(sharpe_0), 2), "max_dd_pct": round(float(dd), 1)}


def part_nav():
    rows = []
    for name, f in NAV_RUNS.items():
        df = pd.read_csv(f, parse_dates=["date"]).set_index("date")
        rows.append({"run": name, "part": "NAV (saved curve, not re-simulated)", **new_definition_metrics(df["nav"])})
    res = pd.DataFrame(rows)
    target = OUT / "nav_recomputed.csv"
    if target.exists():
        raise FileExistsError(target)
    res.to_csv(target, index=False)
    print(res.to_string(index=False))


def part_resim(keys):
    from nse_quant_lab.config import BacktestConfig
    from nse_quant_lab.engine import BacktestEngine
    from nse_quant_lab.strategies.alpha_semivol_pit import AlphaSemivolPITStrategy
    from nse_quant_lab.strategies.minervini import MinerviniStrategy
    from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy
    from pymongo import MongoClient
    classes = {"R11": ("trend_momentum_pit", TrendMomentumPITStrategy), "R12": ("alpha_semivol_pit", AlphaSemivolPITStrategy),
               "R13": ("minervini", MinerviniStrategy)}
    col = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)["stock_scanner"]["benchmark_ohlcv"]
    b = pd.DataFrame(list(col.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    b["date"] = pd.to_datetime(b["date"])
    bench = b.drop_duplicates("date", keep="last").sort_values("date").set_index("date")["close"]
    uni = pickle.load(open(ROOT / "research/audit_2026-10/results/ab_regime_catchup/universe_cache.pkl", "rb"))
    for key in keys:
        name, cls = classes[key]
        target = OUT / f"resim_{key}.json"
        if target.exists():
            print("exists, skipping", target)
            continue
        cfg = BacktestConfig(
            initial_capital=CAP, portfolio_size=15, max_open_positions=15, rebalance_hold_rank_buffer=1500,
            rebalance_frequency="MONTHLY", min_turnover_50d=0.0, min_price=0.0, slippage_pct=0.002, unaffordable_policy="TRY_NEXT",
            enforce_pit_universe=False, pit_top_n_liquidity=0, brokerage_per_order=0.0, dp_charge_sell=14.75,
            max_volume_participation_pct=0.10, deduct_annual_tax=True, tax_model="india_fy", execution_approach="APPROACH_3")
        t0 = time.time()
        metrics, trades, _, _, nav = BacktestEngine(cls(config=cfg, benchmark=bench), cfg).run(uni, "2007-01-01", "2026-10-30")
        nav["date"] = pd.to_datetime(nav["date"])
        s = nav.set_index("date")["nav"]
        res = {"run": key, "strategy": name, "basis": "legacy adjusted research cache, current engine", "trades": int(len(trades)),
               "engine_cagr_pct": round(float(metrics["cagr_pct"]), 2), "engine_cagr_sessions_250_pct": round(float(metrics["cagr_sessions_250_pct"]), 2),
               "engine_sharpe": round(float(metrics["sharpe_ratio"]), 2) if "sharpe_ratio" in metrics else None,
               "engine_max_dd_pct": round(float(metrics["max_drawdown_pct"]), 1),
               "total_tax_assessed": round(float(metrics.get("total_tax_assessed", 0.0))), "tax_payable_unpaid": round(float(metrics.get("tax_payable", 0.0))),
               "seconds": round(time.time() - t0), **{"recomputed_" + k: v for k, v in new_definition_metrics(s).items()}}
        pd.Series(res).to_json(target, indent=2)
        s.to_csv(OUT / f"resim_{key}_nav.csv")
        print(res, flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "nav"
    if mode == "nav":
        part_nav()
    else:
        part_resim(sys.argv[2:] or ["R11", "R12", "R13"])
