# Results Register — the one place backtest numbers are defined

Last reconciled: **2026-11-04**, code at `main` = `4c879b8` (+ uncommitted research scripts listed below).
Rule: **no document may quote a return, drawdown or test count that is not in this register or reproducible from the file named here.** Every other doc points here.

## 1. What to quote for `trend_momentum_pit` (production code)

> 20-year (2007-01 → 2026-10), post-tax (dated Indian STCG/LTCG), tiered costs, Rs 1 L start, 15 slots, real clean panel incl. delisted names:
> **21.3% CAGR, max drawdown −41.3%, Sharpe 1.07, vs Nifty 500 price index 10.0%.**
> **One stock (CUPID, 9.4x) is 39% of the final NAV.** Marking it at cost gives **~19.0–19.6%**; marking all seven open positions at cost gives **~17.9–18.8%**.
> The previous exit rule (research spec behind the 2026-10-31 handoff) gave 17.1%.
> **2026-11-07 update:** the 21.3% (R1) is the BASELINE/TRY_NEXT execution mode. The deployed paper setup runs `APPROACH_3` and, re-run through the real engine at `e265ba2`, gives **20.5%** (R11); alpha_semivol_pit 19.7% (R12), Minervini 18.0% (R13). CUPID is the top open holding in all three (ex-CUPID gain: 18.2 / 17.1 / 15.1%; all open positions at cost: 16.8 / 15.4 / 14.0%).
> **Planning range: 15–18% post-tax for the deployed setup (earlier note: 17–19% for R1). Do not plan on 21%.** Not out-of-sample (~180 configurations tried; +3 RS55 filter arms 2026-11-08, R21-R23), costs uncalibrated (no live fills), benchmark is price-only.

> **Metric definitions changed 2026-11-08 (uncommitted, `metrics.py`):** `cagr_pct` is now calendar-based from the actual initial capital (R11 reproduces as **20.75%** vs the **20.50%** first-EOD-NAV / 250-session figure quoted in this register; old value kept as `cagr_sessions_250_pct`); Sharpe uses arithmetic annual excess return / vol with 252 sessions; max drawdown includes initial capital. Registered numbers below were produced under the OLD definitions; do not mix the two. Source: `research/audit_2026-11-08/AUDIT_REPORT.md`.
>
> **Recomputed under the new definitions from SAVED NAV curves (no re-simulation; `research/audit_2026-11-08/recompute_register_metrics.py nav` -> `register_recompute/nav_recomputed.csv`):**
>
> | Run | Final NAV | CAGR new (old) | Sharpe rf 6% / rf 0 | Max DD |
> |---|---:|---:|---:|---:|
> | R11 = R20 (A0) | 4,198,266 | **20.75** (20.50) | 0.75 / 1.05 | −41.4 |
> | R21 (A1 RS55>0) | 3,872,121 | 20.25 (20.01) | 0.72 / 1.02 | −42.9 |
> | R22 (A4 RS55>=20) | 4,376,003 | 21.00 (20.73) | 0.72 / 1.00 | −41.0 |
> | R23 (A5 RS55>=40) | 2,226,975 | 16.94 (16.72) | 0.57 / 0.85 | −37.9 |
>
> The CAGR shift is purely the denominator (actual Rs 100,000 start vs first end-of-day NAV, about +0.2-0.25 pp); it is not new performance. **Not recomputable:** R12/R13 and all other rows have no saved NAV curve, and a re-simulation with the current engine stops at `MISSING_HELD_BAR: MANGCHEFER 2025-10-31` (the terminal data disappearance the old engine turned into a last-close sale; the real corporate treatment is unresolved) and, on the nominal-price panel, at `FRACTIONAL_ENTITLEMENT_UNRESOLVED: AEGISLOG 2010-08-18`. Those rows stay on the old definitions and are labelled as such. Resolve both events from primary evidence, then re-run.

Why a range and not one number: the code changed after the first research run (§3). The conservative end is the realistic expectation; the headline is what the current code produced on this history.

## 2. Run register (each row = one file you can open)

