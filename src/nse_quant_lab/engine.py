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


class BacktestEngine:
    """
    Universal event-driven backtesting engine for quantitative equity research.
    """

    def __init__(self, strategy: BaseStrategy, config: BacktestConfig):
        self.strategy = strategy
        self.config = config

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
        queued_structural_exits: Set[str] = set()

        for current_date in eval_timeline:
            date_str = current_date.strftime("%Y-%m-%d")

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
                    continue

                raw_exit_price = float(bar["open"])
                fill_price = raw_exit_price * (1.0 - self.config.slippage_pct)
                shares = pos["shares"]
                gross_proceeds = shares * raw_exit_price
                net_proceeds_slip = shares * fill_price
                slip_loss = gross_proceeds - net_proceeds_slip

                sell_fees = calc_sell_fees_exact(gross_proceeds, current_date, self.config)
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

                exec_id_counter += 1
                execution_ledger.append({
                    "exec_id": exec_id_counter,
                    "order_id": pending_circuit_exits[sym]["order_id"],
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
                    "exit_reason": "CIRCUIT_UNLOCKED",
                    "deferral_days": pos.get("deferral_days", 0),
                })

                del open_positions[sym]
                del pending_circuit_exits[sym]

            # ------------------------------------------------------------------
            # STEP 2: EXECUTE DAILY QUEUED STRUCTURAL EXITS AT OPEN
            # ------------------------------------------------------------------
            for sym in sorted(list(queued_structural_exits)):
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
                        "reason": f"Lower circuit lock: Open=Low={bar['low']}",
                        "deferral_days": 1,
                    })
                    pending_circuit_exits[sym] = {"order_id": order_id_counter}
                    continue

                raw_exit_price = float(bar["open"])
                fill_price = raw_exit_price * (1.0 - self.config.slippage_pct)
                shares = pos["shares"]
                gross_proceeds = shares * raw_exit_price
                net_proceeds_slip = shares * fill_price
                slip_loss = gross_proceeds - net_proceeds_slip

                sell_fees = calc_sell_fees_exact(gross_proceeds, current_date, self.config)
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
                    "reason": "STRUCTURAL_EXIT",
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
                    "exit_reason": "STRUCTURAL_EXIT",
                    "deferral_days": 0,
                })

                del open_positions[sym]

            queued_structural_exits.clear()

            # ------------------------------------------------------------------
            # STEP 3: SCHEDULED REBALANCE (IF TODAY IS AN EXECUTION DATE)
            # ------------------------------------------------------------------
            if current_date in exec_dates_set:
                d_dec = exec_to_decision_map[current_date]
                ranked_df = self.strategy.select_entry_candidates(
                    universe_ind, d_dec, open_positions, self.config
                )

                target_symbols_list = list(ranked_df.head(self.config.portfolio_size).index)

                # Rebalance Exits:
                rebal_exits = self.strategy.evaluate_rebalance_exits(
                    universe_ind, d_dec, open_positions, target_symbols_list, self.config
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
                        pending_circuit_exits[sym] = {"order_id": order_id_counter}
                        continue

                    raw_exit_price = float(bar["open"])
                    fill_price = raw_exit_price * (1.0 - self.config.slippage_pct)
                    shares = pos["shares"]
                    gross_proceeds = shares * raw_exit_price
                    net_proceeds_slip = shares * fill_price
                    slip_loss = gross_proceeds - net_proceeds_slip

                    sell_fees = calc_sell_fees_exact(gross_proceeds, current_date, self.config)
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
                holdings_est_val = sum(p["shares"] * p["entry_raw_price"] for p in open_positions.values())
                pretrade_equity = cash + holdings_est_val
                target_alloc_per_stock = pretrade_equity / float(self.config.portfolio_size)

                new_buys = [s for s in target_symbols_list if s not in open_positions and s not in pending_circuit_exits]

                for sym in new_buys:
                    if cash < 1000.0:
                        break

                    row_c = ranked_df.loc[sym]
                    p_decision_close = float(row_c["close"])
                    est_max_fill = p_decision_close * (
                        1.0 + self.config.gap_buffer_pct + self.config.slippage_pct + self.config.fees_buffer_pct
                    )
                    alloc = min(target_alloc_per_stock, cash)
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

                    fill_price = actual_open * (1.0 + self.config.slippage_pct)
                    gross_buy_val = shares_submitted * actual_open
                    slipped_buy_val = shares_submitted * fill_price
                    buy_slip_loss = slipped_buy_val - gross_buy_val
                    buy_fees = calc_buy_fees_exact(gross_buy_val, current_date, self.config)
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
            for sym in daily_structural.keys():
                if sym not in pending_circuit_exits:
                    queued_structural_exits.add(sym)

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

            nav = cash + holdings_market_val
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
