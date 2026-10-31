"""Runs the pre-registered grid in PROTOCOL.md. Usage: python variant_runner.py prep|dev|holdout"""
import sys, os, json, pickle, itertools
import numpy as np
import pandas as pd

SCR = r"C:/Users/DEEPJA~1/AppData/Local/Temp/claude/D--Personal-Projects-NSE-Quant-Lab/1dbda2cc-f19e-4d67-8ad9-d06d85c15be9/scratchpad/"
sys.path.insert(0, SCR + "wt_src/src")

from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.indicators import compute_indicators

DEV = ("2006-01-01", "2017-12-31")
HOLD = ("2018-01-01", "2026-10-23")
DROP_MONTHS = {"MONTHLY": None, "QUARTERLY": {3, 6, 9, 12}, "SEMIANNUAL": {6, 12}}
GRID = [{"rebal": r, "slots": s, "regime": g}
        for r, s, g in itertools.product(["MONTHLY", "QUARTERLY", "SEMIANNUAL"], [6, 15], [False, True])]


def load_index():
    from pymongo import MongoClient
    b = pd.DataFrame(list(MongoClient("mongodb://localhost:27017/")["stock_scanner"]["benchmark_ohlcv"].find(
        {"symbol": "^CRSLDX"}, {"_id": 0, "date": 1, "close": 1})))
    b["date"] = pd.to_datetime(b.date)
    return b.set_index("date").close.sort_index()


class Variant(TrendMomentumStrategy):
    def __init__(self, config, drop_months, regime_on, bull):
        super().__init__(config)
        self.drop_months, self.regime_on, self.bull = drop_months, regime_on, bull

    def _bullish(self, d):
        if not self.regime_on:
            return True
        s = self.bull.loc[:d]
        return bool(s.iloc[-1]) if len(s) else True

    def select_entry_candidates(self, universe, decision_date, current_holdings, config):
        if not self._bullish(decision_date):
            return pd.DataFrame(columns=["symbol", "close", "ema100", "supertrend_dir", "mom_6m", "turnover_50d", "rank"])
        return super().select_entry_candidates(universe, decision_date, current_holdings, config)

    def evaluate_rebalance_exits(self, universe, decision_date, open_positions, target_symbols, config):
        if not self._bullish(decision_date):
            return {s: "REGIME_EXIT" for s in sorted(open_positions)}
        if self.drop_months is not None and decision_date.month not in self.drop_months:
            return {}
        return super().evaluate_rebalance_exits(universe, decision_date, open_positions, target_symbols, config)


def make_cfg(slots, slippage=0.0020, tax=False):
    return BacktestConfig(initial_capital=1_000_000.0, portfolio_size=slots, max_open_positions=slots,
                          rebalance_hold_rank_buffer=int(round(2.5 * slots)), rebalance_frequency="MONTHLY",
                          min_turnover_50d=1e7, min_price=10.0, slippage_pct=slippage, unaffordable_policy="TRY_NEXT",
                          enforce_pit_universe=True, pit_top_n_liquidity=200, pit_max_universe_pct=0.45,
                          brokerage_per_order=0.0, dp_charge_sell=14.75, max_volume_participation_pct=0.10,
                          deduct_annual_tax=tax)


