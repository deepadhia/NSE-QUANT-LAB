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
    portfolio_size: int = 6
    max_open_positions: int = 6
    rebalance_hold_rank_buffer: int = 15        # Retain positions if rank <= 15 (Rank Hysteresis for 6 slots)
    rebalance_frequency: str = "MONTHLY"        # "MONTHLY" or "DAILY"
    enforce_lower_circuit_deferral: bool = True # Limit-down exits deferred
    enforce_upper_circuit_rejection: bool = True # Limit-up buys skipped
    unaffordable_policy: str = "TRY_NEXT"        # "TRY_NEXT" (queues backup slots) or "SKIP"
    min_turnover_50d: float = 10_000_000.0       # Rs 1.0 Crore / day default (realistic production floor)
    min_price: float = 20.0                      # Rs 20 minimum stock price (filters penny stocks)
    enforce_pit_universe: bool = True            # Point-in-time constituent gating (Nifty 500)
    pit_top_n_liquidity: int = 200               # Top 200 stocks by 50-day ADT on decision date
    pit_max_universe_pct: float = 0.45           # Relative capacity cap (Top 45% of active pool)
    max_volume_participation_pct: float = 0.10   # Max 10% of 50-day ADT for single morning order
    deduct_annual_tax: bool = False              # Deduct statutory FY tax from cash on March 31

    # Execution limits & buffers
    slippage_pct: float = 0.0010                 # 0.10% per leg
    limit_cap_pct: float = 0.0200                # +2% gap limit cap
    gap_limit_cap_pct: Optional[float] = 0.02    # Alias
    gap_buffer_pct: float = 0.0150               # 1.5% sizing buffer
    fees_buffer_pct: float = 0.0020              # 0.20% fee buffer
    cash_buffer_pct: float = 0.018               # 1.8% cash buffer

    # Statutory cost model (NSE Cash Delivery)
    brokerage_per_order: float = 0.0            # Rs 0.00 equity delivery brokerage on Dhan
    stt_pct: float = 0.100                       # 0.100% on buy & sell gross
    stamp_duty_buy_pct: float = 0.015            # 0.015% on buy
    exchange_txn_pct_legacy: float = 0.00345     # Prior to Oct 1, 2024
    exchange_txn_pct_revised: float = 0.00297    # From Oct 1, 2024 onward
    sebi_pct: float = 0.0001                     # 0.0001% (Rs 10/Cr)
    gst_pct: float = 18.0                        # 18% on (Brokerage + Exch + SEBI)
    dp_charge_sell: float = 14.75                # Rs 12.50 + 18% GST on Dhan delivery sell

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
    rebalance_hold_rank_buffer: int = field(default_factory=lambda: int(os.environ.get("REBALANCE_HOLD_RANK_BUFFER", "15")))
    initial_capital: float = field(default_factory=lambda: float(os.environ.get("INITIAL_CAPITAL", "100000.0")))
    portfolio_size: int = field(default_factory=lambda: int(os.environ.get("PORTFOLIO_SIZE", "6")))
    mongo_uri: str = field(default_factory=lambda: os.environ.get("MONGO_URI", "mongodb://localhost:27017/"))
    db_name: str = field(default_factory=lambda: os.environ.get("MONGO_DB", "stock_scanner"))
    telegram_enabled: bool = field(default_factory=lambda: os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true")
    telegram_bot_token: Optional[str] = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: Optional[str] = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID"))
    min_turnover_50d: float = field(default_factory=lambda: float(os.environ.get("MIN_TURNOVER_50D", "50000000.0")))
    min_price: float = field(default_factory=lambda: float(os.environ.get("MIN_PRICE", "20.0")))
    max_volume_participation_pct: float = field(default_factory=lambda: float(os.environ.get("MAX_VOLUME_PARTICIPATION_PCT", "0.10")))
    enforce_pit_universe: bool = field(default_factory=lambda: os.environ.get("ENFORCE_PIT_UNIVERSE", "true").lower() == "true")
    pit_top_n_liquidity: int = field(default_factory=lambda: int(os.environ.get("PIT_TOP_N_LIQUIDITY", "200")))
    pit_max_universe_pct: float = field(default_factory=lambda: float(os.environ.get("PIT_MAX_UNIVERSE_PCT", "0.45")))
