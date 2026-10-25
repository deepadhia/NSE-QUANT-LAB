# AGENTS.md — Engineering, Research Integrity, and Honest Reporting

## Mission and priority order
Build correct, reproducible, auditable software. For trading research, the objective is to test whether a hypothesis survives realistic conditions—not to produce attractive metrics.

Priority order:
1. Correctness and safety.
2. Data provenance and temporal correctness.
3. Reproducible tests and independently verifiable outputs.
4. Realistic execution and accounting.
5. Maintainability.
6. Speed and convenience.

## Evidence and honesty contract
- Never claim a command, test, backtest, search, API call, or file inspection happened unless it actually happened and the output was inspected.
- Never fabricate code, functions, paths, data, metrics, citations, broker behavior, or repository capabilities.
- Distinguish clearly between VERIFIED, INFERRED, ASSUMED, UNVERIFIED, and FAILED.
- When you cannot verify something, say exactly what is missing and what would verify it.
- Do not state that code is correct merely because it runs, compiles, or passes a small test suite.
- Do not hide failed tests, warnings, exclusions, data gaps, or contradictory evidence.
- Never describe synthetic/mock data as real market data or use its performance as evidence of strategy profitability.
- Never present a backtest as proof of future returns.
- Do not flatter, agree with, or defend the user's hypothesis by default. Evaluate evidence independently and report negative results plainly.
- Do not optimize for a requested or desired CAGR, win rate, profit factor, Sharpe ratio, or drawdown.
- Prefer a correct "unknown", "not established", or negative result over a confident unsupported conclusion.

## Required workflow for code changes
### 1. Inspect first
Before editing:
- Read this file and relevant project documentation.
- Inspect the exact files, functions, tests, configuration, dependencies, and callers involved.
- Trace the current data flow from input to output.
- Establish existing behavior and baseline failures if practical.
- Do not infer implementation from filenames, comments, README claims, or prior model messages alone.

### 2. Plan
State briefly:
- Task and expected behavior.
- Files/functions likely to change.
- Assumptions and ambiguities.
- Risks and regression surface.
- Tests that will verify the change.
If ambiguity can materially affect financial results, ask before choosing an interpretation.

### 3. Implement minimally
- Make the smallest complete change.
- Preserve existing behavior unless the task explicitly requires changing it.
- Do not perform unrelated refactors or add dependencies without justification.
- Do not change strategy rules, API contracts, schemas, risk limits, or public interfaces silently.
- Never remove or weaken a test just to make it pass.
- Keep strategy logic, market data, execution, accounting, and reporting separable.

### 4. Verify actual behavior
- Run targeted tests, then relevant broader tests.
- Run lint/type checks only when available and relevant.
- Inspect the actual exit codes and outputs.
- Review the diff for unintended changes.
- Verify important calculations with independent calculations or hand-checkable fixtures.
- If execution is unavailable, mark the test NOT RUN; never imply it passed.
- If a test fails, diagnose and report it. Do not silently skip or weaken it.

### 5. Final report
Every nontrivial coding task must report:
1. Summary of what changed.
2. Exact files/functions changed.
3. Tests/commands actually executed and observed outcomes.
4. Tests not run and why.
5. VERIFIED findings vs assumptions.
6. Remaining risks, data limitations, and follow-up.
7. Whether the acceptance criteria passed, failed, or remain incomplete.


## Automated Static Analysis & Early Detection Gates
To catch syntax, semantic, temporal, and execution bugs before they reach staging or production, every agent and engineer must adhere to the following automated gates:

### 1. Universal Compilation Gate
- `pytest` alone does NOT execute standalone cron scripts (e.g. `scripts/ingest_daily_upstox_candles.py`, `scripts/run_daily_pipeline.py`).
- Before completing any task, run `python -m py_compile` across all `.py` files in the repository.
- An automated test (`tests/test_compilation_and_contracts.py`) enforces that every Python script compiles cleanly on every test run.

### 2. Metric & Schema Contract Invariants
- When consuming outputs from `metrics.calculate_financial_metrics()` or database models, never guess or fabricate dictionary keys (e.g. `cagr_trading_pct` vs `cagr_pct`).
- Verify key names directly against the producer's return dictionary or model fields.

### 3. Loop State & Accumulator Scope Invariants
- Per-item state flags (e.g. `used_fallback`, `has_error`) must be instantiated and reset inside the per-item loop.
- Never test a cumulative run counter (e.g. `fallback_count == 0`) inside a loop to determine the state of a single item.

### 4. Temporal Correctness & Live Pipeline Invariants
- **No Hardcoded Future/Past Dates in Cron Entrypoints**: Production cron scripts (`run_daily_pipeline.py`, `execute_paper_orders.py`, `ingest_daily_upstox_candles.py`) must NEVER contain hardcoded calendar end-dates. Always use dynamic dates derived from `datetime.now()` or explicit CLI arguments.
- **Bar Availability Separation (09:16 AM vs 15:35 PM)**:
  - At 09:16 IST on day T, day T's completed EOD bar does NOT exist in MongoDB.
  - Morning order fills must query live market opening quotes (via broker API or live ticker feeds), NEVER assume today's EOD bar is already present in historical collections, and NEVER fall back to yesterday's bar for today's orders.
  - Sizing and mark-to-market NAV calculations at 09:16 AM must use opening/LTP prices, never looking ahead to future 15:30 EOD closing prices.
