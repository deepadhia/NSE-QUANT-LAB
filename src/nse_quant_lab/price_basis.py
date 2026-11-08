"""Separate adjusted analytical prices from nominal exchange prices and share units."""
import numpy as np
import pandas as pd

PRICE_COLUMNS = ("open", "high", "low", "close")
BASIS_COLUMNS = tuple("raw_" + c for c in PRICE_COLUMNS) + (
    "price_adjustment_factor", "split_ratio", "bonus_ratio", "share_action_verified")


def nominal_price(row, column="close") -> float:
    value = float(row.get("raw_" + column, row[column]))
    if not np.isfinite(value) or value <= 0:
        raise ValueError("PRICE_BASIS_INVALID: missing/nonpositive nominal price")
    return value


def execution_bar(row):
    bar = row.copy()
    for column in PRICE_COLUMNS:
        bar[column] = nominal_price(row, column)
    return bar


def adjustment_factor(row) -> float:
    factor = float(row.get("price_adjustment_factor", 1.0))
    if not np.isfinite(factor) or factor <= 0:
        raise ValueError("PRICE_BASIS_INVALID: invalid adjustment factor")
    return factor


def previous_close(previous, current) -> float:
    # Exchange circuit/gap reference after a split is on the NEW share basis.
    return nominal_price(previous) / (adjustment_factor(current) / adjustment_factor(previous))


def sale_tax_lots(pos, shares, cash_received, date, fill_price, sell_fees):
    """Allocate actual proceeds/fees over dated acquisition lots, including zero-cost bonus lots."""
    lots = pos.get("tax_lots", [{"shares": shares, "entry_date": pos["entry_date"],
        "outlay": pos["total_entry_outlay"], "buy_stt": pos["buy_fees"]["stt"]}])
    return [{"shares": lot["shares"], "entry_date": str(pd.Timestamp(lot["entry_date"]).date()),
        "exit_date": str(date.date()), "hold_days": (date-pd.Timestamp(lot["entry_date"])).days,
        "net_pnl": cash_received*lot["shares"]/shares-lot["outlay"],
        "entry_fill_price": 0.0, "exit_fill_price": fill_price,
        "stt_paid": lot["buy_stt"]+sell_fees["stt"]*lot["shares"]/shares} for lot in lots]


def apply_share_action(pos, row, date):
    old_factor = pos.get("price_adjustment_factor", 1.0)
    factor = adjustment_factor(row)
    ratio = factor / old_factor
    if np.isclose(ratio, 1., rtol=1e-8):
        return None
    if not bool(row.get("share_action_verified", False)):
        raise ValueError(f"UNVERIFIED_SHARE_ACTION: {pos['symbol']} on {date.date()}")
    split = float(row.get("split_ratio", 1.0))
    bonus = float(row.get("bonus_ratio", 1.0))
    if not np.isclose(split*bonus, ratio, rtol=1e-6):
        raise ValueError("SHARE_ACTION_MISMATCH: raw prices and recorded event disagree")
    old_shares = pos["shares"]
    new_shares = old_shares*ratio
    # Fractional entitlements require an actual cash-in-lieu policy/event, never invented proceeds.
    if not np.isclose(new_shares, round(new_shares), atol=1e-7, rtol=0):
        raise ValueError(f"FRACTIONAL_ENTITLEMENT_UNRESOLVED: {pos['symbol']} on {date.date()}")
    lots = pos["tax_lots"]
    for lot in lots:
        lot["shares"] *= split
    if bonus != 1.0:
        lots.append({"shares": old_shares*split*(bonus-1), "entry_date": date,
                     "outlay": 0.0, "buy_stt": 0.0})
    pos["shares"] = int(round(new_shares))
    pos["entry_raw_price"] /= ratio
    pos["entry_fill_price"] /= ratio
    pos["price_adjustment_factor"] = factor
    return {"symbol": pos["symbol"], "date": str(date.date()), "ratio": ratio,
            "old_shares": old_shares, "new_shares": pos["shares"]}
