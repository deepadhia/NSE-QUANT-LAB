# NSE-Quant-Lab — System & Deployment Guide

Institutional-grade, event-driven quantitative backtesting, strategy research, and prospective forward paper-trading harness for the National Stock Exchange of India (NSE) equity delivery cash market.

---

## 1. System Architecture & Unified Schema Design

`NSE-Quant-Lab` decouples strategy research and decision-making from execution mode. The engine operates uniformly across:
- **Historical Backtesting** (`mode: "backtest"`)
- **Forward Paper Trading** (`mode: "paper"`)
- **Broker Live Trading** (`mode: "real"`)

### Core Engineering Invariants:
1. **Temporal Causality**: Indicators use strictly backward-looking historical bars. Decisions generated at session $t$ close fill exclusively at session $t+1$ morning Open.
2. **Realistic Indian Market Friction**:
   - Full decomposed statutory delivery fee schedule: STT (0.100% on buy & sell), Stamp Duty (0.015% on buy), SEBI turnover fees, 18% GST on broker/exchange/SEBI charges, and Depository Participant (DP) charge of Rs 15.93 per sell.
   - Dynamic Exchange Charges: Applies legacy NSE cash charges (0.00345%) prior to October 1, 2024, and SEBI-mandated revised charges (0.00297%) thereafter.
   - 0.10% adverse execution slippage per leg.
   - Circuit breaker limits: Lower-circuit lock exit deferral with capital freeze, upper-circuit buy rejection, and +2.0% gap limit cancellation.
3. **Penny-Level Cash Conservation**:
   $$\text{NAV}_t = \text{Cash}_t + \sum_{i} (\text{Shares}_{i,t} \times \text{Price}_{i,t})$$
   Cash is strictly non-negative; fractional share orders are rejected.
4. **Cryptographic Tamper-Proofing**:
   Every backtest outputs structured run artifacts sealed with a cryptographic SHA-256 `manifest.json`.

### Unified MongoDB Schema
Every document across the core persistence collections includes the `"mode"` discriminator, ensuring zero collision between forward paper tests and live executions:

- **`strategies`** *(Dashboard Strategy Listing & Performance Summary)*:
  - `strategy_id`: Unique identifier (e.g., `trend_momentum`, `donchian_breakout`).
  - `name`: Display name for UI cards/tables (e.g., `"Trend Momentum"`).
  - `description`: Strategy thesis, indicators, and frequency.
  - `mode`: `"paper"` / `"real"` / `"backtest"`.
  - `status`: `"ACTIVE"`, `"PAUSED"`, `"ARCHIVED"`.
  - `current_nav`: Current total portfolio equity.
  - `cash`: Liquid unallocated cash.
  - `holdings_val`: Current mark-to-market holdings value.
  - `unrealized_pnl`: Sum of open positions unrealized profit/loss.
  - `realized_pnl`: Cumulative net profit/loss from closed trades.
  - `total_return_pct`: Net percentage return from initial capital.
  - `open_positions_count`: Number of active holdings.
  - `closed_trades_count`: Total round-trip trades completed.
  - `win_rate_pct`: Percentage of closed trades profitable.
  - `last_signal_date` & `last_execution_date`: Timestamps of most recent pipeline runs.
  - `updated_at`: ISO timestamp for UI polling and caching.

- **`orders`**:
  - `order_id`: Unique identifier (e.g., `ORD_BUY_2026-10-24_RELIANCE`).
  - `mode`: `"paper"` / `"real"` / `"backtest"`.
  - `symbol`: NSE ticker.
  - `action`: `"BUY"` / `"SELL"`.
  - `shares`: Discrete integer share count.
  - `status`: `"PENDING_OPEN"`, `"FILLED"`, `"DEFERRED_LOWER_CIRCUIT"`, `"SKIPPED_GAP_LIMIT_EXCEEDED"`, `"SKIPPED_UNAFFORDABLE"`.
  - `reason`: Rule trigger (e.g., `SUPERTREND_BEARISH`, `EMA100_BREAKDOWN`, `RANK_1`).
  - `signal_date`: YYYY-MM-DD.

- **`executions`**: Fill log with raw open price, slippage-adjusted fill price, statutory fees breakdown, and net cash outlay.
- **`positions`**: Currently active holdings, entry price, cumulative entry fees, days held, and mark-to-market valuation.
- **`trades`**: Round-trip closed trade ledger with holding days, gross P&L, net P&L, total friction, and return %.
- **`daily_nav`**: Time series equity curve tracking cash balance, holdings valuation, total NAV, and active position count.

---

## 2. Forward Paper Trading & VPS Automation Guide

### Architecture & Daily Two-Phase Pipeline

```
                 Phase 1: Post-Market Signal Scan (15:45 IST)
                                      │
                 - Ingest latest daily EOD candles
                 - Evaluate structural exits & monthly rebalance rankings
                 - Insert orders into MongoDB ('orders')
                   (status="PENDING_OPEN", mode="paper")
                 - Broadcast post-market Telegram alert
                                      │
                                      ▼
                 Phase 2: Morning Open Execution (09:16 IST)
                                      │
                 - Ingest opening tick / 1-min candle
                 - Check lower-circuit locks (defer exit)
                 - Check upper-circuit & +2% gap caps (cancel buy)
                 - Execute valid sells first to release cash
                 - Size buys with whole integer shares & available cash
                 - Deduct statutory delivery fees & record executions
                 - Update positions, closed trades, and daily NAV in DB
                 - Broadcast morning execution summary to Telegram
```

