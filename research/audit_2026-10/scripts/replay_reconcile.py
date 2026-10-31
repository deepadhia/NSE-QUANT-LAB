#!/usr/bin/env python3
"""
Replay reconciliation: run the PAPER manager day by day over a window (signals after each close, fills at the next open) in a
SCRATCH Mongo database, run the BACKTEST engine over the same window with the same strategy, and diff the entries/exits.
Never touches stock_scanner paper collections (writes only to a scratch DB it creates).
  python replay_reconcile.py 2024-03-28 2024-07-31
"""
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
import nse_quant_lab  # noqa: F401,E402  (registers strategies)
from nse_quant_lab.config import BacktestConfig, PaperTradingConfig  # noqa: E402
from nse_quant_lab.data_loader import load_pit_universe  # noqa: E402
from nse_quant_lab.db.mongo_client import MongoDatabase  # noqa: E402
from nse_quant_lab.engine import BacktestEngine  # noqa: E402
from nse_quant_lab.paper.manager import PaperTradingManager  # noqa: E402
from nse_quant_lab.registry import get_strategy  # noqa: E402

URI = "mongodb://localhost:27017/"


def main(start: str, end: str) -> None:
    scratch = f"stock_scanner_replay_{int(time.time())}"
    db = MongoDatabase(URI, scratch)
    cfg_kw = dict(strategy_id="trend_momentum_pit", portfolio_size=15, initial_capital=100000.0, rebalance_hold_rank_buffer=1500,
                  unaffordable_policy="TRY_NEXT", brokerage_per_order=0.0, dp_charge_sell=14.75, mode="paper",
                  rebalance_frequency="MONTHLY", mongo_uri=URI, db_name=scratch, telegram_enabled=False)
    pcfg = PaperTradingConfig(**cfg_kw)
    strat = get_strategy("trend_momentum_pit", pcfg)
    t0 = time.time()
    raw = load_pit_universe(pd.Timestamp(end).strftime("%Y-%m-%d"))
    uni = {s: strat.compute_indicators(g, pcfg) for s, g in raw.items()}
    uni = {s: g for s, g in uni.items() if not g.empty}
    for g in uni.values():
        g["_strategy_computed"] = True
    print(f"universe {len(uni)} symbols, indicators ready in {time.time() - t0:.0f}s", flush=True)
    dates = sorted({d for g in uni.values() for d in g["date"] if pd.Timestamp(start) <= d <= pd.Timestamp(end)})
    mgr = PaperTradingManager(strategy=strat, config=pcfg, db=db)
    mgr.initialize_portfolio()
    for i, d in enumerate(dates[:-1]):
        mgr.generate_post_market_signals(uni, d)
        mgr.execute_morning_fills(uni, dates[i + 1])
    paper_trades = pd.DataFrame(list(db.trades.find({}, {"_id": 0})))
    paper_open = pd.DataFrame(list(db.positions.find({}, {"_id": 0})))
    nav_p = pd.DataFrame(list(db.daily_nav.find({"date": {"$gte": start}}, {"_id": 0}))).sort_values("date")

    ecfg = BacktestConfig(initial_capital=100000.0, portfolio_size=15, max_open_positions=15, rebalance_hold_rank_buffer=1500,
                          rebalance_frequency="MONTHLY", min_turnover_50d=0.0, min_price=0.0, slippage_pct=0.0, unaffordable_policy="TRY_NEXT",
                          enforce_pit_universe=False, pit_top_n_liquidity=0, brokerage_per_order=0.0, dp_charge_sell=14.75,
                          max_volume_participation_pct=0.10, deduct_annual_tax=False)
    es = get_strategy("trend_momentum_pit", ecfg)
    m, trades, orders, execs, nav = BacktestEngine(es, ecfg).run(uni, eval_start_date=dates[1].strftime("%Y-%m-%d"), eval_end_date=end)

    eb = execs[execs["action"] == "BUY"][["date", "symbol"]].assign(k=lambda x: x.date.astype(str) + "|" + x.symbol)
    es_ = execs[execs["action"] == "SELL"][["date", "symbol"]].assign(k=lambda x: x.date.astype(str) + "|" + x.symbol)
    pb = pd.DataFrame(list(db.executions.find({"action": "BUY"}, {"_id": 0, "symbol": 1, "executed_at": 1, "order_id": 1})))
    ps = pd.DataFrame(list(db.executions.find({"action": "SELL"}, {"_id": 0, "symbol": 1, "order_id": 1})))
    # the execution date is encoded in the order/exec ids: EXEC_<date>_<sym>_<uuid>
    ids = pd.DataFrame(list(db.executions.find({}, {"_id": 0, "exec_id": 1, "action": 1, "symbol": 1})))
    ids["date"] = ids["exec_id"].str.split("_").str[1]
    pk_b = set((ids[ids.action == "BUY"]["date"] + "|" + ids[ids.action == "BUY"]["symbol"]))
    pk_s = set((ids[ids.action == "SELL"]["date"] + "|" + ids[ids.action == "SELL"]["symbol"]))
    ek_b, ek_s = set(eb["k"]), set(es_["k"])
    print(f"\nWINDOW {start} -> {end}")
    print(f"BUYS  paper {len(pk_b)} | engine {len(ek_b)} | common {len(pk_b & ek_b)} | paper-only {sorted(pk_b - ek_b)[:12]} | engine-only {sorted(ek_b - pk_b)[:12]}")
    print(f"SELLS paper {len(pk_s)} | engine {len(ek_s)} | common {len(pk_s & ek_s)} | paper-only {sorted(pk_s - ek_s)[:12]} | engine-only {sorted(ek_s - pk_s)[:12]}")
    print(f"NAV end: paper {nav_p['nav'].iloc[-1]:,.0f} | engine {nav['nav'].iloc[-1]:,.0f}  (engine here uses ZERO slippage; paper uses the cost model)")
    print("paper open positions:", sorted(paper_open["symbol"].tolist()) if len(paper_open) else [])
    print("scratch DB:", scratch)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
