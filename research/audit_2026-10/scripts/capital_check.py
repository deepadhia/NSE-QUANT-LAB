"""Same near-true setup (now 43 external stocks), at Rs 10 lakh vs Rs 1 lakh starting capital."""
import pandas as pd
from multiprocessing import Pool
import near_true_test as T
from variant_runner import Variant, make_cfg, stats
from nse_quant_lab.engine import BacktestEngine


def run(job):
    name, slots, capital = job
    cfg = make_cfg(slots, 0.002, False)
    cfg.initial_capital = capital
    strat = Variant(cfg, None, False, None)
    strat.pit_universe.events_by_sym = {}
    m, trades, orders, execs, nav = BacktestEngine(strat, cfg).run(T._U, T.START, T.END)
    nav.to_csv(T.OUT + f"nav_{name}.csv", index=False)
    trades.to_csv(T.OUT + f"trades_{name}.csv", index=False)
    o = orders
    skipped = o[o.status.astype(str).str.startswith(("SKIPPED", "REJECTED"))] if len(o) else o
    s = trades.net_pnl.sort_values(ascending=False)
    return {"run": name, "slots": slots, "capital": capital, **stats(nav, T._IDX, T.START, T.END),
            "trades": len(trades), "avg_open_positions": round(float(nav.open_positions.mean()), 2),
            "avg_cash_pct": round(float((nav.cash / nav.nav).mean() * 100), 1),
            "orders_skipped_or_rejected": len(skipped),
            "top10_share_pct": round(s.head(10).sum() / s.sum() * 100, 1) if s.sum() > 0 else None}


if __name__ == "__main__":
    jobs = [("I_43ext_6_10L", 6, 1_000_000.0), ("J_43ext_15_10L", 15, 1_000_000.0),
            ("K_43ext_6_1L", 6, 100_000.0), ("L_43ext_15_1L", 15, 100_000.0)]
    with Pool(4, initializer=T._init) as p:
        res = p.map(run, jobs)
    pd.set_option("display.width", 250)
    df = pd.DataFrame(res)
    df.to_csv(T.OUT + "capital_check.csv", index=False)
    print(df.to_string(index=False))
