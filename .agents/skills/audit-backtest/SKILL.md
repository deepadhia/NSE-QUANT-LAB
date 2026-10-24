---
name: audit-backtest
description: Audits a trading strategy or backtest for look-ahead bias, survivorship bias, incorrect fills, bad accounting, unrealistic costs, data quality issues, overfitting, and unsupported claims. Use before trusting a new or modified backtest.
---

# Audit a Trading Backtest

## Goal
Produce an evidence-backed audit, not a strategy endorsement. Do not modify files unless the user explicitly asks for fixes.

## Procedure

### Phase 1 — Establish scope and provenance
1. Read `AGENTS.md` and `BACKTEST_VALIDATION.md`.
2. Locate the actual strategy specification, entry point, data loader, indicators, signal generation, execution simulator, accounting, metrics, and tests.
3. Record exact file/function locations. If an expected component is absent, say so.
4. Identify data source, historical coverage, adjustment policy, universe construction, and whether results use synthetic data.
5. Separate repository claims/comments from behavior proven by code or tests.

### Phase 2 — Trace one trade end-to-end
Trace at least one entry and exit through:
data row -> indicator -> signal timestamp -> order eligibility -> fill -> position size -> fees -> stop/exit -> realized P&L -> equity curve -> aggregate metrics.
Record timestamps and formulas. If impossible, mark INCOMPLETE.

### Phase 3 — High-priority bias checks
Inspect for:
- Signal and return computed on the same bar.
- Close-based signal filled at that same close without a feasible order model.
- Negative shifts, centered rolling windows, forward-filled future values, future joins, full-sample normalization/ranking.
- Survivorship bias and current constituents used historically.
- Corporate-action/adjusted-price inconsistencies.
- Same-bar stop/target ordering and gap-through-stop fills.
- Missing candles treated as no signal or filled silently.
- Fees, taxes, spread, slippage, liquidity and impact omitted.
- Portfolio capital constraints ignored; simultaneous trades treated as independently funded.
- Parameter tuning on validation/test/holdout data.
- Metrics hardcoded, derived from different ledgers, or calculated with unclear conventions.

### Phase 4 — Tests and independent checks
1. Run existing targeted tests and record commands/exit codes.
2. Check indicator edge cases and formula conventions.
3. Create or propose minimal hand-checkable fixtures for execution, fees, sizing, accounting and metrics.
4. Independently recompute at least one sample trade and one aggregate metric from saved artifacts.
5. Test temporal integrity by perturbing future data and confirming earlier signals/features do not change.
6. Do not report tests as passed if not run.

### Phase 5 — Findings
Classify each finding:
- `CRITICAL`: invalidates reported results.
- `HIGH`: materially biases results or risk.
- `MEDIUM`: correctness/reproducibility issue with limited or uncertain impact.
- `LOW`: maintainability or minor reporting issue.

For each finding provide:
- Evidence: file/function/line or test output.
- Why it matters.
- Likely direction of bias, if determinable; otherwise say unknown.
- Minimal fix or test needed.
- Whether historical results must be rerun.

### Phase 6 — Verdict
Use only:
- `INVALID`
- `INCOMPLETE`
- `VALID_HISTORICAL_SIMULATION`
- `ROBUSTNESS_NOT_ESTABLISHED`
- `RESEARCH_CANDIDATE`

A valid simulation does not prove future profitability. Do not call any strategy guaranteed or proven profitable.

## Required output format
1. Executive verdict and confidence limits.
2. Scope/data provenance.
3. Critical/high findings first.
4. One-trade trace.
5. Tests run, with actual observed results.
6. Tests not run and why.
7. Required fixes ordered by priority.
8. Whether existing performance metrics can still be trusted.
9. Exact next action.

Be skeptical without being reflexively negative. Report genuine strengths as well as weaknesses, but evidence must support both.
