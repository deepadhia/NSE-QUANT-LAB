"""
test_production_invariants.py -- Static + contract gates that stop audit findings from coming back.

These tests read the SOURCE (not behaviour) so a future edit that re-introduces a known defect class fails CI immediately.
Each gate names the audit finding it guards (docs/AUDIT_2026-11_FINDINGS_AND_REGRESSION_GUARDS.md).
"""

import inspect
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "nse_quant_lab"
SCRIPTS = ROOT / "scripts"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---- C1: order identity -------------------------------------------------------------------------------------------
def test_c1_order_ids_only_built_through_namespaced_helper():
    src = _read(SRC / "paper" / "manager.py")
    helper = src[src.index("def _order_id"):src.index("def _stage_order")]
    assert not re.search(r'f"ORD_', src.replace(helper, "")), "build order IDs with PaperTradingManager._order_id (strategy-namespaced)"
    assert len(re.findall(r"orders\.replace_one\(", src)) == 1, "orders must be upserted only via _stage_order (strategy-scoped filter)"
    stage = src[src.index("def _stage_order"):src.index("def _alert")]
    assert '"strategy_id"' in stage and '"mode"' in stage and '"order_id"' in stage


# ---- H1: idempotent fills ------------------------------------------------------------------------------------------
def test_h1_exec_ids_are_deterministic_and_cash_goes_through_the_idempotent_helper():
    src = _read(SRC / "paper" / "manager.py")
    assert "uuid" not in src, "random exec ids make retries non-idempotent; use EXEC_{order_id}"
    body_outside_helper = src.replace(src[src.index("def _persist_cash"):src.index("def _cash_already_applied")], "")
    assert not re.search(r'strategies\.update_one\([^)]*\{"\$set": \{"cash"', body_outside_helper, re.S), \
        "persist cash only via _persist_cash(cash, date, exec_id) (applied-marker + balance in one atomic update)"


# ---- M3 / playbook rule 2: indicator namespace isolation -----------------------------------------------------------
def _strategy_files():
    return [p for p in (SRC / "strategies").glob("*.py") if "required_indicator_columns" in _read(p)]


def test_m3_no_generic_column_guards_in_strategy_indicator_code():
    offenders = []
    for p in _strategy_files():
        for i, line in enumerate(_read(p).splitlines(), 1):
            if re.search(r'if\s+"[A-Za-z0-9_]+"\s+not\s+in\s+(out|df|src)\.columns', line):
                offenders.append(f"{p.name}:{i}: {line.strip()}")
    assert not offenders, "strategy-specific formulas must be computed unconditionally under formula-specific names:\n" + "\n".join(offenders)


def _pit_strategies():
    from nse_quant_lab.registry import get_strategy, list_strategies
    out = []
    for sid in list_strategies():
        try:
            strat = get_strategy(sid, None)
        except Exception:
            continue
        if getattr(strat, "required_indicator_columns", None):
            out.append((sid, strat))
    return out


def _synthetic_frame(n=330, seed=3):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n)
    c = 100 * np.exp(np.cumsum(0.002 + 0.01 * rng.standard_normal(n)))
    return pd.DataFrame({"date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": 2e6, "turnover": c * 2e6, "series": "EQ"})


def test_declared_required_columns_are_actually_produced():
    strategies = _pit_strategies()
    assert strategies, "no strategies declare required_indicator_columns"
    for sid, strat in strategies:
        out = strat.compute_indicators(_synthetic_frame())
        missing = set(strat.required_indicator_columns) - set(out.columns)
        assert not missing, f"{sid}: required_indicator_columns not produced by compute_indicators: {sorted(missing)}"


def test_precomputed_generic_columns_never_change_any_strategy_ranking():
    """A cache carrying differently defined generic columns must not alter candidate ranking for ANY strategy."""
    poison_cols = ["mom_6m", "mom12_1", "semi_vol126", "sma150", "ma200", "ema100", "sma50", "sma200", "ret_1y",
                   "change_126", "w52low", "w52high", "turnover_50d"]
    for sid, strat in _pit_strategies():
        strat.use_regime = False
        clean, dirty = {}, {}
        for i, sym in enumerate("ABCDEF"):
            df = _synthetic_frame(seed=10 + i)
            clean[sym] = strat.compute_indicators(df.copy())
            p = df.copy()
            rng = np.random.default_rng(99 + i)
            for col in poison_cols:
                p[col] = rng.standard_normal(len(p)) * 50
            dirty[sym] = strat.compute_indicators(p)
        dd = clean["A"]["date"].iloc[-1]
        a = strat.select_entry_candidates(clean, dd, {}, None)
        b = strat.select_entry_candidates(dirty, dd, {}, None)
        assert list(a.index) == list(b.index), f"{sid}: ranking depends on pre-computed generic columns"


# ---- strategy wiring (playbook step 3) -----------------------------------------------------------------------------
def test_every_pit_strategy_is_wired_into_all_operational_scripts():
    wiring = ["execute_paper_orders.py", "run_daily_pipeline.py", "manage_strategy.py", "verify_production_health.py"]
    texts = {w: _read(SCRIPTS / w) for w in wiring}
    for sid, _ in _pit_strategies():
        for w, t in texts.items():
            assert f'"{sid}"' in t, f"strategy '{sid}' is not wired into scripts/{w} (see playbook step 3)"


