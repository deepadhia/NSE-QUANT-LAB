"""
corporate_actions.py -- Autonomous NSE Corporate Actions Ingestion & Parser
============================================================================
Fetches official corporate announcements from NSE's Corporate Actions API,
parses equity splits, consolidations, and share bonuses, and stores them in
MongoDB collection `corporate_actions`.

Used by PaperTradingManager to dynamically adjust position quantities and entry
prices on ex-dates without manual intervention.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# User-Agent matching modern browsers
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_today() -> date:
    """Returns today's date in Indian Standard Time (IST)."""
    return datetime.now(IST).date()
BONUS_KEYWORD = re.compile(r"\bbonus\b|\bbon\b", re.I)
NON_EQUITY_BONUS = re.compile(r"\b(?:deb|debenture\w*|pref|preference\w*|ncrps|nccps|dvr|ncd|redeemable)\b", re.I)
BONUS_PATTERN = re.compile(r"(?<![\d.])(\d{1,6})\s*:\s*(\d{1,6})(?![\d.])")

SPLIT_KEYWORD = re.compile(r"\b(?:split|sub[\s-]?division|\bspl\b|\bfv\b|consolidation|consolidat\w*)\b", re.I)
AMOUNT_PATTERN = r"(?:rs|re|inr)?\.?\s*-?\s*([0-9]+(?:\.[0-9]+)?)"
SPLIT_PATTERN = re.compile(
    r"(?:from\s+)?" + AMOUNT_PATTERN + r"\D*?(?:to\s+)" + AMOUNT_PATTERN,
    re.I
)

SERIES_OK = {"EQ", "BE", "BZ", "SM", "ST"}


# Split/consolidation wording that makes an announcement "relevant" for the manual-review flag (bare "FV" is too common in dividends)
REVIEW_SPLIT_KEYWORD = re.compile(r"\b(?:split|sub[\s-]?division|spl|consolidat\w*)\b", re.I)


def _normalise(subject: str) -> str:
    """Whitespace/'/-' cleanup plus repair of glued tokens seen in NSE subjects ('Rs10tors2' -> 'Rs 10 to rs 2')."""
    s = " ".join(str(subject).replace("/-", " ").split())
    s = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", s)        # split digit/letter junctions
    s = re.sub(r"(?i)\bto(rs|re|inr)\b", r"to \1", s)                       # 'tors' / 'tore' -> 'to rs' / 'to re'
    return s


def _parse_bonus(subj: str) -> Optional[Dict[str, Any]]:
    kw = BONUS_KEYWORD.search(subj)
    if not kw or NON_EQUITY_BONUS.search(subj):
        return None
    # the ratio that follows the bonus keyword (so 'Bonus - 1:5/Rights - 1:2' reads 1:5, never the rights ratio)
    m = BONUS_PATTERN.search(subj[kw.end():]) or BONUS_PATTERN.search(subj)
    if not m:
        return None
    new_shares, held_shares = float(m.group(1)), float(m.group(2))
    if new_shares <= 0 or held_shares <= 0:
        return None
    return {"action_type": "bonus", "ratio": round(1.0 + new_shares / held_shares, 6),
            "details": f"Bonus {int(new_shares)}:{int(held_shares)}"}


def _parse_split(subj: str) -> Optional[Dict[str, Any]]:
    if not SPLIT_KEYWORD.search(subj):
        return None
    m = SPLIT_PATTERN.search(subj)
    if not m:
        return None
    from_fv, to_fv = float(m.group(1)), float(m.group(2))
    if from_fv <= 0 or to_fv <= 0 or abs(from_fv - to_fv) <= 1e-4:
        return None
    ratio = round(from_fv / to_fv, 6)
    return {"action_type": "split" if ratio > 1.0 else "consolidation", "ratio": ratio,
            "details": f"FV {from_fv:g} to {to_fv:g}"}


def parse_action_subject(subject: str) -> Optional[Dict[str, Any]]:
    """
    Parses an NSE announcement subject to detect equity splits, consolidations, bonuses, or a combination.
    Returns {'action_type', 'ratio' (share multiplier), 'details'} or None.

    A subject that announces BOTH a bonus and a split ('Fv Spl-Rs5 To Rs2/Bon 1:1') is returned as one
    'split_bonus' action whose ratio is the PRODUCT of the two multipliers. If one part cannot be parsed the whole
    subject returns None (never apply half an action); `needs_review()` then flags it for a human.

    Examples:
      - 'Face Value Split (Sub-Division) - From Rs 10/- To Re 1/-' -> split, 10.0
      - 'Bonus 1:1' -> bonus, 2.0          - 'Bonus 1:2' -> bonus, 1.5
      - 'Consolidation - From Re 1/- To Rs 10/-' -> consolidation, 0.1
      - 'Fv Spl-Rs5 To Rs2/Bon 1:1' -> split_bonus, 5.0
      - 'Scheme Of Arrangement - Bonus Ncrps 4:1' -> None (non-equity)
    """
    if not subject:
        return None
    subj = _normalise(subject)
    bonus, split = _parse_bonus(subj), _parse_split(subj)
    has_bonus_kw = bool(BONUS_KEYWORD.search(subj)) and not NON_EQUITY_BONUS.search(subj)
    has_split_kw = bool(REVIEW_SPLIT_KEYWORD.search(subj))

    if bonus and split:
        return {"action_type": "split_bonus", "ratio": round(bonus["ratio"] * split["ratio"], 6),
                "details": f"{split['details']} + {bonus['details']}"}
    if bonus and has_split_kw:      # split announced but unparseable: do not apply the bonus alone
        return None
    if split and has_bonus_kw:      # bonus announced but unparseable: do not apply the split alone
        return None
    return bonus or split


