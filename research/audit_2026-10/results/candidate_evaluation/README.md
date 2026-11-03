# candidate_evaluation — NOT AUTHORITATIVE

These CSVs came from `scripts/candidate_backtests.py`, which uses research copies of strategy classes (not the `src/` production classes), and some rows were loaded from a cache.
`tax_paid = 0.0` in `candidate_strategies_comparison.csv` is a cache artifact, not evidence that the runs were pre-tax: the baseline row has the same 594 trades as the production-class
monthly-only run (`results/ab_regime_catchup/ab_results_summary.csv`, Arm0) and a CAGR within 0.4 pp of it.

Authoritative figures and their status: `docs/RESULTS_REGISTER.md`. Production-class results: `results/minervini_20y/`.
