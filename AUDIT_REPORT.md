# NSE-Quant-Lab: Comprehensive Audit & Verification Report

**Date of Audit**: November 1, 2026  
**Audited Target**: NSE-Quant-Lab Release 1.1.0 (Production Core & Paper Trading Engine)  
**Primary Strategy**: `trend_momentum_pit` (Point-in-Time Trend-Momentum on Full Liquid Tier)  
**Verification Verdict (corrected 2026-11-04)**: **Software mechanics well tested; trading edge NOT proven; several original claims withdrawn.**

> **CORRECTION NOTICE (2026-11-04).** This report was drafted on 2026-11-01, before the forensic audit (`docs/AUDIT_2026-11_FINDINGS_AND_REGRESSION_GUARDS.md`). Its "verified production grade" verdict, "survivorship verified" row and the UB/UA return table (22.0% / 21.6%) were not supported by the evidence and have been replaced. Sections 2, 3, 4, 6 and 8 are corrected below. **All return and drawdown numbers are defined in `docs/RESULTS_REGISTER.md`.**

---

## 1. Executive Summary

This comprehensive audit evaluates the software engineering, temporal invariance, mathematical accounting, database persistence, and empirical trading performance of **NSE-Quant-Lab**.

NSE-Quant-Lab is an institutional-grade algorithmic trading research and forward execution engine designed specifically for the National Stock Exchange of India (NSE). It bridges the historical research environment directly to live execution via Upstox API v2, local MongoDB persistence, and automated cloud scheduling on Oracle Cloud VPS.

---

## 2. Invariance & Research Integrity Audit

Quantitative systems routinely fail when deploying to live markets due to invisible methodological flaws. NSE-Quant-Lab enforces rigorous research invariants:

| Audit Dimension | Standard Industry Vulnerability | NSE-Quant-Lab Implementation | Status |
| :--- | :--- | :--- | :--- |
| **Lookahead Bias** | Evaluating Day $T$ close signals using Day $T+1$'s open or intraday price | Signals computed strictly on Day $T$ post-market close ($15:30$ IST). Executions simulate at Day $T+1$ open ($09:16$ IST). | **TESTED** (causal-slice replay and truncation tests; tests cannot prove zero look-ahead) |
| **Survivorship Bias** | Filtering universe using today's current Nifty 500 constituents | Point-in-Time (PIT) reconstitutions tracked back to 2006. Delisted names, past constituents, and ticker changes included via `clean_panel.parquet` (8.78M bhavcopy rows).  Panel provenance and completeness were NOT audited; ~333 once-liquid names were absent from the Upstox DB and split/bonus adjustments for the 90 ingested delisted names are hand-decided (HANDOFF_2026-10-31). | **PARTIAL - NOT AUDITED** |
| **Bar Availability Invariant** | Morning execution engine querying today's end-of-day bar | Execution engine operating at $09:16$ IST operates strictly on previous close and opening quotes; zero reliance on future EOD bar. | **VERIFIED** |
| **Decoupled Exit Semantics** | Periodic rebalance logic prematurely liquidating trend positions | Pure event-driven structural exit on `Close < EMA(100)` at next open. Rebalance drops buffered by 1,500 ranks ($100\times \text{slots}$). | **TESTED** (note: at month-end a held name now stays while close >= EMA(100) and EQ, i.e. HoldFix) |
| **Market Friction & Sizing** | Static slippage or zero transaction cost assumptions | Statutory Indian delivery taxes: STT ($0.1\%$), Stamp Duty ($0.015\%$), Exchange Turnover, SEBI, GST ($18\%$), and DP charges ($₹14.75$). Slippage scaled dynamically by order size vs 50-day ADT. | **TESTED** (costs are assumed tiers, not measured against real fills) |
| **Mark-to-Market Sizing** | Sizing new positions based on historical acquisition cost | Portfolio equity marked to current market prices prior to sizing allocation. Cash buffers ($1.8\%$) preserved. | **TESTED** |

---

## 3. Backtest-to-Paper Mathematical Parity

A rigorous backtest-to-paper parity verification was executed across 83 historical trading sessions (`2024-03-28` to `2024-07-31`):

* **Buys Generated & Executed**: **20 / 20 Identical**
* **Sells Generated & Executed**: **6 / 6 Identical**
* **Portfolio NAV Delta**: **₹0.04 over 4 months** (attributable solely to standard floating-point roundoff).
* **Execution Timing**: Identical order staging on post-market close and execution on next session open.

**Scope of this parity result (added 2026-11-04):** one real-data 4-month window for `trend_momentum_pit` only; the pytest parity gate uses a seeded synthetic market. See `docs/RESULTS_REGISTER.md` section 4.

**Parity Conclusion**: The backtest simulation engine (`BacktestEngine`) and the production forward execution manager (`PaperTradingManager`) share identical sizing logic, circuit checks, fee calculations, and rebalance rules.

---

## 4. Empirical 20-Year Performance (replaced 2026-11-04)

