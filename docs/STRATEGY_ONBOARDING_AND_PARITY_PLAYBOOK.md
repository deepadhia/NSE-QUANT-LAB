# NSE Quant Lab: Strategy Onboarding, Change Management & Backtest-to-Paper Parity Playbook

## 1. Executive Summary: Current System Parity & Confidence Level

Across the three active Point-in-Time (PIT) strategies running in production paper trading (`trend_momentum_pit`, `minervini_trend_template`, and `alpha_semivol_pit`), **logical, mathematical, and execution-rule parity between `BacktestEngine` and `PaperTradingManager` is verified at 100%**.

A deterministic day-by-day causal replay feeding identical point-in-time market slices (`date <= T`) into both `BacktestEngine.run()` and `PaperTradingManager` (`execute_morning_fills()` at `09:16 IST` + `generate_post_market_signals()` at `18:00 IST`) confirms zero rule divergence:

| Strategy ID                | Backtest Final NAV (INR) | Paper Replay Final NAV (INR) |  Absolute NAV Delta | Execution Count (BT vs Paper) | Rule & Signal Parity |
| :------------------------- | -----------------------: | ---------------------------: | ------------------: | ----------------------------: | :------------------- |
| `trend_momentum_pit`       |             1,201,084.80 |                 1,201,084.78 | 0.000001% (Rs 0.02) |                      61 vs 61 | **100% Identical**   |
| `minervini_trend_template` |             1,108,594.79 |                 1,108,594.79 | 0.000000% (Rs 0.00) |                      27 vs 27 | **100% Identical**   |
| `alpha_semivol_pit`        |             1,214,288.43 |                 1,214,288.41 | 0.000002% (Rs 0.02) |                      45 vs 45 | **100% Identical**   |

_Scope note (added 2026-11-04)_: the NAVs in this table come from `tests/test_backtest_paper_parity.py`, which replays a **seeded synthetic market** (5 stocks, 320 days of 2023-24, 2 slots, `numpy` seed 42). They demonstrate engine-vs-paper agreement, not performance or profitability, and "verified at 100%" applies to that fixture (plus the separate real-data replay and 4-month-end checks listed in `docs/RESULTS_REGISTER.md` §4), not to full history or to live execution.

_Note on the Rs 0.02 delta_: `PaperTradingManager` persists `round(cash, 2)` to MongoDB after each fill (`PaperTradingManager._persist_cash`), whereas `BacktestEngine` accumulates `cash` in unrounded 64-bit IEEE-754 floating point in memory (`src/nse_quant_lab/engine.py:501, 692`). Every single candidate rank, entry signal, structural exit, share count, and fill price is identical.

### Why Live Paper Trading Can Still Differ Slightly From Historical Backtests (Expected Microstructure Variance, Not Code Bugs)

1. **09:16 IST Live Quote Snapshot vs Official NSE Opening Print**: At `09:16 IST`, `scripts/execute_paper_orders.py` fetches live quotes via Upstox (`_get_execution_bar`). If a symbol's first tick or live quote differs by a few paisa from the official NSE Bhavcopy `OPEN` recorded at end of day, the fill price and integer share count (`avail // est_cost_per_sh`) can shift by 1 share.
2. **Live Order-Book Circuit Lock Detection vs EOD Bar Proxy**: In historical backtests, upper/lower circuit locks are inferred from `Open == High == Low == Close` and `>= 4.8%` move (`is_locked_up` / `is_locked_down`). In live paper execution (`src/nse_quant_lab/paper/manager.py:740-756, 898-912`), the manager checks actual exchange `upper_circuit` / `lower_circuit` bands and live bid/ask depth (`bid <= 0` or `ask <= 0`), which is strictly more realistic than daily bar proxies.

## 2. Root-Cause Anatomy: Why Previous Code Updates Introduced Bugs

Every bug uncovered across our audits (`V1-1` through `V5-3`) traced back to **four structural anti-patterns** when adding or modifying strategies:

