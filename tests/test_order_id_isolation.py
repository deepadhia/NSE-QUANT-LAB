"""
test_order_id_isolation.py -- Strategies sharing one `orders` collection must never overwrite each other's orders.

Regression for audit finding C1: order IDs were `ORD_BUY_{date}_{sym}` with no strategy_id and the upsert filtered on
order_id alone, so a second strategy staging the same symbol on the same date re-owned the first strategy's order.
"""

import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.holidays import is_trading_day
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.strategies.alpha_semivol_pit import AlphaSemivolPITStrategy
from nse_quant_lab.strategies.minervini import MinerviniStrategy
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy
from test_backtest_paper_parity import ParityMockDb


@pytest.fixture(scope="module")
def market():
    raw = pd.bdate_range("2023-01-02", periods=360)
    dates = pd.DatetimeIndex([d for d in raw if is_trading_day(d.strftime("%Y-%m-%d"))])[:320]
    n = len(dates)
    rng = np.random.default_rng(42)
    bench = pd.Series(1000.0 * np.exp(np.cumsum(0.0005 + 0.005 * rng.standard_normal(n))), index=dates)
    stocks = {}
    for i, sym in enumerate(["SYM_A", "SYM_B", "SYM_C", "SYM_D", "SYM_E"]):
        c = 100.0 * (i + 1) * np.exp(np.cumsum((0.003 if i < 3 else -0.001) + 0.012 * rng.standard_normal(n)))
        stocks[sym] = pd.DataFrame({
            "date": dates, "open": c * 0.999, "high": c * 1.005, "low": c * 0.995, "close": c,
            "volume": 2_000_000.0, "turnover": c * 2_000_000.0, "series": "EQ",
        })
    return dates, bench, stocks


def _manager(cls, strategy_id, bench, db):
    cfg = PaperTradingConfig(strategy_id=strategy_id, initial_capital=100000.0, portfolio_size=2,
                             rebalance_hold_rank_buffer=1500, telegram_enabled=False)
    mgr = PaperTradingManager(cls(config=cfg, benchmark=bench), cfg, db)
    mgr.initialize_portfolio()
    return mgr


def test_two_strategies_same_symbol_same_date_keep_separate_orders(market):
    dates, bench, stocks = market
    db = ParityMockDb()
    alpha = _manager(AlphaSemivolPITStrategy, "alpha_semivol_pit", bench, db)
    mini = _manager(MinerviniStrategy, "minervini_trend_template", bench, db)

    overlap_checked = False
    for d in dates[226:]:
        uni = {s: df[df["date"] <= d].copy() for s, df in stocks.items()}
        ra = alpha.generate_post_market_signals(uni, d)
        rb = mini.generate_post_market_signals(uni, d)
        shared = {b["symbol"] for b in ra["buys"]} & {b["symbol"] for b in rb["buys"]}
        if not shared:
            continue
        overlap_checked = True
        date_str = d.strftime("%Y-%m-%d")
        for sym in shared:
            owners = sorted(
                o["strategy_id"] for o in db.orders.docs
                if o["symbol"] == sym and o["signal_date"] == date_str and o["action"] == "BUY"
            )
            assert owners == ["alpha_semivol_pit", "minervini_trend_template"], f"{sym} on {date_str}: {owners}"
        break
    assert overlap_checked, "fixture produced no date where both strategies buy the same symbol; test is vacuous"

    ids = [o["order_id"] for o in db.orders.docs]
    assert len(ids) == len(set(ids)), "duplicate order_id in orders collection"
    for o in db.orders.docs:
        assert o["strategy_id"] in o["order_id"], f"order_id not namespaced by strategy: {o['order_id']}"


def test_same_strategy_rerun_is_idempotent(market):
    dates, bench, stocks = market
    db = ParityMockDb()
    alpha = _manager(AlphaSemivolPITStrategy, "alpha_semivol_pit", bench, db)
    for d in dates[226:]:
        uni = {s: df[df["date"] <= d].copy() for s, df in stocks.items()}
        res = alpha.generate_post_market_signals(uni, d)
        if res["buys"]:
            before = sorted(o["order_id"] for o in db.orders.docs)
            alpha.generate_post_market_signals(uni, d)
            after = sorted(o["order_id"] for o in db.orders.docs)
            assert before == after
            return
    pytest.fail("no rebalance date with buys; test is vacuous")


def test_all_three_strategies_share_one_db_without_collisions(market):
    dates, bench, stocks = market
    db = ParityMockDb()
    mgrs = {
        "alpha_semivol_pit": _manager(AlphaSemivolPITStrategy, "alpha_semivol_pit", bench, db),
        "minervini_trend_template": _manager(MinerviniStrategy, "minervini_trend_template", bench, db),
        "trend_momentum_pit": _manager(TrendMomentumPITStrategy, "trend_momentum_pit", bench, db),
    }
    expected = set()
    for d in dates[226:]:
        uni = {s: df[df["date"] <= d].copy() for s, df in stocks.items()}
        for sid, m in mgrs.items():
            for b in m.generate_post_market_signals(uni, d)["buys"]:
                expected.add((sid, d.strftime("%Y-%m-%d"), b["symbol"]))
    assert expected, "no buys staged; test is vacuous"
    staged = {(o["strategy_id"], o["signal_date"], o["symbol"]) for o in db.orders.docs if o["action"] == "BUY"}
    # every BUY a strategy reported must exist under that strategy (later dates may delete older PENDING buys, so subset check on last date)
    last = max(d for _, d, _ in expected)
    assert {e for e in expected if e[1] == last} <= staged
    ids = [o["order_id"] for o in db.orders.docs]
    assert len(ids) == len(set(ids))