The original table here (22.0% UB / 21.6% UA, from a research harness before the production tier switched to UH and before the audit fixes) is **withdrawn**. See `docs/RESULTS_REGISTER.md`. Summary:

* Production tier: `UH` (50-session median turnover >= Rs 10 Cr), EQ, >= 226 sessions of history, close >= Rs 10; entry close >= 1.03 x EMA(100) and Supertrend(10,3) bullish; momentum rank; 15 slots; exit close < EMA(100); Nifty 500 200-day regime filter on new entries; Arm 2 catch-up; HoldFix hold rule.
* 20-year post-tax backtest, production code: **21.3% CAGR, max drawdown -41.3%** vs Nifty 500 price 10.0%; **~19% if the single largest open position (CUPID, 9.4x, 39% of final NAV) is marked at cost**, 17.9-18.8% with all open positions at cost; 17.1% under the older hold rule. Planning range **17-19%**.
* Honest limits: nothing out-of-sample (~177 configurations tried), costs are assumed tiers (no real fills, `fill_audit` empty), benchmark is price-only, 2016-20 close to the index, survivorship only partly addressed, terminal wealth concentrated in a few positions, average hold ~110 days so almost all gains are short-term taxed.

---

## 5. Database & Pipeline Architecture (100% DB-Driven)

Paper trading is **strictly MongoDB-driven**. No state is maintained in CSV files.

### MongoDB Collections Schema (`stock_scanner`)
1. **`orders`**: Stores staged orders (`PENDING_OPEN`, `FILLED`, `DEFERRED_LOWER_CIRCUIT`, `SKIPPED_GAP_LIMIT_EXCEEDED`).
2. **`positions`**: Open holdings, shares, entry price, last close mark, and dynamic trailing stop levels.
3. **`daily_nav`**: Session-by-session cash ledger, holdings valuation, and verified NAV.
4. **`executions`**: Fill prices, execution timestamps, model vs quote slippage audit.
5. **`trades`**: Closed trades with gross P&L, net P&L, hold duration, and statutory fees.
6. **`strategies`**: Registered strategies, status (`ACTIVE`, `PAUSED`), capital allocation, and performance aggregates.
7. **`nse_bhavcopy_daily`**: **8,783,192 historical rows** (1995–2026).
8. **`upstox_fulltier_daily`**: Live daily quotes for liquid tier symbols (~1,108 stocks).
9. **`benchmark_ohlcv`**: Official Nifty 50 (`^NSEI`) and Nifty 500 (`^CRSLDX`) benchmark candles.

---

## 6. Verification & Test Suite Execution (re-run 2026-11-04)

* `pytest -q` on `main` = `4c879b8`: **227 passed, 1 skipped, 0 failed** (195 s). The skip is in `tests/test_trend_momentum_pit.py`.
* The earlier "137 tests / 136 passed / 16.28 s" figure was from 2026-11-01 and is stale.
* The compile/contract gate (`tests/test_compilation_and_contracts.py`) is part of that run.
* Warnings fixed earlier: Yahoo `auto_adjust` default, `datetime.utcnow()` deprecation; held-stock missing-bar fallback bounded to <= 5 calendar days.

---

## 7. Cloud VPS Production Deployment Summary

Automated post-market and morning cron workflows on Oracle Cloud VPS (`deploy/crontab.txt` & `scripts/run_scheduler_daemon.py`):

```cron
# 1. 09:16 IST: Execute Morning Market Open Orders (Mon-Fri, Trading Days Only)
16 09 * * 1-5 cd %APP_DIR% && ./venv/bin/python scripts/execute_paper_orders.py --strategy trend_momentum_pit >> logs/cron.log 2>&1

# 2. 17:45 IST: Ingest Today's Final Settled EOD Full-Tier Candles from Upstox API v2 (Post-Settlement)
45 17 * * 1-5 cd %APP_DIR% && ./venv/bin/python scripts/ingest_upstox_fulltier.py >> logs/cron.log 2>&1

# 3. 17:55 IST: Ingest Official Nifty 50 & Nifty 500 Settled Benchmarks (Post-Settlement)
55 17 * * 1-5 cd %APP_DIR% && ./venv/bin/python scripts/ingest_benchmarks.py >> logs/cron.log 2>&1

# 4. 18:00 IST: Evaluate Strategy Signals, Mark Positions to Close, Stage Orders & Alert (Mon-Fri)
00 18 * * 1-5 cd %APP_DIR% && ./venv/bin/python scripts/run_daily_pipeline.py --strategy trend_momentum_pit >> logs/cron.log 2>&1
```

---

## 8. Conclusion (corrected 2026-11-04)

**Verdict: the software is reasonable to run for forward paper trading; the strategy's edge is unproven.** Mechanics are well tested (227 tests, crash/idempotency/duplicate-trigger guards, parity checks of limited scope). The backtest edge versus the index is real on this data but not out-of-sample, not cost-calibrated, partly dependent on one outlier position, and concentrated in 2021-26. Survivorship and panel provenance are not audited. Forward paper trading with real quote logging is the next test, and the account may stay in cash for months while the regime filter is bearish.
