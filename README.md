# NSE-Quant-Lab

**Institutional Quantitative Research, Event-Driven Backtesting & Forward Paper Trading Harness for National Stock Exchange of India (NSE) Equities.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Market: NSE Cash](https://img.shields.io/badge/Market-NSE%20Cash%20Delivery-orange.svg)](#key-highlights)

---

## Key Highlights

1. **Realistic Indian Market Friction**: Full decomposed delivery statutory fee schedule (STT, Stamp Duty, SEBI turnover, Exchange transaction charges with dynamic historical tiers, GST, DP charges) + adverse slippage per side.
2. **Circuit & Execution Invariants**: Enforces lower-circuit lock deferrals with persistent capital locking, upper-circuit buy rejection, +2.0% gap cancellation, and discrete whole-share sizing.
3. **Plug-and-Play Strategy Registry**: Implement any strategy in under 40 lines using `@register_strategy("name")`.
4. **Prospective Forward Paper Trading**: Daily post-market scan and morning execution pipeline designed for automated deployment (e.g. on Oracle Cloud VPS) logging all orders/positions into MongoDB with `mode: "paper"` and Telegram notifications.
5. **Cryptographic Proof**: Every backtest produces a complete JSON manifest with SHA-256 hashes of all ledgers and outputs.
6. **Built-in Agent Rules & Skills**: Full `.agents/skills/audit-backtest/` and `AGENTS.md` guidelines preventing AI hallucination and enforcing research integrity.

---

## Documentation

- [System Architecture & Schema Design](docs/ARCHITECTURE.md) - Event-driven flow and MongoDB data model (`mode: "paper"` vs `"real"`).
- [Forward Paper Trading & VPS Setup Guide](docs/PAPER_TRADING.md) - Setting up automated crontab on an Oracle Cloud / Linux server.
- [Strategy Developer Guide](docs/STRATEGY_GUIDE.md) - Building and registering new quant algorithms in <40 lines.
- [Telegram Bot Alerting Setup](docs/TELEGRAM_SETUP.md) - Configuring real-time alerts for signals, fills, and NAV updates.
- [Research & Integrity Contract](AGENTS.md) - Anti-hallucination, temporal causality, and audit rules.
- [Backtest Validation Standards](BACKTEST_VALIDATION.md) - Guidelines for lookahead prevention, bias avoidance, and execution realism.

---

## Quickstart

```bash
# Clone the repository
git clone https://github.com/deepadhia/NSE-Quant-Lab.git
cd NSE-Quant-Lab

# Install dependencies
pip install -r requirements.txt

# Run automated test suite
pytest -v

# Run Baseline Reproduction (2019-2026 Trend Momentum)
python scripts/reproduce_baseline.py

# Run 9-Cell Capital Feasibility Matrix
python scripts/run_capital_matrix.py

# Run a custom backtest with cryptographic manifest
python scripts/run_backtest.py --strategy km_momentum --capital 1000000 --slots 12
```

---

## Adding a New Strategy in <40 Lines

```python
import pandas as pd
from typing import Dict, Any, Optional
from nse_quant_lab.base_strategy import BaseStrategy
from nse_quant_lab.registry import register_strategy

@register_strategy("my_momentum")
class MyMomentumStrategy(BaseStrategy):
    name = "MY_MOMENTUM"
    rebalance_frequency = "MONTHLY"

    def compute_indicators(self, df: pd.DataFrame, config: Optional[Any] = None) -> pd.DataFrame:
        df = df.copy()
        df["mom_3m"] = df["close"].pct_change(63)
        return df

    def select_entry_candidates(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp, current_holdings: Dict[str, Any], config: Any) -> pd.DataFrame:
        candidates = []
        for sym, df in universe.items():
            if sym in current_holdings:
                continue
            rows = df.loc[df["date"] == decision_date]
            if not rows.empty and pd.notna(rows.iloc[0]["mom_3m"]):
                candidates.append({"symbol": sym, "score": rows.iloc[0]["mom_3m"], "close": rows.iloc[0]["close"]})
        if not candidates:
            return pd.DataFrame()
        cdf = pd.DataFrame(candidates).sort_values(by="score", ascending=False).set_index("symbol", drop=False)
        cdf["rank"] = 1
        return cdf

    def evaluate_daily_exits(self, universe: Dict[str, pd.DataFrame], decision_date: pd.Timestamp, open_positions: Dict[str, Any], config: Any) -> Dict[str, str]:
        return {}  # Managed via monthly rebalance drop
```

---

## Two-Phase Daily Automation Workflow

```
               [15:45 IST] Post-Market Daily Pipeline
                                 │
             Ingest EOD Candles & Evaluate Signals
                                 │
             Insert Orders into MongoDB ('orders')
                     (status='PENDING_OPEN', mode='paper')
                                 │
               Send Post-Market Telegram Alert
                                 ▼
               [09:16 IST] Morning Execution Pipeline
                                 │
             Ingest Opening Ticks & Check Limits:
               - Lower Circuit Lock -> Defer Exit
               - Upper Circuit Lock -> Skip Buy
               - Opening Gap > +2%  -> Cancel Buy
                                 │
               Execute Fills & Deduct Statutory Fees
                                 │
             Update Positions, Trades, Daily NAV
                                 │
               Send Execution Telegram Alert
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
