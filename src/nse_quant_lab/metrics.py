"""
metrics.py -- Quantitative Performance & Risk Analytics
"""

from __future__ import annotations
from typing import Any, Dict
import numpy as np
import pandas as pd


def calculate_financial_metrics(
    initial_capital: float,
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    closed_trades: list,
    stat_fees: float,
    slippage: float,
    skipped_orders: int,
    rf_rate: float = 0.06,
) -> Dict[str, Any]:
    """Calculates comprehensive financial performance and risk metrics."""
    ending_nav = float(nav_df["nav"].iloc[-1]) if not nav_df.empty else initial_capital
    net_pnl = ending_nav - initial_capital
    total_ret_pct = (net_pnl / initial_capital) * 100.0

    n_sessions = len(nav_df)
    years_trading = n_sessions / 250.0 if n_sessions > 0 else 1.0
    cagr_pct = ((ending_nav / initial_capital) ** (1.0 / years_trading) - 1.0) * 100.0 if years_trading > 0 and ending_nav > 0 else 0.0

    if not nav_df.empty and len(nav_df) > 1:
        d0 = pd.to_datetime(nav_df["date"].iloc[0])
        d1 = pd.to_datetime(nav_df["date"].iloc[-1])
        cal_days = (d1 - d0).days
        years_calendar = cal_days / 365.25
        cagr_calendar_pct = ((ending_nav / initial_capital) ** (1.0 / years_calendar) - 1.0) * 100.0 if years_calendar > 0 and ending_nav > 0 else 0.0
    else:
        years_calendar = 0.0
        cagr_calendar_pct = 0.0

    if not nav_df.empty:
        peak = nav_df["nav"].cummax()
        dd = (nav_df["nav"] - peak) / peak
        max_dd_pct = float(dd.min()) * 100.0
    else:
        max_dd_pct = 0.0

    n_trades = len(closed_trades)
    win_trades = [t for t in closed_trades if t.get("net_pnl", 0.0) > 0]
    loss_trades = [t for t in closed_trades if t.get("net_pnl", 0.0) < 0]

    win_rate = (len(win_trades) / n_trades * 100.0) if n_trades > 0 else 0.0
    gross_win = sum(t.get("net_pnl", 0.0) for t in win_trades)
    gross_loss = abs(sum(t.get("net_pnl", 0.0) for t in loss_trades))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    if not nav_df.empty and len(nav_df) > 1:
        daily_rets = nav_df["nav"].pct_change().dropna()
        ann_vol = float(daily_rets.std() * np.sqrt(250))
        sharpe = (cagr_pct / 100.0 - rf_rate) / ann_vol if ann_vol > 0 else 0.0
    else:
        ann_vol = 0.0
        sharpe = 0.0

    return {
        "initial_capital": initial_capital,
        "ending_nav": ending_nav,
        "net_pnl": net_pnl,
        "total_return_pct": total_ret_pct,
        "cagr_pct": cagr_pct,
        "cagr_calendar_pct": cagr_calendar_pct,
        "max_drawdown_pct": max_dd_pct,
        "annualized_vol_pct": ann_vol * 100.0,
        "sharpe_ratio": sharpe,
        "total_trades": n_trades,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "total_statutory_fees": stat_fees,
        "total_slippage": slippage,
        "total_friction": stat_fees + slippage,
        "skipped_orders": skipped_orders,
        "years_trading": round(years_trading, 2),
        "years_calendar": round(years_calendar, 2),
    }
