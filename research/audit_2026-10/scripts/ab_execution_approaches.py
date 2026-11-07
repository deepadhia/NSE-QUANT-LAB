#!/usr/bin/env python3
"""
*** NOT AUTHORITATIVE (2026-11-07) -- DO NOT CITE ITS NUMBERS ***
This script defines `NextDayCatchupEngine`, a re-implementation of the whole BacktestEngine.run() loop (forbidden by
docs/STRATEGY_ONBOARDING_AND_PARITY_PLAYBOOK.md). It diverges from src/nse_quant_lab/engine.py (no delisting force-liquidation, different
circuit-lock retry/exit path, catch-up triggered whenever a slot is empty, budget gate fed from a side-channel `_current_equity`). Its Approach 3
result (22.18%) did not reproduce in the real engine (20.50%; see docs/RESULTS_REGISTER.md R11-R19). Arm 0 matches the real engine exactly
(it uses the real BacktestEngine). For execution-approach questions use research/audit_2026-10/scripts/approach_ablation_real_engine.py.

ab_execution_approaches.py -- Empirical Backtest of Real-World Execution Approaches
===================================================================================
Investigates the impact on 20-year CAGR, Drawdown, and Churn of realistic order placement:

  Arm 0: Baseline Champion (Control)
         - Production TrendMomentumPITStrategy
         - unaffordable_policy = "TRY_NEXT" (cascades down candidate pool)
         - Idealized simulation benchmark

  Arm 1: Approach 1A (Pure Strict Skip)
         - Top 15 target candidates only
         - unaffordable_policy = "SKIP"
         - If any order gaps >2%, hits upper circuit, or has 0 shares -> SKIPPED
         - Slot stays in cash until next monthly rebalance

  Arm 2: Approach 1B (Budget-Gated Strict Skip)
         - Same as 1A, but filters out stocks with price > target_alloc BEFORE ranking
         - Guarantees all 15 staged orders can buy >= 1 whole share
         - If any gaps >2% or hits circuit -> SKIPPED (cash sits in cash)

  Arm 3: Approach 3 (Next-Day Open Catch-Up / Day T+2 Fill)
         - Day T+1 Open: Attempts to fill top 15 budget-gated stocks
         - If any slots remain unfilled (due to gaps/circuits), Day T+2 Open is dynamically
           scheduled to fill the empty slots from the updated post-market scan at Day T+1 close
         - Max 1 catch-up day per cycle (no endless chasing)

Evaluated across:
  - FULL (2007-01-01 to 2026-10-30)
  - DEV  (2007-01-01 to 2015-12-31)
  - VAL  (2016-01-01 to 2020-12-31)
  - LATE (2021-01-01 to 2026-10-30)
  - Y10  (2016-10-01 to 2026-10-30)
"""
from __future__ import annotations

import copy
import logging
import os
import pickle
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pymongo import MongoClient

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum_pit import (
    TrendMomentumPITStrategy,
    MIN_ADT,
    MIN_PRICE,
    MIN_HISTORY,
    ENTRY_EMA_BUFFER,
)
from nse_quant_lab.fees import calc_buy_fees_exact, calc_sell_fees_exact
from nse_quant_lab.execution import is_locked_up, is_locked_down
from nse_quant_lab.tax_india import compute_fy_tax, LTCG_MIN_HOLD_DAYS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ab_execution")

OUT_DIR = REPO / "research" / "audit_2026-10" / "results" / "ab_execution_approaches"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = REPO / "research" / "audit_2026-10" / "results" / "ab_regime_catchup" / "universe_cache.pkl"

START_DATE = "2007-01-01"
END_DATE = "2026-10-30"
WINDOWS = {
    "FULL": (START_DATE, END_DATE),
    "DEV_2007_15": ("2007-01-01", "2015-12-31"),
    "VAL_2016_20": ("2016-01-01", "2020-12-31"),
    "LATE_2021_26": ("2021-01-01", END_DATE),
    "Y10_2016_10": ("2016-10-01", END_DATE),
}


# ----------------------------------------------------------------------------- Strategy Variants
class BudgetGatedPITStrategy(TrendMomentumPITStrategy):
    """Approach 1B: Filters out stocks with price > target_alloc_per_stock before ranking."""

    def select_entry_candidates(
        self,
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        current_holdings: Dict[str, Any],
        config: Any,
    ) -> pd.DataFrame:
        df_candidates = super().select_entry_candidates(universe, decision_date, current_holdings, config)
        if df_candidates.empty:
            return df_candidates

        is_held = df_candidates.index.isin(list(current_holdings.keys()))
        current_eq = getattr(config, "_current_equity", getattr(config, "initial_capital", 100_000.0))
        slot_budget = current_eq / float(getattr(config, "portfolio_size", 15))
        
        # Keep if held OR if close price <= slot_budget (can afford >= 1 share)
        affordable_mask = is_held | (df_candidates["close"] <= slot_budget)
        filtered = df_candidates[affordable_mask].copy()
        
        if not filtered.empty:
            filtered["rank"] = np.arange(1, len(filtered) + 1)
        return filtered


