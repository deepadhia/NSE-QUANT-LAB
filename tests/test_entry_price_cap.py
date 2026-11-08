"""Strategy-agnostic entry-price cap (config.max_entry_price) and forward-test account aliasing (strategy_class).

Backtest-to-paper parity with the cap lives in tests/test_backtest_paper_parity.py. Synthetic fixtures only.
"""
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nse_quant_lab.config import BacktestConfig, PaperTradingConfig
from nse_quant_lab.entry_filters import apply_entry_price_cap, entry_price_cap
from nse_quant_lab.registry import strategy_class_of

ROOT = Path(__file__).resolve().parents[1]


def _cands():
    return pd.DataFrame({"close": [1500.0, 400.0, 999.99, 1000.0, 250.0], "turnover_50d": 1e9, "rank": [1, 2, 3, 4, 5]},
                        index=["EXP", "CHEAP", "EDGE", "AT_CAP", "LOW"])


def test_cap_off_by_default_everywhere_and_is_a_no_op():
    for cfg in (BacktestConfig(), PaperTradingConfig(strategy_id="x")):
        assert cfg.max_entry_price is None and entry_price_cap(cfg) is None
    c = _cands()
    assert apply_entry_price_cap(c, {}, BacktestConfig()) is c


def test_new_entries_at_or_above_cap_are_dropped_and_the_rest_re_ranked():
    out = apply_entry_price_cap(_cands(), {}, BacktestConfig(max_entry_price=1000.0))
    assert list(out.index) == ["CHEAP", "EDGE", "LOW"]                       # strictly below the cap; Rs 1,000 itself is excluded
    assert list(out["rank"]) == [1, 2, 3]


def test_held_names_are_exempt_even_above_the_cap():
    out = apply_entry_price_cap(_cands(), {"EXP": {"shares": 3}}, BacktestConfig(max_entry_price=1000.0))
    assert list(out.index) == ["EXP", "CHEAP", "EDGE", "LOW"] and list(out["rank"]) == [1, 2, 3, 4]


def test_unknown_price_is_never_treated_as_cheap_and_bad_cap_is_rejected():
    bad = _cands(); bad.loc["LOW", "close"] = np.nan
    with pytest.raises(ValueError, match="no usable close"):
        apply_entry_price_cap(bad, {}, BacktestConfig(max_entry_price=1000.0))
    with pytest.raises(ValueError, match="no 'close' column"):
        apply_entry_price_cap(_cands().drop(columns="close"), {}, BacktestConfig(max_entry_price=1000.0))
    with pytest.raises(ValueError):
        entry_price_cap(BacktestConfig(max_entry_price=-5.0))


def test_empty_candidate_frame_passes_through():
    empty = pd.DataFrame(columns=["close", "rank"])
    assert apply_entry_price_cap(empty, {}, BacktestConfig(max_entry_price=1000.0)).empty


def test_strategy_class_defaults_to_the_account_id_and_can_alias_another_class():
    assert strategy_class_of({"strategy_id": "trend_momentum_pit"}) == "trend_momentum_pit"
    assert strategy_class_of({"strategy_id": "tmpit_fwd50k", "strategy_class": "trend_momentum_pit"}) == "trend_momentum_pit"
    assert strategy_class_of({"strategy_id": "x", "strategy_class": None}) == "x"


def test_operational_scripts_pass_the_cap_and_resolve_the_class_from_the_account_document():
    """Wiring gate: a cap or class set on the strategy document must reach the config and the strategy factory in BOTH scripts."""
    for name in ("execute_paper_orders.py", "run_daily_pipeline.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'strat_doc.get("max_entry_price")' in src and "max_entry_price=float(max_entry_price) if max_entry_price else None" in src, name
        assert "strategy_class_of(strat_doc)" in src and "get_strategy(strategy_class, cfg)" in src, name
        assert "get_strategy(strategy_id, cfg)" not in src, f"{name} must build the strategy from its class, not the account id"
    for path in ("src/nse_quant_lab/engine.py", "src/nse_quant_lab/paper/manager.py"):
        assert "apply_entry_price_cap(" in (ROOT / path).read_text(encoding="utf-8"), f"{path} must apply the shared cap"


def test_init_refuses_to_overwrite_an_existing_account(monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import manage_strategy as ms

    class Strategies:
        def find_one(self, q):
            return {"strategy_id": q["strategy_id"], "initial_capital": 100000.0, "status": "ACTIVE"}

    class Db:
        strategies = Strategies()

    monkeypatch.setattr(ms.MongoDatabase, "get_instance", classmethod(lambda cls, *a, **k: Db()))
    with pytest.raises(SystemExit) as exc:
        ms.init_strategy("trend_momentum_pit", 50000.0)
    assert exc.value.code == 2 and "REFUSED" in capsys.readouterr().out