| Anti-Pattern                                               | Where It Happened in Codebase                                                                                                                                                                                                                            | Bug It Caused                                                                                                                                                                                          |
| :--------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Dual Strategy Implementations (Research Copy-Paste)** | `AlphaSemivolCandidate` in `research/audit_2026-10/scripts/candidate_backtests.py` duplicated `AlphaSemivolPITStrategy` in `src/nse_quant_lab/strategies/alpha_semivol_pit.py` instead of importing the production class.                                | Research drifted from production (`V1-1` `mom_6m` vs `raw_mom_6m`, `V5-2` `mom12_1` fallback, `V5-3` 3-arg `evaluate_rebalance_exits` signature crash).                                                |
| **2. Shared Indicator Column Collisions**                  | `augment_universe_indicators()` in `candidate_backtests.py` wrote `mom_6m` (blended risk-adjusted momentum) to `universe_augmented.parquet`, while `alpha_semivol_pit.py` checked `if "mom_6m" not in out.columns:` assuming `mom_6m` was raw 6M return. | Silent 2.09% CAGR degradation and altered stock rankings (`V1-1`) whenever a pre-augmented parquet cache was used.                                                                                     |
| **3. Scattered Hardcoded Strategy Whitelists**             | Six separate places hardcoded `("trend_momentum_pit", "minervini_trend_template", ...)`: `execute_paper_orders.py`, `run_daily_pipeline.py` (3x), `manage_strategy.py`, and `verify_production_health.py`.                                               | Omitting `alpha_semivol_pit` from `execute_paper_orders.py` (`V2-1`) caused morning orders to load the wrong universe, wrong slot count (6 instead of 15), and wrong rank buffer (15 instead of 1500). |
| **4. Engine-Specific Stop-Loss & Sizing Assumptions**      | `PaperTradingManager` used a hardcoded `if ema100 ... elif supertrend_lower` chain (`V5-1`) and single-pass slippage sizing (`V3-1`) while `BacktestEngine` used two-pass slippage sizing.                                                               | Missing `sma150_stop` telemetry in MongoDB/Telegram and 1-share position sizing drift on illiquid names.                                                                                               |

## 3. Mandatory Architectural Refactor Before Adding Strategy #4

To permanently eliminate the 6-file whitelist trap and stop-loss hardcoding, apply these two small declarative conventions to `BaseStrategy` (`src/nse_quant_lab/base_strategy.py`) the next time you touch the core framework:

### A. Replace Hardcoded `PIT_STRATEGY_IDS` Tuples with Strategy Class Attributes

Instead of maintaining hardcoded tuples of strategy names across `run_daily_pipeline.py`, `execute_paper_orders.py`, `manage_strategy.py`, and `verify_production_health.py`, declare universe and portfolio defaults directly on `BaseStrategy` subclasses:

```python
class BaseStrategy(ABC):
    name: str = "BASE_STRATEGY"
    rebalance_frequency: str = "MONTHLY"
    universe_tier: str = "PIT_FULL"          # "PIT_FULL" (load_pit_universe) or "CLEAN_SAMPLE" (load_clean_universe)
    default_portfolio_size: int = 15
    default_hold_rank_buffer: int = 1500
    paper_backup_slots: int = 60
    stop_indicator_column: Optional[str] = None  # e.g., "ema100" or "sma150"
    required_indicator_columns: List[str] = []
```

When scripts read `strategy.universe_tier == "PIT_FULL"`, `strategy.default_portfolio_size`, and `strategy.stop_indicator_column`, **onboarding a new strategy requires zero edits to pipeline, execution, CLI, or health-check scripts**.

### B. Single Source of Truth Between Research & Production

Never define a `*Candidate` copy of a production strategy inside `research/`. Once a strategy graduates from exploratory research to `src/nse_quant_lab/strategies/<strategy_name>.py`:

1. Delete the duplicate class definition in `research/audit_2026-10/scripts/candidate_backtests.py`.
2. Import the exact production class (`from nse_quant_lab.strategies.<strategy_name> import <StrategyClass>`) just like `TrendMomentumPITStrategy` does at `candidate_backtests.py:41`.

## 4. The 7-Step Zero-Regression Strategy Onboarding Checklist

Until the declarative `BaseStrategy` attributes above are adopted, every new strategy **must** complete all 7 steps below before activation:

### Step 1: Implement `BaseStrategy` Contract (`src/nse_quant_lab/strategies/<new_strategy>.py`)

