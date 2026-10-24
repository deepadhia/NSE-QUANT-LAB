from nse_quant_lab.registry import list_strategies, get_strategy
from nse_quant_lab.base_strategy import BaseStrategy


def test_strategy_registration():
    strats = list_strategies()
    assert "trend_momentum" in strats
    assert "donchian_breakout" in strats

    inst = get_strategy("trend_momentum")
    assert isinstance(inst, BaseStrategy)
