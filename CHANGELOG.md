# Changelog

Newest first. Entries describe what changed in the code, not investment performance. Performance numbers live only in `docs/RESULTS_REGISTER.md`.
(Corrected 2026-11-04: everything below is committed and pushed up to `4c879b8`; the earlier "nothing committed" note and the performance claims in
1.1.0 were wrong or unsupported and have been fixed in place.)

## [Unreleased] - 2026-11-09 (benchmark ingestion: Upstox primary)
- Changed: `scripts/ingest_benchmarks.py` now takes Nifty 50 / Nifty 500 candles from Upstox (`NSE_INDEX|...`, last 40 days, re-upserted every run) with Yahoo as the long-history layer and the same-day fallback (Upstox publishes the same-day index candle late). Every bar stores `source` (`upstox` / `yahoo_provisional` / `yahoo`). Special sessions (Diwali Muhurat) are kept from Upstox even when Yahoo drops them.
- Added: freshness check (no bar for the latest completed NSE session -> exit 1, scheduler retries; previously silent), Yahoo-vs-Upstox close cross-check (0.1%), rejection of OHLC-inconsistent bars and of Yahoo-only bars inside Upstox's range, Telegram alert when degraded (Upstox outage/no token/mismatch; exit stays 0 because the daemon holds the 18:00 pipeline while this job retries), `--dry-run`. Tests: `tests/test_benchmark_ingest.py` (13).
- Not changed: strategy rules, regime gate, schema consumers (only an extra `source` field).

## [Unreleased] - 2026-11-07 (live-path fixes + results reconciliation)
- Fixed: open-position shares could be multiplied twice on a split (Upstox ingestion rescale + recorded `corporate_actions` document). Ingestion now defers to a matching recorded action; an unrecorded gap is rescaled once and stamped in `applied_actions` (`SYMBOL:<date>:ingest`), which the manager honours. Tests: `tests/test_split_single_application.py`.
- Fixed: `run_daily_pipeline.py` could never import `scripts.audit_daily_portfolio` when run as a script (repo root missing from `sys.path`); the failure was swallowed. Static gate added.
- Corrected: the "Approach 3 gives 22.18% (+0.86 pp)" claim came from `ab_execution_approaches.py`, a re-implemented copy of the engine loop. Through the real engine: baseline 21.32%, Approach 3 (deployed) 20.50%, other variants 21.28-21.43% (register R11-R19). No return benefit; Approach 3 is retained for execution realism. Guard added: research subclasses of `BacktestEngine` must be labelled NOT AUTHORITATIVE.
- Deployed-setup 20-year results (register R11-R13): trend_momentum_pit 20.5%, alpha_semivol_pit 19.7% (first production-class run), minervini 18.0%; CUPID is the top open holding in all three. Planning range 15-18%.
- Live-path hardening (audit rows S3-S7): Mongo fallback loader checks every session since 14 days before the newest bar and fails loudly on Mongo errors; regime benchmark failures are no longer cached and a benchmark missing the decision date makes the pipeline exit 4; broker quotes from another session are refused; open positions keep receiving ingestion bars; past-date audits no longer show today's positions and the daemon audit waits for pipeline success. 269 tests pass, 1 skipped.
- yfinance is no longer a fill source by default (`ALLOW_YFINANCE_FALLBACK=true` to opt in): its quote has no timestamp and its `open` can be yesterday's. Outage-expired BUYs now flag the catch-up scan. Added `scripts/probe_upstox_quotes.py` (read-only 09:16 payload probe) and `research/audit_2026-10/scripts/minervini_rs_pool_check.py` (RS-pool measurement). Audit rows S8-S10.
- No engine/strategy rule changed in this entry.

## [Unreleased] - 2026-11-04 (documentation reconciliation; no engine/strategy logic changed)
- Added `docs/RESULTS_REGISTER.md` (single source for every backtest number) and the research scripts `research/audit_2026-10/scripts/minervini_20y.py`, `open_positions_check.py`.
- Corrected claims in this file, `README.md`, `AUDIT_REPORT.md`, `docs/STRATEGY_TREND_MOMENTUM_PIT.md`, the `trend_momentum_pit.py` docstring and the parity playbook (see below and the register).
- First real-panel 20-year runs of `minervini_trend_template` (19.3% CAGR, max DD -47.6%) and of the production `trend_momentum_pit` (21.3%, max DD -41.3%): register rows R5/R1.
- Documented a previously unrecorded behaviour change: commit `9c7813f` applied the pre-registered, user-approved "HoldFix" hold rule to `trend_momentum_pit` (held names only need close >= EMA(100) and EQ, not the entry filter). Earlier lab figures (17.1%) were produced with the old rule.

## [1.1.0] - 2026-11-01 (Arm 2 Catch-Up & 100% Backtest/Paper Parity)

