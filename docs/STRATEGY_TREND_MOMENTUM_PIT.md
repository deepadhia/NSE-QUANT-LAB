# Strategy: `trend_momentum_pit` (researched setting S0 / UH liquid tier / R1 / 15 slots / no rank-drops)

Code: `src/nse_quant_lab/strategies/trend_momentum_pit.py` (same class drives the backtest engine and the paper manager).
Evidence and caveats: `research/audit_2026-10/HANDOFF_2026-10-31.md`. Nothing here is investment advice or a promise of returns.

## What it does, step by step (monthly decisions, daily exit checks)
1. **Scan** every stock that traded on the decision date (month-end close). Data = NSE bhavcopy, delisted names included, split/bonus adjusted.
2. **Filter** (all measured with data up to that close): series EQ; at least 226 sessions of history; 50-session median turnover >= **Rs 10 crore** (liquid tier);
   price >= Rs 10; close >= 1.03 x EMA(100); Supertrend(10,3) bullish; momentum score defined.
3. **Rank** by risk-adjusted momentum = (0.5 x 6M return + 0.5 x 12M return) / 6M annualised volatility.
4. **Regime filter (R1)**: if Nifty 500 closes below its 200-session average at that month-end, buy nothing new; current holdings stay under the hold rule in step 6.
   Missing benchmark data is treated as "below" (fails safe). **Arm 2 catch-up:** on any session where Nifty 500 crosses from below to at/above its 200-day average,
   an extra decision date is triggered that fills open slots at the next open; holdings are evaluated only under the hold rule in step 6, so winners are not rotated out.
5. **Buy** the top names into up to **15 equal slots** (target = equity / 15, whole shares, <= 10% of the stock's 50-day turnover, skipped if the next open
   gaps > 2% above the signal close or is locked at the upper circuit). Orders fill at the **next open**.
6. **Sell / hold rule (HoldFix, pre-registered in `research/audit_2026-10/PROTOCOL_HOLDRULE.md`)**: (a) every day, if the close is below EMA(100) -> sell at the next open;
   (b) at each month-end a **held** stock stays eligible while close >= EMA(100) and series is EQ; it does NOT have to re-pass the 1.03x-EMA / Supertrend entry filter
   (only new buys need that), and it is dropped otherwise. Rank is **not** used to drop winners (`rebalance_hold_rank_buffer = 100 x slots`).
   A lower-circuit lock defers the sale to the next tradable open. (Until 2026-11-04 this document and the module docstring described the older rule,
   under which a holding also had to keep passing the entry filter; the 17.1% research figure was produced under that older rule.)
7. **Costs**: statutory delivery fees + DP charge, plus liquidity-tiered, order-size-aware slippage (`cost_model.py`, tier values are ASSUMED until measured).
8. **Tax** (backtests): dated Indian STCG/LTCG rules in `tax_india.py`; nearly all trades are short-term (~70-110 day average hold depending on the hold rule).

## Results
All performance numbers are in [`RESULTS_REGISTER.md`](RESULTS_REGISTER.md); none are repeated here so they cannot drift. Summary of the register: deployed setup (APPROACH_3) 20.5% post-tax 20-year backtest CAGR (R11; baseline execution mode R1: 21.3%) (max DD -41.4%) vs Nifty 500 price 10.0%, ~19% if one 9.4x position (CUPID) is marked at cost, 17.1% under the older hold rule; planning range 15-18% (updated 2026-11-07);
not out-of-sample, costs unmeasured.

## Why the liquid tier (decision 2026-10-31)
The pre-registered liquidity split (old hold rule; `HANDOFF_2026-10-31.md` §14) showed the >= Rs 10 Cr tier with the lowest drawdown and the least dependence on the unmeasured cost model,
and the best results since 2016, while the illiquid names carried the 2007-15 excess. The liquid tier trades lower statistical significance (t ~ 1.3) and a lower 20-year CAGR than the
broad universe for those properties.

## Daily operating loop (after the market closes, orders next morning)
1. Daily bars after settlement: `python scripts/ingest_upstox_fulltier.py` then `python scripts/ingest_benchmarks.py` (the bhavcopy clean panel is the HISTORY layer, rebuilt by hand with
   `research/audit_2026-10/scripts/build_clean_panel.py`; the authoritative schedule is `scripts/run_scheduler_daemon.py` / `deploy/crontab.txt`).
2. `python scripts/run_daily_pipeline.py --strategy trend_momentum_pit` -> writes PENDING_OPEN paper orders (exits daily; entries only on the last session of the month).
3. Next morning ~09:16: `python scripts/execute_paper_orders.py --strategy trend_momentum_pit` -> fills at the modelled price and logs every fill to `fill_audit`
   (`UpstoxQuoteProvider` is attached on live runs to also log bid/ask; implemented and unit-tested with mocks, not yet verified live; `fill_audit` currently has no data).
4. Review `fill_audit`: if real cost per leg is above ~40-60 bps the edge is gone; recalibrate `cost_model.py` before adding capital.

## Known risks (see handoff for numbers)
Drawdowns of about -35% to -47% and ~2-year recoveries; 6 of 20 calendar years lagged the index; 2016-20 about flat vs Nifty 500; costs and fills unmeasured;
new splits/bonuses must be in the corporate-action file or a held stock can show a fake -50% and be sold; dividends excluded.
