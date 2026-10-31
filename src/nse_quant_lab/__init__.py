"""
NSE-Quant-Lab -- Institutional Quantitative Backtesting & Paper Trading Harness
"""

from .config import BacktestConfig, PaperTradingConfig
from .engine import BacktestEngine
from .registry import register_strategy, get_strategy, list_strategies
from .strategies.trend_momentum import TrendMomentumStrategy
from .strategies.donchian import DonchianBreakoutStrategy
from .strategies.trend_momentum_pit import TrendMomentumPITStrategy

__version__ = "1.0.0"
__all__ = [
    "BacktestConfig",
    "PaperTradingConfig",
    "BacktestEngine",
    "register_strategy",
    "get_strategy",
    "list_strategies",
    "TrendMomentumStrategy",
    "DonchianBreakoutStrategy",
    "TrendMomentumPITStrategy",
]
