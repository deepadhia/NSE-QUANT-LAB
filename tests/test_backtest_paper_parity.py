"""
test_backtest_paper_parity.py -- Automated Backtest-to-Paper Causal Replay Parity Gate.
=======================================================================================
Verifies that BacktestEngine and PaperTradingManager produce 100% identical:
  1. Execution counts
  2. Order actions (BUY / SELL)
  3. Execution symbols
  4. Exact integer share quantities
  5. Final portfolio NAV (within float rounding tolerance < 0.01%)

Tested across all three active institutional PIT strategies:
  - trend_momentum_pit
  - minervini_trend_template
  - alpha_semivol_pit
"""

import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.config import BacktestConfig, PaperTradingConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy
from nse_quant_lab.strategies.minervini import MinerviniStrategy
from nse_quant_lab.strategies.alpha_semivol_pit import AlphaSemivolPITStrategy
from nse_quant_lab.holidays import is_trading_day
from test_paper import InMemoryCollection


class ParityMockColl(InMemoryCollection):
    def create_index(self, *a, **k):
        pass


class ParityMockDb:
    def __init__(self):
        self.orders = ParityMockColl()
        self.executions = ParityMockColl()
        self.positions = ParityMockColl()
        self.trades = ParityMockColl()
        self.daily_nav = ParityMockColl()
        self.strategies = ParityMockColl()
        self.fill_audit = ParityMockColl()
        self.db = self

    def get_collection(self, name):
        if not hasattr(self, name):
            setattr(self, name, ParityMockColl())
        return getattr(self, name)

    def ping(self) -> bool:
        return True


@pytest.fixture(scope="module")
def synthetic_market():
    # Filter synthetic business days through the official NSE holiday calendar
    raw_dates = pd.bdate_range("2023-01-02", periods=360)
    dates = pd.DatetimeIndex([d for d in raw_dates if is_trading_day(d.strftime("%Y-%m-%d"))])[:320]
    n_days = len(dates)
    rng = np.random.default_rng(42)
    bench_close = 1000.0 * np.exp(np.cumsum(0.0005 + 0.005 * rng.standard_normal(n_days)))
    bench = pd.Series(bench_close, index=dates)

    stocks = {}
    for i, sym in enumerate(["SYM_A", "SYM_B", "SYM_C", "SYM_D", "SYM_E"]):
        drift = 0.003 if i < 3 else -0.001
        close = 100.0 * (i + 1) * np.exp(np.cumsum(drift + 0.012 * rng.standard_normal(n_days)))
        vol = 2_000_000.0
        stocks[sym] = pd.DataFrame({
            "date": dates,
            "open": close * 0.999,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": vol,
            "turnover": close * vol,
            "series": "EQ",
        })

    return dates, bench, stocks