def needs_review(subject: str) -> bool:
    """True when the subject looks like an equity bonus/split/consolidation that the parser could NOT turn into a ratio."""
    if not subject:
        return False
    subj = _normalise(subject)
    relevant = (bool(BONUS_KEYWORD.search(subj)) and not NON_EQUITY_BONUS.search(subj)) or bool(REVIEW_SPLIT_KEYWORD.search(subj))
    return relevant and parse_action_subject(subject) is None


class NSECorporateActionClient:
    """Session-managed HTTP client for the official NSE corporate actions endpoint."""

    LANDING_URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
    API_URL = "https://www.nseindia.com/api/corporates-corporateActions"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._handshake_done = False

    def _ensure_handshake(self) -> bool:
        """Initializes NSE session cookies via landing page GET."""
        if self._handshake_done:
            return True
        try:
            resp = self.session.get(self.LANDING_URL, timeout=self.timeout)
            if resp.status_code == 200 and len(self.session.cookies) > 0:
                self._handshake_done = True
                return True
            logger.warning("NSE landing page returned status %s with %d cookies", resp.status_code, len(self.session.cookies))
        except Exception as e:
            logger.warning("NSE handshake failed: %s", e)
        return False

    def fetch_actions(self, from_date: date, to_date: date) -> List[Dict[str, Any]]:
        """
        Queries official NSE Corporate Actions endpoint for the given date range.
        Returns raw JSON records list, or empty list on failure.
        """
        if not self._ensure_handshake():
            # Retry handshake once
            self.session.cookies.clear()
            if not self._ensure_handshake():
                logger.error("Unable to establish session with NSE; corporate actions fetch skipped.")
                return []

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.LANDING_URL,
        }
        params = {
            "index": "equities",
            "from_date": from_date.strftime("%d-%m-%Y"),
            "to_date": to_date.strftime("%d-%m-%Y"),
        }

        try:
            resp = self.session.get(self.API_URL, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict) and "data" in data:
                        return data["data"]
                except Exception as json_err:
                    logger.error("Failed to parse NSE corporate actions JSON: %s", json_err)
            else:
                logger.warning("NSE corporate actions API returned status %s: %s", resp.status_code, resp.text[:150])
        except Exception as e:
            logger.error("HTTP error fetching NSE corporate actions: %s", e)

        return []


def sync_corporate_actions(
    db: Any,
    days_back: int = 7,
    days_ahead: int = 30,
    client: Optional[NSECorporateActionClient] = None
) -> Dict[str, int]:
    """
    Synchronizes official NSE corporate actions into MongoDB collection `corporate_actions`.
    Ensures idempotency via unique compound index on (symbol, ex_date, action_type).

    Returns summary stats dict: {"fetched", "parsed", "upserted", "unparsed"} (unparsed = relevant subjects needing manual entry).
    """
    coll = db.get_collection("corporate_actions")
    # Ensure unique index
    try:
        coll.create_index([("symbol", 1), ("ex_date", 1), ("action_type", 1)], unique=True)
    except Exception as idx_err:
        logger.warning("Index creation on corporate_actions: %s", idx_err)

    today = get_ist_today()
    start_dt = today - timedelta(days=days_back)
    end_dt = today + timedelta(days=days_ahead)

    client = client or NSECorporateActionClient()
    records = client.fetch_actions(start_dt, end_dt)

    stats = {"fetched": len(records), "parsed": 0, "upserted": 0, "unparsed": 0}
    if not records:
        return stats

    for r in records:
        if not isinstance(r, dict):
            continue
        series = str(r.get("series", "")).strip().upper()
        if series and series not in SERIES_OK:
            continue

        raw_sym = str(r.get("symbol", "")).strip().upper()
        raw_subj = str(r.get("subject", "")).strip()
        raw_ex = str(r.get("exDate", "")).strip()

        if not raw_sym or not raw_subj or not raw_ex:
            continue

        # Parse date (format e.g. '21-Aug-2026' or '2026-08-21')
        ex_date_iso = None
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                ex_date_iso = datetime.strptime(raw_ex, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

        if not ex_date_iso:
            continue

        parsed = parse_action_subject(raw_subj)
        if not parsed:
            if needs_review(raw_subj):
                stats["unparsed"] += 1
                logger.error("[CORP_ACTION_UNPARSED] %s ex %s: cannot derive a share ratio from %r. Record it by hand "
                             "(scripts/sync_corporate_actions.py --add) if the stock is held.", raw_sym, ex_date_iso, raw_subj)
            continue

        stats["parsed"] += 1
        action_doc = {
            "symbol": raw_sym,
            "ex_date": ex_date_iso,
            "action_type": parsed["action_type"],
            "ratio": float(parsed["ratio"]),
            "description": raw_subj,
            "details": parsed.get("details", ""),
            "source": "NSE_OFFICIAL",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Idempotent upsert
        filter_query = {
            "symbol": raw_sym,
            "ex_date": ex_date_iso,
            "action_type": parsed["action_type"],
        }
        res = coll.update_one(filter_query, {"$set": action_doc}, upsert=True)
        if res.upserted_id or res.modified_count > 0:
            stats["upserted"] += 1
            logger.info(
                "[CORP_ACTION_SYNC] %s ex %s: %s (ratio %.4f)",
                raw_sym, ex_date_iso, parsed["action_type"], parsed["ratio"]
            )

    return stats
