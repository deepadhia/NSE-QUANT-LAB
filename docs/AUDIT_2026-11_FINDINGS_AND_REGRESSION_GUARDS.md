# Forensic Audit 2026-11 — Findings, Fixes and Regression Guards

Audit date: 2026-11-03. Scope: backtest engine, tax/fees, paper manager, DB models, three PIT strategies, pipeline/scheduler scripts.
Evidence labels: **VERIFIED** (executed), **READ** (source inspection), **INFERRED**.

## 1. Findings and status

| ID | Sev | Defect | Fix | Guard (fails if re-introduced) |
|---|---|---|---|---|
| C1 | Critical | Order IDs had no `strategy_id`; upserts filtered on `order_id` alone, so strategy B overwrote strategy A's orders (VERIFIED by repro) | `_order_id()` namespaces IDs; `_stage_order()` upserts with `{order_id, strategy_id, mode}`; the unique `order_id` index now turns any cross-strategy clash into a `DuplicateKeyError` | `tests/test_order_id_isolation.py`, `test_production_invariants::test_c1_*` |
| H1 | High | Fill persistence was non-atomic with random exec IDs: a crash/timeout could double-credit cash or give free shares (READ) | Deterministic `EXEC_{order_id}`; order of writes execution → cash → trade/position → FILLED; `_persist_cash` writes balance + applied-marker in one atomic update; `_recover_fill` resumes any half-finished order; unique `(strategy_id, mode, exec_id)` indexes; ledger-vs-cash reconciliation alert | `test_paper_failure_modes::test_sell/buy_fill_survives_crash*`, `test_exec_ids_are_deterministic_per_order` |
| H2 | High | A data gap queued `DATA_ENDED_FORCED_EXIT` and the morning run sold a healthy stock at a 5% haircut (READ) | Forced exit is cancelled (`DATA_RECOVERED`) when the symbol has a live bar; haircut only when no tradeable quote exists | `test_data_gap_forced_exit_cancelled_when_stock_trades_normally` |
| H3 | High | Splits/bonuses never applied to open paper positions (INFERRED) | `corporate_actions` collection applied once (`applied_actions`); an unrecorded close-to-close move below 0.6x or above 1.67x raises `[POSSIBLE_CORPORATE_ACTION]` CRITICAL + alert | `test_recorded_split_applied_exactly_once`, `test_unrecorded_price_discontinuity_is_loud` |
| H4 | High | Nightly catch-up was skipped after the daily pipeline *failed or timed out* (READ) | `succeeded_today` set; timeouts retry like failures | `test_h4_scheduler_guards_trust_only_successful_runs` |
| H5 | High | Quote outage silently expired the month's BUY orders; yfinance fallback used LTP as the "open" (READ) | Unpriced orders retained (`note=NO_QUOTE`), reported in `no_quote`, morning script exits 2 (scheduler retries); stale-signal guard expires old orders; `open` only | `test_quote_outage_retains_buy_and_reports_no_quote`, `test_retained_buy_is_not_filled_on_a_stale_signal`, `test_h5_*` |
| M1 | Med | Parity gate fed the execution day's EOD bar to the morning run and never touched the live-quote path | `test_live_morning_path_parity_without_execution_day_bar` (all 3 strategies); failure-mode tests above | the tests themselves |
| M2 | Med | Re-running signals for an older date after later activity overwrote NAV and re-staged stale orders (INFERRED) | Refuse when `date < max(last_signal_date, last_execution_date)`; pipeline exits 4 | `test_backdated_signal_run_is_refused` |
| M3 | Med | `alpha_semivol_pit` guarded generic columns (`mom12_1`, `semi_vol126`) so a cache could change the ranking | Unconditional `alpha_*` columns; legacy names kept as aliases, never read | `test_precomputed_generic_columns_*`, `test_m3_*` |
| M4 | Med | Engine/paper edge-path divergences | Engine now re-prices slippage in the cash scale-down loop and tests the 25% minimum on the slipped value (matches paper). Remaining divergences: §2 | `test_backtest_paper_parity.py` |
| M5 | Med | Circuit heuristic on the degraded (yfinance) quote path can defer a stop exit | Kept conservative (paper must not book fills inside a possible lock) but now logs `[DEGRADED_CIRCUIT_HEURISTIC]`; the primary Upstox path uses real bands/depth | log only (documented behaviour) |
| M6 | Med | `except: return day >= 26` calendar fallback; Upstox holiday parser ignored exchange/type | Fallback removed (errors surface); parser keeps only NSE trading holidays for the requested year; missing-year calendar logs CRITICAL | `test_m6_*` |
| M7 | Low/Med | LTCG at `hold_days >= 365` (law: more than 12 months) | `LTCG_MIN_HOLD_DAYS = 366` shared by `tax_india` and the engine's legacy path | `test_m7_long_term_requires_more_than_365_days` |
| L1 | Low | Import-time `datetime.now()` timestamp defaults | `default_factory` | `test_l1_*` |
| L2 | Low | `days_held` never written | Updated on every signal run | — |
| L3 | Low | Engine default `eval_end_date="2026-10-23"` | Defaults to the last session in the data | `test_l3_*`, `test_production_code_has_no_hardcoded_end_date_defaults` |
| L4 | Low | Index-creation failures only warned | CRITICAL log + alert; `ensure_indexes()` runs from both pipelines | health check 2c |
| L5 | Low | `mode="paper"` hardcoded alongside `config.mode` | `self._mode` everywhere in the manager | — |
| L6 | Low | Health check was shallow and could NameError | Adds order-ID namespace, idempotency-index, benchmark-freshness and holiday-calendar checks | `scripts/verify_production_health.py` |

