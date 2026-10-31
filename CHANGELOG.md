# Changelog

Newest first. Entries describe what changed in the code, not investment performance. Nothing below has been committed at the time of
writing; see `research/audit_2026-10/HANDOFF_2026-10-31.md` section 17 for the commit plan and open items.

## [Unreleased] - 2026-10-31

### Added
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
- Paper manager crashed when registering a strategy whose class has no docstring.
- Morning execution asked the loader for data up to the current day, which does not exist at 09:16.
- Cost-model inputs arrived as NaN in the morning flow, so fills were charged 175-415 bps; they are now rebuilt from raw bars (about 9 bps for the liquid tier).
- Paper queued only 4 backup candidates and filled 10 of 15 slots; the strategy now asks for 60 backups so unused orders expire, matching the backtest's TRY_NEXT.

### Known issues / not done
- `scripts/ingest_nse_bhavcopy.py` downloads from NSE with a spoofed browser User-Agent. It was not run and should not be scheduled or committed without a decision (use manually placed bhavcopy files or an authorised broker feed).
- No gated daily operator exists yet; `deploy/crontab.txt` still schedules the old flow and will fail loudly for the new strategy.
- No live quote provider, no corporate-action updater, no automated benchmark refresh; real fill costs are unmeasured.
- Backtest evidence is not out-of-sample and about 170 configurations were tried; see the handoff for verdicts.