# ---- M6: calendar failures must not be guessed around --------------------------------------------------------------
def test_m6_rebalance_calendar_has_no_silent_fallback():
    src = _read(SRC / "base_strategy.py")
    assert "day >= 26" not in src and "Conservative fallback" not in src


def test_m6_calendar_failure_surfaces(monkeypatch):
    import nse_quant_lab.holidays as hol
    from nse_quant_lab.base_strategy import BaseStrategy

    class S(BaseStrategy):
        def compute_indicators(self, df, config=None): return df
        def select_entry_candidates(self, *a, **k): return pd.DataFrame()
        def evaluate_daily_exits(self, *a, **k): return {}

    def boom(*a, **k):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(hol, "is_trading_day", boom)
    d = pd.Timestamp("2026-03-27")
    with pytest.raises(RuntimeError):
        S().is_rebalance_decision_date([d], d, None)


def test_m6_upstox_holiday_feed_filters_other_exchanges_and_settlement_holidays(monkeypatch):
    import json
    import nse_quant_lab.holidays as hol

    payload = {"data": [
        {"date": "2031-03-04", "closed_exchanges": ["NSE", "BSE"], "holiday_type": "TRADING_HOLIDAY"},
        {"date": "2031-03-05", "closed_exchanges": ["MCX"], "holiday_type": "TRADING_HOLIDAY"},
        {"date": "2031-03-06", "closed_exchanges": ["NSE"], "holiday_type": "SETTLEMENT_HOLIDAY"},
        {"date": "2032-03-04", "closed_exchanges": ["NSE"], "holiday_type": "TRADING_HOLIDAY"},
    ]}

    class Resp:
        status = 200
        def read(self): return json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.setattr(hol.urllib.request, "urlopen", lambda *a, **k: Resp())
    hol._MEM_CACHE.pop(2031, None)
    try:
        assert hol.get_nse_holidays(2031) == {"2031-03-04"}
    finally:
        hol._MEM_CACHE.pop(2031, None)


def test_static_holiday_list_is_well_formed():
    import datetime as dt
    from nse_quant_lab.holidays import STATIC_NSE_HOLIDAYS
    for h in STATIC_NSE_HOLIDAYS:
        dt.date.fromisoformat(h)   # raises on a malformed entry
    assert len(STATIC_NSE_HOLIDAYS) == len(set(STATIC_NSE_HOLIDAYS))


# ---- M7 / L1 / L3 --------------------------------------------------------------------------------------------------
def test_m7_long_term_requires_more_than_365_days():
    from nse_quant_lab.tax_india import compute_fy_tax

    def tr(days):
        return {"exit_date": "2026-01-10", "hold_days": days, "shares": 100, "entry_fill_price": 100.0,
                "exit_fill_price": 5100.0, "net_pnl": 499_960.0}

    _, _, _, d365 = compute_fy_tax(2026, [tr(365)], [], [])
    _, _, _, d366 = compute_fy_tax(2026, [tr(366)], [], [])
    assert d365["stcg_gross"] > 0 and d365["ltcg_gross"] == 0
    assert d366["ltcg_gross"] > 0 and d366["stcg_gross"] == 0


def test_l1_model_timestamps_are_evaluated_per_instance_not_at_import():
    src = _read(SRC / "db" / "models.py")
    assert not re.search(r":\s*str\s*=\s*datetime\.now", src), "use field(default_factory=...) for timestamps"
    from nse_quant_lab.db.models import OrderModel
    import time
    a = OrderModel("a", "s", "paper", "X", "BUY", 0, 1.0, "ENTRY_AT_OPEN", "PENDING_OPEN", "r", "2026-01-01")
    time.sleep(0.01)
    b = OrderModel("b", "s", "paper", "X", "BUY", 0, 1.0, "ENTRY_AT_OPEN", "PENDING_OPEN", "r", "2026-01-01")
    assert a.created_at != b.created_at


def test_l3_engine_has_no_hardcoded_default_end_date():
    from nse_quant_lab.engine import BacktestEngine
    sig = inspect.signature(BacktestEngine.run)
    assert sig.parameters["eval_end_date"].default is None


def test_production_code_has_no_hardcoded_end_date_defaults():
    pat = re.compile(r'(end_date\s*(:\s*str)?\s*=\s*"20\d\d-\d\d-\d\d")')
    for p in [SRC / "engine.py", SRC / "paper" / "manager.py", SRC / "base_strategy.py"] + list((SRC / "strategies").glob("*.py")):
        for i, line in enumerate(_read(p).splitlines(), 1):
            assert not pat.search(line), f"{p.name}:{i}: hardcoded date default"


# ---- H4/H5: operational scripts must fail loudly ---------------------------------------------------------------------
def test_h4_scheduler_guards_trust_only_successful_runs():
    src = _read(SCRIPTS / "run_scheduler_daemon.py")
    assert "succeeded_today" in src
    assert '"daily_pipeline" in self.completed_today' not in src


def test_h5_morning_script_exits_nonzero_on_unpriced_or_failed_orders():
    src = _read(SCRIPTS / "execute_paper_orders.py")
    assert "sys.exit(2)" in src and "no_quote" in src
    pipe = _read(SCRIPTS / "run_daily_pipeline.py")
    assert "sys.exit(4)" in pipe
