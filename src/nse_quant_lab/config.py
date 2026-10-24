"""
config.py -- Authoritative Configuration for Backtesting & Paper Trading
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional
import json

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class BacktestConfig:
    """Universal backtest simulation configuration."""
    initial_capital: float = 1_000_000.0
    portfolio_size: int = 12
    max_open_positions: int = 12
    rebalance_frequency: str = "MONTHLY"        # "MONTHLY" or "DAILY"
    enforce_lower_circuit_deferral: bool = True # Limit-down exits deferred
    enforce_upper_circuit_rejection: bool = True # Limit-up buys skipped
    unaffordable_policy: str = "SKIP"            # "SKIP" or "TRY_NEXT"

    # Execution limits & buffers
    slippage_pct: float = 0.0010                 # 0.10% per leg
    limit_cap_pct: float = 0.0200                # +2% gap limit cap
    gap_limit_cap_pct: Optional[float] = 0.02    # Alias
    gap_buffer_pct: float = 0.0150               # 1.5% sizing buffer
    fees_buffer_pct: float = 0.0020              # 0.20% fee buffer
    cash_buffer_pct: float = 0.018               # 1.8% cash buffer

    # Statutory cost model (NSE Cash Delivery)
    brokerage_per_order: float = 20.0
    stt_pct: float = 0.100                       # 0.100% on buy & sell gross
    stamp_duty_buy_pct: float = 0.015            # 0.015% on buy
    exchange_txn_pct_legacy: float = 0.00345     # Prior to Oct 1, 2024
    exchange_txn_pct_revised: float = 0.00297    # From Oct 1, 2024 onward
    sebi_pct: float = 0.0001                     # 0.0001% (Rs 10/Cr)
    gst_pct: float = 18.0                        # 18% on (Brokerage + Exch + SEBI)
    dp_charge_sell: float = 15.93                # Rs 13.50 + 18% GST on sell delivery

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BacktestConfig:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class PaperTradingConfig(BacktestConfig):
    """Configuration for forward paper trading on live daily updates."""
    mode: str = field(default_factory=lambda: os.environ.get("EXECUTION_MODE", "paper"))
    strategy_id: str = field(default_factory=lambda: os.environ.get("STRATEGY_ID", "trend_momentum"))
    initial_capital: float = field(default_factory=lambda: float(os.environ.get("INITIAL_CAPITAL", "1000000.0")))
    portfolio_size: int = field(default_factory=lambda: int(os.environ.get("PORTFOLIO_SIZE", "12")))
    mongo_uri: str = field(default_factory=lambda: os.environ.get("MONGO_URI", "mongodb://localhost:27017/"))
    db_name: str = field(default_factory=lambda: os.environ.get("MONGO_DB", "stock_scanner"))
    telegram_enabled: bool = field(default_factory=lambda: os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true")
    telegram_bot_token: Optional[str] = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: Optional[str] = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID"))
