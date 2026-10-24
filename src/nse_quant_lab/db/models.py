"""
models.py -- Unified Schema Models for Backtest, Paper Trading & Real Execution
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from datetime import datetime


@dataclass
class OrderModel:
    order_id: str
    strategy_id: str
    mode: str                           # "paper", "real", "backtest"
    symbol: str
    action: str                         # "BUY", "SELL"
    shares: int
    price: float
    order_type: str                     # "ENTRY_AT_OPEN", "EXIT_AT_OPEN"
    status: str                         # "PENDING_OPEN", "FILLED", "DEFERRED_LOWER_CIRCUIT", "SKIPPED_GAP_LIMIT_EXCEEDED", "REJECTED_INSUFFICIENT_CASH"
    reason: str
    signal_date: str                    # YYYY-MM-DD
    execution_date: Optional[str] = None
    deferral_days: int = 0
    created_at: str = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionModel:
    exec_id: str
    order_id: str
    strategy_id: str
    mode: str                           # "paper", "real", "backtest"
    symbol: str
    action: str                         # "BUY", "SELL"
    shares: int
    raw_price: float
    fill_price: float
    gross_val: float
    net_cash: float
    fees: float
    slippage: float
    executed_at: str = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PositionModel:
    strategy_id: str
    mode: str                           # "paper", "real"
    symbol: str
    shares: int
    entry_price: float
    entry_fill_price: float
    entry_date: str
    entry_outlay: float
    entry_fees: float
    last_close: float
    market_value: float
    unrealized_pnl: float
    days_held: int = 0
    deferral_days: int = 0
    updated_at: str = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TradeModel:
    strategy_id: str
    mode: str                           # "paper", "real", "backtest"
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: int
    hold_days: int
    gross_pnl: float
    net_pnl: float
    total_fees: float
    total_slippage: float
    return_pct: float
    exit_reason: str
    created_at: str = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DailyNavModel:
    strategy_id: str
    mode: str                           # "paper", "real", "backtest"
    date: str                           # YYYY-MM-DD
    cash: float
    holdings_val: float
    nav: float
    open_positions_count: int
    updated_at: str = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyModel:
    strategy_id: str
    name: str
    description: str
    mode: str                           # "paper", "real", "backtest"
    rebalance_frequency: str            # "MONTHLY", "DAILY"
    portfolio_size: int
    initial_capital: float
    current_nav: float
    cash: float
    holdings_val: float
    unrealized_pnl: float
    realized_pnl: float
    total_return_pct: float
    open_positions_count: int
    closed_trades_count: int
    win_rate_pct: float
    status: str = "ACTIVE"              # "ACTIVE", "PAUSED", "ARCHIVED"
    last_signal_date: Optional[str] = None
    last_execution_date: Optional[str] = None
    last_run_at: Optional[str] = None
    created_at: str = datetime.utcnow().isoformat()
    updated_at: str = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
