"""
Unit tests (synthetic, hand-checkable) for AlphaSemivolPITStrategy.
Verifies Sortino Momentum ranking, Stage-2 MA stack, Champion / Hold rule,
daily SMA150 exit, regime protection, zero-lookahead temporal correctness,
raw_mom_6m parity, and mom12_1 NaN fallback.
"""

from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.config import BacktestConfig, PaperTradingConfig
from nse_quant_lab.registry import get_strategy
from nse_quant_lab.strategies.alpha_semivol_pit import AlphaSemivolPITStrategy
from nse_quant_lab.paper.manager import PaperTradingManager

REPO = Path(__file__).resolve().parents[1]
N = 420
DATES = pd.bdate_range("2023-01-02", periods=N)


def make_df(drift, series="EQ", volume=1_000_000, seed=0, start=100.0, periods=N):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + 0.012 * rng.standard_normal(periods)))
    dates = DATES[-periods:]
    return pd.DataFrame({
        "date": dates,
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
    strat = AlphaSemivolPITStrategy(BacktestConfig(), benchmark=benchmark, **kw)
    uni = {s: strat.compute_indicators(df, strat.config) for s, df in universe_raw.items()}
    return strat, uni


RAW = {
    "LEADER": make_df(0.0035, "EQ", seed=1),                   # Strong smooth uptrend (high Sortino)
    "RUNNER": make_df(0.0025, "EQ", seed=2),                   # Uptrend runner
    "UPBE": make_df(0.0035, "BE", seed=3),                     # BE series (ineligible)
    "UPILLIQ": make_df(0.0035, "EQ", volume=500, seed=4),      # Turnover below Rs 10 Cr floor
    "DOWN": make_df(-0.0020, "EQ", seed=5),                    # Downtrend (fails Stage-2)
}
D = DATES[-1]


def test_registry_registration():
    strat = get_strategy("alpha_semivol_pit")
    assert isinstance(strat, AlphaSemivolPITStrategy)
    assert strat.name == "alpha_semivol_pit"
    assert strat.rebalance_frequency == "MONTHLY"


def test_indicator_enrichment():
    strat, uni = build(RAW, bench(True))
    df = uni["LEADER"]
    for col in ["sma150", "ma200", "raw_mom_6m", "mom_6m", "mom12_1", "semi_vol126", "adt50m", "hist_n"]:
        assert col in df.columns
    last_row = df.iloc[-1]
    assert not np.isnan(last_row["sma150"])
    assert not np.isnan(last_row["ma200"])
    assert not np.isnan(last_row["raw_mom_6m"])
    assert not np.isnan(last_row["semi_vol126"])
    assert last_row["hist_n"] == N


def test_candidate_filters():
    strat, uni = build(RAW, bench(True))
    c = strat.select_entry_candidates(uni, D, {}, strat.config)
    # BE, ILLIQ, and DOWN must be excluded
    assert "UPBE" not in c.index
    assert "UPILLIQ" not in c.index
    assert "DOWN" not in c.index
    # LEADER and RUNNER must qualify
    assert "LEADER" in c.index
    assert "RUNNER" in c.index
    # LEADER has higher Sortino score, so rank 1 < rank 2
    assert c.loc["LEADER", "rank"] < c.loc["RUNNER", "rank"]


def test_mom12_1_fallback_for_newer_listings():
    """Finding V5-2: Stocks with 226 <= History < 252 days must NOT have their momentum score halved."""
    strat = AlphaSemivolPITStrategy(BacktestConfig(), benchmark=bench(True))
    # 235 trading days: mature enough for hist_n >= 226, SMA150, MA200, raw_mom_6m, but mom12_1 is NaN
    df_235 = make_df(0.0035, "EQ", seed=1, periods=235)
    uni_new = {"NEW_LISTING": strat.compute_indicators(df_235, strat.config)}
    
    assert np.isnan(uni_new["NEW_LISTING"].iloc[-1]["mom12_1"])
    assert not np.isnan(uni_new["NEW_LISTING"].iloc[-1]["raw_mom_6m"])
    assert uni_new["NEW_LISTING"].iloc[-1]["hist_n"] == 235

    c = strat.select_entry_candidates(uni_new, D, {}, strat.config)
    assert "NEW_LISTING" in c.index
    assert c.loc["NEW_LISTING", "rank"] == 1


def test_no_lookahead_temporal_correctness():
    strat, uni = build(RAW, bench(True))
    dec = DATES[-30]
    full = strat.select_entry_candidates(uni, dec, {}, strat.config)
    cut = {s: df[df["date"] <= dec].reset_index(drop=True) for s, df in uni.items()}
    trunc = strat.select_entry_candidates(cut, dec, {}, strat.config)
    pd.testing.assert_frame_equal(full, trunc)


def test_regime_blocks_new_entries_but_keeps_qualifying_holdings():
    strat, uni = build(RAW, bench(False))
    assert strat.regime_below(D) is True
    # In bear regime, new candidates are empty
    new_cands = strat.select_entry_candidates(uni, D, {}, strat.config)
    assert new_cands.empty
    # But existing held positions qualifying above SMA150 are preserved
    held = strat.select_entry_candidates(uni, D, {"LEADER": {}}, strat.config)
    assert list(held.index) == ["LEADER"]


def test_held_stock_retained_below_entry_buffer():
    """Held stocks must NOT be dropped if Close is between SMA150 and 1.02*SMA150."""
    strat, uni = build(RAW, bench(True))
    mod_uni = {k: v.copy() for k, v in uni.items()}
    last_idx = mod_uni["LEADER"].index[-1]
    sma150_val = float(mod_uni["LEADER"].loc[last_idx, "sma150"])
    # Set price to 1.005 * SMA150 (passes hold >= sma150, but fails entry >= 1.02*sma150)
    mod_uni["LEADER"].loc[last_idx, "close"] = sma150_val * 1.005

    new_cands = strat.select_entry_candidates(mod_uni, D, {}, strat.config)
    assert "LEADER" not in new_cands.index

    held = strat.select_entry_candidates(mod_uni, D, {"LEADER": {}}, strat.config)
    assert "LEADER" in held.index


def test_daily_exit_below_sma150():
    strat, uni = build(RAW, bench(True))
    # LEADER close is well above sma150 -> no exit
    exits = strat.evaluate_daily_exits(uni, D, {"LEADER": {"shares": 100}}, strat.config)
    assert "LEADER" not in exits

    # DOWN close is below sma150 -> exit
    exits_down = strat.evaluate_daily_exits(uni, D, {"DOWN": {"shares": 100}}, strat.config)
    assert exits_down.get("DOWN") == "STRUCTURAL_SMA150"


def test_sma150_stop_extraction_in_paper_manager():
    """Finding V5-1: PaperTradingManager must extract sma150 for positions when ema100/supertrend are absent."""
    from test_paper import MockMongoDbWrapper

    mock_db = MockMongoDbWrapper()
    cfg = PaperTradingConfig(
        initial_capital=100_000.0,
        portfolio_size=2,
        strategy_id="alpha_semivol_pit",
        telegram_enabled=False,
        enforce_pit_universe=False,
    )
    strat, uni = build(RAW, bench(True))
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)

    # Seed an open position in mock DB
    mock_db.positions.insert_one({
        "strategy_id": "alpha_semivol_pit",
        "mode": "paper",
        "symbol": "LEADER",
        "shares": 100,
        "entry_price": 100.0,
        "entry_date": DATES[0].strftime("%Y-%m-%d"),
        "market_value": 10000.0,
    })

    # Run post-market signal generation which updates position prices and trailing stops
    mgr.generate_post_market_signals(uni, D)

    # Check that sma150_stop was extracted and recorded
    pos = mock_db.positions.find_one({"symbol": "LEADER"})
    assert pos is not None
    assert pos.get("sma150_stop") is not None
    assert pos.get("stop_loss_price") == pos["sma150_stop"]