### Added
- **Arm 2 Event-Driven Regime Catch-Up (`trend_momentum_pit.py`)**: Asynchronous rebalancing triggered whenever the benchmark (`^CRSLDX` Nifty 500) crosses above its 200-session SMA mid-month (Bear $\to$ Bull transition). Deploys idle cash at next morning open ($T+1$) while preserving existing winning holdings. A/B over 20 years (`results/ab_regime_catchup/ab_results_summary.csv`, 4 arms, post-tax): Arm 2 vs monthly-only baseline = **+1.71 pp** full period (19.31% -> 21.02%, window to 2026-10-23), Sharpe 1.04 -> 1.06, max drawdown 2.5 pp worse; by era +0.54 pp (2007-15), +0.29 pp (2016-20), +5.43 pp (2021-26). The gain is concentrated in 2021-26; Arm 2 was chosen after seeing four arms (selection bias). *(An earlier version of this line claimed +3.02 pp and Sharpe 0.95 -> 1.05 "across all three eras"; the CSV does not support that.)*
- **Hold rule (HoldFix, not previously listed here)**: the PIT strategy's month-end candidate set now keeps a held name while close >= EMA(100) and series is EQ, instead of re-applying the 1.03x-EMA/Supertrend entry filter (pre-registered in `PROTOCOL_HOLDRULE.md`, approved 2026-10-28). Effect in the ledgers: 1,001 -> 594 trades (monthly only).
- **20-Year Multi-Era Validation Harness**: Pre-registered A/B backtest runner `research/audit_2026-10/scripts/ab_regime_catchup.py` testing 4 distinct rebalance arms across 4 windows (`Full_2007_2026`, `W1_2007_2015`, `W2_2016_2020`, `W3_2021_2026`). Summary results tracked in `research/audit_2026-10/results/ab_regime_catchup/ab_results_summary.csv`.
- **Live Circuit Bands & Exchange Quotes in Paper Manager**: `_get_execution_bar()` now extracts live broker quote parameters (`upper_circuit`, `lower_circuit`, `bid`, `ask`), with real-time lower circuit lockout guards during morning sell fills.
- **Atomic Re-Run Cash Durability**: `execute_morning_fills()` persists cash to both `strategies` and `daily_nav` collections on every fill, and `get_current_state()` synchronizes real-time cash when querying today's date, preventing state desynchronization on mid-batch process interruptions.

### Fixed & Aligned
- **Backtest-to-Paper Exact Parity (`engine.py` & `paper/manager.py`)**: Aligned backtest share sizing to calculate shares at $T+1$ execution open using `actual_open`, `fees_buffer_pct`, and the strategy's liquidity-tiered cost model `_slip(...)`, matching `PaperTradingManager`. Added unit-by-unit scale-down loop to prevent cash overdrafts. Confirmed 100% exact parity (20/20 buys, 6/6 sells, ₹0.04 NAV delta across 4 months / 83 trading sessions in `replay_reconcile.py`).
- **100% Strict Point-in-Time Universe Loading (`data_loader.py`)**: Lowered pre-loop filter to `len(g) < 50` and removed post-join whole-life `len(df) >= min_rows` filter, delegating history checks exclusively to `hist_n >= 226` dynamically evaluated on each decision date. Added path fallback for `external/nse_archive/clean_panel.parquet`.
- **Duplicate Buy Order Guard (`paper/manager.py`)**: Purged all unexecuted `PENDING_OPEN` rebalance orders across previous dates on rebalance signals, and added defensive check in morning fill loop to skip symbols already held in `positions` (`SKIPPED_ALREADY_HELD`).
- **Single-Day Missing Bar Fallback (`trend_momentum_pit.py`)**: Added `df.iloc[k - 1]` fallback for held positions on decision dates to prevent accidental `REBALANCE_DROP` liquidations when a held stock halts.
- **Benchmark Timestamp Alignment & Resilience**: Deduplicated benchmark OHLCV records on date, cast `bench.index.get_loc(decision_date)` to integer, and added automatic fallback to local MongoDB if remote Atlas connection fails. Added stale benchmark warning gate in `run_daily_pipeline.py`.
- **Production Crontab & Schedule Alignment**: Harmonized crontab schedules across `deploy/crontab.txt`, `scripts/run_scheduler_daemon.py`, `scripts/run_daily_pipeline.py`, and documentation to **17:45 IST** (settled EOD candle ingestion) and **18:00 IST / 6:00 PM** (post-market strategy pipeline scan).

---

## [Unreleased] - 2026-10-31

