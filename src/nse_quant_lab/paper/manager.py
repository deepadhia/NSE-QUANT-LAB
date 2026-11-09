"""
manager.py -- Forward Paper Trading Engine & Database Lifecycle Manager
"""

from __future__ import annotations
import logging
import math
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import numpy as np
import pandas as pd

from ..base_strategy import BaseStrategy
from ..config import PaperTradingConfig
from ..db.mongo_client import MongoDatabase
from ..db.models import OrderModel, ExecutionModel, PositionModel, TradeModel, DailyNavModel, StrategyModel
from ..execution import is_locked_down, is_locked_up
from ..fees import calc_buy_fees_exact, calc_sell_fees_exact
from ..notifications.telegram import TelegramNotifier
from ..entry_filters import apply_entry_price_cap
from ..corporate_events import COLLECTION as EVENTS_COLLECTION, CASH_OUT, validate_event, split_whole_and_fraction
from .locking import serialized_portfolio
from .lease import PortfolioLease, supports_leases, COLLECTION as LEASE_COLLECTION

try:
    from pymongo.errors import DuplicateKeyError
except ImportError:  # pragma: no cover
    class DuplicateKeyError(Exception):
        pass

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
        self._mode = getattr(config, "mode", "paper")
        self._execution_cash = {}
        self._lease = None
        self._lease_depth = 0
        self._lease_warned = False

    def _open_lease(self):
        """Cross-host lease for this strategy+mode. Real MongoDB always arbitrates; a bare in-memory test double cannot, so it is
        skipped LOUDLY (once per manager) and only the same-host file lock applies."""
        try:
            coll = self.db.get_collection(LEASE_COLLECTION)
        except Exception:
            coll = None
        if not supports_leases(coll):
            if not self._lease_warned:
                logger.warning("[LEASE_UNAVAILABLE] database object cannot arbitrate a cross-host lease; same-host lock only")
                self._lease_warned = True
            return None
        return PortfolioLease(coll, f"{self.config.strategy_id}|{self._mode}")

    def _assert_lease(self) -> None:
        """Fencing: refuse any state-changing write once this worker's lease was lost to another host (also extends the lease)."""
        if self._lease is not None:
            self._lease.assert_held()

    def _order_id(self, kind: str, date_str: str, sym: str) -> str:
        """Order IDs are namespaced by strategy: all strategies share one `orders` collection, so an ID without
        strategy_id lets one strategy's staged order overwrite another's."""
        return f"ORD_{kind}_{self.config.strategy_id}_{date_str}_{sym}"

    def _stage_order(self, order: OrderModel) -> None:
        """Idempotent upsert scoped to this strategy. If the order_id unique index sees the same ID under another
        strategy this raises DuplicateKeyError (fail loud) instead of silently re-owning the other strategy's order."""
        self.db.orders.replace_one(
            {"order_id": order.order_id, "strategy_id": order.strategy_id, "mode": order.mode},
            order.to_dict(),
            upsert=True,
        )

    def _alert(self, text: str) -> None:
        """Best-effort operator alert; never raises (the log line is the system of record)."""
        try:
            self.notifier.send_message(f"[{self.config.strategy_id}] {text}")
        except Exception as exc:
            logger.error("alert send failed: %s", exc)

    def ensure_indexes(self) -> None:
        """Create performance + idempotency indexes (safe to call every run; called from both pipelines)."""
        try:
            self.db.daily_nav.create_index([("strategy_id", 1), ("mode", 1), ("date", -1)], unique=True)
            self.db.positions.create_index([("strategy_id", 1), ("mode", 1), ("symbol", 1)], unique=True)
            self.db.orders.create_index([("order_id", 1)], unique=True)
            self.db.orders.create_index([("strategy_id", 1), ("mode", 1), ("status", 1)])
            self.db.trades.create_index([("strategy_id", 1), ("mode", 1), ("exit_date", -1)])
            # Idempotency keys: one execution / one trade per order, enforced by the database (partial: legacy docs lack exec_id)
            _has_exec_id = {"partialFilterExpression": {"exec_id": {"$type": "string"}}}
            self.db.executions.create_index([("strategy_id", 1), ("mode", 1), ("exec_id", 1)], unique=True, **_has_exec_id)
            self.db.trades.create_index([("strategy_id", 1), ("mode", 1), ("exec_id", 1)], unique=True, **_has_exec_id)
            self.db.get_collection("corporate_actions").create_index(
                [("symbol", 1), ("ex_date", 1), ("action_type", 1)], unique=True
            )
        except Exception as e:
            # Missing unique indexes silently remove duplicate protection: say so loudly instead of a quiet warning.
            logger.critical(f"[INDEX_CREATION_FAILED] Unique/idempotency indexes missing: {e}")
            self._alert(f"INDEX CREATION FAILED for {self.config.strategy_id}: {e}")

    def initialize_portfolio(self):
        """Initializes empty paper trading portfolio in MongoDB if not present."""
        self.ensure_indexes()

        nav_col = self.db.daily_nav
        existing = nav_col.find_one({"strategy_id": self.config.strategy_id, "mode": self._mode})
        if not existing:
            nav_col.insert_one({
                "strategy_id": self.config.strategy_id,
                "mode": self._mode,
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "cash": float(self.config.initial_capital),
                "holdings_val": 0.0,
                "nav": float(self.config.initial_capital),
                "open_positions_count": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        # Register strategy metadata for dashboard listing (preserving existing cumulative metrics & status)
        s_name = getattr(self.strategy, "name", self.config.strategy_id)
        if s_name != self.config.strategy_id:
            s_name = self.config.strategy_id    # alias/forward-test account: show the account id so the strategies table tells accounts apart
        s_name = s_name.replace("_", " ").title()
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
                max_entry_price=getattr(self.config, "max_entry_price", None),
                status="ACTIVE",
                last_run_at=datetime.now(timezone.utc).isoformat(),
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
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }}
            )

    def get_current_state(self, as_of_date: Optional[str] = None) -> Dict[str, Any]:
        """Retrieves active positions and current cash balance from MongoDB up to as_of_date."""
        query = {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")}

        # Find absolute latest NAV date across collection to distinguish live runs from historical replays
        abs_latest_rec = list(self.db.daily_nav.find({"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")}).sort("date", -1).limit(1))
        abs_latest_date = abs_latest_rec[0].get("date") if abs_latest_rec else None

        if as_of_date:
            query["date"] = {"$lte": as_of_date}

        nav_record = self.db.daily_nav.find(query).sort("date", -1).limit(1)
        nav_list = list(nav_record)
        cash = nav_list[0]["cash"] if nav_list else float(self.config.initial_capital)

        # Synchronize with strategies collection cash for real-time durability only when at current live state
        strat_entry = self.db.strategies.find_one({"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")})
        if strat_entry and "cash" in strat_entry:
            if not as_of_date or (abs_latest_date and as_of_date >= abs_latest_date):
                cash = float(strat_entry["cash"])

        positions_cursor = self.db.positions.find(
            {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")}
        )
        positions = {p["symbol"]: p for p in positions_cursor}
        return {"cash": cash, "positions": positions}

    def _apply_corporate_actions(self, positions: Dict[str, Any], as_of_date: str) -> None:
        """Apply recorded splits/bonuses to open positions, exactly once each.
        `corporate_actions` docs: {symbol, ex_date 'YYYY-MM-DD', ratio} where ratio = new shares per 1 old share
        (1:2 split -> 2.0, 1:2 bonus -> 1.5). Price data is split-adjusted by the loader, so share counts must follow
        or the position is valued at a fraction of its true worth. Dividends are NOT modelled (price-only series)."""
        try:
            actions = list(self.db.get_collection("corporate_actions").find({"symbol": {"$in": list(positions.keys())}}))
        except Exception as exc:
            logger.error("corporate_actions lookup failed: %s", exc)
            raise RuntimeError("CORPORATE_ACTIONS_UNAVAILABLE: retry before changing portfolio") from exc
        for act in actions:
            if not isinstance(act, dict):
                continue
            sym, ex_date, ratio = act.get("symbol"), str(act.get("ex_date", ""))[:10], float(act.get("ratio") or 0.0)
            pos = positions.get(sym)
            if pos is None or ratio <= 0 or not ex_date or ex_date > as_of_date:
                continue
            key = f"{sym}:{ex_date}:{ratio}"
            applied = pos.get("applied_actions") or []
            # `SYMBOL:<ex_date>:ingest` is stamped by scripts/ingest_upstox_fulltier.py when it rescaled the position from an
            # unrecorded price discontinuity; a document recorded later for the same ex-date must not multiply the shares again.
            if (ex_date <= str(pos.get("entry_date", ""))[:10] or key in applied
                    or any(str(k).startswith(f"{sym}:{ex_date}:") for k in applied)):
                continue
            new_shares = int(math.floor(pos["shares"] * ratio + 1e-9))
            logger.warning("[CORPORATE_ACTION] %s ex %s ratio %.4f: shares %d -> %d", sym, ex_date, ratio, pos["shares"], new_shares)
            pos["shares"] = new_shares
            for k in ("entry_price", "entry_fill_price"):
                if pos.get(k):
                    pos[k] = float(pos[k]) / ratio
            # The stored mark is on the OLD price basis: rebase it with the share count (market value is unchanged by a
            # split). Otherwise shares x stale mark overstates equity (oversizing buys) and trips the discontinuity alert.
            if pos.get("last_close"):
                pos["last_close"] = float(pos["last_close"]) / ratio
            pos["applied_actions"] = list(pos.get("applied_actions") or []) + [key]
            self.db.positions.update_one(
                {"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym},
                {"$set": {"shares": pos["shares"], "entry_price": pos.get("entry_price"),
                          "entry_fill_price": pos.get("entry_fill_price"), "last_close": pos.get("last_close"),
                          "applied_actions": pos["applied_actions"]}},
            )
            self._alert(f"Corporate action applied {sym} ex {ex_date} x{ratio:g}: shares -> {new_shares}")

    def _apply_corporate_events(self, positions: Dict[str, Any], cash: float, date_str: str,
                                universe: Optional[Dict[str, pd.DataFrame]] = None) -> float:
        """Apply human-recorded, evidenced `corporate_events` (CASH_OUT / CONVERSION) to held names, once per account.
        This is the ONLY way a merger/delisting/suspension becomes a cash credit or a symbol change: missing data alone
        never is (see MISSING_HELD_BAR). Every write is resumable: execution -> cash (once, keyed by exec_id) -> trade ->
        position replace/delete -> pending-order cancel, so a crash at any step is completed by the next run. Returns cash."""
        if not positions:
            return cash
        try:
            events = [e for e in self.db.get_collection(EVENTS_COLLECTION).find({"symbol": {"$in": list(positions.keys())}})
                      if isinstance(e, dict)]
        except Exception as exc:
            logger.error("corporate_events lookup failed: %s", exc)
            raise RuntimeError("CORPORATE_EVENTS_UNAVAILABLE: retry before changing portfolio") from exc
        for ev in sorted(events, key=lambda e: (str(e.get("effective_date", "")), str(e.get("event_id", "")))):
            try:
                validate_event(ev)
            except ValueError as exc:
                logger.critical("[CORPORATE_EVENT_INVALID] %s: %s. Not applied.", ev.get("event_id"), exc)
                self._alert(f"INVALID corporate event {ev.get('event_id')}: {exc}. Not applied.")
                continue
            sym, eff = ev["symbol"], ev["effective_date"]
            pos = positions.get(sym)
            if pos is None or eff > date_str:
                continue
            df_sym = (universe or {}).get(sym)
            if df_sym is not None and not df_sym.empty and bool((df_sym["date"] >= pd.Timestamp(eff)).any()):
                # The stock still trades on/after the event's effective date: the event contradicts the market data (typo, wrong
                # date, or a premature record). Never liquidate or convert a live, priced position on a mistaken event.
                logger.critical("[CORPORATE_EVENT_CONTRADICTED] %s: %s still has market bars on/after effective date %s. Not applied.", ev["event_id"], sym, eff)
                self._alert(f"Corporate event {ev['event_id']} NOT applied: {sym} still trades on/after {eff}. Check the event.")
                continue
            key = f"EVENT:{ev['event_id']}"
            if ev["type"] == CASH_OUT:
                if eff <= str(pos.get("entry_date", ""))[:10]:
                    continue        # position was opened on/after the event: not affected
                cash = self._apply_cash_out(ev, key, pos, positions, cash, date_str)
            else:
                cash = self._apply_conversion(ev, key, pos, positions, cash, date_str)
        return cash

    def _event_exec_id(self, ev: Dict[str, Any]) -> str:
        return f"EXEC_EVENT_{ev['event_id']}_{self.config.strategy_id}"

    def _record_event_execution(self, ev: Dict[str, Any], action: str, shares: int, unit_cash: float, net_cash: float,
                                date_str: str) -> str:
        """Write the event's cash flow to the executions ledger once (the ledger is what `_reconcile_cash` sums)."""
        exec_id = self._event_exec_id(ev)
        if self._prior_event_execution(exec_id) is None:
            self._record_execution(ExecutionModel(
                exec_id=exec_id, order_id=f"EVENT_{ev['event_id']}", strategy_id=self.config.strategy_id, mode=self._mode,
                symbol=ev["symbol"], action=action, shares=int(shares), raw_price=float(unit_cash), fill_price=float(unit_cash),
                gross_val=round(net_cash, 2), net_cash=round(net_cash, 2), fees=0.0, slippage=0.0, session_date=date_str))
        return exec_id

    def _prior_event_execution(self, exec_id: str) -> Optional[Dict[str, Any]]:
        prior = self.db.executions.find_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "exec_id": exec_id})
        return prior if isinstance(prior, dict) else None

    def _credit_event_cash(self, exec_id: str, cash: float, date_str: str) -> float:
        if self._cash_already_applied(exec_id):
            return cash
        prior = self._prior_event_execution(exec_id)
        cash += float(prior["net_cash"]) if prior else self._execution_cash[exec_id]
        self._persist_cash(cash, date_str, exec_id)
        return cash

    def _cancel_pending_orders(self, sym: str, reason: str, date_str: str) -> None:
        for o in list(self.db.orders.find({"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym,
                                           "status": {"$in": ["PENDING_OPEN", "DEFERRED_LOWER_CIRCUIT"]}})):
            self.db.orders.update_one({"order_id": o["order_id"], "strategy_id": self.config.strategy_id, "mode": self._mode},
                                      {"$set": {"status": "CANCELLED", "cancel_reason": reason, "execution_date": date_str}})

    def _apply_cash_out(self, ev: Dict[str, Any], key: str, pos: Dict[str, Any], positions: Dict[str, Any],
                        cash: float, date_str: str) -> float:
        sym, eff = ev["symbol"], ev["effective_date"]
        exec_id = self._event_exec_id(ev)
        prior = self._prior_event_execution(exec_id)
        shares = int(prior["shares"]) if prior else int(pos["shares"])      # resume uses the shares already credited
        unit = float(prior["fill_price"]) if prior else float(ev["cash_per_share"])
        net = round(shares * unit, 2)
        exec_id = self._record_event_execution(ev, "SELL", shares, unit, net, date_str)
        cash = self._credit_event_cash(exec_id, cash, date_str)
        have_trade = isinstance(self.db.trades.find_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "exec_id": exec_id}), dict)
        if not have_trade:
            self._record_trade(pos, {"symbol": sym, "reason": f"EVENT_CASH_OUT:{ev['event_id']}"}, shares, unit, unit, net, 0.0,
                               pd.Timestamp(eff), eff, exec_id)
        self._cancel_pending_orders(sym, f"EVENT_CASH_OUT:{ev['event_id']}", date_str)   # before the delete: stays resumable
        self.db.positions.delete_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym})
        positions.pop(sym, None)
        logger.critical("[CORPORATE_EVENT] %s: %s %d sh closed at Rs %.4f/sh (net Rs %.2f). Evidence: %s", self.config.strategy_id, sym, shares, unit, net, ev["evidence"])
        self._alert(f"Corporate event applied: {sym} CASH_OUT {shares} sh x Rs {unit:g} = Rs {net:,.2f} (effective {eff}).")
        return cash

    def _apply_conversion(self, ev: Dict[str, Any], key: str, pos: Dict[str, Any], positions: Dict[str, Any],
                          cash: float, date_str: str) -> float:
        sym, new_sym, eff = ev["symbol"], ev["new_symbol"].strip(), ev["effective_date"]
        ratio = float(ev["ratio"])
        existing_new = positions.get(new_sym)
        resuming = existing_new is not None and key in (existing_new.get("applied_actions") or [])
        if existing_new is not None and not resuming:
            logger.critical("[CORPORATE_EVENT_CONFLICT] %s: account already holds %s; refusing to merge into it. Resolve manually.", sym, new_sym)
            self._alert(f"Corporate event {ev['event_id']} NOT applied: account already holds {new_sym}. Resolve manually.")
            return cash
        if eff <= str(pos.get("entry_date", ""))[:10]:
            return cash             # position was opened on/after the event: not affected
        prior = self._prior_event_execution(self._event_exec_id(ev))
        old_shares = int(pos["shares"])
        whole, frac = split_whole_and_fraction(old_shares, ratio)
        if frac > 0 and ev.get("fraction_cash_per_new_share") is None:
            logger.critical("[CORPORATE_EVENT_FRACTION_UNRESOLVED] %s: %d sh x %g = %.6f new shares; no fraction_cash_per_new_share "
                            "recorded (0.0 must be stated explicitly). Not applied.", sym, old_shares, ratio, whole + frac)
            self._alert(f"Corporate event {ev['event_id']} NOT applied: fractional entitlement needs fraction_cash_per_new_share (0.0 if none).")
            return cash
        if whole < 1:
            logger.critical("[CORPORATE_EVENT_INVALID] %s: conversion yields no whole share. Not applied.", sym)
            self._alert(f"Corporate event {ev['event_id']} NOT applied: yields no whole shares; record a CASH_OUT instead.")
            return cash
        # 1. fractional cash-in-lieu (cash first: it is idempotent and keyed by exec_id)
        in_lieu = round(frac * float(ev.get("fraction_cash_per_new_share") or 0.0), 2) if not prior else float(prior["net_cash"])
        if in_lieu > 0 or prior:
            exec_id = self._record_event_execution(ev, "EVENT_CASH_IN_LIEU", 0, float(ev.get("fraction_cash_per_new_share") or 0.0), in_lieu, date_str)
            cash = self._credit_event_cash(exec_id, cash, date_str)
        # 2. carry the position to the new symbol on the new share basis (cost basis and entry date are preserved)
        if not resuming:
            new_doc = {k: v for k, v in pos.items() if k != "_id"}
            new_doc.update(symbol=new_sym, shares=whole, applied_actions=list(pos.get("applied_actions") or []) + [key],
                           stop_loss_price=None, supertrend_stop=None, ema100_stop=None, sma150_stop=None, stop_buffer_pct=None)
            # Cost basis is carried over: whole new shares x new entry price == old shares x old entry price x alloc_ratio.
            alloc_ratio = float(whole) / (float(whole) + float(frac)) if (whole + frac) > 0 else 1.0
            for k in ("entry_price", "entry_fill_price"):
                if new_doc.get(k):
                    new_doc[k] = float(new_doc[k]) / ratio
            for k in ("entry_outlay", "entry_fees"):
                if new_doc.get(k) is not None:
                    new_doc[k] = round(float(new_doc[k]) * alloc_ratio, 2)
            if new_doc.get("last_close"):
                new_doc["last_close"] = float(new_doc["last_close"]) / ratio     # stale mark rebased; real price comes from the new symbol's bar
            mark = float(new_doc.get("last_close") or new_doc.get("entry_price") or 0.0)
            new_doc["market_value"] = whole * mark
            new_doc["unrealized_pnl"] = new_doc["market_value"] - float(new_doc.get("entry_outlay", whole * float(new_doc.get("entry_price", mark))))
            self.db.positions.replace_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": new_sym}, new_doc, upsert=True)
            positions[new_sym] = new_doc
        # 3. remove the old symbol and its pending orders
        self._cancel_pending_orders(sym, f"EVENT_CONVERSION:{ev['event_id']}", date_str)  # before the delete: stays resumable
        self.db.positions.delete_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym})
        positions.pop(sym, None)
        logger.critical("[CORPORATE_EVENT] %s: %s -> %s x%g (%d -> %d sh, cash in lieu Rs %.2f). Evidence: %s",
                        self.config.strategy_id, sym, new_sym, ratio, old_shares, whole, in_lieu, ev["evidence"])
        self._alert(f"Corporate event applied: {sym} -> {new_sym} x{ratio:g}: {old_shares} -> {whole} sh, cash in lieu Rs {in_lieu:,.2f} (effective {eff}).")
        return cash

    def _prior_execution(self, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execution already written for this order (crash/retry recovery), else None."""
        prior = self.db.executions.find_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "order_id": order["order_id"]})
        return prior if isinstance(prior, dict) else None

    def _record_execution(self, exec_model: ExecutionModel) -> None:
        self._assert_lease()
        try:
            self.db.executions.insert_one(exec_model.to_dict())
        except DuplicateKeyError:
            raise RuntimeError(f"EXECUTION_CONFLICT: retry recovery for {exec_model.exec_id}")
        self._execution_cash[exec_model.exec_id] = float(exec_model.net_cash)

    def _persist_cash(self, cash: float, date_str: str, exec_id: str) -> None:
        """Persist cash exactly once per execution: the applied-marker and the new balance are one atomic document update."""
        self._assert_lease()
        execution = self.db.executions.find_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "exec_id": exec_id})
        delta = float(execution["net_cash"]) if isinstance(execution, dict) else self._execution_cash.get(exec_id)
        if delta is None:
            raise RuntimeError("CASH_CONFLICT: no authoritative execution cash flow")
        if cash < -0.005:
            raise RuntimeError("CASH_CONFLICT: recovery would create negative cash")
        expected_cash = round(cash - delta, 2)
        strat_key = {"strategy_id": self.config.strategy_id, "mode": self._mode}
        # Legacy strategy documents carry no `cash` (state then came from daily_nav). Seed it once from the balance this
        # run sized against, so the compare-and-swap below has a baseline; the portfolio lock serialises this seeding.
        strat_doc = self.db.strategies.find_one(strat_key)
        if isinstance(strat_doc, dict) and "cash" not in strat_doc:
            self.db.strategies.update_one(strat_key, {"$set": {"cash": expected_cash}})
        # Stored balances are rounded to cents; match within half a cent rather than on exact float equality.
        result = self.db.strategies.update_one(
            {**strat_key, "cash": {"$gte": expected_cash - 0.005, "$lte": expected_cash + 0.005},
             "applied_execs": {"$ne": exec_id}},
            {"$set": {"cash": round(cash, 2)},
             "$push": {"applied_execs": {"$each": [exec_id]}}},
        )
        if result.matched_count != 1:
            if not self._cash_already_applied(exec_id):
                raise RuntimeError("CASH_CONFLICT: portfolio changed since sizing; retry execution recovery")
            return
        self.db.daily_nav.update_one(
            {"strategy_id": self.config.strategy_id, "mode": self._mode, "date": date_str},
            {"$set": {"cash": round(cash, 2), "updated_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )

    def _cash_already_applied(self, exec_id: str) -> bool:
        strat = self.db.strategies.find_one({"strategy_id": self.config.strategy_id, "mode": self._mode})
        return isinstance(strat, dict) and exec_id in (strat.get("applied_execs") or [])

    def _record_trade(self, pos: Dict[str, Any], order: Dict[str, Any], shares: int, open_price: float, fill_price: float,
                      net_proceeds: float, fees_total: float, execution_date: pd.Timestamp, date_str: str, exec_id: str) -> None:
        entry_date_dt = pd.to_datetime(pos.get("entry_date") or execution_date)
        hold_days = (execution_date - entry_date_dt).days
        entry_price = float(pos["entry_price"])
        entry_fill_px = float(pos.get("entry_fill_price", entry_price))
        outlay = float(pos.get("entry_outlay", shares * entry_fill_px + float(pos.get("entry_fees", 0.0))))
        gross_pnl = (shares * fill_price) - (shares * entry_fill_px)
        total_fees = float(pos.get("entry_fees", 0.0)) + fees_total
        net_pnl = net_proceeds - outlay
        ret_pct = (net_pnl / outlay) * 100.0 if outlay > 0 else 0.0
        pos_entry_slip = float(pos.get("entry_slip_loss", (entry_fill_px - entry_price) * shares))
        exit_slip = (open_price - fill_price) * shares
        trade_model = TradeModel(
            strategy_id=self.config.strategy_id,
            mode=self._mode,
            symbol=order["symbol"],
            entry_date=pos["entry_date"],
            entry_price=entry_fill_px,
            exit_date=date_str,
            exit_price=fill_price,
            shares=shares,
            hold_days=hold_days,
            gross_pnl=round(gross_pnl, 2),
            net_pnl=round(net_pnl, 2),
            total_fees=round(total_fees, 2),
            total_slippage=round(pos_entry_slip + exit_slip, 2),
            return_pct=round(ret_pct, 4),
            exit_reason=order.get("reason") or "FORCED_DELISTING_LIQUIDATION",
            exec_id=exec_id,
        )
        try:
            self.db.trades.insert_one(trade_model.to_dict())
        except DuplicateKeyError:
            logger.warning("trade for %s already recorded; continuing idempotently", exec_id)

    def _recover_fill(self, order: Dict[str, Any], prior: Dict[str, Any], positions: Dict[str, Any], cash: float,
                      date_str: str, execution_date: pd.Timestamp, fills_log: List[Dict[str, Any]]) -> float:
        """An execution exists for this order but the order is not FILLED: a previous run crashed mid-fill.
        Finish the remaining bookkeeping steps idempotently (cash once, trade/position once, order FILLED). Never re-fill."""
        sym, exec_id = order["symbol"], prior["exec_id"]
        # Recovery bookkeeping belongs to the original fill session, never the restart date.
        original_date = str(prior.get("session_date") or order.get("execution_date") or "")[:10]
        if not original_date:
            original_date = pd.Timestamp(prior["executed_at"]).tz_convert("Asia/Kolkata").strftime("%Y-%m-%d")
        execution_date = pd.Timestamp(original_date)
        logger.critical("[FILL_RECOVERY] order %s already executed as %s; completing bookkeeping without re-filling.", order["order_id"], exec_id)
        if not self._cash_already_applied(exec_id):
            cash += float(prior["net_cash"])
            self._persist_cash(cash, date_str, exec_id)
        shares, fill_price = int(prior["shares"]), float(prior["fill_price"])
        if order["action"] == "SELL":
            pos = positions.get(sym)
            have_trade = isinstance(self.db.trades.find_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "exec_id": exec_id}), dict)
            if pos and not have_trade:
                self._record_trade(pos, order, shares, float(prior["raw_price"]), fill_price, float(prior["net_cash"]),
                                   float(prior["fees"]), execution_date, original_date, exec_id)
            self.db.positions.delete_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym})
            positions.pop(sym, None)
        else:
            if sym not in positions:
                outlay = -float(prior["net_cash"])
                pos_model = PositionModel(
                    strategy_id=self.config.strategy_id, mode=self._mode, symbol=sym, shares=shares,
                    entry_price=float(prior["raw_price"]), entry_fill_price=fill_price, entry_date=original_date,
                    entry_outlay=outlay, entry_fees=float(prior["fees"]), last_close=float(prior["raw_price"]),
                    market_value=float(prior["gross_val"]), unrealized_pnl=0.0,
                )
                self.db.positions.replace_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym},
                                              pos_model.to_dict(), upsert=True)
                positions[sym] = pos_model.to_dict()
        self.db.orders.update_one({"order_id": order["order_id"]},
                                  {"$set": {"status": "FILLED", "shares": shares, "execution_date": original_date}})
        fills_log.append({"action": order["action"], "symbol": sym, "shares": shares, "fill_price": fill_price, "recovered": True})
        return cash

    def _reconcile_cash(self, cash: float) -> None:
        """Fail loudly if stored cash disagrees with initial capital + execution ledger (paper has no other cash flows)."""
        try:
            execs = [e for e in self.db.executions.find({"strategy_id": self.config.strategy_id, "mode": self._mode}) if isinstance(e, dict)]
        except Exception as exc:
            logger.error("cash reconciliation skipped: %s", exc)
            return
        if not execs:
            return
        ledger = float(self.config.initial_capital) + sum(float(e.get("net_cash", 0.0)) for e in execs)
        tol = max(1.0, 0.01 * len(execs))
        if abs(ledger - cash) > tol:
            msg = f"CASH DRIFT: stored {cash:,.2f} vs ledger {ledger:,.2f} (delta {cash - ledger:+,.2f}) over {len(execs)} executions"
            logger.critical("[CASH_RECONCILIATION_FAILED] %s", msg)
            self._alert(msg)

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

    @serialized_portfolio
    def generate_post_market_signals(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp) -> Dict[str, Any]:
        # Check strategy status
        strat_entry = self.db.strategies.find_one({
            "strategy_id": self.config.strategy_id,
            "mode": getattr(self.config, "mode", "paper")
        })
        prior_signal_date = strat_entry.get("last_signal_date") if strat_entry else None
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

        # Back-dated run guard: signals for a date older than the newest signal/execution already recorded would mix
        # as-of-T cash with current positions, overwrite the T NAV record and re-stage orders from stale prices.
        newest_recorded = max(
            [d for d in (strat_entry.get("last_signal_date") if strat_entry else None,
                         strat_entry.get("last_execution_date") if strat_entry else None) if isinstance(d, str)],
            default=None,
        )
        if newest_recorded is not None and date_str < newest_recorded:
            msg = (f"Refusing signals for {date_str}: strategy '{self.config.strategy_id}' already recorded activity "
                   f"through {newest_recorded}. Re-running an older decision date is unsafe.")
            logger.critical("[REFUSED_STALE_DECISION_DATE] %s", msg)
            self._alert(msg)
            return {"status": "REFUSED_STALE_DECISION_DATE", "exits": [], "buys": [], "message": msg}

        state = self.get_current_state(as_of_date=date_str)
        cash = state["cash"]
        positions = state["positions"]
        self._apply_corporate_actions(positions, date_str)
        cash = self._apply_corporate_events(positions, cash, date_str, universe)
        missing =[sym for sym in positions if sym not in universe or
                   universe[sym].loc[universe[sym]["date"] == decision_date].empty]
        if missing:
            logger.critical(f"[MISSING_HELD_BAR] {date_str}: missing market data for held position(s): {missing}. "
                            f"Retaining position(s) at last close; daily exits and stop-loss will NOT be evaluated for missing symbol(s).")
            self._alert(f"🚨 MISSING HELD BAR on {date_str}: {', '.join(missing)}. Position(s) retained at last close; stops NOT evaluated for these symbol(s). Retrying ingestion.")

        # Mark open positions to today's EOD close from universe
        current_holdings_val = 0.0
        for sym, pos in list(positions.items()):
            if sym in missing:
                last_pr = float(pos.get("last_close") or pos.get("entry_price", 0.0))
                pos["last_close"] = last_pr
                pos["market_value"] = pos["shares"] * last_pr
                entry_px = float(pos.get("entry_price", pos.get("last_close", 0.0)))
                entry_outlay = float(pos.get("entry_outlay", pos["shares"] * entry_px))
                pos["unrealized_pnl"] = pos["market_value"] - entry_outlay
                current_holdings_val += pos["market_value"]
                continue

            df_sym = universe.get(sym)
            if df_sym is not None and not df_sym.empty:
                c_rows = df_sym.loc[df_sym["date"] <= decision_date]
                if not c_rows.empty:
                    last_row = c_rows.iloc[-1]
                    last_pr = float(last_row["close"])
                    prev_mark = float(pos.get("last_close") or 0.0)
                    if prev_mark > 0 and not (0.6 <= last_pr / prev_mark <= 1.0 / 0.6):
                        # Splits/bonuses must be recorded in `corporate_actions`; an unrecorded one halves NAV silently.
                        logger.critical(f"[POSSIBLE_CORPORATE_ACTION] {sym}: close moved {prev_mark:.2f} -> {last_pr:.2f} "
                                        f"({last_pr / prev_mark - 1:+.1%}). Record it in `corporate_actions` or verify the data.")
                        self._alert(f"POSSIBLE CORPORATE ACTION {sym}: {prev_mark:.2f} -> {last_pr:.2f}. Check `corporate_actions`.")
                    pos["last_close"] = last_pr
                    pos["market_value"] = pos["shares"] * last_pr
                    entry_px = float(pos.get("entry_price", pos.get("last_close", 0.0)))
                    entry_outlay = float(pos.get("entry_outlay", pos["shares"] * entry_px))
                    pos["unrealized_pnl"] = pos["market_value"] - entry_outlay

                    # Extract dynamic trailing stop levels
                    ema_stop = float(last_row["ema100"]) if "ema100" in last_row and not np.isnan(last_row["ema100"]) else None
                    st_stop = float(last_row["supertrend_lower"]) if "supertrend_lower" in last_row and not np.isnan(last_row["supertrend_lower"]) else None
                    sma150_stop = float(last_row["sma150"]) if "sma150" in last_row and not np.isnan(last_row["sma150"]) else None

                    # Structural trailing stop:
                    # - TrendMomentum: Close < EMA(100) or Supertrend
                    # - Minervini / AlphaSemivol: Close < SMA(150)
                    if ema_stop is not None:
                        effective_stop = ema_stop
                    elif st_stop is not None:
                        effective_stop = st_stop
                    elif sma150_stop is not None:
                        effective_stop = sma150_stop
                    else:
                        effective_stop = None

                    buffer_pct = None
                    if effective_stop is not None and last_pr > 0:
                        buffer_pct = round(((last_pr - effective_stop) / last_pr) * 100.0, 2)

                    pos["ema100_stop"] = round(ema_stop, 2) if ema_stop else None
                    pos["supertrend_stop"] = round(st_stop, 2) if st_stop else None
                    pos["sma150_stop"] = round(sma150_stop, 2) if sma150_stop else None
                    pos["stop_loss_price"] = round(effective_stop, 2) if effective_stop else None
                    pos["stop_buffer_pct"] = buffer_pct

                    update_dict = {
                        "last_close": last_pr,
                        "market_value": pos["market_value"],
                        "unrealized_pnl": pos["unrealized_pnl"],
                    }
                    try:
                        update_dict["days_held"] = int((decision_date - pd.to_datetime(pos.get("entry_date"))).days)
                    except (TypeError, ValueError):
                        pass
                    if effective_stop is not None:
                        update_dict["stop_loss_price"] = round(effective_stop, 2)
                        update_dict["supertrend_stop"] = round(st_stop, 2) if st_stop else None
                        update_dict["ema100_stop"] = round(ema_stop, 2) if ema_stop else None
                        update_dict["sma150_stop"] = round(sma150_stop, 2) if sma150_stop else None
                        update_dict["stop_buffer_pct"] = buffer_pct

                    self.db.positions.update_one(
                        {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper"), "symbol": sym},
                        {"$set": update_dict}
                    )
            current_holdings_val += pos.get("market_value", pos["shares"] * pos.get("last_close", pos.get("entry_price", 0.0)))

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
                # V3-A: Do NOT count DEFERRED_LOWER_CIRCUIT orders in exits_to_log (prevents inflating open_slots)
                if existing_exit.get("status") == "PENDING_OPEN":
                    exits_to_log.append({
                        "symbol": sym,
                        "shares": pos["shares"],
                        "close": pos.get("last_close", 0.0),
                        "reason": reason,
                    })
                continue

            ord_id = self._order_id("EXIT", date_str, sym)
            order = OrderModel(
                order_id=ord_id,
                strategy_id=self.config.strategy_id,
                mode=self._mode,
                symbol=sym,
                action="SELL",
                shares=pos["shares"],
                price=pos.get("last_close", 0.0),
                order_type="EXIT_AT_OPEN",
                status="PENDING_OPEN",
                reason=reason,
                signal_date=date_str,
            )
            self._stage_order(order)
            exits_to_log.append({
                "symbol": sym,
                "shares": pos["shares"],
                "close": pos.get("last_close", 0.0),
                "reason": reason,
            })

        # Capture existing pending orders before any rebalance modification (for V5-A repeat alert idempotency)
        existing_pending_buys = set()
        existing_pending_sells = set()
        if hasattr(self.db.orders, "distinct"):
            existing_pending_buys = set(self.db.orders.distinct("symbol", {
                "strategy_id": self.config.strategy_id,
                "mode": getattr(self.config, "mode", "paper"),
                "status": "PENDING_OPEN",
                "action": "BUY",
            }))
            existing_pending_sells = set(self.db.orders.distinct("symbol", {
                "strategy_id": self.config.strategy_id,
                "mode": getattr(self.config, "mode", "paper"),
                "status": "PENDING_OPEN",
                "action": "SELL",
            }))
        elif hasattr(self.db.orders, "find"):
            for o in self.db.orders.find({
                "strategy_id": self.config.strategy_id,
                "mode": getattr(self.config, "mode", "paper"),
                "status": "PENDING_OPEN",
            }):
                if o.get("action") == "BUY":
                    existing_pending_buys.add(o["symbol"])
                elif o.get("action") == "SELL":
                    existing_pending_sells.add(o["symbol"])

        # 2. Evaluate Rebalance Entries
        cal = sorted(list(set(d for df in universe.values() for d in df["date"])))
        buys_to_log = []
        rebal_drops: Dict[str, str] = {}
        approach = getattr(self.config, "execution_approach", "APPROACH_3")
        is_rebal = self.strategy.is_rebalance_decision_date(cal, decision_date, self.config)
        is_catchup = False
        if approach == "APPROACH_3" and not is_rebal:
            strat_doc = self.db.strategies.find_one({
                "strategy_id": self.config.strategy_id,
                "mode": getattr(self.config, "mode", "paper"),
            })
            if isinstance(strat_doc, dict) and bool(strat_doc.get("catchup_eligible", False)):
                is_catchup = True

        if (is_rebal or is_catchup) and missing:
            logger.warning(f"[REBALANCE_SKIPPED] {self.config.strategy_id}: held position(s) {missing} are unpriced; skipping rebalance entry staging to protect cash.")
        elif is_rebal or is_catchup:
            # Idempotency: clear any existing PENDING_OPEN rebalance buy orders for this signal date before staging fresh batch
            if hasattr(self.db.orders, "delete_many"):
                self.db.orders.delete_many({
                    "strategy_id": self.config.strategy_id,
                    "mode": getattr(self.config, "mode", "paper"),
                    "status": "PENDING_OPEN",
                    "action": "BUY",
                })
            candidates_df = apply_entry_price_cap(
                self.strategy.select_entry_candidates(universe, decision_date, positions, self.config), positions, self.config)
            target_symbols = list(candidates_df.head(self.config.portfolio_size).index)

            # Rank Hysteresis / Hold Buffer:
            hold_buf = getattr(self.config, "rebalance_hold_rank_buffer", self.config.portfolio_size)
            allowed_hold_symbols = list(candidates_df.head(hold_buf).index)

            # Rebalance drops: only evaluate on scheduled rebalances (never on 1-day catch-up sessions)
            if not is_catchup:
                rebal_drops = self.strategy.evaluate_rebalance_exits(universe, decision_date, positions, allowed_hold_symbols, self.config)
                for sym, reason in rebal_drops.items():
                    if sym not in requested_exits and sym in positions and sym not in missing:
                        existing_active_sell = self.db.orders.find_one({
                            "strategy_id": self.config.strategy_id,
                            "mode": getattr(self.config, "mode", "paper"),
                            "symbol": sym,
                            "action": "SELL",
                            "status": {"$in": ["PENDING_OPEN", "DEFERRED_LOWER_CIRCUIT"]},
                        })
                        if existing_active_sell:
                            logger.info(f"Active sell order {existing_active_sell.get('order_id')} already exists for rebal drop {sym}; skipping duplicate.")
                            continue
                        pos = positions[sym]
                        ord_id = self._order_id("DROP", date_str, sym)
                        order = OrderModel(
                            order_id=ord_id,
                            strategy_id=self.config.strategy_id,
                            mode=self._mode,
                            symbol=sym,
                            action="SELL",
                            shares=pos["shares"],
                            price=pos.get("last_close", 0.0),
                            order_type="EXIT_AT_OPEN",
                            status="PENDING_OPEN",
                            reason=reason,
                            signal_date=date_str,
                        )
                        self._stage_order(order)
                        exits_to_log.append({
                            "symbol": sym,
                            "shares": pos["shares"],
                            "close": pos.get("last_close", 0.0),
                            "reason": reason,
                        })

            # New candidate buys
            open_slots = self.config.portfolio_size - len(positions) + len(exits_to_log)
            holdings_est_val = sum(p["shares"] * p.get("last_close", 0.0) for p in positions.values())
            est_total_equity = cash + holdings_est_val
            target_alloc = est_total_equity / float(self.config.portfolio_size)

            # Approach-aware candidate filtering and backup slots
            if approach in ("APPROACH_1B", "APPROACH_3"):
                # Multiplicative buffer with dynamic slippage ceiling (25 bps) + cash-per-slot bounding
                slip_cap = max(float(self.config.slippage_pct), 0.0025)
                buffer_div = (1.0 + self.config.limit_cap_pct) * (1.0 + slip_cap) * (1.0 + self.config.fees_buffer_pct)
                est_avail_cash = cash + sum(
                    positions[e["symbol"]]["shares"] * float(positions[e["symbol"]].get("last_close", 0.0))
                    for e in exits_to_log if e["symbol"] in positions
                )
                effective_slot_alloc = min(target_alloc, est_avail_cash / max(1, open_slots))
                max_alloc_px = effective_slot_alloc / buffer_div
                candidate_pool = [
                    s for s in list(candidates_df.index)
                    if s not in positions and s not in rebal_drops
                    and float(candidates_df.loc[s, "close"]) <= max_alloc_px
                ]
                backup_slots = 0
            elif approach == "APPROACH_1A":
                candidate_pool = [s for s in list(candidates_df.index) if s not in positions and s not in rebal_drops]
                backup_slots = 0
            else:
                candidate_pool = [s for s in list(candidates_df.index) if s not in positions and s not in rebal_drops]
                backup_slots = int(getattr(self.strategy, "paper_backup_slots", 4)) if getattr(self.config, "unaffordable_policy", "SKIP") == "TRY_NEXT" else 0

            new_buys = candidate_pool[:max(0, open_slots + backup_slots)]

            for sym in new_buys:
                ord_id = self._order_id("BUY", date_str, sym)
                c_px = float(candidates_df.loc[sym, "close"])
                lim_px = c_px * (1.0 + self.config.limit_cap_pct)
                denom = c_px * (1.0 + self.config.slippage_pct + self.config.fees_buffer_pct)
                est_sh = int(target_alloc // denom) if denom > 0 else 0
                rk = f"RANK_{candidates_df.loc[sym, 'rank'] if 'rank' in candidates_df.columns else 1}"

                order = OrderModel(
                    order_id=ord_id,
                    strategy_id=self.config.strategy_id,
                    mode=self._mode,
                    symbol=sym,
                    action="BUY",
                    shares=0,  # Sized dynamically at open fill
                    price=c_px,
                    order_type="ENTRY_AT_OPEN",
                    status="PENDING_OPEN",
                    reason=rk,
                    signal_date=date_str,
                )
                self._stage_order(order)
                buys_to_log.append({
                    "symbol": sym,
                    "alloc": target_alloc,
                    "close": c_px,
                    "limit_price": lim_px,
                    "est_shares": est_sh,
                    "reason": rk,
                })

            if is_rebal:
                self.db.strategies.update_one(
                    {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")},
                    {"$set": {"catchup_eligible": False, "is_catchup_session": False}}
                )
            elif is_catchup:
                # Catch-up session: clear eligibility flag and set is_catchup_session only if orders were staged
                self.db.strategies.update_one(
                    {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")},
                    {"$set": {"catchup_eligible": False, "is_catchup_session": bool(len(new_buys) > 0)}}
                )

        # Update strategies collection with full post-market mark-to-market metadata
        unrealized_pnl = sum(float(p.get("unrealized_pnl", p.get("market_value", 0.0) - p.get("entry_outlay", 0.0))) for p in positions.values())
        tot_ret_pct = ((current_nav - float(self.config.initial_capital)) / float(self.config.initial_capital)) * 100.0

        self.db.strategies.update_one(
            {"strategy_id": self.config.strategy_id, "mode": getattr(self.config, "mode", "paper")},
            {"$set": {
                "current_nav": round(current_nav, 2),
                "holdings_val": round(current_holdings_val, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_return_pct": round(tot_ret_pct, 4),
                "open_positions_count": len(positions),
                "last_signal_date": date_str,
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
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
                    "sma150_stop": pos.get("sma150_stop"),
                    "stop_buffer_pct": pos.get("stop_buffer_pct"),
                })

        # Broadcast Telegram with Idempotency Guard (V5-A defense)
        is_repeat_run = (prior_signal_date == date_str)
        curr_buy_symbols = {b["symbol"] for b in buys_to_log}
        curr_exit_symbols = {e["symbol"] for e in exits_to_log}
        is_orders_unchanged = (curr_buy_symbols == existing_pending_buys) and (curr_exit_symbols.issubset(existing_pending_sells) or not exits_to_log)

        if is_repeat_run and is_orders_unchanged:
            logger.info(f"Signal alert already sent for {self.config.strategy_id} on {date_str} and pending orders unchanged. Skipping duplicate broadcast.")
        else:
            self.notifier.send_signal_alert(
                self.config.strategy_id,
                date_str,
                buys_to_log,
                exits_to_log,
                active_holdings=active_holdings_to_log,
            )
        return {
            "exits": exits_to_log,
            "buys": buys_to_log,
            "active_holdings": active_holdings_to_log,
            "missing_held_bars": missing,
        }

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
            "strategy_id": self.config.strategy_id, "mode": self._mode, "date": date_str, "symbol": sym, "side": side,
            "shares": shares, "order_id": order["order_id"], "open_price": open_price, "fill_price": fill_price,
            "model_slip_bps": round((fill_price / open_price - 1.0) * 1e4 * (1 if side == "BUY" else -1), 2),
            "adt_prev": bar.get("adt_prev"), "sig_prev": bar.get("sig_prev"), "series": bar.get("series"),
            "quote": quote, "ts": datetime.now(timezone.utc).isoformat(),
        }
        side_qty = (quote or {}).get("ask_qty" if side == "BUY" else "bid_qty")
        if side_qty is not None:
            # Paper fills assume the whole order trades at the quoted price; flag orders larger than the quantity displayed there.
            doc["quoted_qty_at_price"] = side_qty
            doc["depth_shortfall"] = bool(shares > side_qty)
        if quote and quote.get("ask") and quote.get("bid"):
            ref = quote["ask"] if side == "BUY" else quote["bid"]
            doc["quote_vs_open_bps"] = round((ref / open_price - 1.0) * 1e4 * (1 if side == "BUY" else -1), 2)
            doc["half_spread_bps"] = round((quote["ask"] - quote["bid"]) / (quote["ask"] + quote["bid"]) * 1e4, 2)
        try:
            self.db.get_collection("fill_audit").insert_one(doc)
        except Exception as exc:
            logger.error("fill_audit insert failed: %s", exc)

    @staticmethod
    def _quote_session_date(ts: Any) -> Optional[Any]:
        """IST calendar date of a broker quote timestamp (epoch seconds/ms or ISO string; naive strings are taken as IST), else None."""
        if ts is None or ts == "":
            return None
        try:
            if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.strip().replace(".", "", 1).isdigit()):
                v = float(ts)
                t = pd.to_datetime(v, unit="ms" if v > 1e11 else "s", utc=True)
            else:
                t = pd.to_datetime(ts)
                if t.tzinfo is None:
                    return t.date()
                t = t.tz_convert("UTC")
            return t.tz_convert("Asia/Kolkata").date()
        except Exception:
            return None

    def _quote_is_stale(self, q: Dict[str, Any], sym: str, execution_date: pd.Timestamp) -> bool:
        """True (refuse the quote) when it has no parseable timestamp or carries one from a different session than `execution_date`.
        Fail-closed: freshness cannot be established without a timestamp. If every live fill is refused with
        QUOTE_FRESHNESS_UNVERIFIED, the broker payload lacks the field the feed maps to `ts` (upstox_feed `timestamp`).
        Verified live 2026-11-08: `timestamp` is the quote snapshot time (not `last_trade_time`), so it vouches for the bid/ask depth, not for LTP recency."""
        d = self._quote_session_date(q.get("ts"))
        if d is None:
            logger.warning("[QUOTE_FRESHNESS_UNVERIFIED] %s: broker quote has no parseable timestamp (ts=%r); refusing fill", sym, q.get("ts"))
            return True
        if d != pd.Timestamp(execution_date).date():
            logger.error("[STALE_QUOTE] %s: broker quote is from %s, execution date is %s; refusing to fill on it",
                         sym, d, pd.Timestamp(execution_date).date())
            return True
        return False

    @staticmethod
    def _executable_price(bar: Dict[str, Any], side: str) -> Optional[float]:
        key = ("ask" if side == "BUY" else "bid") if bar.get("is_live_quote") else "open"
        price = float(bar.get(key) or 0.0)
        return price if np.isfinite(price) and price > 0 else None

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
                turnover50 = tail50["turnover"] if "turnover" in tail50.columns else tail50["close"] * tail50["volume"]
                to50 = float(turnover50.median())

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
            if q and float(q.get("open") or 0.0) > 0 and self._quote_is_stale(q, sym, execution_date):
                # A previous-session quote must never price today's fill (calendar mistake, stock not yet traded, lagging feed).
                # Return None (no yfinance fallback: its freshness cannot be checked either): the order is retained as NO_QUOTE,
                # the morning run exits non-zero and the scheduler retries until the 15:25 window closes.
                return None
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
                    "lower_circuit": q.get("lower_circuit") or q.get("lower_circuit_limit"),
                    "upper_circuit": q.get("upper_circuit") or q.get("upper_circuit_limit"),
                    "bid": q.get("bid"),
                    "ask": q.get("ask"),
                    **self._cost_inputs(prev_rows),
                }
            logger.error("no usable broker quote for %s at execution time; falling back to the yfinance quote", sym)

        # yfinance exposes no quote timestamp, and fast_info.open is the last row of a 1y DAILY history, i.e. YESTERDAY's open whenever today's
        # row is not there yet: staleness is undetectable. Not used for fills unless explicitly enabled (ALLOW_YFINANCE_FALLBACK=true).
        if not getattr(self.config, "allow_yfinance_fallback", False):
            logger.error("no verifiable broker quote for %s; yfinance fallback is disabled (its open cannot be checked for freshness): order retained, run will exit non-zero and retry", sym)
            return None
        logger.warning("[YFINANCE_FALLBACK_ENABLED] %s: filling from an unverifiable yfinance open (ALLOW_YFINANCE_FALLBACK=true)", sym)
        # Live quote fallback at 09:16 IST (never fallback to prev_close as open_px)
        try:
            import yfinance as yf
            ticker = sym if sym.endswith(".NS") else f"{sym}.NS"
            fast_info = yf.Ticker(ticker).fast_info
            # `open` only: at 09:16 `last_price` is NOT the opening price, and a wrong open corrupts gap checks and fills.
            open_val = getattr(fast_info, "open", None)
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

    @serialized_portfolio
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
                "no_quote": [],
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
        self.ensure_indexes()
        self._reconcile_cash(cash)
        no_quote: List[Dict[str, Any]] = []        # orders that could not be priced this run (retained; run exits non-zero)
        retained_ids: set = set()

        pending_orders = list(self.db.orders.find({
            "strategy_id": self.config.strategy_id,
            "mode": self._mode,
            "status": {"$in": ["PENDING_OPEN", "DEFERRED_LOWER_CIRCUIT"]},
            "signal_date": {"$lt": date_str},
        }))

        sell_orders = [o for o in pending_orders if o["action"] == "SELL"]
        buy_orders = [o for o in pending_orders if o["action"] == "BUY"]

        def _order_rank(o: Dict[str, Any]) -> int:
            r = str(o.get("reason", ""))
            if r.startswith("RANK_"):
                try:
                    return int(r.split("_")[1])
                except (ValueError, IndexError):
                    return 999999
            return 999999

        buy_orders.sort(key=_order_rank)

        fills_log: List[Dict[str, Any]] = []
        deferred_log: List[Dict[str, Any]] = []
        # Finish already-recorded executions BEFORE stale expiry, quote lookup or new fills.
        recovered_ids = set()
        for order in sell_orders + buy_orders:
            prior = self._prior_execution(order)
            if prior is not None:
                cash = self._recover_fill(order, prior, positions, cash, date_str, execution_date, fills_log)
                recovered_ids.add(order['order_id'])
        sell_orders = [o for o in sell_orders if o['order_id'] not in recovered_ids]
        buy_orders = [o for o in buy_orders if o['order_id'] not in recovered_ids]
        self._apply_corporate_actions(positions, date_str)

        # Stale-signal guard: a BUY retained after an outage must not fill on a signal older than the last completed session.
        last_session = None
        for o in buy_orders:
            df_o = universe.get(o["symbol"])
            if df_o is not None and not df_o.empty:
                prior_dates = df_o.loc[df_o["date"] < execution_date, "date"]
                if not prior_dates.empty:
                    d_last = prior_dates.max()
                    last_session = d_last if last_session is None else max(last_session, d_last)
        if last_session is not None:
            last_session_str = last_session.strftime("%Y-%m-%d")
            fresh = []
            for o in buy_orders:
                if str(o.get("signal_date", "")) < last_session_str:
                    self.db.orders.update_one({"order_id": o["order_id"]},
                                              {"$set": {"status": "SKIPPED_EXPIRED", "cancel_reason": "STALE_SIGNAL", "execution_date": date_str}})
                    logger.warning("BUY %s expired: signal %s older than last session %s", o["order_id"], o.get("signal_date"), last_session_str)
                else:
                    fresh.append(o)
            buy_orders = fresh

        # 1. Execute Sells First
        for order in sell_orders:
            sym = order["symbol"]
            # Crash/retry recovery: an execution already exists for this order -> finish bookkeeping, never re-fill.
            prior = self._prior_execution(order)
            if prior is not None:
                cash = self._recover_fill(order, prior, positions, cash, date_str, execution_date, fills_log)
                continue
            # Guard against double-sell / phantom cash if position already sold
            if sym not in positions:
                logger.warning(f"Position {sym} already closed or not found. Cancelling orphan/duplicate sell order {order['order_id']}.")
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "CANCELLED", "cancel_reason": "POSITION_ALREADY_CLOSED", "execution_date": date_str}}
                )
                continue

            pos = positions[sym]
            df = universe.get(sym)
            bar = self._get_execution_bar(sym, execution_date, universe) if df is not None else None
            # Legacy data-ended requests are not evidence of a sale or delisting.
            if order.get("reason") == "DATA_ENDED_FORCED_EXIT" and bar is not None:
                self.db.orders.update_one({"order_id": order["order_id"], "strategy_id": self.config.strategy_id, "mode": self._mode},
                    {"$set": {"status": "CANCELLED", "cancel_reason": "DATA_RECOVERED", "execution_date": date_str}})
                continue
            if bar is None:
                retained_ids.add(order["order_id"])
                no_quote.append({"symbol": sym, "reason": "NO_QUOTE: sell retained; no delisting inferred"})
                continue
            prev_close = bar["prev_close"]
            open_px, high_px, low_px, close_px = (bar[k] for k in ("open", "high", "low", "close"))
            row = bar

            # Circuit breaker check: use exchange limits and real buyer liquidity (execution-time quote)
            is_live = bar.get("is_live_quote", False)
            lc = float(bar.get("lower_circuit") or 0.0) if bar.get("lower_circuit") else None
            if lc is not None and lc > 0:
                # Stock is at/below LC band AND zero buyer bids exist (if live bids available)
                no_buyers = (bar.get("bid") is None) or (float(bar.get("bid") or 0.0) <= 0.0)
                is_down = (close_px <= lc * 1.001) and (no_buyers if is_live else True)
            elif not is_live:
                is_down = is_locked_down(open_px, high_px, low_px, close_px, prev_close)
            else:
                # Live quote without lower_circuit band (e.g. yfinance fallback):
                pct_down = (prev_close - open_px) / prev_close if prev_close > 0 else 0.0
                no_buyers = (bar.get("bid") is None) or (float(bar.get("bid") or 0.0) <= 0.0)
                flat_tick = abs(high_px - low_px) / prev_close < 0.001 if prev_close > 0 else (open_px == high_px == low_px == close_px)
                is_down = (pct_down >= 0.048) and flat_tick and no_buyers
                if is_down:
                    logger.warning("[DEGRADED_CIRCUIT_HEURISTIC] %s deferred on yfinance fallback (no circuit band/bid data): gap %.1f%%", sym, pct_down * 100)

            if is_down:
                def_days = int(order.get("deferral_days", 0)) + 1
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "DEFERRED_LOWER_CIRCUIT", "deferral_days": def_days}}
                )
                if sym in positions:
                    self.db.positions.update_one(
                        {"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym},
                        {"$set": {"deferral_days": def_days}}
                    )
                deferred_log.append({"symbol": sym, "days": def_days})
                continue

            open_price = self._executable_price(bar, "SELL")
            if open_price is None:
                retained_ids.add(order["order_id"])
                no_quote.append({"symbol": sym, "reason": "NO_LIQUIDITY: no executable bid; sell retained"})
                continue
            shares = int(pos["shares"])  # always the CURRENT holding (a corporate action may have changed it since staging)
            fill_price = open_price * (1.0 - self._slip(sym, bar, open_price, shares, "SELL"))
            gross_val = shares * fill_price
            fees_dict = calc_sell_fees_exact(gross_val, execution_date, self.config)
            net_proceeds = gross_val - fees_dict["total"]
            cash += net_proceeds

            # Ordered, idempotent writes (each step is safe to repeat; _recover_fill resumes from any crash point):
            # execution -> cash (once, keyed by exec_id) -> trade (once) -> position delete -> order FILLED.
            exec_id = f"EXEC_{order['order_id']}"
            exec_model = ExecutionModel(
                exec_id=exec_id,
                order_id=order["order_id"],
                strategy_id=self.config.strategy_id,
                mode=self._mode,
                symbol=sym,
                action="SELL",
                shares=shares,
                raw_price=open_price,
                fill_price=fill_price,
                gross_val=gross_val,
                net_cash=net_proceeds,
                fees=fees_dict["total"],
                slippage=round((open_price - fill_price) * shares, 2),
                session_date=date_str,
            )
            self._record_execution(exec_model)
            self._persist_cash(cash, date_str, exec_id)
            self._audit_fill(order, sym, "SELL", shares, bar, open_price, fill_price, date_str)
            self._record_trade(pos, order, shares, open_price, fill_price, net_proceeds, fees_dict["total"], execution_date, date_str, exec_id)
            self.db.positions.delete_one({"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym})
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
        had_rebal_skips = False
        approach = getattr(self.config, "execution_approach", "APPROACH_3")

        for order in buy_orders:
            sym = order["symbol"]
            prior = self._prior_execution(order)
            if prior is not None:
                cash = self._recover_fill(order, prior, positions, cash, date_str, execution_date, fills_log)
                continue
            
            # Prevent duplicate buy if symbol is already held in open positions
            if sym in positions:
                logger.info(f"Symbol {sym} already held in portfolio. Skipping duplicate buy order {order['order_id']}.")
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_ALREADY_HELD", "execution_date": date_str}}
                )
                continue

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
                retained_ids.add(order["order_id"])
                no_quote.append({"symbol": sym, "reason": "NO_QUOTE: missing symbol history; buy retained"})
                continue
            bar = self._get_execution_bar(sym, execution_date, universe)
            if bar is None:
                # An outage must not silently forfeit the month's entry: retain the order, say so, and make the run fail
                # (the scheduler retries; stale signals are expired by the stale-signal guard on the next session).
                logger.error("[NO_QUOTE] cannot price BUY %s for %s; order retained for the next attempt", order["order_id"], sym)
                self.db.orders.update_one({"order_id": order["order_id"]},
                                          {"$set": {"note": "NO_QUOTE", "last_attempt": date_str}})
                retained_ids.add(order["order_id"])
                no_quote.append({"symbol": sym, "reason": "NO_QUOTE: buy retained"})
                continue
            prev_close = bar["prev_close"]
            open_px = bar["open"]
            high_px = bar["high"]
            low_px = bar["low"]
            close_px = bar["close"]
            row = bar

            # Circuit breaker check: use exchange limits and real seller liquidity
            is_live = bar.get("is_live_quote", False)
            uc = float(bar.get("upper_circuit") or 0.0) if bar.get("upper_circuit") else None
            if uc is not None and uc > 0:
                # Stock is at/above UC band AND zero seller asks exist (if live asks available)
                no_sellers = (bar.get("ask") is None) or (float(bar.get("ask") or 0.0) <= 0.0)
                is_up = (close_px >= uc * 0.999) and (no_sellers if is_live else True)
            elif not is_live:
                is_up = is_locked_up(open_px, high_px, low_px, close_px, prev_close)
            else:
                # Live quote without upper_circuit band (e.g. yfinance fallback):
                pct_up = (open_px - prev_close) / prev_close if prev_close > 0 else 0.0
                no_sellers = (bar.get("ask") is None) or (float(bar.get("ask") or 0.0) <= 0.0)
                flat_tick = abs(high_px - low_px) / prev_close < 0.001 if prev_close > 0 else (open_px == high_px == low_px == close_px)
                is_up = (pct_up >= 0.048) and flat_tick and no_sellers

            if is_up:
                order["status"] = "SKIPPED_LOCKED_UP"
                had_rebal_skips = True
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_LOCKED_UP", "execution_date": date_str}}
                )
                deferred_log.append({"symbol": sym, "reason": f"Skipped: Upper Circuit Lock (Open ₹{open_px:.2f})"})
                continue

            # Gap limit cap check (anchored to signal close when valid, fallback to prev_close)
            open_price = self._executable_price(bar, "BUY")
            if open_price is None:
                retained_ids.add(order["order_id"])
                no_quote.append({"symbol": sym, "reason": "NO_LIQUIDITY: no executable ask; buy retained"})
                continue
            signal_close = float(order.get("price") or 0.0)
            ref_close = signal_close if signal_close > 0 else (prev_close if (prev_close and prev_close > 0) else 0.0)
            if ref_close > 0:
                limit_cap_price = ref_close * (1.0 + self.config.limit_cap_pct)
                if open_price > limit_cap_price:
                    order["status"] = "SKIPPED_GAP_LIMIT_EXCEEDED"
                    had_rebal_skips = True
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
            open_slots_remain = max(1, self.config.portfolio_size - len(positions))
            slot_cash_share = cash / float(open_slots_remain) if approach in ("APPROACH_1B", "APPROACH_3") else cash
            avail = min(target_alloc, slot_cash_share, cash, max_exec_val)

            # Order size is set from the pre-slippage open (as the backtest engine does), then the cost model prices it
            buffer_mult = 1.0 + self.config.fees_buffer_pct
            sized_shares = int(avail // (open_price * buffer_mult))
            est_fill_px = open_price * (1.0 + self._slip(sym, bar, open_price, max(sized_shares, 1), "BUY"))
            est_cost_per_sh = est_fill_px * buffer_mult
            shares = int(avail // est_cost_per_sh)

            if shares <= 0:
                order["status"] = "SKIPPED_UNAFFORDABLE"
                had_rebal_skips = True
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_UNAFFORDABLE", "execution_date": date_str}}
                )
                continue

            # Second-pass pricing: recompute fill_price at exact submitted share count (matches BacktestEngine)
            fill_price = open_price * (1.0 + self._slip(sym, bar, open_price, shares, "BUY"))
            gross_val = shares * fill_price
            buy_fees = calc_buy_fees_exact(gross_val, execution_date, self.config)
            total_outlay = gross_val + buy_fees["total"]

            while total_outlay > cash and shares > 0:
                shares -= 1
                fill_price = open_price * (1.0 + self._slip(sym, bar, open_price, max(shares, 1), "BUY"))
                gross_val = shares * fill_price
                buy_fees = calc_buy_fees_exact(gross_val, execution_date, self.config)
                total_outlay = gross_val + buy_fees["total"]

            if shares <= 0 or gross_val < 0.25 * target_alloc:
                order["status"] = "SKIPPED_UNAFFORDABLE"
                had_rebal_skips = True
                self.db.orders.update_one(
                    {"order_id": order["order_id"]},
                    {"$set": {"status": "SKIPPED_UNAFFORDABLE", "execution_date": date_str}}
                )
                continue

            cash -= total_outlay
            buy_slip_total = round((fill_price - open_price) * shares, 2)

            # Record Execution (deterministic id: a retry can never create a second execution for the same order)
            exec_id = f"EXEC_{order['order_id']}"
            exec_model = ExecutionModel(
                exec_id=exec_id,
                order_id=order["order_id"],
                strategy_id=self.config.strategy_id,
                mode=self._mode,
                symbol=sym,
                action="BUY",
                shares=shares,
                raw_price=open_price,
                fill_price=fill_price,
                gross_val=gross_val,
                net_cash=-total_outlay,
                fees=buy_fees["total"],
                slippage=buy_slip_total,
                session_date=date_str,
            )
            self._record_execution(exec_model)
            self._persist_cash(cash, date_str, exec_id)
            self._audit_fill(order, sym, "BUY", shares, bar, open_price, fill_price, date_str)

            # Add Position
            pos_model = PositionModel(
                strategy_id=self.config.strategy_id,
                mode=self._mode,
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
                {"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym},
                pos_model.to_dict(),
                upsert=True
            )
            positions[sym] = pos_model.to_dict()

            # Update Order
            order["status"] = "FILLED"
            self.db.orders.update_one(
                {"order_id": order["order_id"]},
                {"$set": {"status": "FILLED", "shares": shares, "execution_date": date_str}}
            )
            fills_log.append({"action": "BUY", "symbol": sym, "shares": shares, "fill_price": fill_price})

        # Expire leftover PENDING_OPEN buys from earlier signal dates (e.g. unreached backup slots), except orders
        # retained this run because no price was available.
        for pending_o in list(self.db.orders.find({
            "strategy_id": self.config.strategy_id,
            "mode": self._mode,
            "action": "BUY",
            "status": "PENDING_OPEN",
            "signal_date": {"$lt": date_str},
        })):
            if not isinstance(pending_o, dict) or pending_o.get("order_id") in retained_ids:
                continue
            self.db.orders.update_one(
                {"order_id": pending_o["order_id"]},
                {"$set": {"status": "SKIPPED_EXPIRED", "execution_date": date_str,
                          "updated_at": datetime.now(timezone.utc).isoformat()}}
            )

        # Check if Approach 3 catch-up is eligible for the upcoming post-market scan
        approach = getattr(self.config, "execution_approach", "APPROACH_3")
        if approach == "APPROACH_3" and not no_quote:
            db_skips_today = list(self.db.orders.find({
                "strategy_id": self.config.strategy_id,
                "mode": self._mode,
                "action": "BUY",
                "execution_date": date_str,
                "signal_date": {"$lt": date_str},
                "status": {"$in": [
                    "SKIPPED_GAP_LIMIT_EXCEEDED",
                    "SKIPPED_LOCKED_UP",
                    "SKIPPED_UNAFFORDABLE",
                    "SKIPPED_MIN_OUTLAY",
                    "SKIPPED_LOW_LIQUIDITY",
                ]},
            }))
            # BUYs expired by the stale-signal guard (e.g. the daemon was down for a session) are lost entries just like skipped ones:
            # without this the slots stay empty until the next month-end. Flag the catch-up scan so the next post-market run refills them.
            db_stale_today = list(self.db.orders.find({
                "strategy_id": self.config.strategy_id,
                "mode": self._mode,
                "action": "BUY",
                "execution_date": date_str,
                "signal_date": {"$lt": date_str},
                "status": "SKIPPED_EXPIRED",
                "cancel_reason": "STALE_SIGNAL",
            }))
            had_skips_today = had_rebal_skips or (len(db_skips_today) > 0) or (len(db_stale_today) > 0)
            strat_doc = self.db.strategies.find_one({
                "strategy_id": self.config.strategy_id,
                "mode": self._mode,
            })
            was_catchup_session = bool(strat_doc.get("is_catchup_session", False)) if isinstance(strat_doc, dict) else False

            if len(positions) < self.config.portfolio_size and had_skips_today and not was_catchup_session:
                logger.info(
                    f"[{self.config.strategy_id}] Empty slots ({len(positions)}/{self.config.portfolio_size}) after skips. "
                    f"Flagging catch-up eligible for post-market scan."
                )
                self.db.strategies.update_one(
                    {"strategy_id": self.config.strategy_id, "mode": self._mode},
                    {"$set": {"catchup_eligible": True}}
                )
            else:
                self.db.strategies.update_one(
                    {"strategy_id": self.config.strategy_id, "mode": self._mode},
                    {"$set": {"catchup_eligible": False, "is_catchup_session": False}}
                )

        # 3. Update NAV Record
        current_holdings_val = 0.0
        for sym, pos in positions.items():
            df = universe.get(sym)
            if df is not None:
                rows = df[df["date"] == execution_date]
                if not rows.empty:
                    last_pr = float(rows.iloc[0]["close"])
                elif pos.get("entry_date") == date_str:
                    # V3-D: On morning of entry, mark to fill price rather than raw open to avoid artificial loss
                    last_pr = float(pos.get("entry_fill_price", pos.get("entry_price", pos.get("last_close", 0.0))))
                else:
                    last_pr = float(pos.get("last_close", pos.get("entry_price", 0.0)))
                pos["last_close"] = last_pr
                pos["market_value"] = pos["shares"] * last_pr
                entry_outlay = float(pos.get("entry_outlay", pos["shares"] * pos["entry_price"]))
                pos["unrealized_pnl"] = pos["market_value"] - entry_outlay
                self.db.positions.update_one(
                    {"strategy_id": self.config.strategy_id, "mode": self._mode, "symbol": sym},
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
            mode=self._mode,
            date=date_str,
            cash=round(cash, 2),
            holdings_val=round(current_holdings_val, 2),
            nav=round(current_nav, 2),
            open_positions_count=len(positions),
        )
        self.db.daily_nav.replace_one(
            {"strategy_id": self.config.strategy_id, "mode": self._mode, "date": date_str},
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
                "holdings_val": round(current_holdings_val, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "realized_pnl": round(realized_pnl, 2),
                "total_return_pct": round(tot_ret_pct, 4),
                "open_positions_count": len(positions),
                "closed_trades_count": closed_cnt,
                "win_rate_pct": round(win_rate, 2),
                "last_execution_date": date_str,
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }}
        )

        # 4. Broadcast Telegram Execution Alert
        self.notifier.send_execution_alert(self.config.strategy_id, date_str, fills_log, deferred_log + no_quote, current_nav, cash=cash)

        return {
            "fills": fills_log,
            "deferred": deferred_log,
            "no_quote": no_quote,
            "cash": cash,
            "holdings_val": current_holdings_val,
            "nav": current_nav,
        }
