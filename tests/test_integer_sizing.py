import pandas as pd
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.data_loader import make_synthetic_stock


def test_integer_shares_and_unaffordable_policy():
    cfg = BacktestConfig(initial_capital=100_000.0, portfolio_size=12)
    # Stock 1: Rs 15,000 price (unaffordable with Rs 8,333 slot)
    df_exp = make_synthetic_stock("EXP.NS", n_days=280, start_price=15000.0, daily_ret=0.003, start_date="2018-01-01")
    # Stock 2: Rs 100 price (affordable)
    df_aff = make_synthetic_stock("CHEAP.NS", n_days=280, start_price=100.0, daily_ret=0.002, start_date="2018-01-01")

    universe = {"EXP.NS": df_exp, "CHEAP.NS": df_aff}
    strat = TrendMomentumStrategy(cfg)
    engine = BacktestEngine(strategy=strat, config=cfg)

    metrics, trades, orders, execs, nav = engine.run(
        universe, eval_start_date="2019-01-01", eval_end_date="2019-01-10"
    )

    bought_syms = execs[execs["action"] == "BUY"]["symbol"].tolist()
    assert "EXP.NS" not in bought_syms
    assert "CHEAP.NS" in bought_syms
    for sh in execs["shares"]:
        assert float(sh).is_integer() and int(sh) > 0
