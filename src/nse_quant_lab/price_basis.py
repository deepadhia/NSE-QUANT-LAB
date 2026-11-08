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


def apply_share_action(pos, row, date, fractional_policy="strict", unverified_policy="strict"):
    old_factor = pos.get("price_adjustment_factor", 1.0)
    factor = adjustment_factor(row)
    ratio = factor / old_factor
    if np.isclose(ratio, 1., rtol=1e-8):
        return None
    if bool(row.get("share_action_verified", False)):
        split = float(row.get("split_ratio", 1.0))
        bonus = float(row.get("bonus_ratio", 1.0))
        if not np.isclose(split*bonus, ratio, rtol=1e-6):
            raise ValueError("SHARE_ACTION_MISMATCH: raw prices and recorded event disagree")
    elif unverified_policy == "accept_factor":
        split, bonus = ratio, 1.0     # RESEARCH-ONLY: the price-factor jump is taken as a split of that ratio (no official event matched)
    else:
        raise ValueError(f"UNVERIFIED_SHARE_ACTION: {pos['symbol']} on {date.date()}")
    old_shares = pos["shares"]
    new_shares = old_shares*ratio
    # Fractional entitlements require an actual cash-in-lieu policy/event, never invented proceeds.
    fractional = not np.isclose(new_shares, round(new_shares), atol=1e-7, rtol=0)
    if fractional and fractional_policy != "floor_no_cash":
        raise ValueError(f"FRACTIONAL_ENTITLEMENT_UNRESOLVED: {pos['symbol']} on {date.date()}")
    lots = pos["tax_lots"]
    for lot in lots:
        lot["shares"] *= split
    if bonus != 1.0:
        lots.append({"shares": old_shares*split*(bonus-1), "entry_date": date,
                     "outlay": 0.0, "buy_stt": 0.0})
    pos["shares"] = int(np.floor(new_shares + 1e-9)) if fractional else int(round(new_shares))
    if fractional:   # the forfeited fraction leaves the tax lots too, so lot shares still sum to the position
        total = sum(lot["shares"] for lot in lots)
        for lot in lots:
            lot["shares"] *= pos["shares"] / total
    pos["entry_raw_price"] /= ratio
    pos["entry_fill_price"] /= ratio
    pos["price_adjustment_factor"] = factor
    return {"symbol": pos["symbol"], "date": str(date.date()), "ratio": ratio,
            "old_shares": old_shares, "new_shares": pos["shares"]}
