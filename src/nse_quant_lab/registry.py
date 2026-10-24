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


def list_strategies() -> List[str]:
    """Returns list of all registered strategy identifiers."""
    return sorted(list(_STRATEGY_REGISTRY.keys()))