def stats(nav, idx, start, end):
    nav = nav.set_index(pd.to_datetime(nav["date"])).nav
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    b = idx.loc[nav.index[0]:nav.index[-1]]
    m = nav.resample("ME").last().pct_change().dropna()
    bm = b.resample("ME").last().pct_change().reindex(m.index)
    ex = (m - bm).dropna()
    return {
        "cagr": round(((nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1) * 100, 2),
        "n500_cagr": round(((b.iloc[-1] / b.iloc[0]) ** (1 / yrs) - 1) * 100, 2),
        "mdd": round(((nav / nav.cummax()) - 1).min() * 100, 1),
        "n500_mdd": round(((b / b.cummax()) - 1).min() * 100, 1),
        "vol": round(m.std() * np.sqrt(12) * 100, 1),
        "info_ratio": round(ex.mean() / ex.std() * np.sqrt(12), 3),
        "excess_t": round(ex.mean() / ex.std() * np.sqrt(len(ex)), 2),
    }


_U, _IDX, _BULL = None, None, None


def _init():
    global _U, _IDX, _BULL
    with open(SCR + "universe_ind.pkl", "rb") as f:
        _U = pickle.load(f)
    _IDX = load_index()
    _BULL = _IDX >= _IDX.rolling(200, min_periods=200).mean()
    _BULL[_IDX.rolling(200, min_periods=200).mean().isna()] = True


def run_one(job):
    v, (start, end), slippage, tax, tag = job
    cfg = make_cfg(v["slots"], slippage, tax)
    strat = Variant(cfg, DROP_MONTHS[v["rebal"]], v["regime"], _BULL)
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(_U, start, end)
    name = f"{tag}_{v['rebal']}_{v['slots']}_{'REG' if v['regime'] else 'NOREG'}"
    nav.to_csv(SCR + f"grid/nav_{name}.csv", index=False)
    trades.to_csv(SCR + f"grid/trades_{name}.csv", index=False)
    out = {"name": name, **v, "slippage_bps": int(slippage * 1e4), "post_tax": tax, **stats(nav, _IDX, start, end),
           "trades": len(trades), "fees_slip": round(m["total_friction"]), "tax": round(m.get("total_tax_paid", 0))}
    if len(trades):
        s = trades.net_pnl.sort_values(ascending=False)
        out["top10_share_pct"] = round(s.head(10).sum() / s.sum() * 100, 1) if s.sum() > 0 else None
    return out


def prep():
    raw = pd.read_parquet(SCR + "ohlcv.parquet")
    raw = raw[(raw.date >= "2004-01-01") & (raw.date <= "2026-10-23") & (raw.open > 0) & (raw.high > 0)
              & (raw.low > 0) & (raw.close > 0) & (raw.volume >= 0)]
    raw = raw[(raw.high >= raw.low) & (raw.high >= raw.open) & (raw.high >= raw.close) & (raw.low <= raw.open) & (raw.low <= raw.close)]
    cfg = make_cfg(6)
    U = {}
    for s, g in raw.groupby("symbol"):
        ind = compute_indicators(g[["symbol", "date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True), cfg)
        if not ind.empty:
            U[s] = ind
    assert len(U) >= 490, len(U)
    with open(SCR + "universe_ind.pkl", "wb") as f:
        pickle.dump(U, f)
    print("prepared", len(U), "symbols")


if __name__ == "__main__":
    from multiprocessing import Pool
    os.makedirs(SCR + "grid", exist_ok=True)
    phase = sys.argv[1]
    if phase == "prep":
        prep()
        sys.exit(0)
    if phase == "dev":
        jobs = [(v, DEV, 0.0020, False, "DEV") for v in GRID]
    else:
        sel = json.load(open(SCR + "selected.json"))
        v = {k: sel[k] for k in ("rebal", "slots", "regime")}
        jobs = [(v, HOLD, 0.0020, False, "HOLD_SELECTED"),
                (v, HOLD, 0.0040, False, "HOLD_SELECTED_40BPS"),
                (v, HOLD, 0.0020, True, "HOLD_SELECTED_POSTTAX")]
        jobs += [(g, HOLD, 0.0020, False, "HOLD_CONTEXT") for g in GRID]
    with Pool(6, initializer=_init) as p:
        rows = p.map(run_one, jobs)
    df = pd.DataFrame(rows)
    df.to_csv(SCR + f"grid/{phase}_results.csv", index=False)
    pd.set_option("display.width", 250)
    print(df.drop(columns=["name"]).to_string())
    if phase == "dev":
        best = df.sort_values(["info_ratio", "trades"], ascending=[False, True]).iloc[0]
        sel = {k: (bool(best[k]) if k == "regime" else (int(best[k]) if k == "slots" else best[k])) for k in ("rebal", "slots", "regime")}
        sel["dev_info_ratio"] = float(best["info_ratio"])
        json.dump(sel, open(SCR + "selected.json", "w"), indent=2)
        print("SELECTED BY RULE:", sel)