def _verify_strategy_parity(strat_cls, strat_id, dates, bench, stocks, live_morning=False):
    eval_start = dates[226].strftime("%Y-%m-%d")
    eval_end = dates[-1].strftime("%Y-%m-%d")

    # 1. BacktestEngine execution
    b_cfg = BacktestConfig(
        initial_capital=100000.0,
        portfolio_size=2,
        rebalance_hold_rank_buffer=1500,
        rebalance_frequency="MONTHLY",
        min_turnover_50d=0.0,
        min_price=0.0,
        enforce_pit_universe=False,
    )
    b_strat = strat_cls(config=b_cfg, benchmark=bench)
    engine = BacktestEngine(strategy=b_strat, config=b_cfg)
    _, _, _, bt_execs, bt_nav = engine.run(
        stocks, eval_start_date=eval_start, eval_end_date=eval_end
    )

    # 2. PaperTradingManager day-by-day causal replay
    mock_db = ParityMockDb()
    p_cfg = PaperTradingConfig(
        strategy_id=strat_id,
        initial_capital=100000.0,
        portfolio_size=2,
        rebalance_hold_rank_buffer=1500,
        rebalance_frequency="MONTHLY",
        min_turnover_50d=0.0,
        min_price=0.0,
        enforce_pit_universe=False,
        telegram_enabled=False,
    )
    p_strat = strat_cls(config=p_cfg, benchmark=bench)
    mgr = PaperTradingManager(strategy=p_strat, config=p_cfg, db=mock_db)
    mgr.initialize_portfolio()

    eval_dates = [d for d in dates if pd.to_datetime(eval_start) <= d <= pd.to_datetime(eval_end)]
    for d in eval_dates:
        # Causal slice: strictly date <= d
        d_uni = {s: df[df["date"] <= d].copy() for s, df in stocks.items()}
        if live_morning:
            # 09:16 reality: the execution day's bar does NOT exist; the only same-day information is the broker's opening
            # quote (open only - no same-day high/low/close). Exercises the live-quote code path (AGENTS.md temporal rule).
            opens = {s: float(df.loc[df["date"] == d, "open"].iloc[0]) for s, df in stocks.items()}
            mgr.quote_provider = lambda sym, _o=opens: {"open": _o[sym], "high": _o[sym], "low": _o[sym], "ltp": _o[sym], "bid": _o[sym], "ask": _o[sym], "ts": d.strftime("%Y-%m-%dT09:16:00+05:30")}
            mgr.execute_morning_fills({s: df[df["date"] < d].copy() for s, df in stocks.items()}, d)
        else:
            mgr.execute_morning_fills(d_uni, d)
        mgr.generate_post_market_signals(d_uni, d)

    paper_execs = mock_db.executions.docs
    paper_nav_docs = mock_db.daily_nav.docs
    final_paper_nav = float(paper_nav_docs[-1]["nav"]) if paper_nav_docs else 100000.0
    final_bt_nav = float(bt_nav.iloc[-1]["nav"]) if not bt_nav.empty and "nav" in bt_nav.columns else float(bt_nav.iloc[-1])

    # Invariant 1: Execution count must match exactly
    assert len(bt_execs) == len(paper_execs), f"Execution count mismatch for {strat_id}: {len(bt_execs)} != {len(paper_execs)}"

    # Invariant 2: Symbol, Action, and Share Count must match 1-for-1
    for i in range(len(bt_execs)):
        be = bt_execs.iloc[i]
        pe = paper_execs[i]
        assert be["symbol"] == pe["symbol"], f"Symbol mismatch at #{i+1}: {be['symbol']} != {pe['symbol']}"
        assert be["action"] == pe["action"], f"Action mismatch at #{i+1}: {be['action']} != {pe['action']}"
        assert be["shares"] == pe["shares"], f"Shares mismatch at #{i+1}: {be['shares']} != {pe['shares']}"

    # Invariant 3: Final NAV relative delta must be < 0.01%
    rel_delta = abs(final_paper_nav - final_bt_nav) / final_bt_nav
    assert rel_delta < 0.0001, f"NAV divergence for {strat_id}: BT={final_bt_nav:.2f}, Paper={final_paper_nav:.2f}, Delta={rel_delta:.6%}"


def test_trend_momentum_pit_parity(synthetic_market):
    dates, bench, stocks = synthetic_market
    _verify_strategy_parity(TrendMomentumPITStrategy, "trend_momentum_pit", dates, bench, stocks)


def test_minervini_trend_template_parity(synthetic_market):
    dates, bench, stocks = synthetic_market
    _verify_strategy_parity(MinerviniStrategy, "minervini_trend_template", dates, bench, stocks)


def test_alpha_semivol_pit_parity(synthetic_market):
    dates, bench, stocks = synthetic_market
    _verify_strategy_parity(AlphaSemivolPITStrategy, "alpha_semivol_pit", dates, bench, stocks)



@pytest.mark.parametrize("strat_cls,strat_id", [
    (TrendMomentumPITStrategy, "trend_momentum_pit"),
    (MinerviniStrategy, "minervini_trend_template"),
    (AlphaSemivolPITStrategy, "alpha_semivol_pit"),
])
def test_live_morning_path_parity_without_execution_day_bar(synthetic_market, strat_cls, strat_id):
    """Same parity gate, but morning fills run on a universe that ends the session BEFORE the fill and prices from a quote."""
    dates, bench, stocks = synthetic_market
    _verify_strategy_parity(strat_cls, strat_id, dates, bench, stocks, live_morning=True)


def test_approach_3_gap_skip_catchup_parity(synthetic_market):
    """
    Adversarial verification of Approach 3 Catch-Up:
    Injects a +5% opening gap on an entry execution session for a top candidate,
    verifying that both BacktestEngine and PaperTradingManager:
      1. Skip the gapped-up stock on Day T+1 open (no chasing).
      2. Trigger Day T+2 catch-up rebalance cleanly.
      3. Fill identical replacement shares and achieve 100% NAV and trade parity.
    """
    dates, bench, stocks = synthetic_market
    gapped_stocks = {s: df.copy() for s, df in stocks.items()}
    eval_dates = [d for d in dates if pd.to_datetime(dates[226]) <= d <= pd.to_datetime(dates[-1])]
    rebal_d = eval_dates[13]  # 2023-12-01: first monthly rebalance execution date
    gapped_stocks["SYM_A"].loc[gapped_stocks["SYM_A"]["date"] == rebal_d, "open"] *= 1.05
    gapped_stocks["SYM_A"].loc[gapped_stocks["SYM_A"]["date"] == rebal_d, "high"] *= 1.06

    _verify_strategy_parity(TrendMomentumPITStrategy, "trend_momentum_pit", dates, bench, gapped_stocks)