### Added
- Upstox full-tier daily feed (verified live 2026-10-31: 1,108 of 1,108 stocks fetched, closes identical to the bhavcopy): `src/nse_quant_lab/upstox_feed.py` (ISIN-based instrument keys, V3 daily candles at a safe request pace, batched market quotes with bid/ask, `UpstoxQuoteProvider`), `scripts/ingest_upstox_fulltier.py` (`--dry-run`, `--limit`, `--symbols`, `--anchors`; coverage gate; split/gap flags), live-bar join with a session-coverage gate and history re-anchoring in `data_loader.load_pit_universe`, broker-quote opening price for 09:16 fills in the paper manager. 20 new tests.
- `src/nse_quant_lab/tax_india.py`: dated Indian capital-gains tax for listed equity (STCG 10% / 15% / 20% by sale date; LTCG exempt before 2018-04-01, then 10% over Rs 1 L and 12.5% over Rs 1.25 L; cess; loss set-off and 8-year carry-forward; STT add-back). 11 tests.
- `src/nse_quant_lab/cost_model.py`: liquidity-tiered, order-size-aware slippage (spread tiers, tick floor, square-root impact) using previous-close inputs only. Tier values and the 0.7 impact coefficient are ASSUMED, not measured. 5 tests.
- `src/nse_quant_lab/strategies/trend_momentum_pit.py` (registered `trend_momentum_pit`): production version of the researched strategy. EQ stocks with 50-session median turnover >= Rs 10 Cr and 226+ sessions of history as of the date; close >= 1.03 x EMA100 with a bullish Supertrend; risk-adjusted momentum rank; 15 slots; monthly entries; daily EMA100 exit; no rank-based drops; Nifty 500 200-day regime filter on new entries that fails safe when the benchmark bar is missing. 9 tests, including a real-data parity test against the research harness.
- `data_loader.load_pit_universe`: full-market, split-adjusted universe from `clean_panel.parquet`; fails loudly if the panel is missing or older than the requested date.
- Paper manager: fills are priced through the strategy's `slippage_pct_for` hook (flat config slippage remains the fallback); every fill is logged to Mongo `fill_audit`, with an optional `quote_provider(sym)` hook for live bid/ask (none implemented). `tests/test_paper_cost_inputs.py`.
- Engine hook `BacktestEngine._slip`; `BacktestConfig.tax_model` (`india_fy` default, `flat_legacy` for the old behaviour).
- Research harness under `research/audit_2026-10/`: `build_clean_panel.py`, `alpha_lab.py`, `basket_baseline.py`, `analyze_grid.py`, `champion_pit_test.py`, `main_v32_on_our_data.py`, `replay_reconcile.py`; pre-registered protocols (`PROTOCOL_2026-10-31.md`, addenda 1-6) and handoffs.
- `docs/STRATEGY_TREND_MOMENTUM_PIT.md`.

### Changed
- Engine NAV now nets accrued-but-unpaid tax (it was overstated while taxes were pending).
- `scripts/run_daily_pipeline.py` and `scripts/execute_paper_orders.py` load the PIT universe for `trend_momentum_pit` (default document: 15 slots, hold buffer 1500); the morning script loads all available data when no `--date` is given, because today's bar does not exist at 09:16.
- Research harness history rule: a stock needs 226 sessions of history as of the decision date. It was previously judged from the symbol's total future length, a look-ahead found by the parity test; results moved within noise.
- `.gitignore`: bulk per-run research outputs (nav/trades/orders CSVs, signal pickles, Mongo backups).
- Dev Mongo: old `trend_momentum` record set to ARCHIVED (backup JSON kept locally); `trend_momentum_pit` inserted ACTIVE.

### Fixed (found by running the pipeline end to end in scratch databases)
- Upstox full-tier ingestion now rescales open paper positions and pending sell orders when a split/bonus is flagged (the old ingestion script did this; the new one had dropped it).
- Upstox requests identify honestly as `nse-quant-lab/1.0`; Python's default signature is blocked by Cloudflare (error 1010) in front of the API, and auth failures now print Upstox's own error message.
- Paper manager crashed when registering a strategy whose class has no docstring.
- Morning execution asked the loader for data up to the current day, which does not exist at 09:16.
- Cost-model inputs arrived as NaN in the morning flow, so fills were charged 175-415 bps; they are now rebuilt from raw bars (about 9 bps for the liquid tier).
- Paper queued only 4 backup candidates and filled 10 of 15 slots; the strategy now asks for 60 backups so unused orders expire, matching the backtest's TRY_NEXT.

### Known issues / not done
- The Upstox target list is computed from the bhavcopy panel as of its last date and does NOT refresh on its own; a monthly instrument-master refresh is designed but not built (see handoff section 19).
- Not yet exercised live: a new session's bars (first possible 2026-11-02), the gap/split flag path and the 09:16 quote provider. The earlier HTTP 403 was a Cloudflare signature block on Python's default client (fixed by an honest User-Agent), not a token problem.
- `scripts/ingest_nse_bhavcopy.py` downloads from NSE with a spoofed browser User-Agent. It was not run and should not be scheduled or committed without a decision (use manually placed bhavcopy files or an authorised broker feed).
- No gated daily operator exists yet; `deploy/crontab.txt` still schedules the old flow and will fail loudly for the new strategy.
- No live quote provider, no corporate-action updater, no automated benchmark refresh; real fill costs are unmeasured.
- Backtest evidence is not out-of-sample and about 170 configurations were tried; see the handoff for verdicts.
