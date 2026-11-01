# NSE-Quant-Lab: Comprehensive Audit & Verification Report

**Date of Audit**: November 1, 2026  
**Audited Target**: NSE-Quant-Lab Release 1.1.0 (Production Core & Paper Trading Engine)  
**Primary Strategy**: `trend_momentum_pit` (Point-in-Time Trend-Momentum on Full Liquid Tier)  
**Verification Verdict**: **VERIFIED PRODUCTION GRADE & STRICT HISTORICAL AUDIT CONCORDANCE**

---

## 1. Executive Summary

This comprehensive audit evaluates the software engineering, temporal invariance, mathematical accounting, database persistence, and empirical trading performance of **NSE-Quant-Lab**.

NSE-Quant-Lab is an institutional-grade algorithmic trading research and forward execution engine designed specifically for the National Stock Exchange of India (NSE). It bridges the historical research environment directly to live execution via Upstox API v2, local MongoDB persistence, and automated cloud scheduling on Oracle Cloud VPS.

---

## 2. Invariance & Research Integrity Audit

Quantitative systems routinely fail when deploying to live markets due to invisible methodological flaws. NSE-Quant-Lab enforces rigorous research invariants:

| Audit Dimension | Standard Industry Vulnerability | NSE-Quant-Lab Implementation | Status |
| :--- | :--- | :--- | :--- |
| **Lookahead Bias** | Evaluating Day $T$ close signals using Day $T+1$'s open or intraday price | Signals computed strictly on Day $T$ post-market close ($15:30$ IST). Executions simulate at Day $T+1$ open ($09:16$ IST). | **VERIFIED (0 Lookahead)** |
| **Survivorship Bias** | Filtering universe using today's current Nifty 500 constituents | Point-in-Time (PIT) reconstitutions tracked back to 2006. Delisted names, past constituents, and ticker changes included via `clean_panel.parquet` (8.78M bhavcopy rows). | **VERIFIED** |
| **Bar Availability Invariant** | Morning execution engine querying today's end-of-day bar | Execution engine operating at $09:16$ IST operates strictly on previous close and opening quotes; zero reliance on future EOD bar. | **VERIFIED** |
| **Decoupled Exit Semantics** | Periodic rebalance logic prematurely liquidating trend positions | Pure event-driven structural exit on `Close < EMA(100)` at next open. Rebalance drops buffered by 1,500 ranks ($100\times \text{slots}$). | **VERIFIED** |
| **Market Friction & Sizing** | Static slippage or zero transaction cost assumptions | Statutory Indian delivery taxes: STT ($0.1\%$), Stamp Duty ($0.015\%$), Exchange Turnover, SEBI, GST ($18\%$), and DP charges ($₹14.75$). Slippage scaled dynamically by order size vs 50-day ADT. | **VERIFIED** |
| **Mark-to-Market Sizing** | Sizing new positions based on historical acquisition cost | Portfolio equity marked to current market prices prior to sizing allocation. Cash buffers ($1.8\%$) preserved. | **VERIFIED** |

---

## 3. Backtest-to-Paper Mathematical Parity

A rigorous backtest-to-paper parity verification was executed across 83 historical trading sessions (`2024-03-28` to `2024-07-31`):

* **Buys Generated & Executed**: **20 / 20 Identical**
* **Sells Generated & Executed**: **6 / 6 Identical**
* **Portfolio NAV Delta**: **₹0.04 over 4 months** (attributable solely to standard floating-point roundoff).
* **Execution Timing**: Identical order staging on post-market close and execution on next session open.

**Parity Conclusion**: The backtest simulation engine (`BacktestEngine`) and the production forward execution manager (`PaperTradingManager`) share identical sizing logic, circuit checks, fee calculations, and rebalance rules.

---

## 4. Empirical 20-Year Post-Tax Performance (2007 – 2026)

Evaluated on the unconstrained, survivorship-bias-free PIT clean panel (`clean_panel.parquet`, 8.78M bhavcopy rows, 2,737 stocks) with **dated statutory Indian capital gains tax** and **realistic liquidity-tiered slippage** (from `HANDOFF_2026-10-31.md` §3 & §9):