1. Decorate the class with `@register_strategy("<strategy_id>")`.
2. Define `required_indicator_columns: List[str]` listing every column accessed in `select_entry_candidates()` and `evaluate_daily_exits()`.
3. Use **unambiguous, formula-specific column names** in `compute_indicators()` (e.g., `raw_mom_6m` vs `risk_adj_mom_6m`, `sma150` vs `ema100`). Never guard a strategy-specific formula behind `if "generic_col" not in out.columns:` unless the formula is 100% identical across all strategies.
4. Compute all indicators strictly causally (`df.sort_values("date")`, rolling windows with `.shift(1)` for cost inputs `adt_prev` and `sig_prev`, and point-in-time bar counter `hist_n = np.arange(1, len(out) + 1)`).
5. Match the exact 6-argument signature if overriding `evaluate_rebalance_exits`:
   ```python
   def evaluate_rebalance_exits(
       self,
       universe: Dict[str, pd.DataFrame],
       decision_date: pd.Timestamp,
       open_positions: Dict[str, Any],
       target_symbols: List[str],
       config: Any = None,
       *args,
       **kwargs,
   ) -> Dict[str, str]:
   ```
6. Fail safe to **Bear Regime (`return True` in `regime_below`)** whenever benchmark (`^CRSLDX`) data is missing, stale (`decision_date not in bench.index`), or shorter than 200 bars.

### Step 2: Register Package Exports

1. Import and expose the class in `src/nse_quant_lab/strategies/__init__.py`.
2. Import and expose the class in `src/nse_quant_lab/__init__.py`.

### Step 3: Wire Operational Scripts & Stop Telemetry

If the strategy uses the full-market PIT universe (`load_pit_universe`), 15 slots, and `rebalance_hold_rank_buffer = 1500`, add `"<strategy_id>"` to all four operational files:

| File                                                                    | Lines / Variables to Update                                                                  | Purpose                                                                                                         |
| :---------------------------------------------------------------------- | :------------------------------------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------- |
| `scripts/execute_paper_orders.py`                                       | `PIT_STRATEGY_IDS` (`line 34`)                                                               | Loads PIT universe, sets 15 slots / 1500 buffer fallback, and prefetches live Upstox quotes at 09:16 IST.       |
| `scripts/run_daily_pipeline.py`                                         | `is_pit` (`line 37`), `args.strategy in (...)` (`line 103`), `pit_strategy_ids` (`line 113`) | Loads PIT universe and enforces 15 slots / 1500 buffer during 18:00 IST post-market scan.                       |
| `scripts/manage_strategy.py`                                            | `is_pit` (`line 62`)                                                                         | Seeds MongoDB `strategies` document with `portfolio_size=15` and `rebalance_hold_rank_buffer=1500` on `--init`. |
| `scripts/verify_production_health.py`                                   | `pit_strategies` (`line 56`)                                                                 | Audits MongoDB configuration and order invariants for the new strategy.                                         |
| `src/nse_quant_lab/paper/manager.py` & `src/nse_quant_lab/db/models.py` | `PositionModel` fields (`models.py:70-72`) & stop extraction (`manager.py:204-240, 472-476`) | Persists the strategy's structural trailing stop price and buffer % to MongoDB and Telegram alerts.             |

### Step 4: Verify Causal Integrity (The "Truncated T" Test)

Every new strategy must pass a causal invariance test proving that `compute_indicators()`, `select_entry_candidates()`, and `evaluate_daily_exits()` on date $T$ produce the exact same output whether the input universe ends at $T$ or includes future bars $T+1 \dots T+100$:

```python
def test_causal_invariance(strategy, universe, test_date):
    # Full universe (includes future bars after test_date)
    u_full = {s: strategy.compute_indicators(df.copy()) for s, df in universe.items()}
    ranks_full = strategy.select_entry_candidates(u_full, test_date, {}, strategy.config)

    # Truncated universe (strictly <= test_date)
    u_trunc = {s: strategy.compute_indicators(df[df["date"] <= test_date].copy()) for s, df in universe.items()}
    ranks_trunc = strategy.select_entry_candidates(u_trunc, test_date, {}, strategy.config)

    pd.testing.assert_frame_equal(ranks_full, ranks_trunc)
```

