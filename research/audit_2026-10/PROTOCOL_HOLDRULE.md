# Pre-registered A/B: hold-rule consistency fix (written before any run)

Change (single, no new parameters): at monthly rebalance, a stock that is ALREADY HELD stays in
the ranked pool while close >= EMA100 (same as the daily exit rule). New candidates still need the
full entry filter (price, turnover, top-200 liquidity, close >= 1.03*EMA100, Supertrend bullish).
Held stocks are still ranked by momentum and dropped if rank > hold buffer.

Fixed: 15 positions, hold buffer 38, monthly, no regime filter, Rs 10 lakh, 20 bps/leg, all fees,
liquidity-PIT universe (no index events), Upstox 500 + 43 external stocks (external data from 2014 only).

Windows: W1 2006-01-01..2015-12-31 (warm-up from 2004), W2 2016-01-01..2026-10-23.

KEEP the fix only if, in BOTH windows: post-tax CAGR higher AND trade count lower.
Otherwise reject. Also reported: pre-tax CAGR, max drawdown, info ratio, holds >= 365 days.
