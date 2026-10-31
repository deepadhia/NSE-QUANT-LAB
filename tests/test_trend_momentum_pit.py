"""Unit tests (synthetic, hand-checkable) and a real-data parity test for TrendMomentumPITStrategy."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy

REPO = Path(__file__).resolve().parents[1]
N = 420
DATES = pd.bdate_range("2023-01-02", periods=N)


def make_df(drift, series="EQ", volume=1_000_000, seed=0, start=100.0):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + 0.004 * rng.standard_normal(N)))
    return pd.DataFrame({
        "date": DATES, "open": close * 0.999, "high": close * 1.004, "low": close * 0.996, "close": close,
        "volume": float(volume), "turnover": close * volume, "series": series,
    })


def bench(up=True):
    px = np.exp(np.cumsum(np.full(N, 0.001 if up else -0.001))) * 1000
    return pd.Series(px, index=DATES)


def build(universe_raw, benchmark, **kw):
    strat = TrendMomentumPITStrategy(BacktestConfig(), benchmark=benchmark, **kw)
    uni = {s: strat.compute_indicators(df, strat.config) for s, df in universe_raw.items()}
    return strat, uni


RAW = {
    "UPEQ": make_df(0.004, "EQ", seed=1),
    "UPBE": make_df(0.004, "BE", seed=2),                  # wrong series
    "UPILLIQ": make_df(0.004, "EQ", volume=500, seed=3),   # turnover below the Rs 10 Cr floor
    "UPMID": make_df(0.004, "EQ", volume=50_000, seed=5),  # ~Rs 2.5 Cr: passes the old Rs 25 L floor, fails Rs 10 Cr
    "DOWN": make_df(-0.002, "EQ", seed=4),                 # no uptrend
}
D = DATES[-1]


def test_candidate_filters():
    strat, uni = build(RAW, bench(True))
    c = strat.select_entry_candidates(uni, D, {}, strat.config)
    assert list(c.index) == ["UPEQ"]
    assert c.loc["UPEQ", "rank"] == 1


def test_no_lookahead_when_future_bars_removed():
    strat, uni = build(RAW, bench(True))
    dec = DATES[-30]
    full = strat.select_entry_candidates(uni, dec, {}, strat.config)
    cut = {s: df[df["date"] <= dec].reset_index(drop=True) for s, df in uni.items()}
    trunc = strat.select_entry_candidates(cut, dec, {}, strat.config)
    pd.testing.assert_frame_equal(full, trunc)


def test_regime_blocks_new_entries_but_keeps_qualifying_holdings():
    strat, uni = build(RAW, bench(False))
    assert strat.regime_below(D) is True
    assert strat.select_entry_candidates(uni, D, {}, strat.config).empty
    held = strat.select_entry_candidates(uni, D, {"UPEQ": {}}, strat.config)
    assert list(held.index) == ["UPEQ"]


def test_missing_benchmark_date_fails_safe():
    short = bench(True).iloc[:-5]  # decision date absent from the benchmark
    strat, uni = build(RAW, short)
    assert strat.regime_below(D) is True
    assert strat.select_entry_candidates(uni, D, {}, strat.config).empty


def test_regime_off_ignores_benchmark():
    strat, uni = build(RAW, bench(False), use_regime=False)
    assert list(strat.select_entry_candidates(uni, D, {}, strat.config).index) == ["UPEQ"]


def test_daily_exit_on_close_below_ema100():
    strat, uni = build(RAW, bench(True))
    bar_ok = strat.evaluate_daily_exits(uni, D, {"UPEQ": {}}, strat.config)
    assert bar_ok == {}
    bad = uni["UPEQ"].copy()
    bad.loc[bad.index[-1], "close"] = float(bad["ema100"].iloc[-1]) * 0.9
    assert strat.evaluate_daily_exits({"UPEQ": bad}, D, {"UPEQ": {}}, strat.config) == {"UPEQ": "STRUCTURAL_EMA100"}


def test_adt_prev_is_lagged_one_session():
    strat, uni = build(RAW, bench(True))
    df = uni["UPEQ"]
    assert df["adt_prev"].iloc[100] == pytest.approx(df["adt50m"].iloc[99])


def test_slippage_hook_is_liquidity_aware_and_haircuts_delistings():
    strat, uni = build(RAW, bench(True), delist_haircut=0.3)
    liquid = uni["UPEQ"].iloc[-1]
    illiq = uni["UPILLIQ"].iloc[-1]
    assert strat.slippage_pct_for("UPILLIQ", illiq, 100.0, 100, "BUY") > strat.slippage_pct_for("UPEQ", liquid, 100.0, 100, "BUY")
    base = strat.slippage_pct_for("UPEQ", liquid, 100.0, 100, "SELL")
    assert strat.slippage_pct_for("UPEQ", liquid, 100.0, 100, "SELL_DELIST") == pytest.approx(base + 0.3)


@pytest.mark.skipif(
    not (REPO / "data/external/nse_archive/clean_panel.parquet").exists()
    or not (REPO / "data/external/nse_archive/lab_universe.parquet").exists(),
    reason="research panel not present",
)
def test_parity_with_research_harness_on_real_data():
    """The production strategy must pick exactly the same names, in the same order, as the research harness."""
    sys.path.insert(0, str(REPO / "research" / "audit_2026-10" / "scripts"))
    try:
        import alpha_lab
        bench_real = alpha_lab.load_bench()
    except Exception as exc:  # no Mongo
        pytest.skip(f"benchmark unavailable: {exc}")
    from nse_quant_lab.data_loader import load_pit_universe

    lab = pd.read_parquet(REPO / "data/external/nse_archive/lab_universe.parquet")
    syms = sorted(lab["symbol"].unique())
    rng = np.random.default_rng(7)
    subset = set(rng.choice(syms, 300, replace=False)) | {"RELIANCE", "TRENT", "DIXON", "TITAGARH"}
    lab_u = {s: g.reset_index(drop=True) for s, g in lab[lab["symbol"].isin(subset)].groupby("symbol", sort=False)}
    del lab
    prod_raw = {s: g for s, g in load_pit_universe("2024-12-31").items() if s in subset}
    cfg = BacktestConfig()
    prod = TrendMomentumPITStrategy(cfg, benchmark=bench_real)
    prod_u = {s: prod.compute_indicators(g, cfg) for s, g in prod_raw.items()}
    research = alpha_lab.LabStrategy("S0", "UH", "R1", bench_real, cfg)
    dates = sorted({d for df in prod_u.values() for d in df["date"]})
    me = pd.Series(dates, index=pd.DatetimeIndex(dates)).groupby(pd.DatetimeIndex(dates).to_period("M")).max()
    checked = 0
    for dec in [me.loc["2016-12"], me.loc["2019-03"], me.loc["2021-06"], me.loc["2024-11"]]:
        a = research.select_entry_candidates(lab_u, dec, {}, cfg)
        b = prod.select_entry_candidates(prod_u, dec, {}, cfg)
        assert list(a.index) == list(b.index), f"candidate mismatch on {dec.date()}"
        checked += len(a)
    assert checked > 0