### VPS Deployment Setup (Oracle Cloud Free Tier / Linux VPS)
1. **Clone & Install**:
   ```bash
   git clone https://github.com/deepadhia/NSE-Quant-Lab.git
   cd NSE-Quant-Lab
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Environment Variables (`.env`)**:
   ```ini
   MONGO_URI=mongodb://localhost:27017/
   MONGO_DB=stock_scanner
   MONGO_COLLECTION=upstox_daily_ohlcv

   TELEGRAM_ENABLED=true
   TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
   TELEGRAM_CHAT_ID=your_chat_id
   ```

3. **Database Sizing & Efficient Warm-Up Seeding**:
   - For live forward paper trading, you **do not** need 10-20 years of historical data on your live server!
   - Strategies require only ~150 to 200 trading sessions of historical warm-up (for 100-day EMA, Supertrend 10,3, and 6-month momentum).
   - For the Nifty 500 universe over 200 trading sessions, the total dataset is only ~100,000 documents (~7-10 MB of compressed disk storage in MongoDB).
   - Seed the initial portfolio balance once:
     ```bash
     python scripts/seed_database.py --strategy trend_momentum --capital 1000000
     ```

4. **Crontab Configuration (`crontab -e`)**:
   ```cron
   # 15:45 IST (10:15 UTC): Post-Market Daily Pipeline (Mon-Fri)
   45 10 * * 1-5 cd /home/ubuntu/NSE-Quant-Lab && /home/ubuntu/NSE-Quant-Lab/venv/bin/python scripts/run_daily_pipeline.py --strategy trend_momentum >> logs/pipeline.log 2>&1

   # 09:16 IST (03:46 UTC): Morning Execution Pipeline (Mon-Fri)
   46 03 * * 1-5 cd /home/ubuntu/NSE-Quant-Lab && /home/ubuntu/NSE-Quant-Lab/venv/bin/python scripts/execute_paper_orders.py --strategy trend_momentum >> logs/execution.log 2>&1
   ```

---

## 3. Strategy Developer Guide: Adding New Algos in <40 Lines

To introduce a new algorithm, inherit from `BaseStrategy` and decorate it with `@register_strategy("name")`. Zero modifications are needed to the execution engine, fee calculations, or paper trading runner.

### Minimal Example: Donchian 50-Day High Breakout
Create `src/nse_quant_lab/strategies/donchian_breakout.py`:

```python
import pandas as pd
from typing import Dict, Any, Optional
from ..base_strategy import BaseStrategy
from ..registry import register_strategy

@register_strategy("donchian_breakout")
class DonchianBreakoutStrategy(BaseStrategy):
    name = "DONCHIAN_BREAKOUT"
    rebalance_frequency = "DAILY"

    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        df = df.copy()
        df["high_50"] = df["high"].rolling(50).max().shift(1)  # Causal 50-day high
        df["low_20"] = df["low"].rolling(20).min().shift(1)    # Causal 20-day low
        df["turnover_50d"] = (df["close"] * df["volume"]).rolling(50).mean()
        return df

    def select_entry_candidates(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp, current_holdings: Dict[str, Any], config: Any) -> pd.DataFrame:
        candidates = []
        for sym, df in universe.items():
            if sym in current_holdings:
                continue
            rows = df[df["date"] == decision_date]
            if rows.empty:
                continue
            r = rows.iloc[0]
            if r["close"] > r["high_50"] and r["turnover_50d"] >= 10_000_000:
                candidates.append({"symbol": sym, "close": r["close"], "margin": r["close"] / r["high_50"]})
        if not candidates:
            return pd.DataFrame()
        return pd.DataFrame(candidates).sort_values("margin", ascending=False).set_index("symbol")

    def evaluate_daily_exits(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp, open_positions: Dict[str, Any], config: Any) -> Dict[str, str]:
        exits = {}
        for sym in open_positions:
            df = universe.get(sym)
            if df is None:
                continue
            rows = df[df["date"] == decision_date]
            if not rows.empty and rows.iloc[0]["close"] < rows.iloc[0]["low_20"]:
                exits[sym] = "BREAK_20D_LOW"
        return exits
```

### Instant CLI Usage
The strategy is immediately available across all tools:
```bash
# Run Backtest with SHA-256 Manifest
python scripts/run_backtest.py --strategy donchian_breakout --start 2021-01-01 --end 2026-10-23

# Forward Paper Trade
python scripts/run_daily_pipeline.py --strategy donchian_breakout
```

---

## 4. Real-Time Telegram Alerting Setup

The built-in `TelegramNotifier` uses standard Python HTTP libraries (zero external dependencies) to dispatch real-time alerts.

### 1. Bot Setup:
1. Message `@BotFather` on Telegram.
2. Send `/newbot`, name your bot (e.g., `NSE Quant Lab Alerts`), and save the HTTP API token:
   `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`
3. Start a chat with `@userinfobot` to retrieve your numeric `Id` (e.g. `987654321`).
4. Update `.env`:
   ```ini
   TELEGRAM_ENABLED=true
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVWXyz
   TELEGRAM_CHAT_ID=987654321
   ```

### 2. Alert Types Dispatched:
- **Post-Market Signals**: Triggered exits with reasons, scheduled buys with target allocations.
- **Morning Executions**: Fills executed with shares & prices, circuit lock deferrals, updated portfolio NAV.
