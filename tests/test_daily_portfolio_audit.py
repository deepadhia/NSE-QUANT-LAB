import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.audit_daily_portfolio import compute_slippage_metrics, audit_strategy_for_date
from nse_quant_lab.db.models import DailyPortfolioAuditModel


def test_compute_slippage_metrics_empty():
    res = compute_slippage_metrics([])
    assert res["fill_count"] == 0
    assert res["verdict"] == "NO_FILLS_RECORDED"


def test_compute_slippage_metrics_conservative():
    fills = [
        {"model_slip_bps": 10.0, "half_spread_bps": 5.0, "quote_vs_open_bps": -20.0},
        {"model_slip_bps": 8.0, "half_spread_bps": 4.0, "quote_vs_open_bps": -10.0},
    ]
    res = compute_slippage_metrics(fills)
    assert res["fill_count"] == 2
    assert res["avg_model_slip_bps"] == 9.0
    assert res["avg_observed_half_spread_bps"] == 4.5
    assert res["avg_quote_vs_open_bps"] == -15.0
    assert res["verdict"] == "CONSERVATIVE"


def test_compute_slippage_metrics_optimistic():
    fills = [
        {"model_slip_bps": 3.0, "half_spread_bps": 12.0, "quote_vs_open_bps": 30.0},
    ]
    res = compute_slippage_metrics(fills)
    assert res["fill_count"] == 1
    assert res["verdict"] == "OPTIMISTIC_UNDERESTIMATED"


def test_daily_portfolio_audit_model_to_dict():
    model = DailyPortfolioAuditModel(
        strategy_id="test_strat",
        mode="paper",
        date="2026-11-06",
        nav=100000.0,
        cash=10000.0,
        holdings_val=90000.0,
        open_positions_count=5,
        unrealized_pnl=500.0,
        unrealized_pnl_pct=0.5,
        positions=[{"symbol": "TEST", "pnl_pct": 2.5}],
        daily_fills=[],
        slippage_summary={"session": {}, "cumulative": {}},
    )
    d = model.to_dict()
    assert d["strategy_id"] == "test_strat"
    assert d["nav"] == 100000.0
    assert len(d["positions"]) == 1
    assert "created_at" in d
