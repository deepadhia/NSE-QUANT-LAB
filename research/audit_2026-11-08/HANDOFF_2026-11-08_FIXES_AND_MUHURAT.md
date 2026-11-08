# Learning Lab handoff — audit remediation and Muhurat session

Date: 8 November 2026, Asia/Kolkata. Repository: `D:\Personal Projects\Learning-Lab`.

**Current state: INCOMPLETE, uncommitted implementation. Do not deploy or describe the fixes as fully validated.** The user interrupted implementation to request this handoff. Stop implementation here and resume from the outstanding checks below when asked.

## User intent and authorization

1. Deeply audit the quant system and whether its approximately 20% twenty-year CAGR is realistic.
2. User subsequently authorized proper local fixes, with concern about breaking the system.
3. User also asked how to manage today's Diwali/Muhurat short session.
4. **Do not commit or push without the user's approval.** No commits, pushes, remote deployment, broker orders, or production database mutations were performed in this session. Database inspection was read-only. Local code and a NEW derived data file were written.
5. Latest instruction: **write a handoff for this session**. No permission question is pending.

Read `AGENTS.md`, `BACKTEST_VALIDATION.md`, `docs/STRATEGY_ONBOARDING_AND_PARITY_PLAYBOOK.md`, and `docs/AUDIT_2026-11_FINDINGS_AND_REGRESSION_GUARDS.md` before continuing. They were read this session. The diagnosing-bugs skill was read and used for executable counterexamples; no subagents were spawned.

## Preserve pre-existing user work

These were already modified/untracked BEFORE this audit and must not be reverted, overwritten, or included accidentally in a future commit:

- `docs/RESULTS_REGISTER.md`
- `research/audit_2026-10/HANDOFF_2026-11-08.md`
- `research/audit_2026-10/scripts/ab_rs55_filter.py`
- `research/audit_2026-10/results/ab_rs55_filter/`

HEAD at audit start was `0c7dfe3` (no new commit was made). Current `git diff --stat` also includes that pre-existing results-register edit.

## Completed audit and reproducible evidence

Read `research/audit_2026-11-08/AUDIT_REPORT.md`. It is a **frozen pre-fix audit**, so its statement that production source was unchanged describes the audit phase, not the current working tree. The old `verification.json` and source manifest likewise describe the pre-fix snapshot. Do not present them as post-fix verification.

Fresh pre-fix production-engine replay, without the cached indicator pickle:

- Command: `python research/audit_2026-11-08/reproduce_baseline.py`.
- Config: Rs 100,000; 15 slots; APPROACH_3; tiered costs; dated tax; 2007-01-01 through 2026-10-30.
- 3,402 loaded symbols, 4,913 NAV sessions, 709 closed trades, 719 buys.
- Final NAV **Rs 4,198,266.071447399**; max DD **-41.4038%**.
- Research's first-EOD-NAV CAGR **20.5018%** versus actual-initial-capital calendar CAGR **20.7455%**. The first EOD NAV is Rs 104,086.36, so the reporting discrepancy understates CAGR.
- Entire closed-trade CSV byte-identical to prior RS55 A0 baseline.
- Cash/execs/tax/terminal holdings reconciliation residual about Rs 0.018.
- CUPID terminal weight **35.54%**, unrealised gain Rs 1,332,640.26. Marking CUPID at its entry outlay gives CAGR 18.44%; marking all ten open holdings at outlay gives 17.46%. These are endpoint concentration sensitivities, NOT forecasts or resimulated strategies.
- Pre-fix existing suite: **269 passed, 1 skipped, 190.62 s**. The skip was real-data research-harness parity due a guard checking `data/external/...` although files exist at `external/...`.
- Nine original adversarial probes failed; logs in `probes_initial.txt` and `probes_final.txt`. These audit probes were deliberately preserved and are outside normal pytest testpaths. Do not confuse them with the new regression suite.
- Read-only health check passed, stored at `health_check.txt`.
- Read-only paper snapshot (`paper_snapshot.json`): three active strategies, each 15 executions/positions/fill-audit records, cash reconciled, signals through Nov 6 and executions Nov 5. Forty-five paper records are not independent brokerage fills. Older docs saying no fill-audit data exists are stale.

Other evidence/scripts in this directory:

- `baseline/`: preserved original benchmark/config/NAV/orders/executions/trades/metrics/manifests. **Do not overwrite.** Original reproduction script refuses to overwrite.
- `artifact_analysis.json`, `terminal_positions.csv`, `annual_returns.csv`, `recomputed_fy_tax.csv`.
- `integer_lot_analysis.json`, `integer_lot_reconciliation.csv`: 653/719 buys matched raw EQ prices; 205 imply fractional contemporary shares; DIVISLAB 2007-07-02 buy is 0.95 actual shares. CUPID 2025-09-01 implies 919.4 contemporary shares.
- `panel_analysis.json`: 8,367,109 rows / 3,989 symbols; no duplicate symbol/dates, NaN/nonpositive OHLC, invalid OHLC envelopes or nonpositive volumes. This is structural QA, not provenance certification.
- 337 liquid EQ stock-days across ten symbols in 2024 have actual close >= Rs 10 but adjusted close < Rs 10.
- 1,395 official-labelled adjustment events and 52 inferred extreme-gap events. None of those 52 dates overlaps a baseline CLOSED holding, but indirect selection/history effects remain possible.

## Main audit findings

1. Future-adjusted OHLC used for nominal rupee filters, integer lots, budget gating and tick costs.
2. Engine invents last-close liquidation after terminal missing history (actual baseline occurrence MANGCHEFER 2025-10-31).
3. 09:16 paper fills use an unavailable opening print rather than current bid/ask.
4. Live circuit checks compare an earlier open with current circuit bands/depth.
5. A buy recorded/debited before a crash can have its stale order expired next session before recovering its missing position.
6. Absolute cash writes can overwrite another worker's cash delta.
7. Quote outages / ten circuit deferrals can be converted into fictional haircut sales.
8. Final partial-FY realised tax omitted (baseline omitted estimated Rs 2,692.75; small effect here).
9. FY2024-25 LTCG branch applied the post-budget rate to pre-July sales; the supposed blend branch was unreachable.
10. Inconsistent CAGR/Sharpe conventions, inferred corporate-action provenance, live-universe/series drift, and a research reproduction depending on a changed execution default.

The audit does NOT establish a corrected forward CAGR or a justified 15–18% forecast. Roughly 180 tried configurations and repeatedly inspected periods mean there is no untouched holdout.

## Local implementation made so far

### Paper safety

- NEW `src/nse_quant_lab/paper/locking.py`: thread and OS file lock, hashed account identity, Windows `msvcrt` / Unix `fcntl`, bounded wait, OS releases process lock on exit. Decorator serializes signal generation and fills on **one deployment host**. Multi-host active/active is NOT supported by this lock.
- `paper/manager.py`: `_persist_cash` derives net cash from an authoritative execution, checks expected prior balance with Mongo compare-and-swap, preserves idempotency markers without the old 500-marker eviction, raises `CASH_CONFLICT` rather than overwriting a changed balance. Duplicate execution insert now fails for recovery rather than proceeding with duplicate bookkeeping.
- `db/models.py`: optional `ExecutionModel.session_date`; both fill paths set it. Recovery preserves the original acquisition/fill session.
- Recovery of already-inserted executions now runs BEFORE stale BUY expiry and corporate-action application in morning execution.
- Current executable ask/bid used for live fills; missing side liquidity retains order and reports `no_quote`; replay bars still use their open. Circuit bands use quote `close` (LTP) instead of old open.
- Missing/unparseable broker timestamp now refuses the quote (with diagnostic), rather than accepting it. Timestamp check still primarily establishes session date; intraday age handling was not implemented.
- Missing BUY history retained/reported rather than silently expiring.
- Removed guessed haircut liquidations and the ten-day forced-sale rule. Legacy data-ended request cancels if price data returns; absent data leaves it unfilled.
- Post-market scan requires the decision-date bar for every held name and raises `MISSING_HELD_BAR` before signals/NAV are produced. Corporate-action lookup failure now raises instead of silently continuing.
- Removed cash from existing-account summary writes where replacement matched, so cash mutations go through the cash helper. Review all paths for any remaining stale balance writes.

### Historical execution basis and accounting

