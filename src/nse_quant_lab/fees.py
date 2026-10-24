"""
fees.py -- Decomposed Indian Equity Delivery Statutory Fee Calculator
"""

from __future__ import annotations
from typing import Dict
import pandas as pd
from .config import BacktestConfig


def get_effective_exchange_fee_pct(dt: pd.Timestamp, config: BacktestConfig) -> float:
    """Returns effective NSE cash delivery transaction charge pct on date dt."""
    if pd.to_datetime(dt) < pd.to_datetime("2024-10-01"):
        return config.exchange_txn_pct_legacy
    return config.exchange_txn_pct_revised


def calc_buy_fees_exact(gross_val: float, dt: pd.Timestamp, config: BacktestConfig) -> Dict[str, float]:
    """Computes decomposed buy delivery charges."""
    if gross_val <= 0:
        return {"total": 0.0, "brokerage": 0.0, "stt": 0.0, "stamp_duty": 0.0, "exchange_txn": 0.0, "sebi": 0.0, "gst": 0.0}
    brokerage = config.brokerage_per_order
    stt = gross_val * (config.stt_pct / 100.0)
    stamp = gross_val * (config.stamp_duty_buy_pct / 100.0)
    exch_pct = get_effective_exchange_fee_pct(dt, config)
    exch = gross_val * (exch_pct / 100.0)
    sebi = gross_val * (config.sebi_pct / 100.0)
    gst = (brokerage + exch + sebi) * (config.gst_pct / 100.0)
    total = brokerage + stt + stamp + exch + sebi + gst
    return {
        "brokerage": round(brokerage, 4), "stt": round(stt, 4), "stamp_duty": round(stamp, 4),
        "exchange_txn": round(exch, 4), "sebi": round(sebi, 4), "gst": round(gst, 4), "total": round(total, 4),
    }


def calc_sell_fees_exact(gross_val: float, dt: pd.Timestamp, config: BacktestConfig) -> Dict[str, float]:
    """Computes decomposed sell delivery charges."""
    if gross_val <= 0:
        return {"total": 0.0, "brokerage": 0.0, "stt": 0.0, "stamp_duty": 0.0, "exchange_txn": 0.0, "sebi": 0.0, "gst": 0.0, "dp_charges": 0.0}
    brokerage = config.brokerage_per_order
    stt = gross_val * (config.stt_pct / 100.0)
    stamp = 0.0
    exch_pct = get_effective_exchange_fee_pct(dt, config)
    exch = gross_val * (exch_pct / 100.0)
    sebi = gross_val * (config.sebi_pct / 100.0)
    gst = (brokerage + exch + sebi) * (config.gst_pct / 100.0)
    dp = config.dp_charge_sell
    total = brokerage + stt + stamp + exch + sebi + gst + dp
    return {
        "brokerage": round(brokerage, 4), "stt": round(stt, 4), "stamp_duty": round(stamp, 4),
        "exchange_txn": round(exch, 4), "sebi": round(sebi, 4), "gst": round(gst, 4), "dp_charges": round(dp, 4), "total": round(total, 4),
    }
