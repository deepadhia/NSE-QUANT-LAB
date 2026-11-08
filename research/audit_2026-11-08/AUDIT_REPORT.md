# Learning Lab quantitative audit — 8 November 2026

**Verdict: the headline is reproducible, but execution realism and historical validity do not pass the repository's acceptance standard. Do not treat 20% as an established forward CAGR.**

Audited HEAD: `0c7dfe3`. Existing uncommitted research and `docs/RESULTS_REGISTER.md` were preserved. No production source, strategy parameters, broker settings, or database documents were changed. Nothing was committed or pushed. Audit scripts, evidence and this report are new local files in this directory.

`VERIFIED` below means executed and inspected; synthetic examples are correctness tests, **not performance evidence**. `READ` means source inspected. `UNVERIFIED` means the necessary independent evidence is absent. No issue below is claimed to have occurred in a real brokerage account.

## 1. Fresh historical reproduction

Command: `python research/audit_2026-11-08/reproduce_baseline.py`.

Uses the actual production `TrendMomentumPITStrategy` and `BacktestEngine`, fresh `load_pit_universe(..., include_live=False)`, and the local Mongo Nifty 500 series. No cached indicator pickle or replacement engine loop. Rs 100,000, 15 slots, APPROACH_3, dated tax model, existing tiered slippage, 2007-01-01 through 2026-10-30. Runtime 389.9 seconds; 3,402 loaded symbols.

| Measure | Independently checked result |
|---|---:|
| First / last actual NAV session | 2007-01-02 / 2026-10-30 |
| Sessions | 4,913 |
| Starting capital | Rs 100,000 |
| First day's closing NAV | Rs 104,086.36 |
| Final NAV | Rs 4,198,266.07 |
| Closed trades / buys | 709 / 719 |
| CAGR from first EOD NAV, used by research reports | 20.5018% |
| Calendar CAGR from actual initial capital | 20.7455% |
| Maximum drawdown | -41.4038% |
| Arithmetic Sharpe, 252 sessions, zero risk-free rate | 1.0513 |
| Arithmetic Sharpe, 252 sessions, 6% risk-free rate | 0.7506 |
| Worst calendar year | -36.75% |
| Lowest rolling 1,260-session annualised return, five-year approximation | 2.06% |

The entire new closed-trade CSV is **byte-identical** to the pre-existing A0 ledger. This verifies reproduction of R11/R20 on the supplied panel. It does not independently certify that panel or its fills.

Independent reconciliation of initial cash + signed execution cash flows - assessed annual tax + terminal marked holdings reaches reported NAV within **Rs 0.018** (rounding). That is positive evidence for the ordinary accounting path. It does not validate hypothetical executions.

Evidence: `baseline/` contains config, benchmark, daily NAV, orders, executions, trades, metrics and manifests; `source_manifest.json` hashes production Python files; `artifact_analysis.json` contains independent calculations.

## 2. Findings established by executable probes

Command: `python -m pytest research/audit_2026-11-08/probes.py -q --tb=short`.

**Nine probes failed against audited HEAD.** These explicitly assert desired invariants and intentionally remain red as audit evidence. They live outside normal `testpaths=tests`. See `probes_final.txt` for observed output. They are not nine newly broken existing tests.

### F1 — HIGH: future-adjusted prices determine historical executable lots and eligibility

Locations: `research/audit_2026-10/scripts/build_clean_panel.py:136`, `:166`; `src/nse_quant_lab/strategies/trend_momentum_pit.py:251`; `src/nse_quant_lab/engine.py:685`; `src/nse_quant_lab/cost_model.py:53`.

The panel back-adjusts OHLC for later splits/bonuses. The strategy then treats adjusted close as the nominal Rs 10 price, and the engine floors quantities in adjusted units. Budget gates and tick costs also use this basis. Ratio indicators can use adjusted histories; historical rupee floors and integer trading units cannot use the latest share basis without conversion.

**VERIFIED:** adding a later 10:1 split to an otherwise identical synthetic economic history removes a previously eligible candidate. In 2024 raw-v-panel data, **337 liquid EQ stock-days across 10 symbols** have actual close >= Rs 10 and adjusted close < Rs 10. These are threshold mismatches, not a claim that every such observation passes all other entry conditions.