### Strategy Rules (`trend_momentum_pit`)
* **Universe**: Series `EQ`, $\ge 226$ history sessions, 50-session median turnover $\ge ₹2.5\text{ Cr}$ (`UB` tier) or $\ge ₹10\text{ Cr}$ (`UA` tier), `Close >= ₹10`.
* **Entry Filter**: `Close >= 1.03 * EMA(100)` AND `Supertrend(10, 3.0) == 1` (bullish).
* **Ranking**: Blended 6M (126d) and 12M (252d) return divided by 6M annualized volatility (`mom_6m`).
* **Exits**: Daily `Close < EMA(100)` executed at next open (`STRUCTURAL_EMA100`); no Supertrend flip exit; no rank-drop exits (`rebalance_hold_rank_buffer = 1500`).
* **Regime Filter (`R1`)**: Nifty 500 (`^CRSLDX`) `Close < 200-session SMA` blocks new entries.

### Verified Post-Tax Results (S0 R1 15 Slots, 100x Hold Buffer)
| Metric | UB Tier (cost x1.0) | UB Tier (cost x1.5) | UA Tier (cost x1.0) | Nifty 500 Price Benchmark |
| :--- | :--- | :--- | :--- | :--- |
| **20-Year Post-Tax CAGR (2007–2026)** | **22.0%** | **21.0%** | **21.6%** | **9.4% – 9.9%** |
| **10-Year Post-Tax CAGR (2016–2026)** | **16.6%** | **15.5%** | **16.3%** | **10.9%** |
| **Maximum Drawdown** | **-44.2%** | **-46.8%** | **-43.0%** | **-64.3%** |
| **Total Completed Trades** | **968** | **968** | **973** | N/A |
| **Average Hold Duration** | **~74 days** | **~74 days** | **~74 days** | Buy & Hold |
| **Tax Regime** | Dated STCG/LTCG + Cess | Dated STCG/LTCG + Cess | Dated STCG/LTCG + Cess | 12.5% LTCG |

### Honest Reality Checks & Limitations
1. **Tax Drag**: Because average hold duration is ~74 days, >99% of gains are taxed as Short-Term Capital Gains (STCG). Realized profits pay significant statutory tax on March 31.
2. **Recent Alpha Compression**: During 2016–2020, strategy returns were modest (~11–14% vs Nifty 500 ~11%). The 2021–2026 surge was largely small/mid-cap beta (no-skill basket returned ~21–22%).
3. **Drawdown Tolerance**: The strategy experiences maximum drawdowns of -43% to -47% (e.g., 2008 crash). Investors must expect 1–2 losing or flat years per 5-year cycle.

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

## 6. Verification & Test Suite Execution

### Automated Test Suite
* **Total Tests Executed**: 137
* **Passed**: **136**
* **Skipped**: **1** (optional parity check requiring external live disk archive)
* **Failed**: **0**
* **Execution Time**: 16.28 seconds
* **Compiler & Contracts Gate**: 100% clean compilation via `python -m py_compile` across all repository modules.

### Warnings & Deprecations Audit
* **Yahoo Finance `auto_adjust` Warning**: Resolved by explicitly setting `auto_adjust=False` across all benchmark and price ingestion pipelines.
* **Python 3.12 `datetime.utcnow()` Deprecation**: Replaced throughout all database models and paper managers with `datetime.now(timezone.utc)`.
* **Missing-Bar Bound**: Held stock fallback bounded to $\le 5$ calendar days to prevent zombie positions on halted/delisted stocks.

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

## 8. Conclusion

**Verdict: READY FOR FORWARD PAPER DEPLOYMENT**  
The codebase meets all requirements of institutional quantitative research: deterministic accounting, zero forward-looking bias, survivorship-free point-in-time universe loading, 100% database persistence, bound halt protection, and automated cloud scheduling. Forward paper trading on live Upstox data is the final proving ground.
