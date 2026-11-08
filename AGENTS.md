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
- Read this file, relevant project documentation, and `docs/STRATEGY_ONBOARDING_AND_PARITY_PLAYBOOK.md` whenever adding, modifying, or evaluating a strategy.
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


## Real-World Invariance, Anti-Bias & Adversarial Falsification Contract

The primary failure mode in quantitative software is producing **biased outputs and false confidence**—code and backtests that pass clean unit tests under artificial assumptions, but catastrophically fail, crash, or bleed capital in the real world.

To eliminate fake outputs and ensure software survives real-world market reality, all implementations and tests must adhere to the following strategy-agnostic invariants:

### 1. The Falsification Principle (Testing to Break, Not to Confirm)
- **Anti-Confirmation Bias**: Writing tests that only verify the "happy path" with clean, complete synthetic data creates dangerous delusion. Tests must actively test real-world failure modes.
- **Mandatory Failure-Mode Tests**: Every workflow must be tested against:
  - Missing, dropped, or delayed market data.
  - Circuit limit locks (upper circuit buy rejections, lower circuit exit deferrals).
  - Cash exhaustion, unaffordable integer sizing, and cash buffer preservation.
  - Exchange holiday calendars, shortened market weeks, and non-standard month-ends.
  - Unintended duplicate triggers (cron retries, network timeouts, server reboots).
- **Invalid Test Classification**: Any test that passes only by feeding future data, complete end-of-day bars to intraday processes, or swallowing errors into neutral defaults is classified as **biased and defective**.

### 2. Temporal Asymmetry & Bar Availability Invariant
- **Market Timing Reality**: In the real world, a trading session is chronological. At `09:16 IST`, today's completed daily EOD bar (`15:30 IST`) does **not exist**.
- **No Intraday Reliance on Future EOD Data**:
  - Any process executing at market open must operate strictly on information available at that minute (live opening quotes, previous close).
  - Tests for morning execution engines must actively omit today's EOD bar to ensure the system does not crash or look ahead in live production.
- **Zero-Lookahead in Signal Generation**: Post-market signal generation on Day $T$ close must never access, infer, or incorporate Day $T+1$'s open, high, low, or volume.

### 3. Decoupled Exit Semantics (Strategy-Agnostic Invariant)
Quantitative systems employ two fundamentally distinct exit mechanisms that must never be conflated:
1. **Periodic Rank-Based Rebalance Liquidation**: Liquidating positions because they fell out of the top $N$ cross-sectional universe rank on scheduled rebalance dates.
2. **Rule/Event-Driven Technical Exits**: Liquidating positions because an intrinsic technical condition was met (trailing stop, ATR channel, volatility breakout, time stop).
- **Rule**: Event-driven exit strategies must NEVER inherit default periodic rebalance drop logic that causes 1-day phantom churn on held positions.
- **Rule**: Periodic rebalancing strategies must NEVER fail to liquidate dropped ranks.
- The exit semantics of every strategy must be explicit, strategy-agnostic, and verified by tests.

### 4. Idempotency & Re-Run Safety
- Real-world production servers experience restarts, network retries, and duplicate cron triggers.
- **Idempotency Invariant**: Executing an order generation or order fill script multiple times on the same session must produce the exact same state:
  - Never generate duplicate orders.
  - Never execute duplicate fills.
  - Never double-decrement cash or corrupt position ledgers.

### 5. Mark-to-Market Valuation & Friction Accounting
- **No Stale Sizing**: Portfolio equity for position sizing (`pretrade_equity = cash + holdings_val`) must mark existing holdings to current decision-timestamp market prices, never historical entry costs. Sizing on static acquisition costs creates fictional backtest compounding that diverges wildly from real accounts.
- **True Traded Value Friction**: Statutory transaction fees (STT, stamp duty, exchange turnover charges) must be assessed on actual executed turnover (`shares * fill_price`).
- **Non-Duplicated Sizing Buffers**: Sizing allocation divisors must not apply slippage buffers twice once the opening price is already known.

### 6. Universal Static Analysis & Contract Gates
- **Compilation Gate**: `python -m py_compile` must compile all repository scripts with 0 errors (enforced via `test_compilation_and_contracts.py`).
- **Dynamic Date Invariant**: Production pipelines must never contain hardcoded static past dates in data loading or universe filters.
- **Contract Schema Integrity**: Consumers of metrics and database models must strictly adhere to producer schemas without guessing or inventing unverified keys.
- **Fail-Loud Principle**: When data is missing, corrupted, or an API call fails, the system must log complete diagnostic traces, preserve audit ledgers, and fail visibly. Never swallow failures silently into "default success".

## Strategy Onboarding, Change Management & Parity Mandate
Whenever adding, onboarding, refactoring, or evaluating a strategy, you MUST read and strictly adhere to `docs/STRATEGY_ONBOARDING_AND_PARITY_PLAYBOOK.md`.

