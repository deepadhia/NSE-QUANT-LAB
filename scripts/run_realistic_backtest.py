"""
scripts/run_realistic_backtest.py -- Long-Horizon Multi-Regime Benchmark Backtest
================================================================================
Executes backtest over historical periods in local DB (e.g. 2005-2026):
- Warmup period: 2004-01-01 (100d EMA, Supertrend, 50d Turnover)
- Evaluation period: 2005-01-01 to 2026-10-23 (21.8 Years)
- Head-to-Head Benchmarks: Nifty 50 (^NSEI) and Nifty 500 (^CRSLDX) from 2005 to 2026
- Point-in-Time (PIT) Gating: Top 450 stocks by 50d ADT + Reconstitution additions calendar
- Volume Participation Cap: Max 10% of 50-day ADT for single morning order
- Year-by-Year Calendar Return Matrix
- Statutory Post-Tax Accounting under Indian Income Tax Act (Sections 70 & 74):
  * Annual March 31 statutory cash deduction from portfolio
  * Intra-head & inter-head set-off (STCL legally offsets both STCG and LTCG)
  * Unabsorbed losses carried forward across financial years
  * Resizes subsequent April 1 position allocations to true post-tax equity
- Realistic statutory delivery fees (Brokerage, STT, DP, GST, Stamp Duty)
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime
from typing import Dict, Any, List, Tuple
import numpy as np
import pandas as pd
from pymongo import MongoClient

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.data_loader import load_clean_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("realistic_backtest")


def calc_max_drawdown(equity_series: pd.Series) -> float:
    roll_max = equity_series.cummax()
    drawdown = (equity_series - roll_max) / roll_max
    return float(drawdown.min())


def compute_indian_capital_gains_tax(trades_df: pd.DataFrame) -> Tuple[float, float, float, List[Dict[str, Any]]]:
    """
    Computes statutory Indian Capital Gains Tax under Sections 70 & 74 of the Income Tax Act.
    - Financial Year: April 1 to March 31
    - STCG (< 365 days): Taxed at 20% (Section 111A)
    - LTCG (>= 365 days): Taxed at 12.5% with Rs 1.25 Lakh exemption per FY (Section 112A)
    - Section 70(2): Short-term capital losses offset both STCG and LTCG in same FY.
    - Section 74(1)(b): Unabsorbed STCL carried forward offsets future STCG and LTCG.
    """
    if trades_df.empty:
        return 0.0, 0.0, 0.0, []

    df = trades_df.copy()
    df["exit_dt"] = pd.to_datetime(df["exit_date"])

    def get_fy(dt: pd.Timestamp) -> str:
        year = dt.year
        return f"FY{year}-{year+1}" if dt.month >= 4 else f"FY{year-1}-{year}"

    df["fy"] = df["exit_dt"].apply(get_fy)

    stcg_tax_total = 0.0
    ltcg_tax_total = 0.0
    carried_forward_stcl = 0.0
    carried_forward_ltcl = 0.0
    fy_ledger = []

    for fy, group in df.groupby("fy", sort=True):
        stcg_gross = float(group[group["hold_days"] < 365]["net_pnl"].sum())
        ltcg_gross = float(group[group["hold_days"] >= 365]["net_pnl"].sum())

        # 1. Apply carried forward STCL against current STCG
        net_stcg = stcg_gross - carried_forward_stcl
        if net_stcg < 0:
            carried_forward_stcl = abs(net_stcg)
            taxable_stcg = 0.0
        else:
            taxable_stcg = net_stcg
            carried_forward_stcl = 0.0

        # 2. Apply carried forward LTCL against current LTCG
        net_ltcg = ltcg_gross - carried_forward_ltcl
        if net_ltcg < 0:
            carried_forward_ltcl = abs(net_ltcg)
        else:
            carried_forward_ltcl = 0.0

        # 3. Section 70(2): Short-term capital loss offsets long-term capital gain
        if carried_forward_stcl > 0 and net_ltcg > 0:
            stcl_offset = min(carried_forward_stcl, net_ltcg)
            net_ltcg -= stcl_offset
            carried_forward_stcl -= stcl_offset

        # 4. Taxable LTCG after Rs 1.25 Lakh annual exemption
        taxable_ltcg = max(0.0, net_ltcg - 125_000.0) if net_ltcg > 0 else 0.0

        tax_stcg = taxable_stcg * 0.20
        tax_ltcg = taxable_ltcg * 0.125
        fy_tax = tax_stcg + tax_ltcg

        stcg_tax_total += tax_stcg
        ltcg_tax_total += tax_ltcg

        fy_ledger.append({
            "fy": fy,
            "stcg_gross": round(stcg_gross, 2),
            "ltcg_gross": round(ltcg_gross, 2),
            "taxable_stcg": round(taxable_stcg, 2),
            "taxable_ltcg": round(taxable_ltcg, 2),
            "tax_paid": round(fy_tax, 2),
            "carried_forward_stcl": round(carried_forward_stcl, 2),
            "carried_forward_ltcl": round(carried_forward_ltcl, 2),
        })

    total_tax = stcg_tax_total + ltcg_tax_total
    return total_tax, stcg_tax_total, ltcg_tax_total, fy_ledger


def run_realistic_backtest(
    start_warmup: str = "2004-01-01",
    eval_start: str = "2005-01-01",
    eval_end: str = "2026-10-23",
    min_turnover_cr: float = 1.0,
    initial_capital: float = 1_000_000.0,
    portfolio_size: int = 6,
    hold_buffer: int = 15,
    slippage_pct: float = 0.0020,
    enforce_pit: bool = True,
    top_n_liquidity: int = 200,
    max_universe_pct: float = 0.45,
    max_volume_participation_pct: float = 0.10,
    deduct_annual_tax: bool = True,
    out_dir_name: str = "realistic_pit_max_period",
    mongo_uri: str = "mongodb://localhost:27017/",
    db_name: str = "stock_scanner",
) -> None:
    logger.info("=" * 85)
    logger.info(f"   NSE QUANT LAB: MAXIMUM PERIOD BENCHMARK-COMPARED BACKTEST ({eval_start[:4]}-{eval_end[:4]})")
    logger.info("=" * 85)
    logger.info(f"Warmup Start:         {start_warmup}")
    logger.info(f"Evaluation Window:    {eval_start} to {eval_end}")
    logger.info(f"Liquidity Floor:      Rs {min_turnover_cr:.1f} Crore / day (50d ADT)")
    logger.info(f"Cross-Sectional Rank: Top {top_n_liquidity} liquid stocks (Cap: {max_universe_pct*100:.0f}% of active pool) on each decision date")
    logger.info(f"Volume Part. Cap:     {max_volume_participation_pct * 100:.1f}% of 50-day ADT (TRY_NEXT spillover)")
    logger.info(f"Annual Tax Cash Flow: {deduct_annual_tax} (Deducted each March 31 under Sections 70/74)")
    logger.info(f"Slippage Assumption:  {slippage_pct * 100:.2f}% per leg ({int(slippage_pct * 10000)} bps)")
    logger.info(f"Initial Capital:      Rs {initial_capital:,.2f} across {portfolio_size} equal-weight slots")
    logger.info(f"Point-in-Time Gating: {enforce_pit} (filters future index additions before inclusion date)")

    # 1. Load Clean Universe
    logger.info("Loading clean universe from MongoDB...")
    universe = load_clean_universe(
        start_date=start_warmup,
        end_date=eval_end,
        mongo_uri=mongo_uri,
        db_name=db_name,
    )
    logger.info(f"Loaded {len(universe)} symbols with historical candles.")

    # 2. Configure Strategy & Engine
    min_to_rupees = min_turnover_cr * 10_000_000.0
    cfg = BacktestConfig(
        initial_capital=initial_capital,
        portfolio_size=portfolio_size,
        rebalance_hold_rank_buffer=hold_buffer,
        max_open_positions=portfolio_size,
        rebalance_frequency="MONTHLY",
        min_turnover_50d=min_to_rupees,
        min_price=10.0,
        slippage_pct=slippage_pct,
        unaffordable_policy="TRY_NEXT",
        enforce_lower_circuit_deferral=True,
        enforce_upper_circuit_rejection=True,
        enforce_pit_universe=enforce_pit,
        pit_top_n_liquidity=top_n_liquidity,
        pit_max_universe_pct=max_universe_pct,
        brokerage_per_order=0.0,
        dp_charge_sell=14.75,
        max_volume_participation_pct=max_volume_participation_pct,
        deduct_annual_tax=deduct_annual_tax,
    )
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    # 3. Run Event-Driven Simulation
    logger.info("Executing event-driven simulation...")
    t0 = datetime.now()
    metrics, trades_df, orders_df, execs_df, nav_df = engine.run(
        universe, eval_start_date=eval_start, eval_end_date=eval_end
    )
    t_elapsed = (datetime.now() - t0).total_seconds()
    logger.info(f"Simulation completed in {t_elapsed:.2f} seconds.")

    # 4. Fetch Benchmark Data from MongoDB
    client = MongoClient(mongo_uri)
    b_col = client[db_name]["benchmark_ohlcv"]

    nifty50_docs = list(b_col.find(
        {"symbol": "^NSEI", "date": {"$gte": eval_start, "$lte": eval_end}},
        {"_id": 0, "date": 1, "close": 1}
    ).sort("date", 1))
    nifty50_df = pd.DataFrame(nifty50_docs)

    nifty500_docs = list(b_col.find(
        {"symbol": "^CRSLDX", "date": {"$gte": eval_start, "$lte": eval_end}},
        {"_id": 0, "date": 1, "close": 1}
    ).sort("date", 1))
    nifty500_df = pd.DataFrame(nifty500_docs)

    # 5. Compute Cumulative Metrics
    nav_df["date_str"] = pd.to_datetime(nav_df["date"]).dt.strftime("%Y-%m-%d")
    first_nav_date = nav_df["date_str"].iloc[0]
    last_nav_date = nav_df["date_str"].iloc[-1]
    years = (pd.to_datetime(last_nav_date) - pd.to_datetime(first_nav_date)).days / 365.25

    final_equity = float(nav_df["nav"].iloc[-1])
    strat_net_ret = (final_equity / initial_capital) - 1.0
    strat_net_cagr = ((final_equity / initial_capital) ** (1.0 / years)) - 1.0 if years > 0 else 0.0
    strat_mdd = calc_max_drawdown(nav_df["nav"])

    # Nifty 50 Metrics
    n50_cagr, n50_mdd, n50_ret = 0.0, 0.0, 0.0
    if not nifty50_df.empty:
        n50_first = nifty50_df["close"].iloc[0]
        n50_last = nifty50_df["close"].iloc[-1]
        n50_ret = (n50_last / n50_first) - 1.0
        n50_cagr = ((n50_last / n50_first) ** (1.0 / years)) - 1.0 if years > 0 else 0.0
        n50_mdd = calc_max_drawdown(nifty50_df["close"])

    # Nifty 500 Metrics
    n500_cagr, n500_mdd, n500_ret = 0.0, 0.0, 0.0
    if not nifty500_df.empty:
        n500_first = nifty500_df["close"].iloc[0]
        n500_last = nifty500_df["close"].iloc[-1]
        n500_ret = (n500_last / n500_first) - 1.0
        n500_cagr = ((n500_last / n500_first) ** (1.0 / years)) - 1.0 if years > 0 else 0.0
        n500_mdd = calc_max_drawdown(nifty500_df["close"])

    # 6. Statutory Post-Tax Net Ledger
    fy_ledger = metrics.get("fy_tax_ledger", [])
    total_tax_paid = float(metrics.get("total_tax_paid", 0.0))
    if not fy_ledger and not trades_df.empty:
        total_tax_paid, _, _, fy_ledger = compute_indian_capital_gains_tax(trades_df)

    stcg_trades = len(trades_df[trades_df["hold_days"] < 365]) if not trades_df.empty else 0
    ltcg_trades = len(trades_df[trades_df["hold_days"] >= 365]) if not trades_df.empty else 0

    # 7. Annual Calendar Breakdown
    nav_df["year"] = pd.to_datetime(nav_df["date"]).dt.year
    nifty50_df["year"] = pd.to_datetime(nifty50_df["date"]).dt.year
    nifty500_df["year"] = pd.to_datetime(nifty500_df["date"]).dt.year

    all_years = sorted(list(nav_df["year"].unique()))
    annual_data = []

    for y in all_years:
        sub_nav = nav_df[nav_df["year"] == y]
        s_ret = (sub_nav["nav"].iloc[-1] / sub_nav["nav"].iloc[0]) - 1.0

        n50_sub = nifty50_df[nifty50_df["year"] == y]
        n50_yr_ret = (n50_sub["close"].iloc[-1] / n50_sub["close"].iloc[0] - 1.0) if len(n50_sub) >= 2 else 0.0

        n500_sub = nifty500_df[nifty500_df["year"] == y]
        n500_yr_ret = (n500_sub["close"].iloc[-1] / n500_sub["close"].iloc[0] - 1.0) if len(n500_sub) >= 2 else 0.0

        alpha_n50 = s_ret - n50_yr_ret
        alpha_n500 = s_ret - n500_yr_ret
        annual_data.append({
            "year": y,
            "strat_ret": s_ret,
            "nifty50_ret": n50_yr_ret,
            "nifty500_ret": n500_yr_ret,
            "alpha_n50": alpha_n50,
            "alpha_n500": alpha_n500,
        })

    # 8. Print Executive Table
    print("\n" + "=" * 90)
    print(f"      MAXIMUM-PERIOD REALISTIC BACKTEST & BENCHMARK REPORT ({first_nav_date} to {last_nav_date})")
    print(f"      Duration: {years:.2f} Years | Universe: {len(universe)} Stocks | Liquidity Floor: >= Rs {min_turnover_cr} Cr | PIT Top {top_n_liquidity}")
    print("=" * 90)
    print(f"{'Performance Metric':<32} | {'Strategy (Post-Tax)':<19} | {'Nifty 50':<12} | {'Nifty 500':<12}")
    print("-" * 90)
    print(f"{'Initial Capital':<32} | Rs {initial_capital:>16,.0f} | {'--':>12} | {'--':>12}")
    print(f"{'Final Portfolio Value':<32} | Rs {final_equity:>16,.0f} | {'--':>12} | {'--':>12}")
    print(f"{'Total Return':<32} | {strat_net_ret * 100:>18.2f}% | {n50_ret * 100:>11.2f}% | {n500_ret * 100:>11.2f}%")
    print(f"{'Annualized Return (CAGR)':<32} | {strat_net_cagr * 100:>18.2f}% | {n50_cagr * 100:>11.2f}% | {n500_cagr * 100:>11.2f}%")
    print(f"{'Maximum Drawdown':<32} | {strat_mdd * 100:>18.2f}% | {n50_mdd * 100:>11.2f}% | {n500_mdd * 100:>11.2f}%")
    print(f"{'Active Alpha vs Nifty 50':<32} | {(strat_net_cagr - n50_cagr) * 100:>+18.2f}% | {'--':>12} | {'--':>12}")
    print(f"{'Active Alpha vs Nifty 500':<32} | {(strat_net_cagr - n500_cagr) * 100:>+18.2f}% | {'--':>12} | {'--':>12}")
    print("-" * 90)
    print(f"{'Total Closed Trades':<32} | {len(trades_df):>16} |")
    win_trades = trades_df[trades_df['net_pnl'] > 0] if not trades_df.empty else pd.DataFrame()
    win_rate = (len(win_trades) / len(trades_df) * 100.0) if not trades_df.empty else 0.0
    print(f"{'Win Rate (%)':<32} | {win_rate:>15.1f}% |")
    print(f"{'Trades Held < 1 Year (STCG)':<32} | {stcg_trades:>16} |")
    print(f"{'Trades Held >= 1 Year (LTCG)':<32} | {ltcg_trades:>16} |")
    print(f"{'Total FY Tax Paid (Sec 70/74)':<32} | Rs {total_tax_paid:>13,.0f} | (Deducted each March 31)")
    print(f"{'Total Slippage Loss (20 bps)':<32} | Rs {float(trades_df['total_slippage'].sum() if not trades_df.empty else 0):>13,.0f} |")
    print(f"{'Total Statutory Fees Paid':<32} | Rs {float(trades_df['total_fees'].sum() if not trades_df.empty else 0):>13,.0f} | (STT, DP charges, GST)")
    print("=" * 90)

    # 9. Print Year-by-Year Calendar Matrix
    print("\n" + "=" * 80)
    print(f"{'Year':<8} | {'Strategy (Post-Tax)':<20} | {'Nifty 50':<12} | {'Nifty 500':<12} | {'Alpha vs N50':<12} | {'Alpha vs N500':<12}")
    print("-" * 80)
    for row in annual_data:
        print(f"{row['year']:<8} | {row['strat_ret']*100:>19.2f}% | {row['nifty50_ret']*100:>10.2f}% | {row['nifty500_ret']*100:>10.2f}% | {row['alpha_n50']*100:>+10.2f}% | {row['alpha_n500']*100:>+11.2f}%")
    print("=" * 80 + "\n")

    # 10. Save Artifacts
    out_dir = os.path.join(PROJECT_ROOT, "results", out_dir_name)
    os.makedirs(out_dir, exist_ok=True)
    trades_df.to_csv(os.path.join(out_dir, "trades.csv"), index=False)
    nav_df.to_csv(os.path.join(out_dir, "daily_nav.csv"), index=False)
    orders_df.to_csv(os.path.join(out_dir, "orders.csv"), index=False)
    pd.DataFrame(annual_data).to_csv(os.path.join(out_dir, "calendar_returns.csv"), index=False)
    pd.DataFrame(fy_ledger).to_csv(os.path.join(out_dir, "fy_tax_breakdown.csv"), index=False)

    summary = {
        "evaluation_period": f"{first_nav_date} to {last_nav_date}",
        "duration_years": round(years, 2),
        "liquidity_filter_cr": min_turnover_cr,
        "pit_top_n_liquidity": top_n_liquidity,
        "max_volume_participation_pct": max_volume_participation_pct,
        "deduct_annual_tax": deduct_annual_tax,
        "slippage_bps": int(slippage_pct * 10000),
        "enforce_pit_universe": enforce_pit,
        "initial_capital": initial_capital,
        "strategy": {
            "post_tax_final_equity": round(final_equity, 2),
            "net_post_tax_cagr_pct": round(strat_net_cagr * 100, 2),
            "max_drawdown_pct": round(strat_mdd * 100, 2),
            "win_rate_pct": round(win_rate, 2),
            "closed_trades": len(trades_df),
            "total_tax_paid": round(total_tax_paid, 2),
        },
        "benchmarks": {
            "nifty50_cagr_pct": round(n50_cagr * 100, 2),
            "nifty50_max_dd_pct": round(n50_mdd * 100, 2),
            "nifty500_cagr_pct": round(n500_cagr * 100, 2),
            "nifty500_max_dd_pct": round(n500_mdd * 100, 2),
        },
        "alpha": {
            "net_post_tax_vs_nifty50_bps": round((strat_net_cagr - n50_cagr) * 10000, 0),
            "net_post_tax_vs_nifty500_bps": round((strat_net_cagr - n500_cagr) * 10000, 0),
        }
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    logger.info(f"Saved artifacts to {out_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Realistic Multi-Regime Backtest")
    parser.add_argument("--eval-start", default="2005-01-01", help="Evaluation start date YYYY-MM-DD")
    parser.add_argument("--start-warmup", default="2004-01-01", help="Warmup start date YYYY-MM-DD")
    parser.add_argument("--eval-end", default="2026-10-23", help="Evaluation end date YYYY-MM-DD")
    parser.add_argument("--min-turnover-cr", type=float, default=1.0, help="Min 50-day daily turnover in Crores")
    parser.add_argument("--capital", type=float, default=1_000_000.0, help="Initial capital in Rupees")
    parser.add_argument("--portfolio-size", type=int, default=6, help="Portfolio size")
    parser.add_argument("--hold-buffer", type=int, default=15, help="Rank hold buffer (hysteresis)")
    parser.add_argument("--slippage-bps", type=float, default=20.0, help="Slippage in basis points")
    parser.add_argument("--top-n", type=int, default=200, help="Cross-sectional top N liquid stocks on decision date")
    parser.add_argument("--max-universe-pct", type=float, default=0.45, help="Relative capacity cap of active stock pool")
    parser.add_argument("--volume-participation-pct", type=float, default=0.10, help="Max volume participation pct (e.g. 0.10 for 10%)")
    parser.add_argument("--no-pit", action="store_true", help="Disable Point-in-Time universe filtering")
    parser.add_argument("--no-tax-deduct", action="store_true", help="Disable annual March 31 tax deduction")
    parser.add_argument("--out-dir", default="realistic_pit_max_period", help="Output directory name inside results/")
    args = parser.parse_args()

    run_realistic_backtest(
        start_warmup=args.start_warmup,
        eval_start=args.eval_start,
        eval_end=args.eval_end,
        min_turnover_cr=args.min_turnover_cr,
        initial_capital=args.capital,
        portfolio_size=args.portfolio_size,
        hold_buffer=args.hold_buffer,
        slippage_pct=args.slippage_bps / 10000.0,
        enforce_pit=not args.no_pit,
        top_n_liquidity=args.top_n,
        max_universe_pct=args.max_universe_pct,
        max_volume_participation_pct=args.volume_participation_pct,
        deduct_annual_tax=not args.no_tax_deduct,
        out_dir_name=args.out_dir,
    )
