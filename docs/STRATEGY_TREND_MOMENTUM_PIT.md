# Strategy: `trend_momentum_pit` (researched setting S0 / UH liquid tier / R1 / 15 slots / no rank-drops)

Code: `src/nse_quant_lab/strategies/trend_momentum_pit.py` (same class drives the backtest engine and the paper manager).
Evidence and caveats: `research/audit_2026-10/HANDOFF_2026-10-31.md`. Nothing here is investment advice or a promise of returns.

## What it does, step by step (monthly decisions, daily exit checks)
1. **Scan** every stock that traded on the decision date (month-end close). Data = NSE bhavcopy, delisted names included, split/bonus adjusted.
2. **Filter** (all measured with data up to that close): series EQ; at least 226 sessions of history; 50-session median turnover >= **Rs 10 crore** (liquid tier);
   price >= Rs 10; close >= 1.03 x EMA(100); Supertrend(10,3) bullish; momentum score defined.
3. **Rank** by risk-adjusted momentum = (0.5 x 6M return + 0.5 x 12M return) / 6M annualised volatility.
4. **Regime filter (R1)**: if Nifty 500 closes below its 200-session average at that month-end, buy nothing new; current holdings are kept only if they still pass step 2.
   Missing benchmark data is treated as "below" (fails safe).
5. **Buy** the top names into up to **15 equal slots** (target = equity / 15, whole shares, <= 10% of the stock's 50-day turnover, skipped if the next open
   gaps > 2% above the signal close or is locked at the upper circuit). Orders fill at the **next open**.
6. **Sell**: (a) every day, if the close is below EMA(100) -> sell at the next open; (b) at each month-end, if a holding no longer passes the entry filter.
   Rank is **not** used to drop winners (`rebalance_hold_rank_buffer = 100 x slots`). A lower-circuit lock defers the sale to the next tradable open.
7. **Costs**: statutory delivery fees + DP charge, plus liquidity-tiered, order-size-aware slippage (`cost_model.py`, tier values are ASSUMED until measured).
8. **Tax** (backtests): dated Indian STCG/LTCG rules in `tax_india.py`; nearly all trades are short-term (~70-day average hold).

## Why the liquid tier (decision 2026-10-31)
Pre-registered liquidity split of this strategy (post-tax, tiered costs, Rs 1 L): >= Rs 10 Cr turnover 17.1% (20y) / 17.0% (2016-26), max DD -37.8%, t 1.3;
Rs 1-10 Cr 19.0% / 11.5%, DD -43%; Rs 25 L-1 Cr 21.5% / 12.8%, DD -50%. The illiquid names carried the 2007-15 excess; since 2016 the liquid tier did best.
It has the lowest drawdown, the least dependence on the unmeasured cost model and the most capacity, at the price of lower statistical significance and a
lower 20-year CAGR than the broad universe (22.2%). Index (Nifty 500 price): 9.9% / 10.9%.

## Daily operating loop (after the market closes, orders next morning)
1. Ingest the day's bhavcopy, then rebuild the panel: `python research/audit_2026-10/scripts/build_clean_panel.py` (the cache refreshes when the archive changes).
2. `python scripts/run_daily_pipeline.py --strategy trend_momentum_pit` -> writes PENDING_OPEN paper orders (exits daily; entries only on the last session of the month).
3. Next morning ~09:16: `python scripts/execute_paper_orders.py --strategy trend_momentum_pit` -> fills at the modelled price and logs every fill to `fill_audit`
   (set `manager.quote_provider` to also log live bid/ask; no provider is implemented yet).
4. Review `fill_audit`: if real cost per leg is above ~40-60 bps the edge is gone; recalibrate `cost_model.py` before adding capital.

## Known risks (see handoff for numbers)
Drawdowns of about -35% to -47% and ~2-year recoveries; 6 of 20 calendar years lagged the index; 2016-20 about flat vs Nifty 500; costs and fills unmeasured;
new splits/bonuses must be in the corporate-action file or a held stock can show a fake -50% and be sold; dividends excluded.
