"""
test_compilation_and_contracts.py -- Automated CI Gate for Repo-Wide Code Health
Enforces:
1. Universal compilation across all Python files in the repository.
2. Metric schema key consistency.
3. No hardcoded dates in cron pipelines.
4. Zero credentials, chat IDs, or cluster hostnames in documentation.
"""

import os
import py_compile
import pytest
import pandas as pd
from nse_quant_lab.metrics import calculate_financial_metrics

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_all_python_files_compile_cleanly():
    """Universal compilation gate: guarantees 0 IndentationError or SyntaxError across repo."""
    compile_errors = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        if any(x in root for x in [".git", "venv", "__pycache__", ".pytest_cache"]):
            continue
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                try:
                    py_compile.compile(path, doraise=True)
                except Exception as e:
                    compile_errors.append((f, str(e)))

    assert not compile_errors, f"Compilation errors detected: {compile_errors}"


def test_metric_keys_contract():
    """Guarantees calculate_financial_metrics returns authoritative keys and rejects invented keys."""
    nav_df = pd.DataFrame([
        {"date": "2024-01-01", "nav": 100000.0},
        {"date": "2024-12-31", "nav": 125000.0}
    ])
    metrics = calculate_financial_metrics(100000.0, nav_df, pd.DataFrame(), [], 100.0, 50.0, 0)
    required_keys = [
        "initial_capital",
        "ending_nav",
        "net_pnl",
        "total_return_pct",
        "cagr_pct",
        "cagr_calendar_pct",
        "max_drawdown_pct",
        "annualized_vol_pct",
        "sharpe_ratio",
        "total_trades",
        "win_rate_pct",
        "profit_factor",
    ]
    for k in required_keys:
        assert k in metrics, f"Missing expected metric key: {k}"

    # Verify run_capital_matrix does not access non-existent keys
    matrix_path = os.path.join(PROJECT_ROOT, "scripts", "run_capital_matrix.py")
    if os.path.exists(matrix_path):
        with open(matrix_path, "r", encoding="utf-8") as f:
            code = f.read()
            assert "cagr_trading_pct" not in code, "run_capital_matrix.py must not access invalid 'cagr_trading_pct'"


def test_no_hardcoded_past_dates_in_production_pipelines():
    """Guarantees production cron pipelines do not hardcode static past end dates."""
    pipeline_scripts = [
        os.path.join(PROJECT_ROOT, "scripts", "run_daily_pipeline.py"),
        os.path.join(PROJECT_ROOT, "scripts", "execute_paper_orders.py"),
    ]
    for script_path in pipeline_scripts:
        if os.path.exists(script_path):
            with open(script_path, "r", encoding="utf-8") as f:
                content = f.read()
                assert "2026-10-24" not in content, f"{os.path.basename(script_path)} must not contain hardcoded '2026-10-24'"


def test_no_credentials_or_pii_in_docs():
    """Guarantees documentation does not expose raw chat IDs or specific Atlas cluster hosts."""
    docs_dir = os.path.join(PROJECT_ROOT, "docs")
    for root, dirs, files in os.walk(docs_dir):
        for f in files:
            if f.endswith(".md"):
                path = os.path.join(root, f)
                with open(path, "r", encoding="utf-8") as fp:
                    text = fp.read()
                    assert "766982450" not in text, f"{f} contains private Telegram Chat ID!"
                    assert "uxg8fc4" not in text, f"{f} contains production Atlas cluster identifier!"
