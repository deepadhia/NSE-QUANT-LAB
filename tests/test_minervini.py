"""
Unit tests (synthetic, hand-checkable) for MinerviniStrategy.
Verifies Stage 2 Trend Template alignment, 52-week filters, RS rank,
Champion / Hold rule, daily SMA150 exit, and zero-lookahead temporal correctness.
"""

from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.registry import get_strategy
from nse_quant_lab.strategies.minervini import MinerviniStrategy

REPO = Path(__file__).resolve().parents[1]
N = 420
DATES = pd.bdate_range("2023-01-02", periods=N)


def make_df(drift, series="EQ", volume=1_000_000, seed=0, start=100.0):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + 0.003 * rng.standard_normal(N)))
    return pd.DataFrame({
        "date": DATES,
        "open": close * 0.999,
        "high": close * 1.004,
        "low": close * 0.996,
        "close": close,
        "volume": float(volume),
        "turnover": close * volume,
        "series": series,
    })


def bench(up=True):
    px = np.exp(np.cumsum(np.full(N, 0.001 if up else -0.001))) * 1000
    return pd.Series(px, index=DATES)


def build(universe_raw, benchmark, **kw):
    strat = MinerviniStrategy(BacktestConfig(), benchmark=benchmark, **kw)
    uni = {s: strat.compute_indicators(df, strat.config) for s, df in universe_raw.items()}
    return strat, uni


RAW = {
    "UPEQ": make_df(0.008, "EQ", seed=1),                    # Highest return leader (RS rank = 100%)
    "UPBE": make_df(0.004, "BE", seed=2),                    # Series BE (ineligible)
    "UPILLIQ": make_df(0.003, "EQ", volume=500, seed=3),     # Turnover below Rs 10 Cr
    "DOWN": make_df(-0.003, "EQ", seed=4),                   # Downtrend
}
D = DATES[-1]


def test_registry_registration():
    strat = get_strategy("minervini_trend_template")
    assert isinstance(strat, MinerviniStrategy)
    assert strat.name == "minervini_trend_template"
    assert strat.rebalance_frequency == "MONTHLY"


def test_candidate_filters():
    strat, uni = build(RAW, bench(True))
    c = strat.select_entry_candidates(uni, D, {}, strat.config)
    assert list(c.index) == ["UPEQ"]
    assert c.loc["UPEQ", "rank"] == 1


def test_regime_filter_blocks_new_entries():
    strat, uni = build(RAW, bench(False))  # Bear regime
    c = strat.select_entry_candidates(uni, D, {}, strat.config)
    assert c.empty


def test_champion_rule_preserves_held_position():
    strat, uni = build(RAW, bench(False))  # Bear regime blocks new buys
    holdings = {"UPEQ": {"shares": 100, "entry_price": 100.0}}
    c = strat.select_entry_candidates(uni, D, holdings, strat.config)
    # Held stock bypasses bear gate as long as close >= sma150
    assert "UPEQ" in c.index


def test_daily_exit_below_sma150():
    strat, uni = build(RAW, bench(True))
    # UPEQ close is well above sma150 -> no exit
    exits = strat.evaluate_daily_exits(uni, D, {"UPEQ": {"shares": 100}}, strat.config)
    assert "UPEQ" not in exits

    # DOWN close is below sma150 -> exit
    exits_down = strat.evaluate_daily_exits(uni, D, {"DOWN": {"shares": 100}}, strat.config)
    assert exits_down.get("DOWN") == "STRUCTURAL_SMA150"


def test_no_lookahead_temporal_invariance():
    strat, uni = build(RAW, bench(True))
    dec = DATES[-30]
    full = strat.select_entry_candidates(uni, dec, {}, strat.config)
    cut = {s: df[df["date"] <= dec].reset_index(drop=True) for s, df in uni.items()}
    trunc = strat.select_entry_candidates(cut, dec, {}, strat.config)
    pd.testing.assert_frame_equal(full, trunc)