| ID | 20y CAGR | 10y (2016-10→) | Max DD | Trades | Source file | Code / spec | Status |
|---|---|---|---|---|---|---|---|
| **R1** | **21.32** (curve: 21.57) | 24.56 | −41.3 | 685 | `research/audit_2026-10/results/minervini_20y/summary.csv` (`trend_momentum_pit_control`); script `scripts/minervini_20y.py` | production class at HEAD (HoldFix hold rule + Arm 2 catch-up) | **CURRENT** |
| R1a | 18.96 – 19.58 | | | | `HANDOFF_2026-11-04.md` §8 (computed from `navfull_*.csv`, `execs_*.csv`) | R1 with CUPID gain removed (pre/post ~20% tax) | sensitivity |
| R1b | 17.92 – 18.83 | | | | same | R1 with all 7 open positions marked at cost | sensitivity |
| R2 | 19.31 (to 2026-10-23) | | −38.8 | 594 | `results/ab_regime_catchup/ab_results_summary.csv`, arm `Arm0_Baseline_Monthly` | production class, HoldFix, **no** catch-up | CURRENT (ablation) |
| R2b | 21.02 (to 2026-10-23) | | −41.3 | 685 | same, `Arm2_Pure_Regime_Flip` | = production | CURRENT; Arm 2 effect = **+1.71 pp** full, +0.54 (2007-15), +0.29 (2016-20), +5.43 (2021-26) |
| R3 | 17.13 | 17.00 | −37.8 | 1,001 | `results/lab_2026-10-31/uh_stress_results.csv` and `nav_S0_UH_R1_15_Mb100.0_tier1.0_*_tax.csv` (NAV/trade files are gitignored; local only) | research harness `LabStrategy S0/UH/R1`, **old rule: a holding must still pass the entry filter at month-end**, no Arm 2, pre-v1.1.0 engine | **SUPERSEDED** (describes the code before commit `9c7813f`) |
| R4 | 19.68 | 21.74 | −38.8 | 594 | `results/candidate_evaluation/candidate_strategies_comparison.csv` (`baseline_s0_uh_r1_15`) | research **copy** `S0BaselineCandidate`; rows loaded from cache | NOT AUTHORITATIVE (duplicate class); its 594 trades equal R2 and its CAGR is within 0.4 pp of R2 (window ends 10-30 vs 10-23), so it behaves like production-without-Arm 2, not like R3 |
| R5 | 19.28 | 22.76 | −47.6 | 505 | `minervini_20y/summary.csv` (`minervini_trend_template`) | production class `MinerviniStrategy` | CURRENT; no improvement over R1 (correlation 0.86, deeper DD); open positions not inspected |
| R6 | 17.98 | 16.34 | −42.2 | 446 | `candidate_strategies_comparison.csv` (`alpha_semivol_pit`) | research copy `AlphaSemivolCandidate` | NOT AUTHORITATIVE; no production-class run yet |
| R7 | 16.02 / 10.59 / −20.78 | | | | same file (`fip_momentum_pit`, `pocket_pivot_pit`, `uptrend_pullback_mr`) | research-only classes, not in `src/` | research; MR failed (NAV Rs 987) |
| R8 | 18.91 → 15.76 as flat slippage goes 20 → 100 bps | 21.41 → 18.18 | −38.8 → −40.5 | ~600 | `results/stress_testing/friction_stress_ladder_results.csv` | candidate copies, flat slippage | reference only |
| R9 | 25.81 | | −45.89 | | `docs/SURVIVORSHIP_BIAS_AND_REALISM_GUIDE.md` | older `TrendMomentumStrategy`, 2026-10-25, ~500-stock data, pre-bhavcopy panel | **HISTORICAL** — superseded, do not cite |
| R10 | 22.0 (UB) / 21.6 (UA) | 16.6 / 16.3 | −44.2 / −43.0 | ~970 | `AUDIT_REPORT.md` §4 (was), from `HANDOFF_2026-10-31.md` §9 | lab harness, broader tiers, pre-fix | **SUPERSEDED** (production tier is UH since 2026-10-31) |
| **R11** | **20.50** | 23.41 | −41.4 | 709 | `research/audit_2026-10/results/head_2026-11-07/summary_all.csv` (`trend_momentum_pit_APPROACH_3`) | production class + production engine at `e265ba2`, `execution_approach=APPROACH_3` (**deployed setup**) | **CURRENT** (live config) |
| **R12** | **19.71** | 22.23 | −42.2 | 543 | same file (`alpha_semivol_pit_APPROACH_3`) | production class, first production-class 20y run (R6 research copy: 17.98) | **CURRENT** (live config) |
| **R13** | **18.02** | 20.26 | −45.3 | 532 | same file (`minervini_APPROACH_3`) | production class, APPROACH_3 (R5 baseline mode: 19.28) | **CURRENT** (live config) |
| R14 | 21.32 | 24.56 | −41.3 | 685 | same file (`trend_momentum_pit_BASELINE`) | production engine, BASELINE mode: reproduces R1 to the rupee (NAV 4,802,285) | CURRENT (ablation) |
| R15 | 21.43 | 24.65 | −40.2 | 675 | same file (`v_1a`) | APPROACH_1A (strict top-N, no budget gate, no catch-up, no slot rule) | ablation |
| R16 | 21.31 | 23.42 | −40.8 | 696 | same file (`v_1b`) | APPROACH_1B (budget gate + equal-slot cash rule, no catch-up) | ablation |
| R17 | 21.28 | 25.15 | −41.0 | 678 | same file (`v_a3_noslot`) | APPROACH_3 with the equal-slot cash rule removed (patched engine text) | ablation |
| R18 | 21.38 | 25.18 | −41.0 | 679 | same file (`v_a3_noslot_anycatch`) | R17 + catch-up whenever a slot is empty (closest to the A/B copy's logic) | ablation |
| R19 | 22.06 / 22.06 / 22.18 | 25.51 / 25.51 / 25.95 | −40.6 / −40.6 / −41.2 | 676 / 676 / 683 | `results/ab_execution_approaches/execution_approaches_summary.csv` Arms 1-3 | `ab_execution_approaches.py`: **a re-implemented copy of the engine loop** | **NOT AUTHORITATIVE: did not reproduce** (real engine: R15 / R16 / R11) |
| R20 | 20.50 | | −41.4 | 709 | `research/audit_2026-10/results/ab_rs55_filter/summary_A0_baseline.csv` (win 41.3%, PF 1.87) | `ab_rs55_filter.py` arm A0, production class, no filter; reproduces R11 to the trade (709) | ablation baseline (2026-11-08) |
| R21 | 20.01 | | −42.9 | 710 | same dir, `summary_A1_rs55_gt0.csv` (win 41.8%, PF 1.83) | A0 + RS55 > 0 pp entry filter (new entries only) | **NOT ADOPTED**: win rate +0.5 pp = noise; CAGR, PF, DD slightly worse |
| R22 | 20.73 | | −41.0 | 662 | same dir, `summary_A4_rs55_ge20.csv` (win 45.2%, PF 1.83) | A0 + RS55 >= 20 pp entry filter | **NOT ADOPTED (narrow fail of pre-set rule)**: win rate +3.9 pp, higher in all 4 windows; full-period PF 1.83 vs 1.87; 2021-26 CAGR 26.6 vs 29.4, DD −34.3 vs −29.4 |
| R23 | 16.72 | | −37.9 | 530 | same dir, `summary_A5_rs55_ge40.csv` (win 41.1%, PF 1.73) | A0 + RS55 >= 40 pp entry filter; **added post-hoc** after the bucket table | **NOT ADOPTED**: CAGR −3.8 pp, 25% fewer trades, no win-rate gain |
| Index | 9.98 (2007-01→2026-10) | 11.18 | −64.3 | | `minervini_20y/summary.csv` benchmark columns; `^CRSLDX` in Mongo `benchmark_ohlcv` | Nifty 500 **price** index (no dividends; ~1.3 pp/yr below TRI) | benchmark; handoffs quote 9.86–9.9 for slightly different windows |

## 3. Why the production numbers differ from the first research run (reconciliation)
Four historical numbers for "the same strategy" are three code versions plus different window ends:
1. **R3, 17.1% — old rule.** Holdings dropped at month-end if they failed the entry filter (1.03×EMA100, Supertrend). 1,001 trades, 574 `REBALANCE_DROP` exits, 71-day average hold.
2. **R2/R4, 19.3–19.7% — HoldFix** (applied to the PIT strategy in commit `9c7813f`): held names only need close ≥ EMA(100) and EQ. 594 trades in both R2 (production class) and R4 (research copy); R4's ledger shows 18 `REBALANCE_DROP` exits and a 114-day average hold. HoldFix itself is not new: it was pre-registered in `research/audit_2026-10/PROTOCOL_HOLDRULE.md`, got verdict KEEP in `HANDOFF.md` §3 (higher post-tax CAGR and fewer trades in both windows, `results/holdrule_ab.csv`, tested on the OLD `trend_momentum` with a 38-rank buffer) and was approved by the user on 2026-10-28. What was missing: the lab's UH run (R3) never adopted it, the PIT commit did not update the docstring, strategy doc or changelog, and its effect on the UH/PIT design was only measured later (R2).
3. **R1/R2b, 21.0–21.3% — HoldFix + Arm 2** (event-driven catch-up on a Bear→Bull flip): 685 trades, 109-day hold.
The pairing of trade counts across harnesses is strong evidence (VERIFIED from the ledgers) that the exit-rule change, not tax or caching, explains most of the gap between R3 and R2/R4. The size of each contribution on an identical engine has not been isolated (the v1.1.0 engine sizing change coincides with these commits): INFERRED.
`HoldFix` and Arm 2 were chosen after seeing results (4 arms, plus the hold-rule A/B in `PROTOCOL_HOLDRULE.md`): they add to the tried set.

## 3b. Why Approach 3 was 22.18% in the A/B and 20.50% in the real engine (investigated 2026-11-07)
- **Cause of the A/B number:** `ab_execution_approaches.py` Arms 1-3 do not run the production engine; they run `NextDayCatchupEngine`, a copy of the full `run()` loop that differs from `engine.py` (no delisting force-liquidation, a different circuit-retry/exit path, no `had_rebal_skips` flag, catch-up scheduled whenever any slot is empty). Only Arm 0 used the real engine, which is why Arm 0 matched R1 exactly and Arms 1-3 did not match anything. The same modes through the real engine (R15, R16, R11) are 21.43 / 21.31 / 20.50, i.e. the claimed +0.86 pp for Approach 3 does not exist: there is no measurable return benefit.
- **Why deployed Approach 3 (R11, 20.50) is below its own no-slot-rule ablation (R17, 21.28):** the equal-slot cash rule (commit 38b6c30, `cash / open_slots`) shrinks the first-ranked buys when cash is short. Year gaps appear in 2017 (−7 pp), 2020 (−7 pp) and 2024 (−5 pp), and the final CUPID position is 4,597 shares vs 5,778 in baseline (about 7% of final NAV). That is one stock's position size, i.e. path noise, not a defect: all six real-engine variants sit within 20.5-21.4%.
- **Decision:** the equal-slot rule stays (it is a live-funding safeguard, kept in engine and paper manager in parity); it was NOT chosen or reverted on backtest return, to avoid tuning. Approach 3 is justified by execution realism (no 5x-margin backup queue, one catch-up day), not by return.
- **Reproduce:** `python research/audit_2026-10/scripts/approach_ablation_real_engine.py OUT_DIR` (patches engine text in memory; no copy of the loop).

## 4. What the parity and test evidence does and does not prove
- Test suite on `4c879b8`: **227 passed, 1 skipped** (2026-11-04, 195 s). The skip is in `tests/test_trend_momentum_pit.py`.
- Backtest-vs-paper parity tests (`tests/test_backtest_paper_parity.py`) run on a **seeded synthetic market** (5 stocks, 320 days of 2023-24, 2 slots). The Rs 1,108,594.79 / 1,201,084.80 / 1,214,288.43 NAVs in the playbook table are synthetic outputs, not performance.
- Real-data parity: `trend_momentum_pit` 20/20 buys and 6/6 sells over 83 sessions (2024-03-28 → 2024-07-31, `scripts/replay_reconcile.py`, Rs 0.04 NAV delta); real-data comparison against the research harness at 4 month-ends. Not run over full history; not run for the other two strategies on real data.
- Not verified live: the 09:16 quote path, split rescaling on a real split, the server flow, any real fill (`fill_audit` has no data).

## 5. Known limits that apply to every number above
Nothing out-of-sample; ~180 configurations tried; t ≈ 1.3 for the liquid tier (old rule); 2021-26 edge over a no-skill basket ≈ 0–3 pp; 2016-20 close to the index; costs are assumed tiers, not measured; benchmark price-only and not size-matched (no Smallcap250/Midcap150 history); survivorship only partly addressed (hand-decided adjustments for 90 ingested delisted names, ≈333 once-liquid names absent from the Upstox DB); dividends excluded; panel provenance unaudited; terminal wealth concentrated in few positions.

## 6. Reproduce
```bash
python research/audit_2026-10/scripts/minervini_20y.py          # R1, R5 (≈9 min)
python research/audit_2026-10/scripts/ab_regime_catchup.py      # R2, R2b
python research/audit_2026-10/scripts/open_positions_check.py control   # R1a/R1b inputs
pytest -q                                                       # 227 passed, 1 skipped
```
Files committed vs local: `summary.csv`/`yearly.csv` are committable; `nav_*.csv`, `trades_*.csv`, `orders_*.csv` are gitignored (regenerate with the scripts above).