- NEW `src/nse_quant_lab/price_basis.py`: nominal-price access, adjusted/raw bar conversion, split-aware previous-close reference, explicit verified split/bonus share adjustments, dated acquisition/zero-cost bonus tax lots, unresolved fractional entitlement rejection.
- NEW `scripts/build_execution_panel.py`: reconstructs raw prices from existing `raw_canon_cache.parquet`, matches to the clean panel, checks uniform OHLC scale, attaches split/bonus factors only when a dated official-labelled event matches a factor transition. Refuses to overwrite output.
- **Successfully generated NEW ignored data artifact** `external/nse_archive/clean_panel_execution_v2.parquet`: 8,367,109 rows, 1,446 observed factor transitions, 1,383 matched dated actions. Original clean panel unchanged. Remaining transitions are unverified, not silently accepted.
- `data_loader.py`: historical `include_live=False` prefers the new v2 panel. Operational live loading still uses original panel path. Known legacy clean-panel frames receive `attrs['price_basis']='adjusted_without_raw'`, and the engine rejects them. Cached/unlabelled research frames remain a contract gap: a cache with no attrs/raw metadata can still be assumed raw; address before claiming general correction.
- All three active PIT strategies use nominal prices for Rs 10 eligibility and returned candidate `close`, while indicators remain on adjusted OHLC. Trend strategy explicitly preserves basis metadata through its column subset.
- `engine.py`: converts execution/valuation prices to nominal basis, updates held share counts only on verified split/bonus actions, records action ledger, handles new bonus tax lots, adjusts gap reference across share actions; missing held bars now raise rather than create liquidation proceeds.
- IMPORTANT review target: holdings sizing after a same-morning share action currently uses changed shares with the previous decision-date nominal close. Ensure marks are rebased consistently on that boundary; the new split test verifies NAV but does not fully test a second new purchase at that boundary.
- Final partial FY now assessed; NAV exposes `tax_payable`; top-level metrics separate assessed/payable/paid tax. The per-FY ledger still calls its assessment `tax_paid` (legacy field); finish naming/documentation if appropriate.
- `tax_india.py`: materializes/expands dated lots, accepts actual STT amounts when supplied; FY2024-25 boundary changed from `>= 2025-03-31` to `>`. Mixed-budget setoff/exemption allocation still uses the existing pro-rata blend, not a fully independently certified tax return computation.
- `metrics.py`: primary CAGR now calendar-based; previous session/250 measure gets an explicit alias; Sharpe uses arithmetic annual excess return / annualised vol (252); drawdown includes initial capital.
- `research/audit_2026-10/scripts/minervini_20y.py`: explicitly pins BASELINE instead of relying on the now-APPROACH_3 default. Other research reports/caches and first-EOD CAGR calculations have not all been migrated.

### Tests

- NEW `tests/test_audit_20261108.py`: initially 12 failing regression tests, expanded to **17 tests** for raw lots/split NAV, dated bonus lots, unverified actions, Muhurat boundaries and cross-process locking.
- Existing quote fixtures updated with bid/ask and parseable timestamps; live-morning parity fixture quotes current bid=ask=open to isolate engine agreement.
- Old tests explicitly asserting fictional forced liquidations were replaced with stricter missing-data retention/failure assertions. This is an intentional financial-contract correction, not permission to remove failing tests.
- Updated one split fixture to include the other held stock's decision-date bar; updated deferred-circuit fixture with its held stock's actual date bar.
- Existing `test_quote_without_timestamp_is_accepted_but_logged` changed to assert refusal with logging.

## Muhurat verified schedule and local support

Primary source verified using web tool: **NSE/CMTR/76941, 19 October 2026**:

https://nsearchives.nseindia.com/content/circulars/CMTR76941.pdf

Sunday **8 November 2026**, IST:

- Preopen begins 14:05; continuous market opens **14:20**.
- CAS securities continuous trading ends **15:15**, CAS matching ends **15:35**.
- Non-CAS continuous market ends **15:30**; post-close is 15:50–16:00.
- Therefore there is not one identical one-hour window for every equity.

Implemented locally:

- `holidays.py`: explicit verified special-session override BEFORE Sunday/holiday rejection; `TradingSession`, `get_trading_session`, `execution_window_open`.
- Normal execution starts 09:16; today's special execution starts **14:21** and must stop **before 15:10**, a conservative buffer before earliest CAS close. EOD jobs remain 17:45 ingestion, 17:55 benchmark, 18:00 signals.
- Scheduler `jobs_for_date` changes morning timing/cutoff, including retries. Old persisted Sunday-skip state is cleared in memory when loading a special date without calendar version 2.
- Morning script checks live execution window BEFORE connecting/attempting fills; `--force` does not bypass this window. Dated replay behavior remains separate.
- `deploy/crontab.txt`: jobs eligible every calendar day (scripts reject closed days), extra 14:21 execution entry for afternoon sessions. The extra entry is also within regular-day hours; fills should remain idempotent. Review whether a session-specific wrapper is preferable before deploying cron mode.
- **NOT DEPLOYED.** No remote service restart, cron installation, manual paper execution or production write occurred. Do not imply today's running system has these changes.
- The static holiday table still has known inaccurate/projected entries outside this override. It was NOT fully repaired/certified here.

