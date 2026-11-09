# HANDOFF 2026-11-08 (night) — audit fixes DEPLOYED, forward test staged for 2026-11-09 09:16 IST

Repository: `D:\Personal Projects\Learning-Lab` (GitHub `deepadhia/NSE-QUANT-LAB`, branch `main`). Supersedes the "INCOMPLETE" state in
`HANDOFF_2026-11-08_FIXES_AND_MUHURAT.md` (that file described the work before it was finished, committed and deployed; keep it as history).

Labels: **VERIFIED** = executed and inspected; **INFERRED** = read from code, not run; **UNVERIFIED** = not checked.

## 1. Where things stand (VERIFIED unless noted)

* Server (Oracle, systemd daemon `nse-quant-lab.service`) runs commit **`9571b42`** (user's `git log` on the server). Chain on `main`:
  `0c7dfe3` (pre-session) -> `d974047` RS55 research (not adopted) -> `48712df` audit remediation -> `1cf2652` generic entry-price cap + forward-test accounts + small-capital research -> `9571b42` account consistency, depth capture, isolation tests.
* Working tree is clean except this handoff file (uncommitted). Local suite at `9571b42`: **339 passed, 1 skipped**; server suite: 330 passed, 10 skipped in ~8 min (8 skips = lease tests, no local MongoDB on the server; 2 older skips).
* Server health check passed after each deploy (Atlas ping, 6 active strategies, benchmark current, holiday calendars). Server has **no parquet files**: it loads market data from MongoDB (memory note `learning-lab-deployment-facts`).
* Nothing is real-money. All accounts are `mode=paper`.

## 2. What was wrong and what changed (details: `AUDIT_REPORT.md`, `docs/AUDIT_2026-11_FINDINGS_AND_REGRESSION_GUARDS.md` rows R1-R12)

Live path: no invented sales on missing data (`MISSING_HELD_BAR` stops that strategy's run, positions untouched — **user chose fail-closed**); live fills at ask/bid; quotes without a timestamp refused; cash compare-and-swap; same-host file lock + **MongoDB cross-host lease with fencing token** (`paper/lease.py`, collection `portfolio_leases`); ingestion escalates unrecorded corporate-action rescales (CRITICAL + Telegram + exit 5); verified Muhurat session support (NSE/CMTR/76941); `--preview-schedule`; afternoon cron entry uses `--special-session-only`.
Backtest/accounting: nominal-price basis (`price_basis.py`, `clean_panel_execution_v2.parquet`, laptop only), same-morning split sizing bug fixed (VERIFIED inflated equity, 113 vs 100 shares), dated tax lots, FY2024-25 LTCG blend, calendar CAGR / arithmetic Sharpe (see `docs/RESULTS_REGISTER.md` note).
New this evening: `config.max_entry_price` + `entry_filters.apply_entry_price_cap` (any strategy, engine and paper manager share one function); accounts may alias a strategy class via `strategy_class`; `manage_strategy.py --init` refuses to overwrite an existing account (`--reinit` overrides).

## 3. Accounts (strategies collection, VERIFIED after normalisation)

| strategy_id | class | capital | slots | cap | state |
|---|---|---:|---:|---|---|
| `trend_momentum_pit` | itself | 1,00,000 | 15 | none | live paper, 15 positions |
| `minervini_trend_template` | itself | 1,00,000 | 15 | none | live paper, 15 positions |
| `alpha_semivol_pit` | itself | 1,00,000 | 15 | none | live paper, 15 positions |
| `fwd_trend_momentum_pit_50k_15slots_under1000` | trend_momentum_pit | 50,000 | 15 | Rs 1,000 | 15 BUY staged |
| `fwd_trend_momentum_pit_50k_10slots_under1000` | trend_momentum_pit | 50,000 | 10 | Rs 1,000 | 10 BUY staged |
| `fwd_trend_momentum_pit_50k_15slots_nocap` | trend_momentum_pit | 50,000 | 15 | none | 15 BUY staged (control) |

* `trend_momentum` is ARCHIVED (legacy, left alone). Naming rule: `fwd_<strategy>_<capital>_<slots>_<cap>`; ids are permanent (they key orders/cash/leases). Never give a new account an id that is a prefix of another (115 existing order ids already contain both `trend_momentum` and `trend_momentum_pit`; harmless because code matches exact fields).
* The three `fwd_` accounts were started mid-month with the **one-off catch-up bootstrap**: `catchup_eligible=True` set on the document, then `run_daily_pipeline.py --strategy <id>`. Buys are normally staged only on the monthly rebalance decision date (last trading day of the month, next one 2026-11-30) or in a catch-up session. Orders: signal date 2026-11-08, all `PENDING_OPEN`, capped accounts all < Rs 1,000; the 10 names common to all three are identical. If bootstrap buys get skipped (gap/unaffordable) the bootstrap does not chain another catch-up: empty slots wait for 2026-11-30 unless `catchup_eligible` is set again by hand.
* `normalize_strategy_docs.py --apply` ran once (6 documents, fill-missing only, plus 10-slot hold buffer 1500 -> 1000 to match research). Re-check later with the dry run (no flag).

## 4. Results that exist (all in-sample, descriptive, NOT forecasts)

* **No corrected 20-year CAGR exists.** Strict replay stops: `FRACTIONAL_ENTITLEMENT_UNRESOLVED: AEGISLOG 2010-08-18` (2:3 bonus confirmed from the company's FY11 filing; fraction treatment NOT STATED anywhere found) and `MISSING_HELD_BAR: MANGCHEFER 2025-10-31` (merged into PARADEEP, ISIN INE088F01024, ratio reportedly 187:100, record date 31 Oct 2025, shares tradable ~30 Dec 2025 per company letter; ratio/dates SECONDARY, NSE PDFs not obtained). 20.5-20.75% is still the pre-fix, adjusted-price figure; planning range stays 15-18%.
* Register recomputed under new metric definitions from saved NAVs only (R11 20.75% vs 20.50% old); other rows not recomputable (`docs/RESULTS_REGISTER.md`).
* Small-capital grid (`research/audit_2026-11-08/small_capital_grid/`): Rs 30k/40k/50k x 3 strategies x {15 slots no cap, 15 slots <Rs1000, 10 slots <Rs1000} x pessimistic/optimistic bound, 2016-10..2026-10, Nifty 500 11.2%. Mean CAGR (pessimistic-optimistic): trend 22.8-23.1 / 25.1-25.7 / 25.4-26.2; alpha 18.1-21.1 / 17.9-19.4 / 19.3-22.4; minervini 19.0-20.3 / 17.7-18.5 / 20.5-21.6. Calibration at Rs 1 lakh 23.6-24.1% vs registered 23.4%. 56/56 runs passed `validate_small_capital_grid.py` (cash reconciliation, integer shares, sizing, determinism 18/18). Bound-dependent names: MANGCHEFER, CAIRN, TATAMTRDVR. Research-only engine policies (`missing_held_bar_policy`, `fractional_entitlement_policy`, `unverified_share_action_policy`) default `strict`; guard tests keep the paper manager from reading them. **Never use them for paper/live.**
* Preliminary fill audit (45 fills, 2026-11-05, old code): model slippage ~8.5 bps vs half-spread ~4.5 bps (conservative); utilisation of slot 86% for buys >= Rs 1,000 vs 95-99% below.

## 5. Known gaps / blind spots (read before trusting anything with money)

1. **First live run of the new code is tomorrow 09:16.** Never executed against real broker fills: ask/bid fills, quote-timestamp refusal, cash CAS, lease on Atlas. UNVERIFIED: Atlas lease self-test (command was given, result never seen) and the Telegram test message (alerts depend on `TELEGRAM_ENABLED=true`).
2. Quote `timestamp` field VERIFIED present on 2026-11-08 (RELIANCE) but it is the snapshot time, not last-trade time (`last_trade_time`); it vouches for the depth snapshot, not LTP recency. Not yet seen during live trading hours.
3. Special-session 2026-11-08 bars are **provisional quote snapshots** (`source=upstox_quote`, 1,088 bars); Upstox returned no settled Nov 8 candle for 20 sampled stocks at ~20:30 IST. Signals and the new accounts' orders used them. Check whether nightly catch-up flips them to `upstox`.
4. Paper fills assume the whole order trades at the quoted price. Depth quantity capture (`quoted_qty_at_price`, `depth_shortfall`) is deployed but untested live. Real fills are the only true validation: plan a small real-order comparison stage before real money.
5. No live handler for mergers/delistings of held names (fail-closed). Dividends not modelled. Marking illiquid names at last close with no haircut can overstate sellable value. Backtests: ~180+ configurations tried on the same history (selection bias is the largest inflation risk, not code).
6. `orders` has no unique index on (strategy_id, mode, order_id); staging upserts on all three so duplicates cannot occur today; adding the index is an optional hardening step.
7. Unknown whether the server runs the systemd daemon only or also cron; the `deploy/crontab.txt` change matters only for cron. `ingest_benchmarks.py` has no weekend guard (idempotent, probably harmless).

## 6. Runbook for 2026-11-09 (in order)

1. `./deploy/manage_daemon.sh logs` around 09:16: look for fills on the three `fwd_` accounts; red flags: `ACCOUNT_BUSY`, `LEASE_LOST`, `QUOTE_FRESHNESS_UNVERIFIED`, `no_quote`, exit codes. A 3-failure alert arrives via Telegram.
2. During market hours: `./venv/bin/python scripts/check_upstox_quote_shape.py` (prints OK/FAIL for the timestamp).
3. After fills: `python scripts/report_fill_audit.py --since 2026-11-09` (read-only; runs from server or laptop). Check slot utilisation per account, slippage vs spread, `bigger_than_quoted_qty_%`.
4. Verify accounts: `python scripts/manage_strategy.py --list`; orders via the PENDING_OPEN query used earlier.
5. Still owed: Atlas lease self-test; Telegram test message (commands in the chat history; both are small read/write-then-clean checks).
6. Check whether 2026-11-08 bars flipped from `upstox_quote` to `upstox`.

## 7. Rules for whoever continues

* **Never commit or push without the user's explicit permission** (memory `git-commit-push-permission`); the user pushes and deploys themselves (`./deploy/update_and_deploy.sh`, which runs the whole suite and cancels the restart on any failure; ~8 minutes on the server).
* Do not run `manage_strategy.py --init` on an existing account; do not use `run_daily_pipeline.py --strategy all` to bootstrap new accounts (it re-runs the live ones).
* Any change near the live path needs a failure-mode test and a static guard (`tests/test_production_invariants.py`, rows R1-R12) and must be validated without parquet data (server loads from MongoDB).
* Do not describe results as validated: no corrected 20y CAGR, no out-of-sample holdout, paper only.

## 8. Next work, in priority order

1. Tomorrow's runbook above; fix whatever the first live fills expose.
2. Resolve MANGCHEFER (NSE/BSE scheme documents: ratio, record date, last trading day, fractions) and AEGISLOG (fraction policy), encode as explicit events, rerun the strict replay in "collect all" mode, then re-run `recompute_register_metrics.py`. The NSE PDFs were not obtainable by the fetchers; the user offered to save them to `research/audit_2026-11-08/evidence/` (folder exists, empty).
3. After 3-4 weeks of clean running, consider two more accounts only: `fwd_alpha_semivol_pit_50k_10slots_under1000`, `fwd_minervini_trend_template_50k_10slots_under1000`.
4. Optional hardening: unique index on orders; a live merger/delisting event record; a setter for `max_entry_price` on existing accounts (currently a direct MongoDB edit).

## 9. File map

Code: `src/nse_quant_lab/{entry_filters,price_basis}.py`, `paper/{lease,locking,manager}.py`, `engine.py`, `config.py`, `registry.py`, `db/models.py`, `upstox_feed.py`, `holidays.py`.
Scripts: `manage_strategy.py`, `normalize_strategy_docs.py`, `report_fill_audit.py`, `check_upstox_quote_shape.py`, `run_scheduler_daemon.py --preview-schedule`, `build_execution_panel.py` (laptop).
Tests: `tests/test_audit_20261108.py`, `test_entry_price_cap.py`, `test_distributed_lease.py` (needs local MongoDB), `test_account_consistency.py`, `test_same_class_account_isolation.py`, `test_production_invariants.py`.
Docs: `docs/FORWARD_TEST_SMALL_CAPITAL.md`, `docs/AUDIT_2026-11_FINDINGS_AND_REGRESSION_GUARDS.md`, `docs/RESULTS_REGISTER.md`, `research/audit_2026-11-08/AUDIT_REPORT.md`.
Research outputs: `research/audit_2026-11-08/small_capital_grid/` (`results.csv`, `validation_report.csv`; ledgers in `runs/` are git-ignored and regenerable with `small_capital_grid.py`).

## 10. ADDENDUM 2026-11-09 - UNCOMMITTED, NOT DEPLOYED (server still runs `9571b42`)

Sections 2 and 5 describe `MISSING_HELD_BAR` as fail-closed (live run stops for that strategy). A local working-tree change **replaces that for the live paper manager only** (research engine still raises):

* `generate_post_market_signals` no longer raises on a held name with no bar for the decision date. The name is retained at last close (never sold or written down), CRITICAL log + Telegram alert, healthy names still get their daily exits/stops, **all rebalance staging is skipped** while any held name is unpriced, and the result carries `missing_held_bars`.
* `run_daily_pipeline.py` turns `missing_held_bars` into a problem -> exit 4 -> scheduler retry. It also now reports `REFUSED_STALE_DECISION_DATE` as a problem (it previously exited 0; the check was unreachable inside the PAUSED/ARCHIVED branch).
* Consequences to know: a name that never returns blocks that strategy's monthly rebalance until `catchup_eligible` is set by hand; NAV carries the stale mark; the alert repeats on each retry.
* Tests: full suite 340 passed, 1 skipped before the two new pipeline tests; the five suites required by AGENTS.md (failure modes, remediation, invariants, parity, order-id isolation) 79 passed with them. NOT run: server (no-parquet) suite.
* Needs the user's decision to keep (it reverses their earlier fail-closed choice) and their own commit/push/deploy.
* **Permanent-gap resolution (new, same uncommitted state):** `python scripts/record_corporate_event.py ...` (preview; `--apply` to store) records an evidenced `CASH_OUT` or `CONVERSION` in `corporate_events`; the next daily pipeline run (scheduler retry or `run_daily_pipeline.py --strategy <id>`) applies it once per account. Code: `src/nse_quant_lab/corporate_events.py`, `PaperTradingManager._apply_corporate_events`. Tests: `tests/test_corporate_events.py`, `test_r14_*`. UNVERIFIED: never run against the real MongoDB/Atlas or a real NSE scheme; MANGCHEFER (ratio 187:100 secondary-sourced, fraction treatment unknown) must be recorded from the primary NSE document, never from guesses. Needs the user's commit/push/deploy.
