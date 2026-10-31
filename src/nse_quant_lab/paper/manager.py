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

        # Register strategy metadata for dashboard listing (preserving existing cumulative metrics & status)
        s_name = getattr(self.strategy, "name", self.config.strategy_id).replace("_", " ").title()
        existing_strat = self.db.strategies.find_one({
            "strategy_id": self.config.strategy_id,
            "mode": getattr(self.config, "mode", "paper")
        })

        if not existing_strat:
            strat_doc = StrategyModel(
                strategy_id=self.config.strategy_id,
                name=s_name,
                description=(getattr(self.strategy, "__doc__", None) or "Quantitative Strategy").strip(),
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
                status="ACTIVE",
                last_run_at=datetime.utcnow().isoformat(),
            )
            self.db.strategies.insert_one(strat_doc.to_dict())
        else:
            # Update only structural metadata without wiping cumulative realized P&L, trades, or nav
            self.db.strategies.update_one(
                {"_id": existing_strat["_id"]},
                {"$set": {
                    "name": s_name,
                    "description": (getattr(self.strategy, "__doc__", None) or "Quantitative Strategy").strip(),
                    "portfolio_size": int(self.config.portfolio_size),
                    "rebalance_frequency": getattr(self.strategy, "rebalance_frequency", "MONTHLY"),
                    "updated_at": datetime.utcnow().isoformat(),
                }}
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
                    last_row = c_rows.iloc[-1]
                    last_pr = float(last_row["close"])
                    pos["last_close"] = last_pr
                    pos["market_value"] = pos["shares"] * last_pr
                    entry_outlay = float(pos.get("entry_outlay", pos["shares"] * pos["entry_price"]))
                    pos["unrealized_pnl"] = pos["market_value"] - entry_outlay

                    # Extract dynamic trailing stop levels
                    ema_stop = float(last_row["ema100"]) if "ema100" in last_row and not np.isnan(last_row["ema100"]) else None
                    st_stop = float(last_row["supertrend_lower"]) if "supertrend_lower" in last_row and not np.isnan(last_row["supertrend_lower"]) else None

                    # Structural trailing stop: TrendMomentumStrategy exits strictly on Close < EMA(100)
                    effective_stop = ema_stop if ema_stop is not None else st_stop

                    buffer_pct = None
                    if effective_stop is not None and last_pr > 0:
                        buffer_pct = round(((last_pr - effective_stop) / last_pr) * 100.0, 2)

                    pos["ema100_stop"] = round(ema_stop, 2) if ema_stop else None
                    pos["supertrend_stop"] = round(st_stop, 2) if st_stop else None
                    pos["stop_loss_price"] = round(effective_stop, 2) if effective_stop else None
                    pos["stop_buffer_pct"] = buffer_pct

                    update_dict = {
                        "last_close": last_pr,
                        "market_value": pos["market_value"],
                        "unrealized_pnl": pos["unrealized_pnl"],
                    }
                    if effective_stop is not None:
                        update_dict["stop_loss_price"] = round(effective_stop, 2)
                        update_dict["supertrend_stop"] = round(st_stop, 2) if st_stop else None
                        update_dict["ema100_stop"] = round(ema_stop, 2) if ema_stop else None
                        update_dict["stop_buffer_pct"] = buffer_pct

                    self.db.positions.update_one(
                        {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper"), "symbol": sym},
                        {"$set": update_dict}
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

            # Prevent duplicate exit orders if already pending or deferred in lower circuit
            existing_exit = self.db.orders.find_one({
                "strategy_id": self.config.strategy_id,
                "mode": getattr(self.config, "mode", "paper"),
                "symbol": sym,
                "action": "SELL",
                "status": {"$in": ["PENDING_OPEN", "DEFERRED_LOWER_CIRCUIT"]}
            })
            if existing_exit:
                logger.info(f"Exit order for {sym} already pending ({existing_exit['order_id']}, status={existing_exit['status']}). Skipping duplicate.")
                exits_to_log.append({
                    "symbol": sym,
                    "shares": pos["shares"],
                    "close": pos.get("last_close", 0.0),
                    "reason": reason,
                })
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
            exits_to_log.append({
                "symbol": sym,
                "shares": pos["shares"],
                "close": pos.get("last_close", 0.0),
                "reason": reason,
            })

        # 2. Evaluate Rebalance Entries
        cal = sorted(list(set(d for df in universe.values() for d in df["date"])))
        buys_to_log = []
        rebal_drops: Dict[str, str] = {}
        is_rebal = self.strategy.is_rebalance_decision_date(cal, decision_date, self.config)
        if is_rebal:
            candidates_df = self.strategy.select_entry_candidates(universe, decision_date, positions, self.config)
            target_symbols = list(candidates_df.head(self.config.portfolio_size).index)

            # Rank Hysteresis / Hold Buffer:
            hold_buf = getattr(self.config, "rebalance_hold_rank_buffer", self.config.portfolio_size)
            allowed_hold_symbols = list(candidates_df.head(hold_buf).index)

            # Rebalance drops: retain positions if rank <= hold_buffer and trend intact
            rebal_drops = self.strategy.evaluate_rebalance_exits(universe, decision_date, positions, allowed_hold_symbols, self.config)
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
                    exits_to_log.append({
                        "symbol": sym,
                        "shares": pos["shares"],
                        "close": pos.get("last_close", 0.0),
                        "reason": reason,
                    })

            # New candidate buys
            open_slots = self.config.portfolio_size - len(positions) + len(exits_to_log)
            # Wash trade prevention: do not rebuy dropped stocks on the same rebalance session
            candidate_pool = [s for s in list(candidates_df.index) if s not in positions and s not in rebal_drops]
            # Queue primary open slots + up to 4 backup candidates so morning execution fills empty slots if top picks gap > 2%
            backup_slots = int(getattr(self.strategy, "paper_backup_slots", 4)) if getattr(self.config, "unaffordable_policy", "SKIP") == "TRY_NEXT" else 0
            new_buys = candidate_pool[:max(0, open_slots + backup_slots)]
            holdings_est_val = sum(p["shares"] * p.get("last_close", 0.0) for p in positions.values())
            est_total_equity = cash + holdings_est_val
            target_alloc = est_total_equity / float(self.config.portfolio_size)

            for sym in new_buys:
                ord_id = f"ORD_BUY_{date_str}_{sym}"
                c_px = float(candidates_df.loc[sym, "close"])
                lim_px = c_px * (1.0 + self.config.limit_cap_pct)
                denom = c_px * (1.0 + self.config.slippage_pct + self.config.fees_buffer_pct)
                est_sh = int(target_alloc // denom) if denom > 0 else 0
                rk = f"RANK_{candidates_df.loc[sym, 'rank'] if 'rank' in candidates_df.columns else 1}"

                order = OrderModel(
                    order_id=ord_id,
                    strategy_id=self.config.strategy_id,
                    mode="paper",
                    symbol=sym,
                    action="BUY",
                    shares=0,  # Sized dynamically at open fill
                    price=c_px,
                    order_type="ENTRY_AT_OPEN",
                    status="PENDING_OPEN",
                    reason=rk,
                    signal_date=date_str,
                )
                self.db.orders.replace_one({"order_id": ord_id}, order.to_dict(), upsert=True)
                buys_to_log.append({
                    "symbol": sym,
                    "alloc": target_alloc,
                    "close": c_px,
                    "limit_price": lim_px,
                    "est_shares": est_sh,
                    "reason": rk,
                })

        # Update strategies collection with full post-market mark-to-market metadata
        unrealized_pnl = sum(float(p.get("unrealized_pnl", p.get("market_value", 0.0) - p.get("entry_outlay", 0.0))) for p in positions.values())
        tot_ret_pct = ((current_nav - float(self.config.initial_capital)) / float(self.config.initial_capital)) * 100.0

        self.db.strategies.update_one(
            {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")},
            {"$set": {
                "current_nav": round(current_nav, 2),
                "cash": round(cash, 2),
                "holdings_val": round(current_holdings_val, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_return_pct": round(tot_ret_pct, 4),
                "open_positions_count": len(positions),
                "last_signal_date": date_str,
                "last_run_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }}
        )

        # Collect active continuing holdings (those not exited today)
        active_holdings_to_log = []
        for sym, pos in positions.items():
            if sym not in requested_exits and sym not in rebal_drops:
                active_holdings_to_log.append({
                    "symbol": sym,
                    "shares": pos["shares"],
                    "close": pos.get("last_close", 0.0),
                    "stop_loss_price": pos.get("stop_loss_price"),
                    "supertrend_stop": pos.get("supertrend_stop"),
                    "ema100_stop": pos.get("ema100_stop"),
                    "stop_buffer_pct": pos.get("stop_buffer_pct"),
                })

        # Broadcast Telegram
        self.notifier.send_signal_alert(
            self.config.strategy_id,
            date_str,
            buys_to_log,
            exits_to_log,
            active_holdings=active_holdings_to_log,
        )
        return {"exits": exits_to_log, "buys": buys_to_log, "active_holdings": active_holdings_to_log}

    @staticmethod
    def _cost_inputs(prev_rows: pd.DataFrame) -> Dict[str, Any]:
        """Point-in-time inputs for the cost model, taken from bars strictly BEFORE the execution session."""
        last = prev_rows.iloc[-1]
        if "adt50m" in prev_rows.columns and not pd.isna(last["adt50m"]):
            adt = float(last["adt50m"])
        else:  # morning universe is not indicator-enriched: rebuild the 50-session median turnover from raw bars before the fill
            tail = prev_rows.tail(50)
            turnover = tail["turnover"] if "turnover" in tail.columns else tail["close"] * tail["volume"]
            adt = float(turnover.median()) if len(tail) >= 20 else float("nan")
        sig = float(prev_rows["close"].pct_change().tail(20).std()) if len(prev_rows) > 5 else float("nan")
        return {"adt_prev": adt, "sig_prev": sig, "series": str(last["series"]) if "series" in prev_rows.columns else "EQ"}

    def _slip(self, sym: str, bar: Dict[str, Any], price: float, shares: int, side: str) -> float:
        """Per-leg slippage fraction: the strategy's liquidity-tiered model when it defines one, else flat config."""
        fn = getattr(self.strategy, "slippage_pct_for", None)
        if fn is None:
            return self.config.slippage_pct
        return float(fn(sym, bar, float(price), int(shares), side))

    def _audit_fill(self, order: Dict[str, Any], sym: str, side: str, shares: int, bar: Dict[str, Any],
                    open_price: float, fill_price: float, date_str: str) -> None:
        """Log model fill next to an optional live bid/ask quote so real execution cost can be measured and the
        cost tiers recalibrated. quote_provider(sym) -> {'bid','ask','ltp'} or None (set it at ~09:16 IST)."""
        quote = None
        provider = getattr(self, "quote_provider", None)
        if provider is not None:
            try:
                quote = provider(sym)
            except Exception as exc:  # audit only: never block paper execution, but say so loudly
                logger.error("quote_provider failed for %s: %s", sym, exc)
        doc = {
            "strategy_id": self.config.strategy_id, "mode": "paper", "date": date_str, "symbol": sym, "side": side,
            "shares": shares, "order_id": order["order_id"], "open_price": open_price, "fill_price": fill_price,
            "model_slip_bps": round((fill_price / open_price - 1.0) * 1e4 * (1 if side == "BUY" else -1), 2),
            "adt_prev": bar.get("adt_prev"), "sig_prev": bar.get("sig_prev"), "series": bar.get("series"),
            "quote": quote, "ts": datetime.utcnow().isoformat(),
        }
        if quote and quote.get("ask") and quote.get("bid"):
            ref = quote["ask"] if side == "BUY" else quote["bid"]
            doc["quote_vs_open_bps"] = round((ref / open_price - 1.0) * 1e4 * (1 if side == "BUY" else -1), 2)
            doc["half_spread_bps"] = round((quote["ask"] - quote["bid"]) / (quote["ask"] + quote["bid"]) * 1e4, 2)
        try:
            self.db.get_collection("fill_audit").insert_one(doc)
        except Exception as exc:
            logger.error("fill_audit insert failed: %s", exc)

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

        # Compute or extract 50-day average daily turnover from recent history
        to50 = None
        if "turnover_50d" in prev_rows.iloc[-1] and not pd.isna(prev_rows.iloc[-1]["turnover_50d"]):
            to50 = float(prev_rows.iloc[-1]["turnover_50d"])
        elif "close" in prev_rows and "volume" in prev_rows:
            tail50 = prev_rows.tail(50)
            if not tail50.empty:
                to50 = float((tail50["close"] * tail50["volume"]).mean())

        rows = df[df["date"] == execution_date]
        if not rows.empty:
            r = rows.iloc[0]
            # If today's row already has turnover_50d, prefer it
            if "turnover_50d" in r and not pd.isna(r["turnover_50d"]):
                to50 = float(r["turnover_50d"])
            return {
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "prev_close": prev_close,
                "turnover_50d": to50,
                "is_live_quote": False,
                **self._cost_inputs(prev_rows),
            }

        # Preferred morning source: the injected broker quote (e.g. UpstoxQuoteProvider): the real opening price at 09:16 IST
        provider = getattr(self, "quote_provider", None)
        if provider is not None:
            try:
                q = provider(sym)
            except Exception as exc:
                logger.error("quote_provider failed for %s: %s", sym, exc)
                q = None
            if q and float(q.get("open") or 0.0) > 0:
                o = float(q["open"])
                ltp = float(q.get("ltp") or o)
                return {
                    "open": o,
                    "high": max(o, float(q.get("high") or o), ltp),
                    "low": min(o, float(q.get("low") or o), ltp),
                    "close": ltp,
                    "prev_close": prev_close,
                    "turnover_50d": to50,
                    "is_live_quote": True,
                    **self._cost_inputs(prev_rows),
                }
            logger.error("no usable broker quote for %s at execution time; falling back to the yfinance quote", sym)

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
                    "turnover_50d": to50,
                    "is_live_quote": True,
                    **self._cost_inputs(prev_rows),
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
            # Guard against double-sell / phantom cash if position already sold
            if sym not in positions:
                logger.warning(f"Position {sym} already closed or not found. Cancelling orphan/duplicate sell order {order['order_id']}.")
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "CANCELLED", "cancel_reason": "POSITION_ALREADY_CLOSED", "execution_date": date_str}}
                )
                continue

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
            fill_price = open_price * (1.0 - self._slip(sym, bar, open_price, int(order["shares"]), "SELL"))
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
                slippage=round((open_price - fill_price) * shares, 2),
            )
            self.db.executions.insert_one(exec_model.to_dict())
            self._audit_fill(order, sym, "SELL", shares, bar, open_price, fill_price, date_str)

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
                
                pos_entry_slip = float(pos.get("entry_slip_loss", (float(pos.get("entry_fill_price", entry_price)) - entry_price) * shares))
                exit_slip = (open_price - fill_price) * shares

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
                    total_slippage=round(pos_entry_slip + exit_slip, 2),
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
            
            # Enforce max portfolio size
            if len(positions) >= self.config.portfolio_size:
                logger.info(f"Portfolio size limit reached ({len(positions)}/{self.config.portfolio_size}). Skipping buy for {sym}.")
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_PORTFOLIO_FULL", "execution_date": date_str}}
                )
                continue

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
                deferred_log.append({"symbol": sym, "reason": f"Skipped: Upper Circuit Lock (Open ₹{open_px:.2f})"})
                continue

            # Gap limit cap check (using safely pre-computed prev_close)
            open_price = float(row["open"])
            limit_cap_price = prev_close * (1.0 + self.config.limit_cap_pct)
            if open_price > limit_cap_price:
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_GAP_LIMIT_EXCEEDED", "execution_date": date_str}}
                )
                deferred_log.append({"symbol": sym, "reason": f"Skipped: Open ₹{open_price:.2f} > +2% Cap ₹{limit_cap_price:.2f}"})
                continue

            # Volume participation cap: max 10% of 50-day average daily turnover
            max_vol_pct = getattr(self.config, "max_volume_participation_pct", 0.10)
            to50 = float(bar["turnover_50d"]) if (hasattr(bar, "__getitem__") and "turnover_50d" in bar and not pd.isna(bar["turnover_50d"])) else float("inf")
            max_exec_val = to50 * max_vol_pct if to50 > 0 else float("inf")
            avail = min(target_alloc, cash, max_exec_val)

            # Order size is set from the pre-slippage open (as the backtest engine does), then the cost model prices it
            buffer_mult = 1.0 + self.config.fees_buffer_pct
            sized_shares = int(avail // (open_price * buffer_mult))
            fill_price = open_price * (1.0 + self._slip(sym, bar, open_price, max(sized_shares, 1), "BUY"))
            est_cost_per_sh = fill_price * buffer_mult
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

            if shares <= 0 or gross_val < 0.25 * target_alloc:
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_UNAFFORDABLE", "execution_date": date_str}}
                )
                continue

            cash -= total_outlay
            buy_slip_total = round((fill_price - open_price) * shares, 2)

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
                slippage=buy_slip_total,
            )
            self.db.executions.insert_one(exec_model.to_dict())
            self._audit_fill(order, sym, "BUY", shares, bar, open_price, fill_price, date_str)

            # Add Position
            pos_model = PositionModel(
                strategy_id=self.config.strategy_id,
                mode="paper",
                symbol=sym,
                shares=shares,
                entry_price=open_price,
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

        # Expire any leftover PENDING_OPEN buy orders from this batch (e.g. unreached backup slots)
        if hasattr(self.db.orders, "update_many"):
            self.db.orders.update_many(
                {
                    "strategy_id": self.config.strategy_id,
                    "mode": "paper",
                    "action": "BUY",
                    "status": "PENDING_OPEN",
                },
                {
                    "$set": {
                        "status": "SKIPPED_EXPIRED",
                        "execution_date": date_str,
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                }
            )
        else:
            for pending_o in list(self.db.orders.find({
                "strategy_id": self.config.strategy_id,
                "mode": "paper",
                "action": "BUY",
                "status": "PENDING_OPEN",
            })):
                self.db.orders.update_one(
                    {"order_id": pending_o["order_id"]},
                    {"$set": {"status": "SKIPPED_EXPIRED", "execution_date": date_str}}
                )

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
