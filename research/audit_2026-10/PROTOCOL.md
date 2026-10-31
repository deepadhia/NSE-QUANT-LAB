# Pre-registered experiment: trend_momentum variants (written BEFORE any run)

Written: 2026-10-27, before any variant below was run.

## Data (known limitation)
- MongoDB `upstox_daily_ohlcv`, 501 symbols, ALL still trading on 2026-10-23.
- SURVIVOR-BIASED: no delisted/collapsed stocks. Every result here is labelled
  SURVIVOR_BIASED and is an upper bound on what a real universe would give.
- PIT gating: working-tree `pit_universe.py` (NIFTY_200 events from NSE archive,
  2016-04 onward), sha256 6c2b11d4...c224. Engine sha256 c9bf0bc4...48b4.

## Fixed rules (unchanged from current strategy)
Universe top-200 by 50d turnover (cap 45% of pool), price >= 10, 50d turnover >= Rs 1 Cr;
entry filter close >= 1.03*EMA100 and Supertrend bullish; ranking = blended 6M/12M return / 6M vol;
daily exit close < EMA100 (next open); next-open fills, +2% gap cap, circuit rules,
20 bps slippage/leg, statutory fees, 10% ADT participation cap, Rs 10 lakh start.

## Variant grid (12 configs, all run, all reported)
1. Rank-drop rebalance: MONTHLY | QUARTERLY (Mar/Jun/Sep/Dec) | SEMI-ANNUAL (Jun/Dec).
   Empty slots (after EMA exits) are refilled at every month-end in all variants;
   rank-based drops happen only in rebalance months.
2. Positions: 6 (hold buffer 15) | 15 (hold buffer 38). Buffer = 2.5x positions.
3. Regime filter: OFF | ON. ON = at each month-end decision, if Nifty 500 close <
   its 200-session SMA: sell all holdings next open, no buys. Re-entry at the first
   month-end where Nifty 500 close >= 200-SMA (portfolio rebuilt that day).

## Partitions
- Development: eval 2006-01-01 .. 2017-12-31 (warm-up from 2004).
- Holdout: eval 2018-01-01 .. 2026-10-23, fresh Rs 10 lakh, run ONCE for the selected config.

## Selection rule (development only)
Pick the config with the highest development-period information ratio vs Nifty 500
(annualised mean monthly excess return / annualised tracking error), PRE-TAX, net of
fees and slippage. Ties -> fewer trades.

## Success criteria for the selected config on the holdout (pre-tax, net of costs)
- S1: CAGR beats Nifty 500 price index by >= 3.0 percentage points.
- S2: CAGR beats the no-skill survivor basket (equal-weight of all eligible stocks,
  monthly, gross of costs) over the same holdout window.
- S3: Max drawdown no worse than Nifty 500 max drawdown + 10 pp.
Pass all three = RESEARCH_CANDIDATE (still survivor-biased). Otherwise report as failed.

## Also reported (context, not for selection)
Holdout results of all 12 configs; selected config post-tax; selected config at 40 bps slippage.
