"""Reproduce results/realistic_pit_max_period with current code, then run stress variants.
Writes nothing into the repo."""
import sys, json, time
import numpy as np
import pandas as pd

sys.path.insert(0, r"C:/Users/DEEPJA~1/AppData/Local/Temp/claude/D--Personal-Projects-NSE-Quant-Lab/1dbda2cc-f19e-4d67-8ad9-d06d85c15be9/scratchpad/head_src/src")
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.data_loader import load_clean_universe

SCR = r"C:/Users/DEEPJA~1/AppData/Local/Temp/claude/D--Personal-Projects-NSE-Quant-Lab/1dbda2cc-f19e-4d67-8ad9-d06d85c15be9/scratchpad/"


class DelayedEntry(TrendMomentumStrategy):
    """Rebalance executes one session later than designed (T+2 open)."""
    def get_rebalance_schedule(self, timeline, eval_start, eval_end, config=None):
        base = super().get_rebalance_schedule(timeline, eval_start, eval_end, config)
        idx = {d: i for i, d in enumerate(timeline)}
        out = {}
        for ex, dec in base.items():
            j = idx[ex] + 1
            if j < len(timeline) and timeline[j] <= eval_end:
                out[timeline[j]] = dec
        return out


class MidMonth(TrendMomentumStrategy):
    """Same rules, decision on last session on/before the 15th (timing-luck test)."""
    def get_rebalance_schedule(self, timeline, eval_start, eval_end, config=None):
        df = pd.DataFrame({"date": timeline})
        df = df[df.date.dt.day <= 15]
        decs = df.groupby(df.date.dt.to_period("M")).date.max().tolist()
        idx = {d: i for i, d in enumerate(timeline)}
        out = {}
        for d in decs:
            j = idx[d] + 1
            if j < len(timeline) and eval_start <= timeline[j] <= eval_end:
                out[timeline[j]] = d
        return out


def cfg(**kw):
    base = dict(initial_capital=1_000_000.0, portfolio_size=6, max_open_positions=6,
                rebalance_hold_rank_buffer=15, rebalance_frequency="MONTHLY",
                min_turnover_50d=1e7, min_price=10.0, slippage_pct=0.0020,
                unaffordable_policy="TRY_NEXT", enforce_pit_universe=True,
                pit_top_n_liquidity=200, pit_max_universe_pct=0.45,
                brokerage_per_order=0.0, dp_charge_sell=14.75,
                max_volume_participation_pct=0.10, deduct_annual_tax=True)
    base.update(kw)
    return BacktestConfig(**base)


def summarize(name, nav, trades, m):
    nav = nav.copy(); nav["date"] = pd.to_datetime(nav["date"])
    out = {"run": name}
    for start in ["2005-01-01", "2016-01-01", "2021-01-01"]:
        s = nav[nav.date >= start]
        yrs = (s.date.iloc[-1] - s.date.iloc[0]).days / 365.25
        out[f"cagr_{start[:4]}"] = round(((s.nav.iloc[-1] / s.nav.iloc[0]) ** (1 / yrs) - 1) * 100, 2)
        out[f"mdd_{start[:4]}"] = round(((s.nav / s.nav.cummax()) - 1).min() * 100, 1)
    out["final_nav"] = round(float(nav.nav.iloc[-1]), 2)
    out["trades"] = len(trades)
    out["tax"] = round(m.get("total_tax_paid", 0.0))
    out["fees+slip"] = round(m["total_friction"])
    return out


t0 = time.time()
raw = pd.read_parquet(SCR + "ohlcv.parquet")
raw = raw[(raw.date >= "2004-01-01") & (raw.date <= "2026-10-23") & (raw.open > 0) & (raw.high > 0) & (raw.low > 0) & (raw.close > 0) & (raw.volume >= 0)]
raw = raw[(raw.high >= raw.low) & (raw.high >= raw.open) & (raw.high >= raw.close) & (raw.low <= raw.open) & (raw.low <= raw.close)]
universe = {s: g[["symbol", "date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True) for s, g in raw.groupby("symbol")}
assert len(universe) >= 490, f"universe too small: {len(universe)}"
import nse_quant_lab; print("code from", nse_quant_lab.__file__)
print("symbols", len(universe), "load s", round(time.time() - t0, 1), flush=True)

runs = [
    ("A_reproduce_base", TrendMomentumStrategy, cfg()),
    ("B_slippage_40bps", TrendMomentumStrategy, cfg(slippage_pct=0.0040)),
    ("C_entry_delay_1d", DelayedEntry, cfg()),
    ("D_midmonth_rebal", MidMonth, cfg()),
    ("E_12_slots", TrendMomentumStrategy, cfg(portfolio_size=12, max_open_positions=12, rebalance_hold_rank_buffer=30)),
    ("F_pretax_base", TrendMomentumStrategy, cfg(deduct_annual_tax=False)),
]
rows = []
for name, S, c in runs:
    t1 = time.time()
    strat = S(c)
    m, trades, orders, execs, nav = BacktestEngine(strat, c).run(universe, "2005-01-01", "2026-10-23")
    nav.to_csv(SCR + f"nav_{name}.csv", index=False)
    trades.to_csv(SCR + f"trades_{name}.csv", index=False)
    r = summarize(name, nav, trades, m)
    r["secs"] = round(time.time() - t1)
    rows.append(r)
    print(json.dumps(r), flush=True)

pd.DataFrame(rows).to_csv(SCR + "stress_summary.csv", index=False)
print(pd.DataFrame(rows).to_string())