More decisively, raw EQ opening prices matched **653 of 719 historical buys**. **205** imply fractional contemporary shares. DIVISLAB on 2007-07-02 buys 19 adjusted units at Rs 293.50, equivalent to **0.95 actual shares at Rs 5,870**. The engine therefore books a position smaller than one purchasable share. CUPID's 4,597 adjusted shares bought on 2025-09-01 correspond to 919.4 contemporary shares before the later bonus.

**Effect:** historically infeasible sizing and future-dependent candidate selection. Direction and total CAGR impact are **not established**; the price floor can exclude winners as well as losers. Remediation requires raw execution prices, dated adjustment factors, actual integer holdings, and explicit corporate-action events. Do not simply remove adjustments from indicators.

Evidence: `integer_lot_analysis.json`, `integer_lot_reconciliation.csv`, `raw_adjusted_floor_mismatches_2024.csv`, `cupid_raw_adjusted_example.csv`. The 66 unmatched buys need identity/series reconciliation; they were not treated as validated.

### F2 — HIGH: terminal data disappearance creates fictional cash proceeds

Location: `src/nse_quant_lab/engine.py:138`.

**VERIFIED:** the first timeline session after a symbol's last stored bar sells it at its previous close less normal slippage, without a tradeable quote, suspension/delisting evidence, or corporate consideration. This branch precedes circuit checks. Removing a final observation from a fixture creates a cash-producing `DATA_ENDED_FORCED_EXIT`.

There is **one actual occurrence** in this baseline: MANGCHEFER, bought 2025-09-01, sold 2025-10-31 at stale raw price Rs 308.75, 459 shares. Whether the company merger/cessation entitled a holder to different securities or cash has **not** been independently reconciled. This establishes exposure to the branch, not the correct counterfactual loss.

Remediation: distinguish missing history, suspension, merger and confirmed liquidation; retain blocked capital until an evidence-based treatment is available. Stress zero recovery separately and retain a complete event ledger.

### F3 — HIGH: 09:16 paper buys can receive an already unavailable opening price

Locations: `src/nse_quant_lab/paper/manager.py:884`, `:1247`, `:1273`.

**VERIFIED:** with a fresh 09:16 quote showing open=100, current ask=110 and LTP=110, the manager books **199 shares at 100.10**. The +2% cap is tested against the earlier opening print, so the current ask above the cap does not block it. Bid/ask is logged, but is not the executable fill reference.

This can make paper returns appear to validate an edge that cannot be captured by an order submitted at 09:16. A pre-open auction order model would need pre-submitted orders and known sizing; it cannot be retroactively inferred from the 09:16 snapshot. Choose and implement one explicit timestamp/execution model, then replay and recalibrate costs against that model.

