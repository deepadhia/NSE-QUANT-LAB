"""
corporate_events.py -- explicit, evidenced events for held names that stop trading (mergers, delistings, cash-outs).

Why this exists: a held symbol with no market data (`MISSING_HELD_BAR`) must never be sold or written down because
data is missing. A real merger/delisting needs a human-recorded event with evidence; the paper manager then applies it
exactly once per account. Splits/bonuses stay in `corporate_actions`; this collection is for everything else.

Collection `corporate_events`, one document per event (unique `event_id`):
  common      : event_id, symbol, type, effective_date 'YYYY-MM-DD' (NSE trading cessation / ex-date, not retroactive NCLT appointed date), evidence (free text / URL / circular number), recorded_at
  CASH_OUT    : cash_per_share  NET cash credited per share (0.0 is a legitimate, explicit write-off; it is never a default)
  CONVERSION  : new_symbol, ratio (new shares per 1 old share), fraction_cash_per_new_share
                (cash paid per NEW share for the fractional entitlement; REQUIRED, may be 0.0, whenever shares*ratio is
                not a whole number. Fraction treatment differs by scheme and must be stated, never guessed.)
  FRACTION_CASH : settles the PENDING fractional entitlement that a bonus/consolidation left in `fractional_entitlements`.
                effective_date = the date the cash was actually paid; entitlement_ex_date = ex-date of the share action;
                cash_per_fraction_share = NET cash per ONE whole share of the fraction (credit = fraction_shares x this;
                0.0 is a legitimate, explicit evidenced no-payment). Applied from the ledger, so it works even after the stock was sold.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict

COLLECTION = "corporate_events"
CASH_OUT = "CASH_OUT"
CONVERSION = "CONVERSION"
FRACTION_CASH = "FRACTION_CASH"
EVENT_TYPES = (CASH_OUT, CONVERSION, FRACTION_CASH)
MIN_EVIDENCE_CHARS = 10
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def event_id_for(symbol: str, event_type: str, effective_date: str) -> str:
    return f"{str(symbol).strip()}:{str(event_type).strip()}:{str(effective_date).strip()}"


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_event(doc: Dict[str, Any]) -> None:
    """Raise ValueError describing the first problem. A recorded event that fails this is never applied."""
    if not isinstance(doc, dict):
        raise ValueError("event is not a document")
    symbol, etype, eff = doc.get("symbol"), doc.get("type"), doc.get("effective_date")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol is required")
    if etype not in EVENT_TYPES:
        raise ValueError(f"type must be one of {EVENT_TYPES}, got {etype!r}")
    if not isinstance(eff, str) or not _DATE_RE.match(eff):
        raise ValueError("effective_date must be 'YYYY-MM-DD'")
    try:
        datetime.strptime(eff, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"effective_date is not a real date: {eff}") from exc
    evidence = doc.get("evidence")
    if not isinstance(evidence, str) or len(evidence.strip()) < MIN_EVIDENCE_CHARS:
        raise ValueError(f"evidence is required (circular number, URL or filing reference, >= {MIN_EVIDENCE_CHARS} chars)")
    if doc.get("event_id") != event_id_for(symbol, etype, eff):
        raise ValueError(f"event_id must be {event_id_for(symbol, etype, eff)!r}")
    if etype == CASH_OUT:
        cps = doc.get("cash_per_share")
        if not _finite(cps) or float(cps) < 0:
            raise ValueError("CASH_OUT needs an explicit cash_per_share >= 0 (0 = evidenced write-off)")
    elif etype == FRACTION_CASH:
        ex = doc.get("entitlement_ex_date")
        if not isinstance(ex, str) or not _DATE_RE.match(ex):
            raise ValueError("FRACTION_CASH needs entitlement_ex_date 'YYYY-MM-DD' (ex-date of the share action it settles)")
        try:
            datetime.strptime(ex, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"entitlement_ex_date is not a real date: {ex}") from exc
        if ex > eff:
            raise ValueError("entitlement_ex_date cannot be after the payment (effective) date")
        cpf = doc.get("cash_per_fraction_share")
        if not _finite(cpf) or float(cpf) < 0:
            raise ValueError("FRACTION_CASH needs an explicit cash_per_fraction_share >= 0 (0 = evidenced no payment)")
    else:
        new_symbol, ratio = doc.get("new_symbol"), doc.get("ratio")
        if not isinstance(new_symbol, str) or not new_symbol.strip() or new_symbol.strip() == symbol.strip():
            raise ValueError("CONVERSION needs a new_symbol different from symbol")
        if not _finite(ratio) or float(ratio) <= 0:
            raise ValueError("CONVERSION needs ratio > 0 (new shares per 1 old share)")
        fcs = doc.get("fraction_cash_per_new_share")
        if fcs is not None and (not _finite(fcs) or float(fcs) < 0):
            raise ValueError("fraction_cash_per_new_share must be >= 0 when given")


def build_event(event_type: str, symbol: str, effective_date: str, evidence: str, *,
                cash_per_share: Any = None, new_symbol: Any = None, ratio: Any = None,
                fraction_cash_per_new_share: Any = None, entitlement_ex_date: Any = None,
                cash_per_fraction_share: Any = None) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "event_id": event_id_for(symbol, event_type, effective_date),
        "symbol": str(symbol).strip(),
        "type": event_type,
        "effective_date": effective_date,
        "evidence": evidence,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if event_type == CASH_OUT:
        doc["cash_per_share"] = cash_per_share
    elif event_type == FRACTION_CASH:
        doc.update(entitlement_ex_date=entitlement_ex_date, cash_per_fraction_share=cash_per_fraction_share)
    else:
        doc.update(new_symbol=None if new_symbol is None else str(new_symbol).strip(), ratio=ratio,
                   fraction_cash_per_new_share=fraction_cash_per_new_share)
    validate_event(doc)
    return doc


def split_whole_and_fraction(shares: int, ratio: float):
    """(whole new shares, fractional remainder in new shares) for a CONVERSION."""
    exact = float(shares) * float(ratio)
    whole = int(math.floor(exact + 1e-9))
    frac = exact - whole
    return whole, (frac if frac > 1e-9 else 0.0)