## 2. Known limitations that remain (documented, not hidden)

* **Engine vs paper, intentionally different:** paper writes down forced delistings at a 5% haircut and force-liquidates after 10 days of lower-circuit deferral; the engine uses `delist_haircut` (default 0) and no 10-day rule. Under `TRY_NEXT` paper stages `open_slots + paper_backup_slots` (60) orders; the engine's pool is unbounded. Both are rare.
* **Engine upper-circuit rejection** uses the fill day's full bar (conservative but not knowable at 09:16); live uses quote bands/depth.
* **Taxes:** paper charges none; the backtest never taxes the final partial FY; `flat_legacy` omits cess; `taxable_pnl` hard-codes STT 0.1%; FY2024-25 blending is pro-rata.
* **Dividends** are not modelled (price-only series). Splits/bonuses require a `corporate_actions` document (the system cannot discover them).
* **Holiday calendar:** `STATIC_NSE_HOLIDAYS` is hand-curated and was NOT verified against the NSE circular (7 entries fall on weekends: 2025-01-26, 2025-06-07, 2026-08-15, 2027-05-01, 2027-08-15, 2027-10-02, 2027-12-25 — harmless but a sign it is not authoritative). The Upstox field names (`closed_exchanges`, `holiday_type`) were taken from memory of the API, not from its documentation; the filters are no-ops if the fields are absent.
* **Fee rates** (exchange 0.00345%/0.00297%, DP Rs 14.75) were checked for internal consistency only, not against a current broker schedule.
* **Survivorship / panel provenance** was not audited here (see `research/audit_2026-10/HANDOFF*.md`).

## 3. Deployment notes for these fixes

1. **Pending orders from before the fix** use old non-namespaced IDs and may be cross-owned. `verify_production_health.py` fails if any exist; cancel/re-stage them (next signal run re-creates buys).
2. **Indexes** (`executions`/`trades` unique `(strategy_id, mode, exec_id)`) are created by `ensure_indexes()` at the next pipeline/morning run. The health check fails until they exist.
3. **Corporate actions:** insert `{symbol, ex_date: "YYYY-MM-DD", ratio}` into `corporate_actions` (ratio = new shares per old share; 1:2 split → `2.0`, 1:2 bonus → `1.5`) on or before the ex-date.
4. Exit codes: `execute_paper_orders.py` exits 2 and `run_daily_pipeline.py` exits 4 when any strategy fails or any order is unpriced/refused; the scheduler retries 3× then alerts.

## 4. Rules for changing this code (read before editing)

Every audit finding was one of six recurring mistakes. Check your change against all six.

1. **Identity.** Does any new key, ID, filter or upsert in a shared collection include `strategy_id` and `mode`? (C1)
2. **Crash points.** For each multi-write sequence, if the process dies after write *k*, does a rerun finish the job exactly once? Use deterministic keys; never random IDs. Add a crash test that raises between every pair of writes. (H1)
3. **Missing data ≠ decision.** Does any new branch turn "no data / no quote / no bar" into a sale, expiry, haircut or neutral default? It must retain state, alert and fail the run. (H2, H5, M6)
4. **Time.** Does the code path run at 09:16 with only the previous close and a quote? Test it with a universe that ends *before* the execution day. Does a re-run for an old date refuse? (M1, M2)
5. **Shared names.** Does a strategy read a column it did not compute unconditionally under a formula-specific name? Does a script hard-code a strategy list that a new strategy must also join? (M3, wiring gate)
6. **Defaults.** No import-time clocks, hardcoded dates, or `except Exception: return <guess>`. (L1, L3, M6)

When you fix a bug of this kind, (a) write the failing test first, (b) add or extend a static gate in `tests/test_production_invariants.py` for the defect *class*, (c) add a row to §1 of this file.

Required before merging any change to the areas above:

```bash
pytest tests/test_production_invariants.py tests/test_paper_failure_modes.py tests/test_order_id_isolation.py tests/test_backtest_paper_parity.py -v
pytest -m "not live_upstox"
python scripts/verify_production_health.py
```