## Validation timeline — do not overstate

1. Pre-fix entire existing suite: **269 passed, 1 skipped**.
2. Original audit probes: **9 failed**, intentional pre-fix evidence.
3. New first regression run: **12 failed** before implementation.
4. Focused new regressions + tax + Minervini + alpha: **39 passed** after first fixes.
5. Required subset earlier: **71 passed, 2 failed**. Failures were missing held bars in old test fixtures; those fixtures have since been corrected.
6. Another focused run before the last fixture correction: **45 passed, 1 failed**, same deferred-symbol missing-bar fixture, since corrected.
7. Most recent broad suite is **RUNNING / ALREADY HAS FAILURES**. Command:

   `python -m pytest -m "not live_upstox" -q --tb=short > research/audit_2026-11-08/fix_suite_1.txt 2>&1`

   Last inspected: 287 collected; all **17 new regression tests passed**, but failures were visible in `test_alpha_semivol_pit.py` and several `test_audit_regressions.py` tests. Final tracebacks/counts were not yet available. **This is not a passing final suite.**

8. `git diff --check` had passed before the last interruption; no completed final diff/semantic review.
9. `verify_production_health.py` has NOT been rerun after these code changes. Its earlier pass belongs to the audit baseline.
10. Mandatory static gate additions and remediation-document updates required by AGENTS have NOT yet been made.

## Background processes at handoff

These unified-exec sessions were still running at the last check. Poll with `tools.write_stdin`, empty input; do not start duplicates.

- **76625** — broad pytest, writes `research/audit_2026-11-08/fix_suite_1.txt`. Already has failures; inspect final tracebacks.
- **9644** — `python research/audit_2026-11-08/validate_corrected_history.py`. Uses v2 raw/adjusted panel and preserved baseline benchmark/config, production strategy and engine. Reported **Loaded 3402 symbols; nominal prices present: True**. No final result yet.
- Session **45622** was a read/diff check and has finished exit 0.

Corrected replay writes only `research/audit_2026-11-08/corrected_history/`. It refuses to overwrite an existing `validation.json`. On a missing bar/unverified action it catches ValueError, writes `INCOMPLETE_DATA_REQUIRES_REVIEW` with the exact reason and exits 2. That is a useful diagnostic, not a corrected CAGR. Do not substitute a guessed liquidation to make it finish.

## Next work, in order

1. Finish/poll the two sessions above and inspect actual failure traces and corrected-history diagnostic.
2. Diagnose broad-suite failures; do not guess they are merely fixtures. Re-check strategy snap schemas/nominal-price fallbacks and engine contract changes. Do not weaken tests.
3. Review cash recovery/locking semantics, account-summary writes and original-date handling across crashes, quotes, splits and retries. Multi-host execution still needs a distributed lease if that topology is desired.
4. Review raw-share accounting around same-morning split sizing, fractional entitlements, tax lots, pending orders, and circuit comparisons. Unlabelled adjusted research caches need an explicit fail-loud provenance contract.
5. Review operational gate behavior for special sessions and retries; avoid a `--test-job` call that would mutate the real portfolio. Add an offline schedule preview/dry-run if needed.
6. Address live-universe/series drift and unrecorded price-gap rescaling in ingestion: the existing ingestion code can still infer/rescale holdings from gaps. That was inspected but NOT yet repaired in this implementation.
7. Add class-level regression/static guards to `tests/test_production_invariants.py` and document new fixes in `docs/AUDIT_2026-11_FINDINGS_AND_REGRESSION_GUARDS.md`; refresh strategy/parity docs to distinguish historical next-open replay from current bid/ask live execution. Update stale comments/docstrings.
8. Run targeted tests, then required paper/parity/isolation gates, full suite, compilation/contracts and read-only health check. Record final outcomes in a NEW remediation verification report, preserving original audit evidence.
9. Strict replay may identify unresolved real corporate events/missing held data. Resolve from primary evidence, never by choosing the treatment with the highest return. Existing 20% claim remains unvalidated until corrected history/robustness work is complete.
10. Review final diff, keep existing user research separate, summarize what is verified/remaining. No commit, push, deployment or production mutation without the relevant user authorization.

No claims of production readiness, unchanged returns, or zero risk are justified at this checkpoint.
