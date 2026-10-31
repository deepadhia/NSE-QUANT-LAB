"""PIT loader: joining live Upstox bars onto the bhavcopy panel (synthetic panel, injected live source; no network, no Mongo)."""
import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.data_loader import load_pit_universe

PANEL_LAST = pd.Timestamp("2026-10-30")
NEXT_DAY = "2026-11-02"  # Monday, an NSE trading day


def make_panel(tmp_path):
    dates = pd.bdate_range(end=PANEL_LAST, periods=300)
    frames = []
    for sym in ("AAA", "BBB"):
        close = 200.0 + np.arange(300) * 0.1
        frames.append(pd.DataFrame({"symbol": sym, "date": dates, "series": "EQ", "open": close, "high": close + 1, "low": close - 1,
                                    "close": close, "volume": 1_000_000.0, "turnover": close * 1_000_000.0}))
    p = tmp_path / "panel.parquet"
    pd.concat(frames, ignore_index=True).to_parquet(p)
    return p


def live_rows(sym, anchor_close, new_close=210.0):
    return [{"symbol": sym, "date": PANEL_LAST.strftime("%Y-%m-%d"), "open": anchor_close, "high": anchor_close, "low": anchor_close,
             "close": anchor_close, "volume": 1_000_000.0},
            {"symbol": sym, "date": NEXT_DAY, "open": new_close, "high": new_close + 1, "low": new_close - 1, "close": new_close,
             "volume": 900_000.0}]


def source(rows):
    return lambda f, t: pd.DataFrame(rows)


def test_new_session_is_appended_with_turnover_and_series(tmp_path):
    p = make_panel(tmp_path)
    anchor = {s: 229.9 for s in ("AAA", "BBB")}  # panel last close = 200 + 299*0.1 = 229.9
    rows = live_rows("AAA", 229.9) + live_rows("BBB", 229.9)
    u = load_pit_universe(NEXT_DAY, panel_path=str(p), live_source=source(rows))
    a = u["AAA"]
    assert a["date"].iloc[-1] == pd.Timestamp(NEXT_DAY) and len(a) == 301
    assert a["turnover"].iloc[-1] == pytest.approx(210.0 * 900_000.0) and a["series"].iloc[-1] == "EQ"
    assert list(a.columns) == ["symbol", "date", "series", "open", "high", "low", "close", "volume", "turnover"]


def test_history_is_rescaled_when_anchor_shows_a_split_but_not_for_small_differences(tmp_path):
    p = make_panel(tmp_path)
    rows = live_rows("AAA", 229.9 * 0.5, new_close=105.0) + live_rows("BBB", 229.9 * 0.99)
    u = load_pit_universe(NEXT_DAY, panel_path=str(p), live_source=source(rows))
    assert u["AAA"]["close"].iloc[298] == pytest.approx((200.0 + 298 * 0.1) * 0.5)    # history halved
    assert u["AAA"]["volume"].iloc[0] == pytest.approx(2_000_000.0)                  # share count doubled
    assert u["BBB"]["close"].iloc[298] == pytest.approx(200.0 + 298 * 0.1)           # 1% close difference: untouched


def test_missing_session_coverage_fails_loudly(tmp_path):
    p = make_panel(tmp_path)
    with pytest.raises(ValueError, match="live data incomplete"):
        load_pit_universe(NEXT_DAY, panel_path=str(p), live_source=source(live_rows("AAA", 229.9)))  # BBB has no bar


def test_no_live_source_is_touched_when_panel_already_covers_the_date(tmp_path):
    p = make_panel(tmp_path)

    def boom(f, t):
        raise AssertionError("live source must not be called")

    u = load_pit_universe("2026-10-29", panel_path=str(p), live_source=boom)
    assert u["AAA"]["date"].iloc[-1] == pd.Timestamp("2026-10-29")


def test_future_date_without_live_data_allowed_raises(tmp_path):
    p = make_panel(tmp_path)
    with pytest.raises(ValueError):
        load_pit_universe(NEXT_DAY, panel_path=str(p), include_live=False)