# ----------------------------------------------------------------------------- Custom Engine for Approach 3
class NextDayCatchupEngine(BacktestEngine):
    """
    BacktestEngine subclass that tracks real-time equity for budget gating
    and optionally supports Day T+2 catch-up if Day T+1 rebalance
    leaves unfilled slots due to gap caps or circuit locks.
    """

    def __init__(self, strategy: Any, config: Any, enable_catchup: bool = False):
        super().__init__(strategy=strategy, config=config)
        self.enable_catchup = enable_catchup

    def run(
        self,
        universe: Dict[str, pd.DataFrame],
        eval_start_date: str = "2007-01-01",
        eval_end_date: str = "2026-10-30",
        benchmark_series: Optional[pd.Series] = None,
    ) -> Tuple[Dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        # Precompute indicators and timeline exactly as base engine
        universe_ind = universe
        all_dates = set()
        for df in universe_ind.values():
            if not df.empty and "date" in df.columns:
                all_dates.update(df["date"].tolist())
        timeline = sorted(list(all_dates))

        eval_start_dt = pd.to_datetime(eval_start_date)
        eval_end_dt = pd.to_datetime(eval_end_date)
        eval_timeline = [d for d in timeline if eval_start_dt <= d <= eval_end_dt]
        if not eval_timeline:
            raise ValueError(f"No trading sessions in range {eval_start_date} to {eval_end_date}")

        exec_to_decision_map = self.strategy.get_rebalance_schedule(
            timeline, eval_start_dt, eval_end_dt, self.config
        )
        exec_dates_set = set(exec_to_decision_map.keys())

        # State
        cash = float(self.config.initial_capital)
        open_positions: Dict[str, Dict[str, Any]] = {}
        closed_trades: List[Dict[str, Any]] = []
        order_ledger: List[Dict[str, Any]] = []
        execution_ledger: List[Dict[str, Any]] = []
        daily_nav_history: List[Dict[str, Any]] = []

        order_id_counter = 0
        exec_id_counter = 0
        total_brokerage = 0.0
        total_stt = 0.0
        total_stamp_duty = 0.0
        total_exchange_txn = 0.0
        total_sebi = 0.0
        total_gst = 0.0
        total_dp_charges = 0.0
        total_slippage = 0.0
        total_turnover = 0.0
        skipped_orders_count = 0
        fy_tax_payable_accrued = 0.0
        fy_tax_paid_total = 0.0
        last_tax_processed_trade_idx = 0
        cf_st_list: List[Tuple[int, float]] = []
        cf_lt_list: List[Tuple[int, float]] = []
        fy_tax_ledger: List[Dict[str, Any]] = []
        current_tax_year = None
        pending_circuit_exits: Dict[str, Dict[str, Any]] = {}
        queued_structural_exits: Dict[str, str] = {}
        scheduled_catchup_sessions: set = set()

        for day_idx, current_date in enumerate(eval_timeline):
            date_str = current_date.strftime("%Y-%m-%d")
            sim_tax_year = current_date.year if current_date.month > 3 else current_date.year - 1

            if current_tax_year is None:
                current_tax_year = sim_tax_year
            elif sim_tax_year != current_tax_year:
                if self.config.deduct_annual_tax and fy_tax_payable_accrued > 0:
                    tax_payment = min(cash, fy_tax_payable_accrued)
                    cash -= tax_payment
                    fy_tax_paid_total += tax_payment
                    fy_tax_payable_accrued = 0.0
                current_tax_year = sim_tax_year

            # 1. Deferred lower circuit exits
            for sym in sorted(list(pending_circuit_exits.keys())):
                pos = open_positions[sym]
                df_sym = universe_ind.get(sym)
                if df_sym is None:
                    continue
                day_rows = df_sym.loc[df_sym["date"] == current_date]
                if day_rows.empty:
                    continue
                bar = day_rows.iloc[0]
                prev_rows = df_sym.loc[df_sym["date"] < current_date]
                prev_close = float(prev_rows.iloc[-1]["close"]) if not prev_rows.empty else float(bar["open"])
                is_down = is_locked_down(float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]), prev_close)
                if is_down:
                    pos["deferral_days"] = pos.get("deferral_days", 0) + 1
                    continue

                raw_exit_price = float(bar["open"])
                shares = pos["shares"]
                orig_exit_reason = pos.get("exit_reason", "EXIT_AT_OPEN")
                fill_price = raw_exit_price * (1.0 - self._slip(sym, bar, raw_exit_price, shares, "SELL"))
                gross_proceeds = shares * raw_exit_price
                net_proceeds_slip = shares * fill_price
                slip_loss = gross_proceeds - net_proceeds_slip
                sell_fees = calc_sell_fees_exact(net_proceeds_slip, current_date, self.config)
                cash_received = net_proceeds_slip - sell_fees["total"]
                cash += cash_received

                total_brokerage += sell_fees["brokerage"]
                total_stt += sell_fees["stt"]
                total_exchange_txn += sell_fees["exchange_txn"]
                total_sebi += sell_fees["sebi"]
                total_gst += sell_fees["gst"]
                total_dp_charges += sell_fees["dp_charges"]
                total_slippage += slip_loss
                total_turnover += gross_proceeds

                order_id_counter += 1
                order_ledger.append({
                    "order_id": order_id_counter, "date": date_str, "symbol": sym, "action": "SELL",
                    "shares": shares, "order_type": "EXIT_AT_OPEN", "status": "FILLED",
                    "reason": f"{orig_exit_reason}_UNLOCKED", "deferral_days": pos.get("deferral_days", 0)
                })
                exec_id_counter += 1
                execution_ledger.append({
                    "exec_id": exec_id_counter, "order_id": order_id_counter, "date": date_str,
                    "symbol": sym, "action": "SELL", "shares": shares, "raw_price": raw_exit_price,
                    "fill_price": fill_price, "gross_val": gross_proceeds, "net_cash": cash_received,
                    "fees": sell_fees["total"], "slippage": slip_loss
                })
                gross_pnl = (raw_exit_price - pos["entry_raw_price"]) * shares
                net_pnl = cash_received - pos["total_entry_outlay"]
                closed_trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                    "entry_price": pos["entry_raw_price"], "entry_fill_price": pos["entry_fill_price"],
                    "exit_date": date_str, "exit_price": raw_exit_price, "exit_fill_price": fill_price,
                    "shares": shares, "hold_days": (current_date - pos["entry_date"]).days,
                    "gross_pnl": gross_pnl, "net_pnl": net_pnl,
                    "total_fees": pos["buy_fees"]["total"] + sell_fees["total"],
                    "total_slippage": pos["entry_slip_loss"] + slip_loss,
                    "exit_reason": f"{orig_exit_reason}_UNLOCKED", "deferral_days": pos.get("deferral_days", 0)
                })
                del open_positions[sym]
                del pending_circuit_exits[sym]

            # 2. Daily structural exits
            processed_structural_exits = []
            for sym in sorted(list(queued_structural_exits.keys())):
                if sym not in open_positions or sym in pending_circuit_exits:
                    processed_structural_exits.append(sym)
                    continue
                pos = open_positions[sym]
                df_sym = universe_ind.get(sym)
                if df_sym is None:
                    continue
                day_rows = df_sym.loc[df_sym["date"] == current_date]
                if day_rows.empty:
                    continue
                bar = day_rows.iloc[0]
                prev_rows = df_sym.loc[df_sym["date"] < current_date]
                prev_close = float(prev_rows.iloc[-1]["close"]) if not prev_rows.empty else float(bar["open"])
                is_down = is_locked_down(float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]), prev_close)
                if is_down:
                    pending_circuit_exits[sym] = {"exit_reason": queued_structural_exits[sym]}
                    processed_structural_exits.append(sym)
                    pos["deferral_days"] = 1
                    continue

                raw_exit_price = float(bar["open"])
                shares = pos["shares"]
                exit_reason = queued_structural_exits[sym]
                fill_price = raw_exit_price * (1.0 - self._slip(sym, bar, raw_exit_price, shares, "SELL"))
                gross_proceeds = shares * raw_exit_price
                net_proceeds_slip = shares * fill_price
                slip_loss = gross_proceeds - net_proceeds_slip
                sell_fees = calc_sell_fees_exact(net_proceeds_slip, current_date, self.config)
                cash_received = net_proceeds_slip - sell_fees["total"]
                cash += cash_received

                total_brokerage += sell_fees["brokerage"]
                total_stt += sell_fees["stt"]
                total_exchange_txn += sell_fees["exchange_txn"]
                total_sebi += sell_fees["sebi"]
                total_gst += sell_fees["gst"]
                total_dp_charges += sell_fees["dp_charges"]
                total_slippage += slip_loss
                total_turnover += gross_proceeds

                order_id_counter += 1
                order_ledger.append({
                    "order_id": order_id_counter, "date": date_str, "symbol": sym, "action": "SELL",
                    "shares": shares, "order_type": "EXIT_AT_OPEN", "status": "FILLED",
                    "reason": exit_reason, "deferral_days": 0
                })
                exec_id_counter += 1
                execution_ledger.append({
                    "exec_id": exec_id_counter, "order_id": order_id_counter, "date": date_str,
                    "symbol": sym, "action": "SELL", "shares": shares, "raw_price": raw_exit_price,
                    "fill_price": fill_price, "gross_val": gross_proceeds, "net_cash": cash_received,
                    "fees": sell_fees["total"], "slippage": slip_loss
                })
                gross_pnl = (raw_exit_price - pos["entry_raw_price"]) * shares
                net_pnl = cash_received - pos["total_entry_outlay"]
                closed_trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                    "entry_price": pos["entry_raw_price"], "entry_fill_price": pos["entry_fill_price"],
                    "exit_date": date_str, "exit_price": raw_exit_price, "exit_fill_price": fill_price,
                    "shares": shares, "hold_days": (current_date - pos["entry_date"]).days,
                    "gross_pnl": gross_pnl, "net_pnl": net_pnl,
                    "total_fees": pos["buy_fees"]["total"] + sell_fees["total"],
                    "total_slippage": pos["entry_slip_loss"] + slip_loss,
                    "exit_reason": exit_reason, "deferral_days": 0
                })
                del open_positions[sym]
                processed_structural_exits.append(sym)

            for sym in processed_structural_exits:
                queued_structural_exits.pop(sym, None)

            # 3. Scheduled Rebalance (or Catch-up Rebalance)
            is_rebal_day = current_date in exec_dates_set
            slots_before_entries = len(open_positions)
            
            if is_rebal_day:
                d_dec = exec_to_decision_map[current_date]
                est_h_val = sum(
                    p["shares"] * (
                        float(universe[sym].loc[universe[sym]["date"] == d_dec].iloc[0]["close"])
                        if (sym in universe and not universe[sym].loc[universe[sym]["date"] == d_dec].empty)
                        else p["entry_raw_price"]
                    )
                    for sym, p in open_positions.items()
                )
                self.config._current_equity = cash + est_h_val
                ranked_df = self.strategy.select_entry_candidates(universe_ind, d_dec, open_positions, self.config)
                hold_buf = getattr(self.config, "rebalance_hold_rank_buffer", self.config.portfolio_size)
                allowed_hold_symbols = list(ranked_df.head(hold_buf).index)
                target_symbols_list = list(ranked_df.head(self.config.portfolio_size).index)

                # Only evaluate rebalance drops if this is NOT a catchup session
                rebal_exits = {}
                if current_date not in scheduled_catchup_sessions:
                    rebal_exits = self.strategy.evaluate_rebalance_exits(
                        universe_ind, d_dec, open_positions, allowed_hold_symbols, self.config
                    )
                    for sym, exit_reason in rebal_exits.items():
                        if sym not in open_positions or sym in pending_circuit_exits:
                            continue
                        pos = open_positions[sym]
                        df_sym = universe_ind.get(sym)
                        if df_sym is None:
                            continue
                        day_rows = df_sym.loc[df_sym["date"] == current_date]
                        if day_rows.empty:
                            continue
                        bar = day_rows.iloc[0]
                        prev_rows = df_sym.loc[df_sym["date"] < current_date]
                        prev_close = float(prev_rows.iloc[-1]["close"]) if not prev_rows.empty else float(bar["open"])
                        is_down = is_locked_down(float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]), prev_close)
                        if is_down:
                            pending_circuit_exits[sym] = {"exit_reason": exit_reason}
                            pos["deferral_days"] = 1
                            continue

                        raw_exit_price = float(bar["open"])
                        shares = pos["shares"]
                        fill_price = raw_exit_price * (1.0 - self._slip(sym, bar, raw_exit_price, shares, "SELL"))
                        gross_proceeds = shares * raw_exit_price
                        net_proceeds_slip = shares * fill_price
                        slip_loss = gross_proceeds - net_proceeds_slip
                        sell_fees = calc_sell_fees_exact(net_proceeds_slip, current_date, self.config)
                        cash_received = net_proceeds_slip - sell_fees["total"]
                        cash += cash_received

                        total_brokerage += sell_fees["brokerage"]
                        total_stt += sell_fees["stt"]
                        total_exchange_txn += sell_fees["exchange_txn"]
                        total_sebi += sell_fees["sebi"]
                        total_gst += sell_fees["gst"]
                        total_dp_charges += sell_fees["dp_charges"]
                        total_slippage += slip_loss
                        total_turnover += gross_proceeds

                        order_id_counter += 1
                        order_ledger.append({
                            "order_id": order_id_counter, "date": date_str, "symbol": sym, "action": "SELL",
                            "shares": shares, "order_type": "EXIT_AT_OPEN", "status": "FILLED",
                            "reason": exit_reason, "deferral_days": 0
                        })
                        exec_id_counter += 1
                        execution_ledger.append({
                            "exec_id": exec_id_counter, "order_id": order_id_counter, "date": date_str,
                            "symbol": sym, "action": "SELL", "shares": shares, "raw_price": raw_exit_price,
                            "fill_price": fill_price, "gross_val": gross_proceeds, "net_cash": cash_received,
                            "fees": sell_fees["total"], "slippage": slip_loss
                        })
                        gross_pnl = (raw_exit_price - pos["entry_raw_price"]) * shares
                        net_pnl = cash_received - pos["total_entry_outlay"]
                        closed_trades.append({
                            "symbol": sym, "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                            "entry_price": pos["entry_raw_price"], "entry_fill_price": pos["entry_fill_price"],
                            "exit_date": date_str, "exit_price": raw_exit_price, "exit_fill_price": fill_price,
                            "shares": shares, "hold_days": (current_date - pos["entry_date"]).days,
                            "gross_pnl": gross_pnl, "net_pnl": net_pnl,
                            "total_fees": pos["buy_fees"]["total"] + sell_fees["total"],
                            "total_slippage": pos["entry_slip_loss"] + slip_loss,
                            "exit_reason": exit_reason, "deferral_days": 0
                        })
                        del open_positions[sym]

                # Rebalance Entries:
                holdings_est_val = sum(
                    p["shares"] * (
                        float(universe[sym].loc[universe[sym]["date"] == d_dec].iloc[0]["close"])
                        if (sym in universe and not universe[sym].loc[universe[sym]["date"] == d_dec].empty)
                        else p["entry_raw_price"]
                    )
                    for sym, p in open_positions.items()
                )
                pretrade_equity = cash + holdings_est_val
                target_alloc_per_stock = pretrade_equity / float(self.config.portfolio_size)

                if fy_tax_payable_accrued > 0 and cash > 1000.0:
                    tax_payment = min(cash - 1000.0, fy_tax_payable_accrued)
                    cash -= tax_payment
                    fy_tax_payable_accrued -= tax_payment

                open_slots = max(0, self.config.portfolio_size - len(open_positions))
                just_sold_today = set(rebal_exits.keys())
                if getattr(self.config, "unaffordable_policy", "SKIP") == "TRY_NEXT":
                    candidate_pool = list(ranked_df.index)
                    new_buys = [s for s in candidate_pool if s not in open_positions and s not in just_sold_today and s not in pending_circuit_exits]
                else:
                    candidate_pool = [
                        s for s in list(ranked_df.index)
                        if s not in open_positions and s not in just_sold_today and s not in pending_circuit_exits
                    ]
                    new_buys = candidate_pool[:open_slots]

                for sym in new_buys:
                    if len(open_positions) >= self.config.portfolio_size or cash < 1000.0:
                        break

                    row_c = ranked_df.loc[sym]
                    p_decision_close = float(row_c["close"])
                    max_vol_pct = getattr(self.config, "max_volume_participation_pct", 0.10)
                    to50 = float(row_c["turnover_50d"]) if "turnover_50d" in row_c else float("inf")
                    max_exec_val = to50 * max_vol_pct if to50 > 0 else float("inf")

                    alloc = min(target_alloc_per_stock, cash, max_exec_val)
                    if alloc < 0.25 * target_alloc_per_stock:
                        continue

                    df_sym = universe_ind.get(sym)
                    if df_sym is None:
                        continue
                    day_rows = df_sym.loc[df_sym["date"] == current_date]
                    if day_rows.empty:
                        continue
                    bar = day_rows.iloc[0]
                    actual_open = float(bar["open"])

                    # Buy limit price cap (+2%)
                    limit_cap_price = p_decision_close * (1.0 + self.config.limit_cap_pct)
                    if actual_open > limit_cap_price:
                        skipped_orders_count += 1
                        order_id_counter += 1
                        order_ledger.append({
                            "order_id": order_id_counter, "date": date_str, "symbol": sym, "action": "BUY",
                            "shares": 0, "order_type": "ENTRY_AT_OPEN", "status": "SKIPPED_GAP_LIMIT_EXCEEDED",
                            "reason": f"Open {actual_open:.2f} > Cap {limit_cap_price:.2f}", "deferral_days": 0
                        })
                        continue

                    prev_rows = df_sym.loc[df_sym["date"] < current_date]
                    prev_close = float(prev_rows.iloc[-1]["close"]) if not prev_rows.empty else actual_open
                    if self.config.enforce_upper_circuit_rejection and is_locked_up(
                        actual_open, float(bar["high"]), float(bar["low"]), float(bar["close"]), prev_close
                    ):
                        skipped_orders_count += 1
                        order_id_counter += 1
                        order_ledger.append({
                            "order_id": order_id_counter, "date": date_str, "symbol": sym, "action": "BUY",
                            "shares": 0, "order_type": "ENTRY_AT_OPEN", "status": "SKIPPED_UPPER_CIRCUIT",
                            "reason": "Locked upper circuit", "deferral_days": 0
                        })
                        continue

                    buffer_mult = 1.0 + self.config.fees_buffer_pct
                    sized_shares = int(alloc // (actual_open * buffer_mult))
                    est_fill_px = actual_open * (1.0 + self._slip(sym, bar, actual_open, max(sized_shares, 1), "BUY"))
                    shares_submitted = int(alloc // (est_fill_px * buffer_mult))
                    if shares_submitted <= 0:
                        skipped_orders_count += 1
                        continue

                    fill_price = actual_open * (1.0 + self._slip(sym, bar, actual_open, shares_submitted, "BUY"))
                    gross_buy_val = shares_submitted * actual_open
                    slipped_buy_val = shares_submitted * fill_price
                    buy_slip_loss = slipped_buy_val - gross_buy_val
                    buy_fees = calc_buy_fees_exact(slipped_buy_val, current_date, self.config)
                    total_outlay = slipped_buy_val + buy_fees["total"]

                    while total_outlay > cash and shares_submitted > 0:
                        shares_submitted -= 1
                        fill_price = actual_open * (1.0 + self._slip(sym, bar, actual_open, max(shares_submitted, 1), "BUY"))
                        gross_buy_val = shares_submitted * actual_open
                        slipped_buy_val = shares_submitted * fill_price
                        buy_slip_loss = slipped_buy_val - gross_buy_val
                        buy_fees = calc_buy_fees_exact(slipped_buy_val, current_date, self.config)
                        total_outlay = slipped_buy_val + buy_fees["total"]

                    if shares_submitted <= 0 or slipped_buy_val < 0.25 * target_alloc_per_stock:
                        skipped_orders_count += 1
                        order_id_counter += 1
                        order_ledger.append({
                            "order_id": order_id_counter, "date": date_str, "symbol": sym, "action": "BUY",
                            "shares": shares_submitted, "order_type": "ENTRY_AT_OPEN",
                            "status": "SKIPPED_MIN_ALLOCATION",
                            "reason": f"Alloc {slipped_buy_val:.2f} < 25% of target {target_alloc_per_stock:.2f}",
                            "deferral_days": 0
                        })
                        continue

                    cash -= total_outlay
                    open_positions[sym] = {
                        "shares": shares_submitted, "entry_date": current_date, "entry_raw_price": actual_open,
                        "entry_fill_price": fill_price, "entry_outlay": gross_buy_val,
                        "entry_slip_loss": buy_slip_loss, "buy_fees": buy_fees,
                        "total_entry_outlay": total_outlay, "last_close": float(bar["close"]),
                        "deferral_days": 0
                    }
                    total_brokerage += buy_fees["brokerage"]
                    total_stt += buy_fees["stt"]
                    total_stamp_duty += buy_fees["stamp_duty"]
                    total_exchange_txn += buy_fees["exchange_txn"]
                    total_sebi += buy_fees["sebi"]
                    total_gst += buy_fees["gst"]
                    total_slippage += buy_slip_loss
                    total_turnover += gross_buy_val

                    order_id_counter += 1
                    order_ledger.append({
                        "order_id": order_id_counter, "date": date_str, "symbol": sym, "action": "BUY",
                        "shares": shares_submitted, "order_type": "ENTRY_AT_OPEN", "status": "FILLED",
                        "reason": "ENTRY_REBALANCE", "deferral_days": 0
                    })
                    exec_id_counter += 1
                    execution_ledger.append({
                        "exec_id": exec_id_counter, "order_id": order_id_counter, "date": date_str,
                        "symbol": sym, "action": "BUY", "shares": shares_submitted, "raw_price": actual_open,
                        "fill_price": fill_price, "gross_val": gross_buy_val, "net_cash": -total_outlay,
                        "fees": buy_fees["total"], "slippage": buy_slip_loss
                    })

                # Approach 3 Catch-Up Scheduling:
                # If after rebalance entries, slots remain unfilled and today was NOT already a catchup session,
                # schedule Day T+2 morning open as a 1-day catch-up attempt!
                if self.enable_catchup and len(open_positions) < self.config.portfolio_size and current_date not in scheduled_catchup_sessions:
                    if day_idx + 1 < len(eval_timeline):
                        next_day = eval_timeline[day_idx + 1]
                        exec_dates_set.add(next_day)
                        exec_to_decision_map[next_day] = current_date  # Decision based on Day T+1 close
                        scheduled_catchup_sessions.add(next_day)

            # 4. EOD Marking & Structural Exit Evaluation
            current_holdings_val = 0.0
            for sym, pos in list(open_positions.items()):
                df_sym = universe_ind.get(sym)
                if df_sym is not None and not df_sym.empty:
                    c_rows = df_sym.loc[df_sym["date"] <= current_date]
                    if not c_rows.empty:
                        last_c = float(c_rows.iloc[-1]["close"])
                        pos["last_close"] = last_c
                        current_holdings_val += pos["shares"] * last_c
                    else:
                        current_holdings_val += pos["shares"] * pos["entry_raw_price"]
                else:
                    current_holdings_val += pos["shares"] * pos["entry_raw_price"]

            daily_structural_exits = self.strategy.evaluate_daily_exits(
                universe_ind, current_date, open_positions, self.config
            )
            for sym, reason in daily_structural_exits.items():
                if sym in open_positions and sym not in pending_circuit_exits:
                    queued_structural_exits[sym] = reason

            # 5. Financial Year Annual Tax Settlement (Dated Indian Rules)
            if getattr(self.config, "deduct_annual_tax", False):
                is_last_day_of_march = (current_date.month == 3) and (
                    day_idx == len(eval_timeline) - 1 or eval_timeline[day_idx + 1].month != 3
                )
                if is_last_day_of_march:
                    fy_name = f"FY{current_date.year - 1}-{current_date.year}"
                    fy_trades = closed_trades[last_tax_processed_trade_idx:]
                    last_tax_processed_trade_idx = len(closed_trades)

                    fy_tax, cf_st_list, cf_lt_list, det = compute_fy_tax(
                        current_date.year, fy_trades, cf_st_list, cf_lt_list
                    )
                    stcg_gross, ltcg_gross = det["stcg_gross"], det["ltcg_gross"]
                    taxable_stcg, taxable_ltcg = det["taxable_stcg"], det["taxable_ltcg"]
                    carried_forward_stcl = sum(a for _, a in cf_st_list)
                    carried_forward_ltcl = sum(a for _, a in cf_lt_list)

                    if fy_tax > 0:
                        fy_tax_payable_accrued += fy_tax
                        immediate_pay = min(max(0.0, cash - 1000.0), fy_tax_payable_accrued)
                        cash -= immediate_pay
                        fy_tax_payable_accrued -= immediate_pay
                        fy_tax_paid_total += immediate_pay

                    fy_tax_ledger.append({
                        "fy": fy_name, "date": date_str, "stcg_gross": round(stcg_gross, 2),
                        "ltcg_gross": round(ltcg_gross, 2), "taxable_stcg": round(taxable_stcg, 2),
                        "taxable_ltcg": round(taxable_ltcg, 2), "tax_paid": round(fy_tax, 2),
                        "carried_forward_stcl": round(carried_forward_stcl, 2),
                        "carried_forward_ltcl": round(carried_forward_ltcl, 2),
                        "cash_after_tax": round(cash, 2), "accrued_unpaid_tax": round(fy_tax_payable_accrued, 2),
                    })

            total_equity = cash + current_holdings_val - fy_tax_payable_accrued
            daily_nav_history.append({
                "date": date_str, "cash": cash, "holdings_val": current_holdings_val,
                "nav": total_equity, "open_positions": len(open_positions),
                "unrealized_pnl": sum(p["shares"] * p["last_close"] - p["total_entry_outlay"] for p in open_positions.values())
            })

        nav_df = pd.DataFrame(daily_nav_history)
        trades_df = pd.DataFrame(closed_trades)
        orders_df = pd.DataFrame(order_ledger)
        execs_df = pd.DataFrame(execution_ledger)

        metrics = {
            "ending_nav": nav_df["nav"].iloc[-1] if not nav_df.empty else cash,
            "total_tax_paid": fy_tax_paid_total,
            "total_friction": total_brokerage + total_stt + total_stamp_duty + total_exchange_txn + total_sebi + total_gst + total_dp_charges + total_slippage,
            "total_trades": len(trades_df),
        }
        return metrics, trades_df, orders_df, execs_df, nav_df


# ----------------------------------------------------------------------------- Benchmark & Metrics
def load_data() -> Tuple[Dict[str, pd.DataFrame], pd.Series]:
    col_bench = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=2000)["stock_scanner"]["benchmark_ohlcv"]
    b_df = pd.DataFrame(list(col_bench.find({"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    b_df["date"] = pd.to_datetime(b_df["date"])
    bench = b_df.drop_duplicates("date", keep="last").sort_values("date").set_index("date")["close"]

    logger.info("Loading universe from cache: %s", CACHE_FILE)
    with open(CACHE_FILE, "rb") as f:
        universe = pickle.load(f)
    return universe, bench


def stats(s: pd.Series, bench: pd.Series, a: str, b: str) -> dict:
    sub = s[(s.index >= a) & (s.index <= b)]
    if len(sub) < 40:
        return {}
    be = bench[(bench.index >= sub.index[0]) & (bench.index <= sub.index[-1])]
    yrs = (sub.index[-1] - sub.index[0]).days / 365.25
    r = sub.pct_change().dropna()
    cagr = ((sub.iloc[-1] / sub.iloc[0]) ** (1 / yrs) - 1) * 100
    bc = ((be.iloc[-1] / be.iloc[0]) ** (1 / yrs) - 1) * 100
    mdd = float((sub / sub.cummax() - 1).min()) * 100
    sharpe = float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0
    return {"cagr": round(cagr, 2), "bench": round(bc, 2), "excess": round(cagr - bc, 2), "mdd": round(mdd, 1), "sharpe": round(sharpe, 2)}


# ----------------------------------------------------------------------------- Main Evaluation
def main():
    logger.info("=" * 80)
    logger.info("EMPIRICAL BACKTEST: REAL-WORLD EXECUTION APPROACHES (2007 - 2026)")
    logger.info("=" * 80)

    universe, bench = load_data()

    experiments = [
        ("Arm0_Baseline_Control", TrendMomentumPITStrategy, BacktestEngine, "TRY_NEXT", False),
        ("Arm1_Approach1A_StrictSkip", TrendMomentumPITStrategy, NextDayCatchupEngine, "SKIP", False),
        ("Arm2_Approach1B_BudgetGateSkip", BudgetGatedPITStrategy, NextDayCatchupEngine, "SKIP", False),
        ("Arm3_Approach3_NextDayCatchup", BudgetGatedPITStrategy, NextDayCatchupEngine, "SKIP", True),
    ]

    summary_rows = []

    for arm_name, strat_cls, engine_cls, unaffordable_pol, is_approach3 in experiments:
        logger.info("=" * 70)
        logger.info("RUNNING ARM: %s (unaffordable_policy=%s)", arm_name, unaffordable_pol)
        logger.info("=" * 70)

        t0 = time.time()
        cfg = BacktestConfig(
            initial_capital=100_000.0,
            portfolio_size=15,
            max_open_positions=15,
            rebalance_hold_rank_buffer=1500,  # Champion HoldFix rule
            rebalance_frequency="MONTHLY",
            min_turnover_50d=0.0,
            min_price=0.0,
            slippage_pct=0.002,
            unaffordable_policy=unaffordable_pol,
            enforce_pit_universe=False,
            pit_top_n_liquidity=0,
            brokerage_per_order=0.0,
            dp_charge_sell=14.75,
            max_volume_participation_pct=0.10,
            deduct_annual_tax=True,
            tax_model="india_fy",
        )

        strat = strat_cls(config=cfg, benchmark=bench)
        if engine_cls == NextDayCatchupEngine:
            engine = engine_cls(strategy=strat, config=cfg, enable_catchup=is_approach3)
        else:
            engine = engine_cls(strategy=strat, config=cfg)
        metrics, trades, orders, execs, nav = engine.run(universe, eval_start_date=START_DATE, eval_end_date=END_DATE)

        nav["date"] = pd.to_datetime(nav["date"])
        navs = nav.set_index("date")["nav"]
        navs.to_csv(OUT_DIR / f"nav_{arm_name}.csv")
        trades.to_csv(OUT_DIR / f"trades_{arm_name}.csv", index=False)

        skipped = orders[orders["status"].astype(str).str.startswith("SKIPPED")] if not orders.empty else pd.DataFrame()
        avg_positions = round(float(nav["open_positions"].mean()), 2)
        avg_cash_pct = round(float((nav["cash"] / nav["nav"]).mean() * 100), 1)

        row = {
            "Arm": arm_name,
            "Policy": unaffordable_pol,
            "Secs": round(time.time() - t0),
            "Trades": len(trades),
            "Ending_NAV": round(float(navs.iloc[-1])),
            "Tax_Paid": round(float(metrics.get("total_tax_paid", 0.0))),
            "Friction": round(float(metrics.get("total_friction", 0.0))),
            "Avg_Open_Positions": avg_positions,
            "Avg_Cash_Pct": avg_cash_pct,
            "Orders_Skipped": len(skipped),
            "Avg_Hold_Days": round(float(trades["hold_days"].mean()), 1) if len(trades) else 0.0,
            "Win_Rate_Pct": round(float((trades["net_pnl"] > 0).mean() * 100), 1) if len(trades) else 0.0,
        }

        for w_name, (w_start, w_end) in WINDOWS.items():
            st = stats(navs, bench, w_start, w_end)
            row.update({
                f"{w_name}_cagr": st.get("cagr"),
                f"{w_name}_mdd": st.get("mdd"),
                f"{w_name}_sharpe": st.get("sharpe"),
            })

        logger.info("COMPLETED %s: FULL CAGR=%.2f%%, MaxDD=%.1f%%, Trades=%d, AvgPos=%.2f, Cash=%.1f%%",
                    arm_name, row["FULL_cagr"], row["FULL_mdd"], row["Trades"], avg_positions, avg_cash_pct)
        summary_rows.append(row)

    df_summary = pd.DataFrame(summary_rows)
    df_summary.to_csv(OUT_DIR / "execution_approaches_summary.csv", index=False)
    print("\n" + "=" * 100)
    print("FINAL COMPARISON MATRIX:")
    print("=" * 100)
    cols = ["Arm", "FULL_cagr", "FULL_mdd", "FULL_sharpe", "Y10_2016_10_cagr", "Trades", "Avg_Open_Positions", "Avg_Cash_Pct", "Orders_Skipped"]
    print(df_summary[cols].to_string(index=False))


if __name__ == "__main__":
    main()