Upstox distinguishes OHLC opening price, latest price and current depth in its [official full-quote schema](https://upstox.com/developer/api-documentation/get-full-market-quote/).

### F4 — HIGH: live circuit decisions compare the old open with current circuit/depth

Locations: `src/nse_quant_lab/paper/manager.py:1098`, `:1226`.

**VERIFIED:** a stock opens at 100, reaches its 95 lower circuit by 09:16, has no bid, and the manager nevertheless sells 100 shares at **99.90**. It compares `open_px` to the circuit band rather than the price at the execution timestamp. The symmetric upper-circuit path has the same source pattern (`READ`).

Use current executable quotes and side-specific depth/bands; defer when liquidity is unavailable. Daily historical OHLC cannot establish when the opening-price liquidity was available to this scheduler.

### F5 — HIGH: overnight crash recovery can permanently lose an executed buy

Locations: `src/nse_quant_lab/paper/manager.py:1019`, `:1177`.

**VERIFIED:** crash after execution insertion and cash persistence but before position persistence. On a next-session restart, the stale-signal guard expires the BUY **before** `_prior_execution`/`_recover_fill` can run. The fixture ends with cash **Rs 80,056.47**, one execution, no position, and `SKIPPED_EXPIRED` status. Existing same-session restart tests miss this boundary.

Recover every partially persisted execution before stale-order policy, holiday logic or signal replacement; preserve the original execution date when restoring positions and trades. Add next-session and multi-day crash tests.

### F6 — HIGH: overlapping workers can lose cash deltas despite deterministic execution IDs

Location: `src/nse_quant_lab/paper/manager.py:226`.

**VERIFIED at the persistence seam:** two workers read cash=100,000, then persist distinct executions using balances 99,000 and 98,000. Stored cash becomes **98,000 instead of 97,000**, while both execution markers are accepted. Atomic absolute `$set` plus a unique execution marker does not serialize two different executions. Duplicate-key exceptions are also caught without proving which execution owns the subsequent bookkeeping.

This is a deterministic interleaving demonstration, **not a live concurrent Mongo load test**. No cross-process account lease was found in the inspected manager/scheduler/deployment paths. Implement an account-level lease or transactional/compare-and-swap state machine and test independent processes and ambiguous write acknowledgments. A post-hoc log-only cash warning does not repair lost deltas.

### F7 — HIGH: a quote outage can be booked as a delisting sale

Locations: `src/nse_quant_lab/paper/manager.py:405`, `:470`, `:1054`, `:1065`.

**VERIFIED:** a staged `DATA_ENDED_FORCED_EXIT` plus no available quote books a sale of 100 shares at **95** from a stored mark of 100. No authoritative delisting is required. The production signal path can stage this reason solely because a holding is missing from the universe or >=14 calendar days stale. Ten circuit deferrals can similarly force a paper cash realisation even though no buyer has been established.

The earlier audit's recovered-data cancellation handles a returning quote; it does not handle a continuing outage. Missing data must remain a failed/unpriced valuation, not a filled order. Separate an accounting write-down from an executable sale and keep illiquid capital unavailable.

### F8 — MEDIUM: final partial financial-year realised tax is omitted

Location: `src/nse_quant_lab/engine.py:827`.

**VERIFIED:** tax is assessed only at March-end. An April profitable exit with the backtest ending in April reports no tax; its own tax helper calculates Rs 2,022.45 in the fixture. For this actual 20-year ledger, the final partial FY's estimated realised liability is **Rs 2,692.75** after existing loss carry-forwards. Subtracting only this liability gives NAV Rs 4,195,573.32 and capital-based CAGR **20.7415%**. Thus this defect is real but explains only ~0.004 percentage points here.

This is not a terminal liquidation-tax estimate: unrealised holdings, bonus-share tax lots, grandfathering and investor-specific tax treatment need separate handling. Accrue the final partial FY liability even if cash is not yet paid; distinguish assessed tax, paid tax and payable tax (`total_tax_paid` currently sums assessments).

### F9 — MEDIUM: FY2024-25 LTCG sale-date rates are bypassed

Location: `src/nse_quant_lab/tax_india.py:156`.

**VERIFIED:** `fy_end >= 2025-03-31` captures FY2024-25 and applies the post-budget rate to the whole long-term bucket. The commented FY2024-25 blending branch is unreachable for March-ending financial years. A Rs 300,000 gain sold in June 2024 produces **Rs 22,750**, versus **Rs 18,200** under the fixture's Rs 125,000 exemption and pre-change 10% rate plus 4% cess. Here the bug **overstates** tax; it does not inflate CAGR.

The [CBDT FAQ](https://www.pib.gov.in/newsite/erelcontentt.aspx?_d=2036604&lang=2&reg=48) confirms that changed rates apply to transfers from 23 July 2024 and the Rs 125,000 exemption applies to FY2024-25. Repair the dated buckets and set-off allocation with hand-checkable mixed-date fixtures, rather than only moving this boundary.

## 3. Additional source/data findings

**F10 — MEDIUM, VERIFIED calculations / READ definitions: inconsistent reported metrics.** `metrics.py:27` annualises using observed sessions/250; `:59` uses CAGR minus 6% divided by volatility as "Sharpe". Research `minervini_20y.py:50` and `ab_rs55_filter.py:110` instead use terminal/first-EOD wealth and arithmetic zero-RF Sharpe. For this run, headline Sharpe 1.05 versus ~0.75 with 6% RF is a convention difference, not different trades. CAGR 20.50 versus 20.75 likewise comes from the starting denominator. Standardise names, initial NAV anchoring, RF and annualisation. Do not interpret the reporting correction as an improved edge.

**F11 — HIGH research-integrity risk, VERIFIED panel evidence / UNVERIFIED economic attribution: inferred corporate actions.** The panel contains **1,395 official-labelled adjustments and 52 `B_extreme_clean` adjustments** inferred from price ratios (`build_clean_panel.py:154`). For example, ADANIENT's 2015-06-03 event is treated as a 1/6 price factor; [NSE lists a scheme of arrangement](https://www.nseindia.com/companies-listing/corporate-filings-actions?symbol=ADANIENT&tabIndex=equity). A demerger cannot automatically be represented as owning more of the same security; consideration and resulting securities require reconciliation. None of the 52 inferred dates overlaps a baseline **closed** holding, which limits direct P&L attribution; they can still change earlier nominal filters, indicators, ranks and selection. Open-position and indirect selection effects were not exhaustively isolated. Neither the "official" label nor successful parsing independently certifies event provenance. Quarantine unresolved events and document security identities and entitlements.

**F12 — MEDIUM, READ: live universe drifts from the historical contract.** The panel-backed append path only loops over existing panel symbols (`data_loader.py:518`), so new live-source symbols are not added. The Mongo fallback applies today's series metadata across the whole history (`:381`) and defaults missing symbols to EQ. This differs from the historical panel's daily EQ/BE status and can omit future qualifying IPOs or admit an unverified series. Coverage gates operate on an aggregate liquid set and do not guarantee every held symbol is current. Add explicit held-symbol gates, dated membership/series, and a universe refresh/onboarding path. No return impact was quantified in this audit.

**F13 — MEDIUM, READ: documented reproduction depends on a changed default.** `minervini_20y.py` does not explicitly set `execution_approach`; current `BacktestConfig` defaults to APPROACH_3. Thus the documented command for old BASELINE R1/R5 no longer pins their execution mode. `reproduce_baseline.py` in this audit explicitly pins APPROACH_3 and reproduces deployed R11. Freeze strategy/engine/data hashes, complete config, benchmark and experiment selection record for every published number. A SHA manifest of output files alone is insufficient provenance.

## 4. Is 20% realistic?

**Reproducible on this dataset: yes. Established as a realistic expected return: no.** The main unresolved objections are execution and selection validity, not an inability to calculate compound returns.

- **Concentration:** CUPID is Rs 1,491,956, **35.54% of terminal NAV**, with Rs 1,332,640 unrealised gain. Marking only CUPID at its entry outlay gives **18.44%** capital-based CAGR; marking all ten terminal holdings at entry outlay gives **17.46%**. These are endpoint sensitivities with no path re-simulation, not alternate strategies, future-return estimates, or automatic reasons to sell winners.
- **Risk experience:** -41.4% maximum drawdown and a roughly five-year rolling stretch near 2.1% annualised. A 20-year average hides long disappointing periods and substantial terminal winner exposure. Position slots limit new buys, not the weight of an appreciated holding.
- **Selection bias:** the results register records roughly 180 tried configurations and later RS55 arms. DEV/VAL/LATE slicing does not make repeatedly inspected periods out-of-sample. An honest forward test must begin after rules, data treatment and execution model are frozen. No future CAGR range, including the register's 15–18% planning range, is statistically established by this audit.
- **Benchmark mismatch:** Nifty 500 price return excludes reinvested dividends and is not a size/exposure-matched alternative. The strategy also omits dividends, so the exact differential correction is not a blanket dividend yield subtraction. Compare both sides on a consistent total-return, cash/exposure and tax basis; also compare a matched momentum/small-midcap or no-skill basket.
- **Cost calibration remains weak:** the tier spreads/impact are assumptions, and a fraction of full-day turnover does not establish opening-minute capacity. Existing stress reports based on copied research engines do not validate the current corrected execution model. Run 1.5x/2x costs, delayed entries, blocked exits and increased capital after the pricing basis is repaired, without selecting new parameters from those results.
- **Data limitations:** this audit checked the supplied panel's structure, not every original exchange download, delisting recovery, ticker/ISIN lineage, revision timestamp or corporate entitlement. "PIT" in a class name does not certify all those dimensions.

## 5. Operational and validation evidence

**VERIFIED existing suite:** `python -m pytest -m "not live_upstox" -q` -> **269 passed, 1 skipped in 190.62 seconds**. The skip is `test_parity_with_research_harness_on_real_data` (`tests/test_trend_momentum_pit.py:189`): its availability guard checks only `data/external/...`, while this checkout's panel is under `external/...`. Thus an available local panel did not make that test execute. Passing engine/paper parity does not prove realistic fills when both paths share an assumption. Several production invariants are source-text checks, not full failure-mode demonstrations.

**VERIFIED preflight:** `python scripts/verify_production_health.py` -> exit 0; all five check groups passed. See `health_check.txt`. Configured credentials and connectivity do not establish executable fills.

**VERIFIED read-only paper snapshot:** all three active strategies have signal date 2026-11-06, execution date 2026-11-05, 15 executions, 15 positions and 15 quote-bearing fill-audit records each. Stored cash reconciles with the execution ledger to rounding; no duplicate execution IDs or nonpositive positions were found in that snapshot. Contrary to older register notes, fill-audit data now exists. These **45 paper records are not 45 independent broker fills**, and strategies may share the same stocks and session. See `paper_snapshot.json`.

The stored directional quote-v-open averages are **-46.65 / -30.23 / -48.52 bps** for trend / Minervini / alpha, versus model slippage around +8.5 bps and half-spreads around 4.5 bps. The contemporaneous quote is worse than the model fill in 2 / 6 / 2 records. This small sample therefore does **not** show uniformly optimistic prices; most quoted buys were cheaper than the opening print. It does show that the opening print and the later quote are different references. It cannot calibrate twenty years of execution or repair the synthetic untradeable-price counterexamples.

**VERIFIED structural panel checks:** 8,367,109 rows, 3,989 symbols, 2006-01-02 through 2026-10-30; zero duplicate symbol/dates, NaN OHLC/volume, nonpositive prices, negative/zero volumes, or invalid OHLC envelopes (1e-9 tolerance). Clean row arithmetic does not validate adjustments or economic identity. See `panel_analysis.json`.

Not run: actual broker orders; live 09:16 capture; multi-process real-Mongo race injection; corrected full-history simulation; current-engine 1.5x/2x cost and one-session-delay grid; certified total-return benchmark comparison; complete delisting/merger reconstruction; prospective holdout. They require a corrected execution/data contract or evidence not available in this audit. No historical return correction for F1–F7 or F11 was invented.

## 6. Remediation order and acceptance

1. Protect paper-account integrity: recover partial executions before expiry; serialize account mutations; stop converting outages/circuit locks into sales.
2. Freeze an executable timestamp model: pre-open orders or 09:16 executable quotes, side-specific liquidity, current circuit checks and auditable rejected orders.
3. Separate raw executable prices, causal analytical prices and actual share units. Apply dated split/bonus events and reconcile merger/demerger entitlements. Audit tax lots for bonus shares separately.
4. Repair dated tax buckets, final-FY accrual and metric definitions; pin all reproduction configs.
5. Re-run the fixed engine on the original history, compare ledgers, then perform predeclared cost/delay/capacity/missing-data stresses and matched benchmarks. Freeze rules before collecting new forward evidence.

No production fix was bundled: changing pricing units, liquidation semantics or execution timestamps without an agreed specification would silently change the financial experiment. The audit establishes concrete reproductions and the repair sequence; it does not label an uncorrected backtest safe.

Under `BACKTEST_VALIDATION.md`: **`VALID_HISTORICAL_SIMULATION` acceptance FAILED** because executable-lot, temporal/execution and missing-data invariants fail; data-event provenance remains **INCOMPLETE** and robustness **NOT ESTABLISHED**. The repository is materially more testable than a typical research notebook, but the supplied evidence does not establish a deployable 20% CAGR.