- **Live Rebalance Detection**:
  - Live post-market signal runs occur on day T when tomorrow's session T+1 is not yet in the historical universe.
  - Rebalance schedules must check if day T is the final trading day of the calendar month (using the market holiday calendar), rather than requiring future bars to exist in the database.

### 5. Quantitative & Portfolio Accounting Invariants
- **Strategy Rebalance vs. Technical Stop Independence**:
  - Breakout and trend-following strategies using technical trailing stops (e.g. Donchian 20-day low, ATR stops) must NEVER inherit a default daily rebalance rule that liquidates positions on daily rank drops (`REBALANCE_DROP`).
  - Held positions must only exit on strategy-defined technical stop breaches (`evaluate_daily_exits()`).
- **Mark-to-Market Pre-Trade Equity**:
  - When calculating portfolio equity for sizing (`pretrade_equity = cash + holdings_val`), held positions must be valued at current decision-day close/LTP, NEVER at historical entry price (`entry_raw_price`).
- **Sizing Friction Buffer Rules**:
  - Do not apply slippage buffers twice when sizing shares. Once the market open price is known, `fill_price = open * (1 + slippage)` is already slipped; do not add slippage again to the share allocation divisor.
  - Statutory fees (STT, stamp duty, turnover charges) must be calculated on actual executed value (`shares * fill_price`).

### 6. Security, Privacy, and Zero-Spam Invariants
- **Zero Secrets & Zero PII in Tracked Files**:
  - Never commit real Telegram Chat IDs, bot tokens, API keys, or production database hostnames into documentation, guides, tests, or examples.
  - Always use standardized placeholders (`your_numeric_chat_id`, `<your_cluster>.mongodb.net`, `your_api_key`).
- **Zero-Spam Notification Policy**:
  - Notifications must be strictly reserved for actionable events: actual order fills, new trade signals, or critical exceptions.
  - Never emit routine heartbeats, "market closed" notices, or routine maintenance logs on weekends, holidays, or zero-action market sessions.

## Trading research invariants
Before changing an indicator, signal generator, historical-data pipeline, backtester, execution simulator, portfolio accounting, or performance report, read `BACKTEST_VALIDATION.md`.

- Treat the checked-in strategy specification and approved configuration as authoritative. If missing or contradictory, stop and report the conflict.
- Never silently change entry/exit thresholds or parameter definitions.
- A signal using bar t's close cannot fill at that same close unless a realistic order was submitted before the close and the model explicitly supports that execution.
- Use only information available at the decision timestamp.
- Do not use future data in features, ranks, filters, normalization, corporate-action handling, or universe membership.
- Model next-session fills, gaps, slippage, transaction charges, available cash, integer quantities, position limits, and overlapping positions explicitly.
- Treat daily-bar stop/target ordering as ambiguous if both are touched and no intraday sequence is available. Use a documented conservative assumption.
- Calculate performance from the order/trade ledger and equity curve, not manually typed metrics.
- Reconcile cash, holdings, costs, realized/unrealized P&L, and total equity.
- Do not infer missing market data as a no-signal condition.
- Never use current constituents as a historical universe without disclosing survivorship bias.
- Report gross and net results separately and clearly label assumptions.
- A strategy can pass software tests and still have no profitable edge.

## External research and repositories
When evaluating a repository or claim:
- Inspect actual source code, tests, license, dependencies, maintenance activity, and data provenance.
- Do not rely on stars, README claims, social posts, screenshots, or headline metrics as proof.
- Reproduce relevant claims before relying on them.
- Cite exact source locations for codebase claims and authoritative docs for current API behavior.
- Treat Reddit posts as anecdotal evidence unless independently reproduced.
- Do not copy code with an unknown or incompatible license.

## Data, secrets, and destructive operations
- Never expose API keys, tokens, passwords, cookies, account identifiers, or secrets in logs, output, tests, screenshots, or commits.
- Never run live orders, change broker settings, delete datasets, overwrite research outputs, or perform destructive migrations unless explicitly authorized.
- Do not replace a failed real-data request with mock data without an explicit, visible mode switch and a loud label.
- Validate data timestamps, duplicates, missing sessions, OHLC constraints, split/dividend handling, and stale records.
- Record exclusions and data-quality warnings.

## Anti-bias requirements
- Keep development, validation, and final holdout periods chronologically separated.
- Record all tried configurations, including unsuccessful experiments.
- Do not repeatedly inspect/tune against the final holdout and still call it untouched.
- Use a historical point-in-time universe where possible; disclose survivorship and delisting limitations.
- Compare with a suitable benchmark over identical dates and comparable exposure/capital assumptions.
- Test parameter stability, increased costs, delays, market regimes, and concentration of profits.
- Do not select the best result from many trials without discussing multiple-testing/selection bias.
- Avoid "confirmation-only" analysis: actively search for failure cases and counterevidence.

## Completion gate
Do not call a task complete until the requested behavior is implemented, relevant checks have been run where possible, the diff is reviewed, and unresolved risks are disclosed. If any critical acceptance check fails, label the result FAILED or INCOMPLETE—not successful.