### Non-Negotiable Strategy Onboarding Rules:
1. **Strict Single Source of Truth**: Never duplicate a strategy class in `research/` (no `*Candidate` copy-pastes). Once implemented in `src/nse_quant_lab/strategies/<name>.py`, research runners, backtests, and paper managers must import the exact production class directly.
2. **Indicator Namespace Isolation**: Never rely on generic column names (e.g. `mom_6m`). Compute formula-specific column names unconditionally (`raw_mom_6m`, `risk_adj_mom_6m`) and declare all required columns in `required_indicator_columns`. Never guard strategy-specific calculations behind generic `if "col" not in out.columns:`.
3. **Decoupled Operational Wiring**: Ensure the strategy is wired into all 4 operational scripts (`scripts/execute_paper_orders.py`, `scripts/run_daily_pipeline.py`, `scripts/manage_strategy.py`, `scripts/verify_production_health.py`), and structural stop telemetry is registered in `PositionModel` and `PaperTradingManager`.
4. **Mandatory Automated Verification Gates**:
   - **Causal Truncation Invariance**: Candidate ranking on date $T$ must be 100% identical with or without future bars $T+1 \dots T+N$.
   - **Backtest-to-Paper Replay Parity Gate**: Run `pytest tests/test_backtest_paper_parity.py -v`. Every execution count, symbol, action, and integer share count must match 1-for-1, and final NAV relative delta must be $< 0.01\%$.
   - **Production Health Verification**: Run `python scripts/verify_production_health.py` and ensure all 5 pre-flight checks pass before seeding in MongoDB.

## Trading research invariants
Before changing an indicator, signal generator, historical-data pipeline, backtester, execution simulator, portfolio accounting, or performance report, read `BACKTEST_VALIDATION.md` and `docs/STRATEGY_ONBOARDING_AND_PARITY_PLAYBOOK.md`.

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

## Audit-derived production safety invariants (2026-11 forensic audit; enforced by tests)
Full finding list, fixes and guard tests: `docs/AUDIT_2026-11_FINDINGS_AND_REGRESSION_GUARDS.md`. Before editing `paper/manager.py`, `engine.py`, `db/`, `scripts/run_*.py`, `scripts/execute_paper_orders.py`, `holidays.py`, `base_strategy.py` or any strategy, read that document's "Rules for changing this code" section.
1. **Shared collections need namespaced identity.** Every ID/upsert key in a collection shared by several strategies must include `strategy_id` (and `mode`). Build order IDs only with `PaperTradingManager._order_id` and write orders only with `_stage_order`. Never upsert on `order_id` alone.
2. **Every state-changing write sequence must be resumable.** Fills write in a fixed order (execution -> cash -> trade/position -> order FILLED); each step is idempotent and keyed by the deterministic `exec_id = EXEC_{order_id}`. Cash changes only through `_persist_cash`. No random IDs (`uuid`) as idempotency keys.
3. **Missing data is never an action.** A data gap must not become a sale, a haircut, an expiry or a "no signal". Retain the order, alert, and make the run exit non-zero so the scheduler retries. No code path may write a position down or sell it on missing data; a suspected delisting needs an explicit, evidenced event record (see `MISSING_HELD_BAR`).
4. **Fail loud, not guessed.** No `except Exception: return <guess>` in calendar, rebalance, pricing or accounting code. Operational scripts must exit non-zero when any strategy fails or any order is unpriced.
5. **Time-travel guard.** Signal generation for a decision date older than the newest recorded signal/execution is refused. Morning-path tests must omit the execution day's EOD bar and supply the open via a quote stub.
6. **Corporate actions are data.** Share counts change only through a recorded `corporate_actions` document (applied once, tracked in `applied_actions`); an unrecorded price discontinuity must produce a CRITICAL alert.
7. **Strategy-specific formulas compute unconditionally under formula-specific names.** No `if "generic_col" not in out.columns:` in strategies; selection reads only the strategy's own columns. Gate: `tests/test_production_invariants.py`.
8. **Scheduler guards trust only successes.** "Completed" (given up / skipped) is not "succeeded"; safety nets (catch-up runs) key off `succeeded_today`. A timeout is a failure, not a completion.
9. **No hardcoded calendar dates in defaults**; model timestamps use `default_factory`, never an import-time `datetime.now()`.
10. **Test to break.** A change to the above areas must add or keep a failure-mode test (crash-mid-fill, outage, gap, circuit, cash exhaustion, duplicate trigger, back-dated rerun). `pytest tests/test_production_invariants.py tests/test_paper_failure_modes.py tests/test_order_id_isolation.py tests/test_backtest_paper_parity.py` must pass.

## Completion gate
Do not call a task complete until the requested behavior is implemented, relevant checks have been run where possible, the diff is reviewed, and unresolved risks are disclosed. If any critical acceptance check fails, label the result FAILED or INCOMPLETE—not successful.