### Step 5: Verify Backtest-to-Paper Replay Parity (The "Zero-Divergence" Gate)

Before any strategy is initialized in MongoDB (`manage_strategy.py --init`), run a multi-month day-by-day replay comparing `BacktestEngine.run()` against `PaperTradingManager` (`execute_morning_fills` + `generate_post_market_signals`) on the same date window:

1. **Execution Count Match**: `len(bt_executions) == len(paper_executions)`.
2. **Symbol & Share Count Match**: Every `(date, symbol, action, shares)` tuple in `bt_executions` must match `paper_executions` 1-for-1.
3. **Final NAV Match**: `abs(paper_nav - bt_nav) / bt_nav < 1e-5` (allowing only for MongoDB 2-decimal `round(cash, 2)` persistence).

### Step 6: Invalidate Research Parquet & CSV Caches When Modifying Indicators

Whenever any indicator formula or strategy logic changes:

1. Delete or force-rebuild `research/audit_2026-10/data/universe_augmented.parquet`.
2. Run `python research/audit_2026-10/scripts/candidate_backtests.py --force` so cached `nav_<strategy>.csv` and `trades_<strategy>.csv` files are never reused from stale runs.

### Step 7: Production Initialization & Health Gate

Initialize the strategy in MongoDB and run the production health verifier before the next scheduled cron window:

```bash
python scripts/manage_strategy.py --strategy <strategy_id> --init --capital 100000
python scripts/verify_production_health.py
pytest -m "not live_upstox"
```

### Step 8: Run the Audit-Regression Gates (added after the 2026-11 forensic audit)

A new or changed strategy must also pass the gates that encode the audit's defect classes. They are generic: they iterate every registered strategy that declares `required_indicator_columns`, so a new strategy is covered automatically.

```bash
pytest tests/test_production_invariants.py tests/test_paper_failure_modes.py tests/test_order_id_isolation.py tests/test_backtest_paper_parity.py -v
```

What they will catch for your strategy:

| Gate | Fails when |
| :--- | :--- |
| `test_declared_required_columns_are_actually_produced` | `required_indicator_columns` lists a column `compute_indicators` does not produce |
| `test_precomputed_generic_columns_never_change_any_strategy_ranking` | ranking changes when the input frame already carries generic columns (`mom12_1`, `sma150`, ...) |
| `test_m3_no_generic_column_guards_in_strategy_indicator_code` | `if "col" not in out.columns:` guards a strategy formula |
| `test_every_pit_strategy_is_wired_into_all_operational_scripts` | the strategy id is missing from any of the four operational scripts (step 3) |
| `test_all_three_strategies_share_one_db_without_collisions` / `test_two_strategies_*` | orders of different strategies collide in the shared `orders` collection |
| `test_live_morning_path_parity_without_execution_day_bar` | the live-quote morning path diverges from the backtest (extend the parametrize list with your strategy) |

**Add your strategy to the parametrized lists** in `test_order_id_isolation.py` (shared-DB test) and `test_backtest_paper_parity.py` (both parity tests).

## 5. Why the Gates Exist: the Second Audit's Six Recurring Mistakes

The 2026-11 audit found a critical order-ID collision, non-resumable fills, data gaps turned into sales, unapplied corporate actions, silent order expiry and a skipped safety net. All six trace to the same habits. Before you touch `paper/manager.py`, `engine.py`, `db/`, the scheduler, the pipeline scripts, the holiday calendar or a strategy, answer the checklist in `docs/AUDIT_2026-11_FINDINGS_AND_REGRESSION_GUARDS.md` §4 (identity, crash points, missing-data-is-not-a-decision, time, shared names, defaults). The rules are mirrored in `AGENTS.md` ("Audit-derived production safety invariants").

Two changes to existing guidance from this audit:

* **Backtest-to-paper parity is now checked on two morning paths:** the replay path (execution day's bar present) and the live-quote path (bar absent, open from a quote). Passing only the first is not sufficient.
* **Adding a strategy still touches the four operational scripts** until the declarative `BaseStrategy` attributes in §3 are adopted; `test_every_pit_strategy_is_wired_into_all_operational_scripts` now fails loudly if one is forgotten.
