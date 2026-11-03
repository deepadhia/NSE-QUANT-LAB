#!/usr/bin/env python3
"""
adversarial_stress_test.py -- Adversarial Falsification & Fragility Audit ("Testing to Break")

Subjecting baseline_s0_uh_r1_15 (trend_momentum_pit) and alpha_semivol_pit to the gauntlet:
1. Cost & Slippage Stress (20 bps, 40 bps, 60 bps, 100 bps per leg).
2. Concentration / Lottery Bias: Remove Top 5 and Top 10 winning trades.
3. Longest Underwater Duration & Recovery Times.
4. Annual Whipsaw Rate and False Breakout Loss Analysis.
5. Sizing & Gap-Up Limit Cap Rejection Audit.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "research" / "audit_2026-10" / "scripts"))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from candidate_backtests import (
    AlphaSemivolCandidate,
    S0BaselineCandidate,
    load_augmented_universe,
    load_benchmark,
    compute_detailed_stats,
    compute_yearly_returns,
)

RESULTS_DIR = REPO / "research" / "audit_2026-10" / "results" / "candidate_evaluation"
STRESS_DIR = REPO / "research" / "audit_2026-10" / "results" / "stress_testing"
STRESS_DIR.mkdir(parents=True, exist_ok=True)

EVAL_START, EVAL_END = "2007-01-01", "2026-10-30"


# =============================================================================
# 1. TRADE LEDGER ADVERSARIAL ANALYSIS (CONCENTRATION & UNDERWATER AUDIT)
# =============================================================================

def analyze_trade_fragility(strat_name: str, trades_csv: Path, nav_csv: Path) -> Dict[str, Any]:
    """Examine lottery ticket bias, tail concentration, and underwater duration."""
    df_t = pd.read_csv(trades_csv)
    df_nav = pd.read_csv(nav_csv)
    df_nav["date"] = pd.to_datetime(df_nav["date"])
    nav = df_nav.set_index("date")["nav"]
    
    total_trades = len(df_t)
    winners = df_t[df_t["net_pnl"] > 0]
    losers = df_t[df_t["net_pnl"] <= 0]
    
    total_profit = winners["net_pnl"].sum()
    total_loss = abs(losers["net_pnl"].sum())
    profit_factor = total_profit / total_loss if total_loss > 0 else np.nan
    
    # Top 5 and Top 10 Profit Share
    top5_profit = winners.nlargest(5, "net_pnl")["net_pnl"].sum()
    top10_profit = winners.nlargest(10, "net_pnl")["net_pnl"].sum()
    top5_pct = (top5_profit / total_profit) * 100.0 if total_profit > 0 else 0.0
    top10_pct = (top10_profit / total_profit) * 100.0 if total_profit > 0 else 0.0
    
    # What happens to final NAV if Top 5 or Top 10 trades were flat (0 return)?
    final_nav_raw = nav.iloc[-1]
    final_nav_ex_top5 = final_nav_raw - top5_profit
    final_nav_ex_top10 = final_nav_raw - top10_profit
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    cagr_raw = (final_nav_raw / nav.iloc[0]) ** (1.0 / yrs) - 1.0
    cagr_ex_top5 = (max(1.0, final_nav_ex_top5) / nav.iloc[0]) ** (1.0 / yrs) - 1.0
    cagr_ex_top10 = (max(1.0, final_nav_ex_top10) / nav.iloc[0]) ** (1.0 / yrs) - 1.0
    
    # Underwater / Drawdown Duration Analysis
    peak = nav.cummax()
    dd = (nav / peak) - 1.0
    is_underwater = dd < 0.0
    
    # Calculate continuous drawdown runs
    underwater_days = 0
    max_underwater_days = 0
    cur_start = None
    max_start = None
    max_end = None
    
    for d, val in is_underwater.items():
        if val:
            if cur_start is None:
                cur_start = d
            dur = (d - cur_start).days
            if dur > max_underwater_days:
                max_underwater_days = dur
                max_start = cur_start
                max_end = d
        else:
            cur_start = None
            
    # Exit Reason Breakdown
    exit_counts = df_t["exit_reason"].value_counts().to_dict()
    
    # Top 5 best and worst trades
    top5_names = winners.nlargest(5, "net_pnl")[["symbol", "entry_date", "exit_date", "net_pnl", "hold_days"]].to_dict("records")
    worst5_names = losers.nsmallest(5, "net_pnl")[["symbol", "entry_date", "exit_date", "net_pnl", "hold_days"]].to_dict("records")
    
    return {
        "strategy": strat_name,
        "total_trades": total_trades,
        "profit_factor": round(profit_factor, 2),
        "top5_profit_share_pct": round(top5_pct, 1),
        "top10_profit_share_pct": round(top10_pct, 1),
        "cagr_raw": round(cagr_raw * 100, 2),
        "cagr_ex_top5": round(cagr_ex_top5 * 100, 2),
        "cagr_ex_top10": round(cagr_ex_top10 * 100, 2),
        "max_drawdown_duration_days": max_underwater_days,
        "max_underwater_start": str(max_start)[:10] if max_start else "N/A",
        "max_underwater_end": str(max_end)[:10] if max_end else "N/A",
        "exit_counts": exit_counts,
        "top5_winners": top5_names,
        "worst5_losers": worst5_names,
    }


# =============================================================================
# 2. SLIPPAGE & TRANSACTION FRICTION STRESS (GAUNTLET RUN)
# =============================================================================

def run_friction_stress_ladder(
    strat_cls: Any,
    strat_name: str,
    uni: Dict[str, pd.DataFrame],
    bench: pd.Series,
    slippage_bps_list: List[int] = [20, 40, 60, 100],
) -> List[Dict[str, Any]]:
    """Simulate increasing execution friction from 20 bps up to 100 bps per leg."""
    results = []
    for bps in slippage_bps_list:
        slip_pct = bps / 10_000.0
        print(f"--> Stress Testing {strat_name} at {bps} bps/leg slippage...")
        
        cfg = BacktestConfig(
            initial_capital=100_000.0,
            portfolio_size=15,
            max_open_positions=15,
            rebalance_hold_rank_buffer=1500,
            rebalance_frequency="MONTHLY",
            min_turnover_50d=0.0,
            min_price=0.0,
            slippage_pct=slip_pct,
            unaffordable_policy="TRY_NEXT",
            enforce_pit_universe=False,
            pit_top_n_liquidity=0,
            brokerage_per_order=0.0,
            dp_charge_sell=14.75,
            max_volume_participation_pct=0.10,
            deduct_annual_tax=True,
            tax_model="india_fy",
        )
        
        strat = strat_cls(strat_name, bench, cfg, flat_slip=True)
        engine = BacktestEngine(strat, cfg)
        
        metrics, trades, orders, execs, nav = engine.run(
            uni, eval_start_date=EVAL_START, eval_end_date=EVAL_END
        )
        
        nav["date"] = pd.to_datetime(nav["date"])
        navs = nav.set_index("date")["nav"]
        
        full_stats = compute_detailed_stats(navs, bench, EVAL_START, EVAL_END)
        y10_stats = compute_detailed_stats(navs, bench, "2016-10-01", EVAL_END)
        dev_stats = compute_detailed_stats(navs, bench, "2007-01-01", "2015-12-31")
        late_stats = compute_detailed_stats(navs, bench, "2021-01-01", EVAL_END)
        
        # Count gap rejections and circuits from orders
        if isinstance(orders, pd.DataFrame) and not orders.empty:
            skipped_gap = int((orders["status"] == "SKIPPED_GAP_LIMIT_EXCEEDED").sum()) if "status" in orders.columns else 0
            skipped_uc = int((orders["status"] == "SKIPPED_UPPER_CIRCUIT").sum()) if "status" in orders.columns else 0
            deferred_lc = int((orders["status"] == "DEFERRED_LOWER_CIRCUIT").sum()) if "status" in orders.columns else 0
        else:
            skipped_gap = sum(1 for o in orders if isinstance(o, dict) and o.get("status") == "SKIPPED_GAP_LIMIT_EXCEEDED")
            skipped_uc = sum(1 for o in orders if isinstance(o, dict) and o.get("status") == "SKIPPED_UPPER_CIRCUIT")
            deferred_lc = sum(1 for o in orders if isinstance(o, dict) and o.get("status") == "DEFERRED_LOWER_CIRCUIT")
        
        results.append({
            "strategy": strat_name,
            "slippage_bps": bps,
            "20y_cagr": full_stats.get("cagr"),
            "10y_cagr": y10_stats.get("cagr"),
            "dev_cagr": dev_stats.get("cagr"),
            "late_cagr": late_stats.get("cagr"),
            "max_dd": full_stats.get("mdd"),
            "sharpe": full_stats.get("sharpe"),
            "final_nav": round(float(navs.iloc[-1]), 2),
            "total_trades": len(trades),
            "skipped_gap_limit": skipped_gap,
            "skipped_upper_circuit": skipped_uc,
            "deferred_lower_circuit": deferred_lc,
        })
        
    return results


# =============================================================================
# 3. MAIN ORCHESTRATION
# =============================================================================

def main():
    print("=" * 80)
    print("ADVERSARIAL FALSIFICATION & FRAGILITY AUDIT: 'TESTING TO BREAK'")
    print("=" * 80)
    
    # 1. Trade Ledger Fragility & Concentration Audit
    print("\n[PART 1] AUDITING TRADE CONCENTRATION & LOTTERY TICKET BIAS...")
    frag_s0 = analyze_trade_fragility(
        "baseline_s0_uh_r1_15",
        RESULTS_DIR / "trades_baseline_s0_uh_r1_15.csv",
        RESULTS_DIR / "nav_baseline_s0_uh_r1_15.csv",
    )
    frag_semivol = analyze_trade_fragility(
        "alpha_semivol_pit",
        RESULTS_DIR / "trades_alpha_semivol_pit.csv",
        RESULTS_DIR / "nav_alpha_semivol_pit.csv",
    )
    
    df_frag = pd.DataFrame([frag_s0, frag_semivol])
    frag_csv = STRESS_DIR / "trade_fragility_and_concentration.csv"
    df_frag[["strategy", "total_trades", "profit_factor", "top5_profit_share_pct", "top10_profit_share_pct",
             "cagr_raw", "cagr_ex_top5", "cagr_ex_top10", "max_drawdown_duration_days",
             "max_underwater_start", "max_underwater_end"]].to_csv(frag_csv, index=False)
    
    print("\n--> Tail Concentration & Recovery Scorecard:")
    print(df_frag[["strategy", "profit_factor", "top5_profit_share_pct", "top10_profit_share_pct",
                   "cagr_raw", "cagr_ex_top5", "cagr_ex_top10", "max_drawdown_duration_days"]].to_string(index=False))
                   
    print("\n--> Top 5 Winning Trades for Baseline S0:")
    for t in frag_s0["top5_winners"]:
        print(f"    {t['symbol']:12s} | Entry: {t['entry_date']} | Exit: {t['exit_date']} | Hold: {t['hold_days']}d | Net PnL: Rs {t['net_pnl']:,.0f}")
        
    print("\n--> Top 5 Winning Trades for Alpha Semivol:")
    for t in frag_semivol["top5_winners"]:
        print(f"    {t['symbol']:12s} | Entry: {t['entry_date']} | Exit: {t['exit_date']} | Hold: {t['hold_days']}d | Net PnL: Rs {t['net_pnl']:,.0f}")

    # 2. Friction Stress Ladder
    print("\n" + "=" * 80)
    print("[PART 2] RUNNING TRANSACTION FRICTION STRESS LADDER (20, 40, 60, 100 BPS/LEG)...")
    print("=" * 80)
    bench = load_benchmark()
    uni = load_augmented_universe()
    
    stress_s0 = run_friction_stress_ladder(S0BaselineCandidate, "baseline_s0_uh_r1_15", uni, bench, [20, 40, 60, 100])
    stress_semivol = run_friction_stress_ladder(AlphaSemivolCandidate, "alpha_semivol_pit", uni, bench, [20, 40, 60, 100])
    
    df_stress = pd.DataFrame(stress_s0 + stress_semivol)
    stress_csv = STRESS_DIR / "friction_stress_ladder_results.csv"
    df_stress.to_csv(stress_csv, index=False)
    
    print("\n--> Friction Stress Ladder Results:")
    print(df_stress[["strategy", "slippage_bps", "20y_cagr", "10y_cagr", "dev_cagr", "late_cagr", "max_dd", "final_nav", "skipped_gap_limit", "skipped_upper_circuit"]].to_string(index=False))


if __name__ == "__main__":
    main()
