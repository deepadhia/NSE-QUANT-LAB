"""
engine.py -- Universal Event-Driven BacktestEngine for NSE Equities
===================================================================
Executes any quantitative strategy implementing BaseStrategy under strict
institutional Indian equity market invariants:
- Causal next-session Open execution with slippage
- +2.0% buy limit gap price protection
- Lower-circuit exit lock deferrals and persistent position retention
- Upper-circuit entry rejection
- Discrete whole-share position sizing with cash friction buffers
- Exact statutory Indian delivery charges (STT, Stamp Duty, Exchange tiers, SEBI, GST, DP)
- Machine-precision daily balance sheet reconciliation: NAV == Cash + Market_Value
- MongoDB run archiving with mode='backtest'
"""

from __future__ import annotations
import math
import os
import json
import hashlib
from typing import Dict, List, Tuple, Any, Optional, Set
from datetime import datetime
import numpy as np
import pandas as pd

from .base_strategy import BaseStrategy
from .config import BacktestConfig
from .execution import is_locked_down, is_locked_up
from .fees import calc_buy_fees_exact, calc_sell_fees_exact
from .metrics import calculate_financial_metrics as compute_metrics
from .tax_india import compute_fy_tax


class BacktestEngine:
    """
    Universal event-driven backtesting engine for quantitative equity research.
    """

    def __init__(self, strategy: BaseStrategy, config: BacktestConfig):
        self.strategy = strategy
        self.config = config

    def _slip(self, sym: str, bar: Any, price: float, shares: int, side: str) -> float:
        """Per-leg slippage fraction. A strategy may provide slippage_pct_for(sym, bar, price, shares, side)
        (liquidity-tiered spread + impact); otherwise the flat config.slippage_pct is used (previous behaviour)."""
        fn = getattr(self.strategy, "slippage_pct_for", None)
        if fn is None:
            return self.config.slippage_pct
        return float(fn(sym, bar, float(price), int(shares), side))

    def run(
        self,
        universe: Dict[str, pd.DataFrame],
        eval_start_date: str = "2019-01-01",
        eval_end_date: str = "2026-10-23",
        save_results: bool = False,
        output_dir: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        # 1. Precompute indicators across all universe stocks
        universe_ind: Dict[str, pd.DataFrame] = {}
        for sym, df in universe.items():
            try:
                ind_df = self.strategy.compute_indicators(df, self.config)
            except TypeError:
                ind_df = self.strategy.compute_indicators(df)
            if not ind_df.empty:
                universe_ind[sym] = ind_df

        # 2. Build common sorted timeline
        all_dates = set()
        for df in universe_ind.values():
            all_dates.update(df["date"].tolist())
        timeline = sorted(list(all_dates))

        eval_start_dt = pd.to_datetime(eval_start_date)
        eval_end_dt = pd.to_datetime(eval_end_date)
        eval_timeline = [d for d in timeline if eval_start_dt <= d <= eval_end_dt]

        if not eval_timeline:
            raise ValueError(f"No trading sessions in range {eval_start_date} to {eval_end_date}")

        # 3. Build rebalance execution schedule
        exec_to_decision_map = self.strategy.get_rebalance_schedule(
            timeline, eval_start_dt, eval_end_dt, self.config
        )
        exec_dates_set = set(exec_to_decision_map.keys())

        # 4. Simulation State
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

        pending_circuit_exits: Dict[str, Dict[str, Any]] = {}
        queued_structural_exits: Dict[str, str] = {}

        carried_forward_stcl = 0.0
        carried_forward_ltcl = 0.0
        cf_st_list: List[Tuple[int, float]] = []
        cf_lt_list: List[Tuple[int, float]] = []
        fy_tax_ledger: List[Dict[str, Any]] = []
        fy_tax_payable_accrued = 0.0
        last_tax_processed_trade_idx = 0

        for idx, current_date in enumerate(eval_timeline):
            date_str = current_date.strftime("%Y-%m-%d")
            # Soft tax settlement from available cash without forced stock sales
            if fy_tax_payable_accrued > 0 and cash > 1000.0:
                tax_payment = min(cash - 1000.0, fy_tax_payable_accrued)
                cash -= tax_payment
                fy_tax_payable_accrued -= tax_payment


            # ------------------------------------------------------------------
            # STEP 0: FORCE-LIQUIDATE DELISTED / CEASED SERIES HOLDINGS
            # ------------------------------------------------------------------
            for sym in sorted(list(open_positions.keys())):
                df_sym = universe_ind.get(sym)
                if df_sym is not None and not df_sym.empty and current_date > df_sym["date"].max():
                    pos = open_positions[sym]
                    last_row = df_sym.iloc[-1]
                    raw_exit_price = float(last_row["close"])
                    fill_price = raw_exit_price * (1.0 - self._slip(sym, last_row, raw_exit_price, pos["shares"], "SELL_DELIST"))
                    shares = pos["shares"]
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
                        "order_id": order_id_counter,
                        "date": date_str,
                        "symbol": sym,
                        "action": "SELL",
                        "shares": shares,
                        "order_type": "EXIT_AT_OPEN",
                        "status": "FILLED",
                        "reason": "DATA_ENDED_FORCED_EXIT",
                        "deferral_days": 0,
                    })

                    exec_id_counter += 1
                    execution_ledger.append({
                        "exec_id": exec_id_counter,
                        "order_id": order_id_counter,
                        "date": date_str,
                        "symbol": sym,
                        "action": "SELL",
                        "shares": shares,
                        "raw_price": raw_exit_price,
                        "fill_price": fill_price,
                        "gross_val": gross_proceeds,
                        "net_cash": cash_received,
                        "fees": sell_fees["total"],
                        "slippage": slip_loss,
                    })

                    gross_pnl = (raw_exit_price - pos["entry_raw_price"]) * shares
                    net_pnl = cash_received - pos["total_entry_outlay"]
                    closed_trades.append({
                        "symbol": sym,
                        "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                        "entry_price": pos["entry_raw_price"],
                        "entry_fill_price": pos["entry_fill_price"],
                        "exit_date": date_str,
                        "exit_price": raw_exit_price,
                        "exit_fill_price": fill_price,
                        "shares": shares,
                        "hold_days": (current_date - pos["entry_date"]).days,
                        "gross_pnl": gross_pnl,
                        "net_pnl": net_pnl,
                        "total_fees": pos["buy_fees"]["total"] + sell_fees["total"],
                        "total_slippage": pos["entry_slip_loss"] + slip_loss,
                        "exit_reason": "DATA_ENDED_FORCED_EXIT",
                        "deferral_days": 0,
                    })

                    del open_positions[sym]
                    if sym in pending_circuit_exits:
                        del pending_circuit_exits[sym]
                    if sym in queued_structural_exits:
                        queued_structural_exits.pop(sym, None)

            # ------------------------------------------------------------------
            # STEP 1: RETRY LOWER-CIRCUIT DEFERRED EXITS AT TODAY'S OPEN
            # ------------------------------------------------------------------
            for sym in sorted(list(pending_circuit_exits.keys())):
                if sym not in open_positions:
                    del pending_circuit_exits[sym]
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
                prev_close = float(prev_rows.iloc[-1]["close"]) if not prev_rows.empty else float(bar["close"])

                orig_exit_reason = pending_circuit_exits[sym].get("original_reason", "CIRCUIT_EXIT")
                if self.config.enforce_lower_circuit_deferral and is_locked_down(
                    bar["open"], bar["high"], bar["low"], bar["close"], prev_close
                ):
                    pos["deferral_days"] = pos.get("deferral_days", 0) + 1
                    order_id_counter += 1
                    order_ledger.append({
                        "order_id": order_id_counter,
                        "date": date_str,
                        "symbol": sym,
                        "action": "SELL",
                        "shares": pos["shares"],
                        "order_type": "EXIT_AT_OPEN",
                        "status": "DEFERRED_LOWER_CIRCUIT",
                        "reason": f"Circuit lock deferral: {bar['low']}",
                        "deferral_days": pos["deferral_days"],
                    })
                    pending_circuit_exits[sym]["order_id"] = order_id_counter
                    continue

                raw_exit_price = float(bar["open"])
                fill_price = raw_exit_price * (1.0 - self._slip(sym, bar, raw_exit_price, pos["shares"], "SELL"))
                shares = pos["shares"]
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
                    "order_id": order_id_counter,
                    "date": date_str,
                    "symbol": sym,
                    "action": "SELL",
                    "shares": shares,
                    "order_type": "EXIT_AT_OPEN",
                    "status": "FILLED",
                    "reason": f"{orig_exit_reason}_UNLOCKED",
                    "deferral_days": pos.get("deferral_days", 0),
                })

                exec_id_counter += 1
                execution_ledger.append({
                    "exec_id": exec_id_counter,
                    "order_id": order_id_counter,
                    "date": date_str,
                    "symbol": sym,
                    "action": "SELL",
                    "shares": shares,
                    "raw_price": raw_exit_price,
                    "fill_price": fill_price,
                    "gross_val": gross_proceeds,
                    "net_cash": cash_received,
                    "fees": sell_fees["total"],
                    "slippage": slip_loss,
                })

                gross_pnl = (raw_exit_price - pos["entry_raw_price"]) * shares
                net_pnl = cash_received - pos["total_entry_outlay"]
                closed_trades.append({
                    "symbol": sym,
                    "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                    "entry_price": pos["entry_raw_price"],
                    "entry_fill_price": pos["entry_fill_price"],
                    "exit_date": date_str,
                    "exit_price": raw_exit_price,
                    "exit_fill_price": fill_price,
                    "shares": shares,
                    "hold_days": (current_date - pos["entry_date"]).days,
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "total_fees": pos["buy_fees"]["total"] + sell_fees["total"],
                    "total_slippage": pos["entry_slip_loss"] + slip_loss,
                    "exit_reason": f"{orig_exit_reason}_UNLOCKED",
                    "deferral_days": pos.get("deferral_days", 0),
                })

                del open_positions[sym]
                del pending_circuit_exits[sym]

            # ------------------------------------------------------------------
            # STEP 2: EXECUTE DAILY QUEUED STRUCTURAL EXITS AT OPEN
            # ------------------------------------------------------------------
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
                    # Missing morning bar; retain exit request for next session
                    continue
                bar = day_rows.iloc[0]
                prev_rows = df_sym.loc[df_sym["date"] < current_date]
                prev_close = float(prev_rows.iloc[-1]["close"]) if not prev_rows.empty else float(bar["close"])

                exit_reason = queued_structural_exits[sym]
                if self.config.enforce_lower_circuit_deferral and is_locked_down(
                    bar["open"], bar["high"], bar["low"], bar["close"], prev_close
                ):
                    pos["deferral_days"] = 1
                    order_id_counter += 1
                    order_ledger.append({
                        "order_id": order_id_counter,
                        "date": date_str,
                        "symbol": sym,
                        "action": "SELL",
                        "shares": pos["shares"],
                        "order_type": "EXIT_AT_OPEN",
                        "status": "DEFERRED_LOWER_CIRCUIT",
                        "reason": f"Lower circuit lock: Open=Low={bar['low']}",
                        "deferral_days": 1,
                    })
                    pending_circuit_exits[sym] = {"order_id": order_id_counter, "original_reason": exit_reason}
                    processed_structural_exits.append(sym)
                    continue

                raw_exit_price = float(bar["open"])
                fill_price = raw_exit_price * (1.0 - self._slip(sym, bar, raw_exit_price, pos["shares"], "SELL"))
                shares = pos["shares"]
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
                    "order_id": order_id_counter,
                    "date": date_str,
                    "symbol": sym,
                    "action": "SELL",
                    "shares": shares,
                    "order_type": "EXIT_AT_OPEN",
                    "status": "FILLED",
                    "reason": exit_reason,
                    "deferral_days": 0,
                })

                exec_id_counter += 1
                execution_ledger.append({
                    "exec_id": exec_id_counter,
                    "order_id": order_id_counter,
                    "date": date_str,
                    "symbol": sym,
                    "action": "SELL",
                    "shares": shares,
                    "raw_price": raw_exit_price,
                    "fill_price": fill_price,
                    "gross_val": gross_proceeds,
                    "net_cash": cash_received,
                    "fees": sell_fees["total"],
                    "slippage": slip_loss,
                })

                gross_pnl = (raw_exit_price - pos["entry_raw_price"]) * shares
                net_pnl = cash_received - pos["total_entry_outlay"]
                closed_trades.append({
                    "symbol": sym,
                    "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                    "entry_price": pos["entry_raw_price"],
                    "entry_fill_price": pos["entry_fill_price"],
                    "exit_date": date_str,
                    "exit_price": raw_exit_price,
                    "exit_fill_price": fill_price,
                    "shares": shares,
                    "hold_days": (current_date - pos["entry_date"]).days,
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "total_fees": pos["buy_fees"]["total"] + sell_fees["total"],
                    "total_slippage": pos["entry_slip_loss"] + slip_loss,
                    "exit_reason": exit_reason,
                    "deferral_days": 0,
                })

                del open_positions[sym]
                processed_structural_exits.append(sym)

            for sym in processed_structural_exits:
                queued_structural_exits.pop(sym, None)

            # ------------------------------------------------------------------
            # STEP 3: SCHEDULED REBALANCE (IF TODAY IS AN EXECUTION DATE)
            # ------------------------------------------------------------------
            if current_date in exec_dates_set:
                d_dec = exec_to_decision_map[current_date]
                ranked_df = self.strategy.select_entry_candidates(
                    universe_ind, d_dec, open_positions, self.config
                )

                # Rank Hysteresis / Hold Buffer:
                # Retain held positions as long as their rank is within hold buffer (e.g. Top 16)
                hold_buf = getattr(self.config, "rebalance_hold_rank_buffer", self.config.portfolio_size)
                allowed_hold_symbols = list(ranked_df.head(hold_buf).index)
                target_symbols_list = list(ranked_df.head(self.config.portfolio_size).index)

                # Rebalance Exits:
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
                    prev_close = float(prev_rows.iloc[-1]["close"]) if not prev_rows.empty else float(bar["close"])

                    if self.config.enforce_lower_circuit_deferral and is_locked_down(
                        bar["open"], bar["high"], bar["low"], bar["close"], prev_close
                    ):
                        pos["deferral_days"] = 1
                        order_id_counter += 1
                        order_ledger.append({
                            "order_id": order_id_counter,
                            "date": date_str,
                            "symbol": sym,
                            "action": "SELL",
                            "shares": pos["shares"],
                            "order_type": "EXIT_AT_OPEN",
                            "status": "DEFERRED_LOWER_CIRCUIT",
                            "reason": f"Circuit lock on rebalance: {bar['low']}",
                            "deferral_days": 1,
                        })
                        pending_circuit_exits[sym] = {"order_id": order_id_counter, "original_reason": exit_reason}
                        continue

                    raw_exit_price = float(bar["open"])
                    fill_price = raw_exit_price * (1.0 - self._slip(sym, bar, raw_exit_price, pos["shares"], "SELL"))
                    shares = pos["shares"]
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
                        "order_id": order_id_counter,
                        "date": date_str,
                        "symbol": sym,
                        "action": "SELL",
                        "shares": shares,
                        "order_type": "EXIT_AT_OPEN",
                        "status": "FILLED",
                        "reason": exit_reason,
                        "deferral_days": 0,
                    })

                    exec_id_counter += 1
                    execution_ledger.append({
                        "exec_id": exec_id_counter,
                        "order_id": order_id_counter,
                        "date": date_str,
                        "symbol": sym,
                        "action": "SELL",
                        "shares": shares,
                        "raw_price": raw_exit_price,
                        "fill_price": fill_price,
                        "gross_val": gross_proceeds,
                        "net_cash": cash_received,
                        "fees": sell_fees["total"],
                        "slippage": slip_loss,
                    })

                    gross_pnl = (raw_exit_price - pos["entry_raw_price"]) * shares
                    net_pnl = cash_received - pos["total_entry_outlay"]
                    closed_trades.append({
                        "symbol": sym,
                        "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                        "entry_price": pos["entry_raw_price"],
                        "entry_fill_price": pos["entry_fill_price"],
                        "exit_date": date_str,
                        "exit_price": raw_exit_price,
                        "exit_fill_price": fill_price,
                        "shares": shares,
                        "hold_days": (current_date - pos["entry_date"]).days,
                        "gross_pnl": gross_pnl,
                        "net_pnl": net_pnl,
                        "total_fees": pos["buy_fees"]["total"] + sell_fees["total"],
                        "total_slippage": pos["entry_slip_loss"] + slip_loss,
                        "exit_reason": exit_reason,
                        "deferral_days": 0,
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

                # Settle accrued tax if fresh sales generated cash
                if fy_tax_payable_accrued > 0 and cash > 1000.0:
                    tax_payment = min(cash - 1000.0, fy_tax_payable_accrued)
                    cash -= tax_payment
                    fy_tax_payable_accrued -= tax_payment

                if getattr(self.config, "unaffordable_policy", "SKIP") == "TRY_NEXT":
                    candidate_pool = list(ranked_df.index)
                else:
                    candidate_pool = target_symbols_list
                # Wash trade prevention: do not rebuy a stock that was just sold today
                just_sold_today = set(rebal_exits.keys())
                new_buys = [s for s in candidate_pool if s not in open_positions and s not in just_sold_today and s not in pending_circuit_exits]

                for sym in new_buys:
                    if len(open_positions) >= self.config.portfolio_size or cash < 1000.0:
                        break

                    row_c = ranked_df.loc[sym]
                    p_decision_close = float(row_c["close"])
                    est_max_fill = p_decision_close * (
                        1.0 + self.config.gap_buffer_pct + self.config.slippage_pct + self.config.fees_buffer_pct
                    )

                    # Volume participation cap: max 10% of 50-day average daily turnover
                    max_vol_pct = getattr(self.config, "max_volume_participation_pct", 0.10)
                    to50 = float(row_c["turnover_50d"]) if "turnover_50d" in row_c else float("inf")
                    max_exec_val = to50 * max_vol_pct if to50 > 0 else float("inf")

                    alloc = min(target_alloc_per_stock, cash, max_exec_val)
                    if alloc < 0.25 * target_alloc_per_stock:
                        continue
                    shares_submitted = int(np.floor(alloc / est_max_fill))

                    if shares_submitted <= 0:
                        skipped_orders_count += 1
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
                            "order_id": order_id_counter,
                            "date": date_str,
                            "symbol": sym,
                            "action": "BUY",
                            "shares": shares_submitted,
                            "order_type": "ENTRY_AT_OPEN",
                            "status": "SKIPPED_GAP_LIMIT_EXCEEDED",
                            "reason": f"Open {actual_open:.2f} > Cap {limit_cap_price:.2f}",
                            "deferral_days": 0,
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
                            "order_id": order_id_counter,
                            "date": date_str,
                            "symbol": sym,
                            "action": "BUY",
                            "shares": shares_submitted,
                            "order_type": "ENTRY_AT_OPEN",
                            "status": "SKIPPED_UPPER_CIRCUIT",
                            "reason": "Locked upper circuit",
                            "deferral_days": 0,
                        })
                        continue

                    fill_price = actual_open * (1.0 + self._slip(sym, bar, actual_open, shares_submitted, "BUY"))
                    gross_buy_val = shares_submitted * actual_open
                    slipped_buy_val = shares_submitted * fill_price
                    buy_slip_loss = slipped_buy_val - gross_buy_val
                    buy_fees = calc_buy_fees_exact(slipped_buy_val, current_date, self.config)
                    total_outlay = slipped_buy_val + buy_fees["total"]

                    if total_outlay > cash:
                        skipped_orders_count += 1
                        order_id_counter += 1
                        order_ledger.append({
                            "order_id": order_id_counter,
                            "date": date_str,
                            "symbol": sym,
                            "action": "BUY",
                            "shares": shares_submitted,
                            "order_type": "ENTRY_AT_OPEN",
                            "status": "REJECTED_INSUFFICIENT_CASH",
                            "reason": f"Required {total_outlay:.2f} > Cash {cash:.2f}",
                            "deferral_days": 0,
                        })
                        continue

                    cash -= total_outlay
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
                        "order_id": order_id_counter,
                        "date": date_str,
                        "symbol": sym,
                        "action": "BUY",
                        "shares": shares_submitted,
                        "order_type": "ENTRY_AT_OPEN",
                        "status": "FILLED",
                        "reason": f"RANK_{ranked_df.loc[sym, 'rank'] if 'rank' in ranked_df.columns else 1}",
                        "deferral_days": 0,
                    })

                    exec_id_counter += 1
                    execution_ledger.append({
                        "exec_id": exec_id_counter,
                        "order_id": order_id_counter,
                        "date": date_str,
                        "symbol": sym,
                        "action": "BUY",
                        "shares": shares_submitted,
                        "raw_price": actual_open,
                        "fill_price": fill_price,
                        "gross_val": gross_buy_val,
                        "net_cash": -total_outlay,
                        "fees": buy_fees["total"],
                        "slippage": buy_slip_loss,
                    })

                    open_positions[sym] = {
                        "symbol": sym,
                        "shares": shares_submitted,
                        "entry_date": current_date,
                        "entry_raw_price": actual_open,
                        "entry_fill_price": fill_price,
                        "entry_slip_loss": buy_slip_loss,
                        "buy_fees": buy_fees,
                        "total_entry_outlay": total_outlay,
                        "deferral_days": 0,
                    }

            # ------------------------------------------------------------------
            # STEP 4: DAILY STRUCTURAL EXIT CHECK AT TODAY'S CLOSE
            # ------------------------------------------------------------------
            daily_structural = self.strategy.evaluate_daily_exits(
                universe_ind, current_date, open_positions, self.config
            )
            for sym, reason in daily_structural.items():
                if sym not in pending_circuit_exits:
                    queued_structural_exits[sym] = reason

            # ------------------------------------------------------------------
            # STEP 5: DAILY MARK-TO-MARKET & BALANCE SHEET RECONCILIATION
            # ------------------------------------------------------------------
            holdings_market_val = 0.0
            for sym in sorted(list(open_positions.keys())):
                pos = open_positions[sym]
                df_sym = universe_ind.get(sym)
                if df_sym is not None:
                    day_rows = df_sym.loc[df_sym["date"] <= current_date]
                    if not day_rows.empty:
                        c_price = float(day_rows.iloc[-1]["close"])
                        holdings_market_val += pos["shares"] * c_price
                    else:
                        holdings_market_val += pos["shares"] * pos["entry_raw_price"]
                else:
                    holdings_market_val += pos["shares"] * pos["entry_raw_price"]

            # ------------------------------------------------------------------
            # STEP 6: STATUTORY ANNUAL FINANCIAL YEAR TAX SETTLEMENT (SECTIONS 70 & 74)
            # ------------------------------------------------------------------
            if getattr(self.config, "deduct_annual_tax", False):
                is_last_day_of_march = (current_date.month == 3) and (
                    idx == len(eval_timeline) - 1 or eval_timeline[idx + 1].month != 3
                )
                if is_last_day_of_march:
                    fy_name = f"FY{current_date.year - 1}-{current_date.year}"
                    fy_trades = closed_trades[last_tax_processed_trade_idx:]
                    last_tax_processed_trade_idx = len(closed_trades)

                    if getattr(self.config, "tax_model", "india_fy") == "flat_legacy":
                        stcg_gross = sum(t["net_pnl"] for t in fy_trades if t["hold_days"] < 365)
                        ltcg_gross = sum(t["net_pnl"] for t in fy_trades if t["hold_days"] >= 365)
                        net_stcg = stcg_gross - carried_forward_stcl
                        if net_stcg < 0:
                            carried_forward_stcl = abs(net_stcg)
                            taxable_stcg = 0.0
                        else:
                            taxable_stcg = net_stcg
                            carried_forward_stcl = 0.0
                        net_ltcg = ltcg_gross - carried_forward_ltcl
                        carried_forward_ltcl = abs(net_ltcg) if net_ltcg < 0 else 0.0
                        if carried_forward_stcl > 0 and net_ltcg > 0:
                            stcl_offset = min(carried_forward_stcl, net_ltcg)
                            net_ltcg -= stcl_offset
                            carried_forward_stcl -= stcl_offset
                        taxable_ltcg = max(0.0, net_ltcg - 125_000.0) if net_ltcg > 0 else 0.0
                        fy_tax = taxable_stcg * 0.20 + taxable_ltcg * 0.125
                    else:
                        fy_tax, cf_st_list, cf_lt_list, det = compute_fy_tax(
                            current_date.year, fy_trades, cf_st_list, cf_lt_list
                        )
                        stcg_gross, ltcg_gross = det["stcg_gross"], det["ltcg_gross"]
                        taxable_stcg, taxable_ltcg = det["taxable_stcg"], det["taxable_ltcg"]
                        carried_forward_stcl = sum(a for _, a in cf_st_list)
                        carried_forward_ltcl = sum(a for _, a in cf_lt_list)

                    if fy_tax > 0:
                        fy_tax_payable_accrued += fy_tax
                        # Pay immediately from available cash up to Rs 1,000 reserve (never forces stock sales)
                        immediate_pay = min(max(0.0, cash - 1000.0), fy_tax_payable_accrued)
                        cash -= immediate_pay
                        fy_tax_payable_accrued -= immediate_pay

                    fy_tax_ledger.append({
                        "fy": fy_name,
                        "date": date_str,
                        "stcg_gross": round(stcg_gross, 2),
                        "ltcg_gross": round(ltcg_gross, 2),
                        "taxable_stcg": round(taxable_stcg, 2),
                        "taxable_ltcg": round(taxable_ltcg, 2),
                        "tax_paid": round(fy_tax, 2),
                        "carried_forward_stcl": round(carried_forward_stcl, 2),
                        "carried_forward_ltcl": round(carried_forward_ltcl, 2),
                        "cash_after_tax": round(cash, 2),
                        "accrued_unpaid_tax": round(fy_tax_payable_accrued, 2),
                    })

            # Accrued-but-unpaid tax is a liability: NAV is shown net of it (0 when taxes are off or fully paid)
            nav = cash + holdings_market_val - fy_tax_payable_accrued
            daily_nav_history.append({
                "date": current_date,
                "cash": cash,
                "holdings_val": holdings_market_val,
                "invested_val": holdings_market_val,
                "nav": nav,
                "open_positions": len(open_positions),
                "open_positions_count": len(open_positions),
            })

        # Compile result DataFrames
        order_cols = ["order_id", "date", "symbol", "action", "shares", "order_type", "status", "reason", "deferral_days"]
        order_ledger_df = pd.DataFrame(order_ledger)
        if order_ledger_df.empty:
            order_ledger_df = pd.DataFrame(columns=order_cols)

        exec_cols = ["exec_id", "order_id", "date", "symbol", "action", "shares", "raw_price", "fill_price", "gross_val", "net_cash", "fees", "slippage"]
        exec_ledger_df = pd.DataFrame(execution_ledger)
        if exec_ledger_df.empty:
            exec_ledger_df = pd.DataFrame(columns=exec_cols)

        trade_cols = ["symbol", "entry_date", "entry_price", "entry_fill_price", "exit_date", "exit_price", "exit_fill_price", "shares", "hold_days", "gross_pnl", "net_pnl", "total_fees", "total_slippage", "exit_reason", "deferral_days"]
        trades_df = pd.DataFrame(closed_trades)
        if trades_df.empty:
            trades_df = pd.DataFrame(columns=trade_cols)

        nav_cols = ["date", "cash", "holdings_val", "invested_val", "nav", "open_positions", "open_positions_count"]
        nav_df = pd.DataFrame(daily_nav_history)
        if nav_df.empty:
            nav_df = pd.DataFrame(columns=nav_cols)

        stat_fees = float(total_brokerage + total_stt + total_stamp_duty + total_exchange_txn + total_sebi + total_gst + total_dp_charges)
        metrics = compute_metrics(
            initial_capital=float(self.config.initial_capital),
            nav_df=nav_df,
            trades_df=trades_df,
            closed_trades=closed_trades,
            stat_fees=stat_fees,
            slippage=float(total_slippage),
            skipped_orders=int(skipped_orders_count),
        )
        metrics["strategy"] = getattr(self.strategy, "name", "STRATEGY")
        metrics["fy_tax_ledger"] = fy_tax_ledger
        metrics["total_tax_paid"] = sum(x["tax_paid"] for x in fy_tax_ledger)
        metrics["start_date"] = eval_start_date
        metrics["end_date"] = eval_end_date
        metrics["total_turnover"] = float(total_turnover)

        # Save to disk with cryptographic proof if requested
        if save_results:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            s_name = getattr(self.strategy, "name", "strategy").lower().replace(" ", "_")
            out_path = output_dir or os.path.join("results", "backtests", f"{s_name}_{ts}")
            os.makedirs(out_path, exist_ok=True)

            nav_df.to_csv(os.path.join(out_path, "daily_nav.csv"), index=False)
            trades_df.to_csv(os.path.join(out_path, "trades.csv"), index=False)
            order_ledger_df.to_csv(os.path.join(out_path, "orders.csv"), index=False)
            exec_ledger_df.to_csv(os.path.join(out_path, "executions.csv"), index=False)

            with open(os.path.join(out_path, "metrics.json"), "w", encoding="utf-8") as f:
                json.dump(metrics, f, indent=2)

            with open(os.path.join(out_path, "config.json"), "w", encoding="utf-8") as f:
                json.dump(self.config.to_dict(), f, indent=2)

            # Generate SHA256 manifest
            manifest = {}
            for fn in ["daily_nav.csv", "trades.csv", "orders.csv", "executions.csv", "metrics.json", "config.json"]:
                fp = os.path.join(out_path, fn)
                with open(fp, "rb") as fh:
                    manifest[fn] = hashlib.sha256(fh.read()).hexdigest()

            with open(os.path.join(out_path, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

        return metrics, trades_df, order_ledger_df, exec_ledger_df, nav_df
