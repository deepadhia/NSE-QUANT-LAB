# Forward test: small-capital paper accounts with an entry-price cap

Purpose: measure REAL execution frictions (09:16 bid/ask fills, Rs 14.75 sale fee, whole-share rounding, idle cash) at Rs 30-50k, and see whether the
research result (price cap < Rs 1,000, 10 vs 15 slots) survives out of sample. Paper only; no real orders. Evidence behind it:
`research/audit_2026-11-08/small_capital_grid/` (descriptive, 2016-10..2026-10, in-sample, optimistic/pessimistic bounds). Do not read it as a forecast.

**2026-11-10 audit update:** The saved `results.csv` cap runs used a filtered ranking for the rebalance hold buffer. That let the
entry-price cap suppress some rank-based exits, contrary to its entry-only definition. The engine and paper path now use the
original ranking for exits. Treat the 36 saved capped rows as stale until the corrected grid is rerun; the script now
writes separate `results_entry_only_hold_rank_v2.csv` artifacts. The original no-cap rows are unaffected by this specific defect.

## How accounts work
* An ACCOUNT is a strategy document keyed by `strategy_id` (it namespaces orders, positions, cash, leases). Its logic comes from `strategy_class`
  (registry key; defaults to `strategy_id`). Any number of accounts of one class can run side by side.
* `max_entry_price` (Rs, nominal close on the decision date) is an ENTRY-ONLY cap applied to every strategy by `entry_filters.apply_entry_price_cap`,
  identically in the backtest engine and the paper manager. Held names are exempt. The cap tests the signal-day close; the fill may open up to +2% higher.
* Settings live on the account document (`initial_capital`, `portfolio_size`, `max_entry_price`, `strategy_class`); the daily scripts read them. No code change per account.

## Create the account (once, on the host that runs the scheduler, after deploying this code)
```bash
python scripts/manage_strategy.py --init --strategy fwd_trend_momentum_pit_50k_15slots_under1000 --strategy-class trend_momentum_pit --capital 50000 --slots 15 --max-entry-price 1000
python scripts/manage_strategy.py --list
```
`--init` refuses to touch an existing account (it would overwrite capital/cash); `--reinit` overrides that deliberately. Never reuse a live account id.
Optional second account for the 10-slot variant: `--strategy fwd_trend_momentum_pit_50k_10slots_under1000 --strategy-class trend_momentum_pit --capital 50000 --slots 10 --max-entry-price 1000`.
Control account (same capital and slots, no cap, to separate the cap effect from the capital effect): `--strategy fwd_trend_momentum_pit_50k_15slots_nocap --strategy-class trend_momentum_pit --capital 50000 --slots 15` (no `--max-entry-price`).
Naming: `fwd_<strategy>_<capital>_<slots>_<cap>`, e.g. `..._15slots_under1000` / `..._15slots_nocap`. The id is permanent (it keys orders, positions, cash).
Pause/stop an account: `python scripts/manage_strategy.py --strategy fwd_trend_momentum_pit_50k_15slots_under1000 --status PAUSED` (or ARCHIVED).

## Timeline
Account created before 18:00 IST on a trading day -> that evening's pipeline run produces its first signals -> first fills at the next 09:16.
Created after 18:00 -> first signals the following evening, UNLESS you stage its first orders by hand right after creating it:
```bash
python scripts/run_daily_pipeline.py --strategy fwd_trend_momentum_pit_50k_15slots_under1000
```
That generates signals from the latest ingested bar, and the next 09:16 run fills them (same as the other accounts' pending orders). Check the 
decision date it logs and that the scheduler's own run does not duplicate it (orders are idempotent per signal date). The scheduler/cron needs no change (`--strategy all` picks up every ACTIVE account).

## What to watch (daily/weekly)
* Fills: `fill_audit` (model fill vs live bid/ask), orders in `no_quote` (exit code non-zero), `ACCOUNT_BUSY` / `LEASE_LOST` messages (two hosts overlapping).
* Cost drag: fees as % of NAV, idle cash %, shares per slot (a Rs 3,300 slot buys whole shares only).
* Compare with the live Rs 1 lakh account on the same dates (same signals, different size): the gap is the small-capital friction.
* Judge only after months and dozens of closed trades; a handful of trades says nothing.

## Server data note
The server has NO parquet files; it loads market data from MongoDB (Upstox candles). Live signals, the price cap and the Rs 10 floor therefore run on Upstox-basis 
prices: correct for the latest bar, which is the one the cap tests. The nominal-price research backtests (`clean_panel_execution_v2.parquet`) run on the laptop only. 
The full test suite was verified in a checkout WITHOUT `external/` data (328 passed, 2 skipped), which is what `update_and_deploy.sh` runs before restarting.
