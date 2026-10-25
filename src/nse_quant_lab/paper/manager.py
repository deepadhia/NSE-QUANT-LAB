"""
manager.py -- Forward Paper Trading Engine & Database Lifecycle Manager
"""

from __future__ import annotations
import logging
import math
import uuid
from typing import Dict, List, Optional, Any
from datetime import datetime
import numpy as np
import pandas as pd

from ..base_strategy import BaseStrategy
from ..config import PaperTradingConfig
from ..db.mongo_client import MongoDatabase
from ..db.models import OrderModel, ExecutionModel, PositionModel, TradeModel, DailyNavModel, StrategyModel
from ..execution import is_locked_down, is_locked_up
from ..fees import calc_buy_fees_exact, calc_sell_fees_exact
from ..notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


class PaperTradingManager:
    """
    Manages prospective forward paper trading with complete database persistence.
    Operates on live daily candles, logs all orders/positions into MongoDB with mode='paper',
    and emits real-time Telegram notifications.
    """

    def __init__(self, strategy: BaseStrategy, config: PaperTradingConfig, db: Optional[MongoDatabase] = None):
        self.strategy = strategy
        self.config = config
        self.db = db or MongoDatabase.get_instance(config.mongo_uri, config.db_name)
        self.notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id, config.telegram_enabled)

    def initialize_portfolio(self):
        """Initializes empty paper trading portfolio in MongoDB if not present."""
        nav_col = self.db.daily_nav
        existing = nav_col.find_one({"strategy_id": self.config.strategy_id, "mode": "paper"})
        if not existing:
            nav_col.insert_one({
                "strategy_id": self.config.strategy_id,
                "mode": "paper",
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "cash": float(self.config.initial_capital),
                "holdings_val": 0.0,
                "nav": float(self.config.initial_capital),
                "open_positions_count": 0,
                "updated_at": datetime.utcnow().isoformat(),
            })

        # Register strategy metadata for dashboard listing (preserving existing status)
        s_name = getattr(self.strategy, "name", self.config.strategy_id).replace("_", " ").title()
        existing_strat = self.db.strategies.find_one({
            "strategy_id": self.config.strategy_id,
            "mode": getattr(self.config, "mode", "paper")
        })
        curr_status = existing_strat.get("status", "ACTIVE") if existing_strat else "ACTIVE"

        strat_doc = StrategyModel(
            strategy_id=self.config.strategy_id,
            name=s_name,
            description=getattr(self.strategy, "__doc__", "Quantitative Strategy").strip(),
            mode=getattr(self.config, "mode", "paper"),
            rebalance_frequency=getattr(self.strategy, "rebalance_frequency", "MONTHLY"),
            portfolio_size=int(self.config.portfolio_size),
            initial_capital=float(self.config.initial_capital),
            current_nav=float(self.config.initial_capital),
            cash=float(self.config.initial_capital),
            holdings_val=0.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            total_return_pct=0.0,
            open_positions_count=0,
            closed_trades_count=0,
            win_rate_pct=0.0,
            status=curr_status,
            last_run_at=datetime.utcnow().isoformat(),
        )
        self.db.strategies.replace_one(
            {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")},
            strat_doc.to_dict(),
            upsert=True
        )

    def get_current_state(self, as_of_date: Optional[str] = None) -> Dict[str, Any]:
        """Retrieves active positions and current cash balance from MongoDB up to as_of_date."""
        query = {"strategy_id": self.config.strategy_id, "mode": "paper"}
        if as_of_date:
            query["date"] = {"$lte": as_of_date}

        nav_record = self.db.daily_nav.find(query).sort("date", -1).limit(1)
        nav_list = list(nav_record)
        cash = nav_list[0]["cash"] if nav_list else float(self.config.initial_capital)

        positions_cursor = self.db.positions.find(
            {"strategy_id": self.config.strategy_id, "mode": "paper"}
        )
        positions = {p["symbol"]: p for p in positions_cursor}
        return {"cash": cash, "positions": positions}

    def _ensure_indicators(self, universe: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        enriched = {}
        # Strategy-aware indicator check
        req_cols = getattr(self.strategy, "required_indicator_columns", None)
        for sym, df in universe.items():
            needs_compute = False
            if req_cols:
                needs_compute = any(c not in df.columns for c in req_cols)
            elif "_strategy_computed" not in df.columns:
                needs_compute = True
            
            if needs_compute:
                df_res = self.strategy.compute_indicators(df, self.config)
                df_res["_strategy_computed"] = True
                enriched[sym] = df_res
            else:
                enriched[sym] = df
        return enriched

    def generate_post_market_signals(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp) -> Dict[str, Any]:
        # Check strategy status
        strat_entry = self.db.strategies.find_one({
            "strategy_id": self.config.strategy_id,
            "mode": getattr(self.config, "mode", "paper")
        })
        status = strat_entry.get("status", "ACTIVE") if strat_entry else "ACTIVE"
        if status in ("PAUSED", "ARCHIVED"):
            return {
                "status": status,
                "exits": [],
                "buys": [],
                "message": f"Strategy '{self.config.strategy_id}' is {status}. Skipping signal generation."
            }

        universe = self._ensure_indicators(universe)
        """
        Executes daily post-market scan (e.g. 15:45 IST on Oracle server):
        1. Evaluates daily structural exits on active positions.
        2. Evaluates scheduled rebalance entries.
        3. Saves orders to MongoDB with status='PENDING_OPEN'.
        4. Broadcasts Telegram alert.
        """
        date_str = decision_date.strftime("%Y-%m-%d")
        state = self.get_current_state(as_of_date=date_str)
        cash = state["cash"]
        positions = state["positions"]

        # Mark open positions to today's EOD close from universe
        current_holdings_val = 0.0
        for sym, pos in list(positions.items()):
            df_sym = universe.get(sym)
            if df_sym is not None and not df_sym.empty:
                c_rows = df_sym.loc[df_sym["date"] <= decision_date]
                if not c_rows.empty:
                    last_pr = float(c_rows.iloc[-1]["close"])
                    pos["last_close"] = last_pr
                    pos["market_value"] = pos["shares"] * last_pr
                    entry_outlay = float(pos.get("entry_outlay", pos["shares"] * pos["entry_price"]))
                    pos["unrealized_pnl"] = pos["market_value"] - entry_outlay
                    self.db.positions.update_one(
                        {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper"), "symbol": sym},
                        {"$set": {
                            "last_close": last_pr,
                            "market_value": pos["market_value"],
                            "unrealized_pnl": pos["unrealized_pnl"]
                        }}
                    )
            current_holdings_val += pos.get("market_value", pos["shares"] * pos.get("last_close", pos["entry_price"]))

        current_nav = cash + current_holdings_val
        nav_model = DailyNavModel(
            strategy_id=self.config.strategy_id,
            mode=getattr(self.config, "mode", "paper"),
            date=date_str,
            cash=round(cash, 2),
            holdings_val=round(current_holdings_val, 2),
            nav=round(current_nav, 2),
            open_positions_count=len(positions),
        )
        self.db.daily_nav.replace_one(
            {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper"), "date": date_str},
            nav_model.to_dict(),
            upsert=True
        )

        # 1. Evaluate Daily Exits
        requested_exits = self.strategy.evaluate_daily_exits(universe, decision_date, positions, self.config)
        exits_to_log = []
        for sym, reason in requested_exits.items():
            pos = positions.get(sym)
            if not pos:
                continue
            ord_id = f"ORD_EXIT_{date_str}_{sym}"
            order = OrderModel(
                order_id=ord_id,
                strategy_id=self.config.strategy_id,
                mode="paper",
                symbol=sym,
                action="SELL",
                shares=pos["shares"],
                price=pos.get("last_close", 0.0),
                order_type="EXIT_AT_OPEN",
                status="PENDING_OPEN",
                reason=reason,
                signal_date=date_str,
            )
            self.db.orders.replace_one({"order_id": ord_id}, order.to_dict(), upsert=True)
            exits_to_log.append({"symbol": sym, "reason": reason})

        # 2. Evaluate Rebalance Entries
        cal = sorted(list(set(d for df in universe.values() for d in df["date"])))
        buys_to_log = []
        is_rebal = self.strategy.is_rebalance_decision_date(cal, decision_date, self.config)
        if is_rebal:
            candidates_df = self.strategy.select_entry_candidates(universe, decision_date, positions, self.config)
            target_symbols = list(candidates_df.head(self.config.portfolio_size).index)

            # Rebalance drops
            rebal_drops = self.strategy.evaluate_rebalance_exits(universe, decision_date, positions, target_symbols, self.config)
            for sym, reason in rebal_drops.items():
                if sym not in requested_exits and sym in positions:
                    pos = positions[sym]
                    ord_id = f"ORD_DROP_{date_str}_{sym}"
                    order = OrderModel(
                        order_id=ord_id,
                        strategy_id=self.config.strategy_id,
                        mode="paper",
                        symbol=sym,
                        action="SELL",
                        shares=pos["shares"],
                        price=pos.get("last_close", 0.0),
                        order_type="EXIT_AT_OPEN",
                        status="PENDING_OPEN",
                        reason=reason,
                        signal_date=date_str,
                    )
                    self.db.orders.replace_one({"order_id": ord_id}, order.to_dict(), upsert=True)
                    exits_to_log.append({"symbol": sym, "reason": reason})

            # New candidate buys
            open_slots = self.config.portfolio_size - len(positions) + len(exits_to_log)
            new_buys = [s for s in target_symbols if s not in positions][:max(0, open_slots)]
            holdings_est_val = sum(p["shares"] * p.get("last_close", 0.0) for p in positions.values())
            est_total_equity = cash + holdings_est_val
            target_alloc = est_total_equity / float(self.config.portfolio_size)

            for sym in new_buys:
                ord_id = f"ORD_BUY_{date_str}_{sym}"
                order = OrderModel(
                    order_id=ord_id,
                    strategy_id=self.config.strategy_id,
                    mode="paper",
                    symbol=sym,
                    action="BUY",
                    shares=0,  # Sized dynamically at open fill
                    price=float(candidates_df.loc[sym, "close"]),
                    order_type="ENTRY_AT_OPEN",
                    status="PENDING_OPEN",
                    reason=f"RANK_{candidates_df.loc[sym, 'rank'] if 'rank' in candidates_df.columns else 1}",
                    signal_date=date_str,
                )
                self.db.orders.replace_one({"order_id": ord_id}, order.to_dict(), upsert=True)
                buys_to_log.append({"symbol": sym, "alloc": target_alloc})

        # Update strategies collection with latest post-market scan metadata
        self.db.strategies.update_one(
            {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")},
            {"$set": {
                "current_nav": round(current_nav, 2),
                "last_signal_date": date_str,
                "last_run_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }}
        )

        # Broadcast Telegram
        self.notifier.send_signal_alert(self.config.strategy_id, date_str, buys_to_log, exits_to_log)
        return {"exits": exits_to_log, "buys": buys_to_log}

    def _get_execution_bar(self, sym: str, execution_date: pd.Timestamp, universe: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Retrieves opening price bar for execution date.
        If today's EOD candle is not in universe (normal at 09:16 AM), queries live market quote.
        Never invents prev_close or 100.0 if data is absent.
        """
        df = universe.get(sym)
        if df is None or df.empty:
            return None
        prev_rows = df.loc[df["date"] < execution_date]
        if prev_rows.empty:
            logger.warning("No previous close found for symbol %s before %s; cannot execute order", sym, execution_date)
            return None
        prev_close = float(prev_rows.iloc[-1]["close"])

        rows = df[df["date"] == execution_date]
        if not rows.empty:
            r = rows.iloc[0]
            return {
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "prev_close": prev_close,
                "is_live_quote": False,
            }

        # Live quote fallback at 09:16 IST (never fallback to prev_close as open_px)
        try:
            import yfinance as yf
            ticker = sym if sym.endswith(".NS") else f"{sym}.NS"
            fast_info = yf.Ticker(ticker).fast_info
            open_val = getattr(fast_info, "open", None) or getattr(fast_info, "last_price", None)
            if open_val is not None and float(open_val) > 0:
                open_px = float(open_val)
                high_px = float(getattr(fast_info, "day_high", None) or open_px)
                low_px = float(getattr(fast_info, "day_low", None) or open_px)
                last_px = float(getattr(fast_info, "last_price", None) or open_px)
                return {
                    "open": open_px,
                    "high": max(open_px, high_px, last_px),
                    "low": min(open_px, low_px, last_px),
                    "close": last_px,
                    "prev_close": prev_close,
                    "is_live_quote": True,
                }
            else:
                logger.error("Live quote for %s returned empty/zero open price at 09:16 IST", ticker)
        except Exception as exc:
            logger.error("Failed to fetch live quote for %s at 09:16 IST: %s", sym, exc)

        return None

    def execute_morning_fills(self, universe: Dict[str, pd.DataFrame], execution_date: pd.Timestamp) -> Dict[str, Any]:
        # Check strategy status
        strat_entry = self.db.strategies.find_one({
            "strategy_id": self.config.strategy_id,
            "mode": getattr(self.config, "mode", "paper")
        })
        status = strat_entry.get("status", "ACTIVE") if strat_entry else "ACTIVE"
        if status in ("PAUSED", "ARCHIVED"):
            cur_state = self.get_current_state()
            return {
                "status": status,
                "fills": [],
                "deferred": [],
                "cash": cur_state["cash"],
                "holdings_val": 0.0,
                "nav": cur_state["cash"],
                "message": f"Strategy '{self.config.strategy_id}' is {status}. Skipping execution."
            }
        """
        Executes pending orders at market open (e.g. 09:16 IST on Oracle server):
        1. Queries MongoDB orders where status == 'PENDING_OPEN' and mode == 'paper'.
        2. Sells are executed first to release cash.
        3. Buys are executed next, subject to available cash, integer sizing, and circuit/gap filters.
        4. Updates positions, trades, orders, executions, and daily_nav collections in MongoDB.
        5. Emits Telegram execution alert.
        """
        date_str = execution_date.strftime("%Y-%m-%d")
        state = self.get_current_state(as_of_date=date_str)
        cash = state["cash"]
        positions = state["positions"]

        pending_orders = list(self.db.orders.find({
            "strategy_id": self.config.strategy_id,
            "mode": "paper",
            "status": {"$in": ["PENDING_OPEN", "DEFERRED_LOWER_CIRCUIT"]},
        }))

        sell_orders = [o for o in pending_orders if o["action"] == "SELL"]
        buy_orders = [o for o in pending_orders if o["action"] == "BUY"]

        fills_log: List[Dict[str, Any]] = []
        deferred_log: List[Dict[str, Any]] = []

        # 1. Execute Sells First
        for order in sell_orders:
            sym = order["symbol"]
            df = universe.get(sym)
            if df is None:
                continue
            bar = self._get_execution_bar(sym, execution_date, universe)
            if bar is None:
                continue
            prev_close = bar["prev_close"]
            open_px = bar["open"]
            high_px = bar["high"]
            low_px = bar["low"]
            close_px = bar["close"]
            row = bar

            # Circuit breaker check
            if is_locked_down(open_px, high_px, low_px, close_px, prev_close):
                def_days = int(order.get("deferral_days", 0)) + 1
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "DEFERRED_LOWER_CIRCUIT", "deferral_days": def_days}}
                )
                if sym in positions:
                    self.db.positions.update_one(
                        {"strategy_id": self.config.strategy_id, "mode": "paper", "symbol": sym},
                        {"$set": {"deferral_days": def_days}}
                    )
                deferred_log.append({"symbol": sym, "days": def_days})
                continue

            open_price = float(row["open"])
            fill_price = open_price * (1.0 - self.config.slippage_pct)
            shares = int(order["shares"])
            gross_val = shares * fill_price
            fees_dict = calc_sell_fees_exact(gross_val, execution_date, self.config)
            net_proceeds = gross_val - fees_dict["total"]
            cash += net_proceeds

            # Record Execution
            exec_id = f"EXEC_{date_str}_{sym}_{uuid.uuid4().hex[:6]}"
            exec_model = ExecutionModel(
                exec_id=exec_id,
                order_id=order["order_id"],
                strategy_id=self.config.strategy_id,
                mode="paper",
                symbol=sym,
                action="SELL",
                shares=shares,
                raw_price=open_price,
                fill_price=fill_price,
                gross_val=gross_val,
                net_cash=net_proceeds,
                fees=fees_dict["total"],
                slippage=open_price - fill_price,
            )
            self.db.executions.insert_one(exec_model.to_dict())

            # Close Position & Record Trade
            pos = positions.get(sym)
            if pos:
                entry_date_dt = pd.to_datetime(pos["entry_date"])
                hold_days = (execution_date - entry_date_dt).days
                entry_price = float(pos["entry_price"])
                gross_pnl = (shares * fill_price) - (shares * entry_price)
                total_fees = float(pos.get("entry_fees", 0.0)) + fees_dict["total"]
                net_pnl = gross_pnl - total_fees
                outlay = float(pos.get("entry_outlay", shares * entry_price))
                ret_pct = (net_pnl / outlay) * 100.0 if outlay > 0 else 0.0

                trade_model = TradeModel(
                    strategy_id=self.config.strategy_id,
                    mode="paper",
                    symbol=sym,
                    entry_date=pos["entry_date"],
                    entry_price=entry_price,
                    exit_date=date_str,
                    exit_price=fill_price,
                    shares=shares,
                    hold_days=hold_days,
                    gross_pnl=round(gross_pnl, 2),
                    net_pnl=round(net_pnl, 2),
                    total_fees=round(total_fees, 2),
                    total_slippage=round((open_price - fill_price) * shares, 2),
                    return_pct=round(ret_pct, 4),
                    exit_reason=order["reason"],
                )
                self.db.trades.insert_one(trade_model.to_dict())
                self.db.positions.delete_one({"strategy_id": self.config.strategy_id, "mode": "paper", "symbol": sym})
                positions.pop(sym, None)

            # Update Order
            self.db.orders.update_one(
                {"order_id": order["order_id"]},
                {"$set": {"status": "FILLED", "execution_date": date_str}}
            )
            fills_log.append({"action": "SELL", "symbol": sym, "shares": shares, "fill_price": fill_price})

        # 2. Execute Buys
        # Re-estimate current equity for sizing
        holdings_val = sum(p["shares"] * float(p.get("last_close", p["entry_price"])) for p in positions.values())
        total_equity = cash + holdings_val
        target_alloc = total_equity / float(self.config.portfolio_size)

        for order in buy_orders:
            sym = order["symbol"]
            df = universe.get(sym)
            if df is None:
                continue
            bar = self._get_execution_bar(sym, execution_date, universe)
            if bar is None:
                continue
            prev_close = bar["prev_close"]
            open_px = bar["open"]
            high_px = bar["high"]
            low_px = bar["low"]
            close_px = bar["close"]
            row = bar

            # Circuit breaker check
            if is_locked_up(open_px, high_px, low_px, close_px, prev_close):
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_LOCKED_UP", "execution_date": date_str}}
                )
                continue

            # Gap limit cap check (using safely pre-computed prev_close)
            open_price = float(row["open"])
            limit_cap_price = prev_close * (1.0 + self.config.limit_cap_pct)
            if open_price > limit_cap_price:
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_GAP_LIMIT_EXCEEDED", "execution_date": date_str}}
                )
                continue

            fill_price = open_price * (1.0 + self.config.slippage_pct)
            # Sizing buffer: open_price and slippage are already known; include statutory fees buffer
            buffer_mult = 1.0 + self.config.fees_buffer_pct
            est_cost_per_sh = fill_price * buffer_mult
            avail = min(target_alloc, cash)
            shares = int(avail // est_cost_per_sh)

            if shares <= 0:
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_UNAFFORDABLE", "execution_date": date_str}}
                )
                continue

            gross_val = shares * fill_price
            buy_fees = calc_buy_fees_exact(gross_val, execution_date, self.config)
            total_outlay = gross_val + buy_fees["total"]

            while total_outlay > cash and shares > 0:
                shares -= 1
                gross_val = shares * fill_price
                buy_fees = calc_buy_fees_exact(gross_val, execution_date, self.config)
                total_outlay = gross_val + buy_fees["total"]

            if shares <= 0:
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_UNAFFORDABLE", "execution_date": date_str}}
                )
                continue

            cash -= total_outlay

            # Record Execution
            exec_id = f"EXEC_{date_str}_{sym}_{uuid.uuid4().hex[:6]}"
            exec_model = ExecutionModel(
                exec_id=exec_id,
                order_id=order["order_id"],
                strategy_id=self.config.strategy_id,
                mode="paper",
                symbol=sym,
                action="BUY",
                shares=shares,
                raw_price=open_price,
                fill_price=fill_price,
                gross_val=gross_val,
                net_cash=-total_outlay,
                fees=buy_fees["total"],
                slippage=fill_price - open_price,
            )
            self.db.executions.insert_one(exec_model.to_dict())

            # Add Position
            pos_model = PositionModel(
                strategy_id=self.config.strategy_id,
                mode="paper",
                symbol=sym,
                shares=shares,
                entry_price=fill_price,
                entry_fill_price=fill_price,
                entry_date=date_str,
                entry_outlay=total_outlay,
                entry_fees=buy_fees["total"],
                last_close=open_price,
                market_value=gross_val,
                unrealized_pnl=0.0,
            )
            self.db.positions.replace_one(
                {"strategy_id": self.config.strategy_id, "mode": "paper", "symbol": sym},
                pos_model.to_dict(),
                upsert=True
            )
            positions[sym] = pos_model.to_dict()

            # Update Order
            self.db.orders.update_one(
                {"order_id": order["order_id"]},
                {"$set": {"status": "FILLED", "shares": shares, "execution_date": date_str}}
            )
            fills_log.append({"action": "BUY", "symbol": sym, "shares": shares, "fill_price": fill_price})

        # 3. Update NAV Record
        current_holdings_val = 0.0
        for sym, pos in positions.items():
            df = universe.get(sym)
            if df is not None:
                rows = df[df["date"] == execution_date]
                last_pr = float(rows.iloc[0]["close"]) if not rows.empty else float(pos.get("last_close", pos["entry_price"]))
                pos["last_close"] = last_pr
                pos["market_value"] = pos["shares"] * last_pr
                entry_outlay = float(pos.get("entry_outlay", pos["shares"] * pos["entry_price"]))
                pos["unrealized_pnl"] = pos["market_value"] - entry_outlay
                self.db.positions.update_one(
                    {"strategy_id": self.config.strategy_id, "mode": "paper", "symbol": sym},
                    {"$set": {
                        "last_close": last_pr,
                        "market_value": pos["market_value"],
                        "unrealized_pnl": pos["unrealized_pnl"]
                    }}
                )
            current_holdings_val += pos.get("market_value", pos["shares"] * pos["entry_price"])

        current_nav = cash + current_holdings_val
        nav_model = DailyNavModel(
            strategy_id=self.config.strategy_id,
            mode="paper",
            date=date_str,
            cash=round(cash, 2),
            holdings_val=round(current_holdings_val, 2),
            nav=round(current_nav, 2),
            open_positions_count=len(positions),
        )
        self.db.daily_nav.replace_one(
            {"strategy_id": self.config.strategy_id, "mode": "paper", "date": date_str},
            nav_model.to_dict(),
            upsert=True
        )

        # Calculate closed trade metrics for dashboard returns
        closed_trades = list(self.db.trades.find({
            "strategy_id": self.config.strategy_id,
            "mode": getattr(self.config, "mode", "paper"),
        }))
        closed_cnt = len(closed_trades)
        realized_pnl = sum(float(t.get("net_pnl", 0.0)) for t in closed_trades)
        wins = sum(1 for t in closed_trades if float(t.get("net_pnl", 0.0)) > 0)
        win_rate = (wins / closed_cnt * 100.0) if closed_cnt > 0 else 0.0

        # Calculate unrealized PnL across active positions
        unrealized_pnl = sum(float(p.get("unrealized_pnl", p.get("market_value", 0.0) - p.get("entry_outlay", 0.0))) for p in positions.values())
        tot_ret_pct = ((current_nav - float(self.config.initial_capital)) / float(self.config.initial_capital)) * 100.0

        # Update strategies collection with live returns summary
        self.db.strategies.update_one(
            {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")},
            {"$set": {
                "current_nav": round(current_nav, 2),
                "cash": round(cash, 2),
                "holdings_val": round(current_holdings_val, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "realized_pnl": round(realized_pnl, 2),
                "total_return_pct": round(tot_ret_pct, 4),
                "open_positions_count": len(positions),
                "closed_trades_count": closed_cnt,
                "win_rate_pct": round(win_rate, 2),
                "last_execution_date": date_str,
                "last_run_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }}
        )

        # 4. Broadcast Telegram Execution Alert
        self.notifier.send_execution_alert(self.config.strategy_id, date_str, fills_log, deferred_log, current_nav, cash=cash)

        return {
            "fills": fills_log,
            "deferred": deferred_log,
            "cash": cash,
            "holdings_val": current_holdings_val,
            "nav": current_nav,
        }
