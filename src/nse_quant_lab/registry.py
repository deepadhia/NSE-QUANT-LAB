"""
registry.py -- Strategy Registry & Pluggability Harness
"""

from typing import Dict, Type, List, Optional
from .base_strategy import BaseStrategy

_STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {}


def register_strategy(name: str):
    """Decorator to register a strategy class into the universal harness."""
    def decorator(cls: Type[BaseStrategy]):
        _STRATEGY_REGISTRY[name.lower()] = cls
        cls.name = name
        return cls
    return decorator


def get_strategy(name: str, *args, **kwargs) -> BaseStrategy:
    """Instantiates a registered strategy by name."""
    key = name.lower()
    if key not in _STRATEGY_REGISTRY:
        raise KeyError(f"Strategy '{name}' not found in registry. Available: {list(_STRATEGY_REGISTRY.keys())}")
    return _STRATEGY_REGISTRY[key](*args, **kwargs)


def strategy_class_of(strat_doc: dict) -> str:
    """Registry key of the strategy CLASS behind a paper account. An account (the `strategy_id` that namespaces orders, positions, cash and the
    portfolio lease) normally IS its class; a forward-test account of the same class at another capital/slot count/price cap sets
    `strategy_class` in its strategy document (e.g. strategy_id 'tmpit_fwd50k', strategy_class 'trend_momentum_pit')."""
    return str(strat_doc.get("strategy_class") or strat_doc["strategy_id"])


def list_strategies() -> List[str]:
    """Returns list of all registered strategy identifiers."""
    return sorted(list(_STRATEGY_REGISTRY.keys()))
