"""
tests/test_corporate_actions.py -- Tests for Autonomous Corporate Actions Parser & Sync
"""

import copy
import math
from datetime import date
from unittest.mock import MagicMock
import pandas as pd
import pytest

from nse_quant_lab.corporate_actions import (
    parse_action_subject,
    sync_corporate_actions,
    NSECorporateActionClient,
)
from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.registry import get_strategy


class MockCollection:
    def __init__(self):
        self.docs = []

    def create_index(self, keys, **kwargs):
        pass

    def find(self, query=None):
        query = query or {}
        res = []
        for d in self.docs:
            match = True
            for k, v in query.items():
                if isinstance(v, dict) and "$in" in v:
                    if d.get(k) not in v["$in"]:
                        match = False
                        break
                elif d.get(k) != v:
                    match = False
                    break
            if match:
                res.append(copy.deepcopy(d))
        return res

    def find_one(self, query=None):
        res = self.find(query)
        return res[0] if res else None

    def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))
        return type("Result", (), {"inserted_id": "mock_id"})()

    def update_one(self, query, update, upsert=False):
        set_vals = update.get("$set", {})
        for i, d in enumerate(self.docs):
            match = True
            for k, v in query.items():
                if d.get(k) != v:
                    match = False
                    break
            if match:
                self.docs[i].update(copy.deepcopy(set_vals))
                return type("Result", (), {"upserted_id": None, "modified_count": 1})()
        if upsert:
            new_doc = copy.deepcopy(query)
            new_doc.update(copy.deepcopy(set_vals))
            self.docs.append(new_doc)
            return type("Result", (), {"upserted_id": "new_id", "modified_count": 0})()
        return type("Result", (), {"upserted_id": None, "modified_count": 0})()


class MockDatabase:
    def __init__(self):
        self.collections = {}

    def get_collection(self, name):
        if name not in self.collections:
            self.collections[name] = MockCollection()
        return self.collections[name]

    def __getattr__(self, name):
        return self.get_collection(name)


def test_parse_split_various_formats():
    # 10 to 1 split
    res1 = parse_action_subject("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share")
    assert res1 is not None
    assert res1["action_type"] == "split"
    assert res1["ratio"] == 10.0

    # 2 to 1 split
    res2 = parse_action_subject("Face Value Split (Sub-Division) - From Rs 2/- Per Share To Re 1/- Per Share")
    assert res2 is not None
    assert res2["action_type"] == "split"
    assert res2["ratio"] == 2.0

    # 10 to 2 split
    res3 = parse_action_subject("Sub-Division of Shares From Rs 10 to Rs 2")
    assert res3 is not None
    assert res3["action_type"] == "split"
    assert res3["ratio"] == 5.0

    # Consolidation 1 to 10 (reverse split)
    res4 = parse_action_subject("Consolidation - From Re 1/- To Rs 10/-")
    assert res4 is not None
    assert res4["action_type"] == "consolidation"
    assert res4["ratio"] == pytest.approx(0.1)


def test_parse_bonus_various_formats():
    # 1:1 Bonus
    res1 = parse_action_subject("Bonus 1:1")
    assert res1 is not None
    assert res1["action_type"] == "bonus"
    assert res1["ratio"] == 2.0

    # 2:1 Bonus
    res2 = parse_action_subject("Bonus 2:1")
    assert res2 is not None
    assert res2["action_type"] == "bonus"
    assert res2["ratio"] == 3.0

    # 1:2 Bonus
    res3 = parse_action_subject("Bonus 1:2")
    assert res3 is not None
    assert res3["action_type"] == "bonus"
    assert res3["ratio"] == 1.5

    # 3:2 Bonus
    res4 = parse_action_subject("Bonus 3:2")
    assert res4 is not None
    assert res4["action_type"] == "bonus"
    assert res4["ratio"] == 2.5


def test_ignore_non_equity_and_irrelevant_actions():
    # NCRPS non-equity bonus
    assert parse_action_subject("Scheme Of Arrangement - Bonus Ncrps 4:1") is None
    # Debentures
    assert parse_action_subject("Bonus Debentures 6:1") is None
    # Preference shares
    assert parse_action_subject("Bonus Redeemable Preference Shares 1:1") is None
    # Bare cash dividend
    assert parse_action_subject("Interim Dividend - Rs 8 Per Share") is None
    # Empty string
    assert parse_action_subject("") is None


def test_sync_corporate_actions_idempotency_and_schema():
    mock_db = MockDatabase()

    raw_records = [
        {
            "symbol": "TEMBO",
            "series": "EQ",
            "exDate": "05-Aug-2026",
            "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
        },
        {
            "symbol": "GOODLUCK",
            "series": "EQ",
            "exDate": "21-Aug-2026",
            "subject": "Bonus 2:1",
        },
        {
            "symbol": "TCS",
            "series": "EQ",
            "exDate": "15-Aug-2026",
            "subject": "Interim Dividend - Rs 10 Per Share",
        },
    ]

    mock_client = MagicMock(spec=NSECorporateActionClient)
    mock_client.fetch_actions.return_value = raw_records

    # First sync
    stats1 = sync_corporate_actions(mock_db, days_back=7, days_ahead=30, client=mock_client)
    assert stats1["fetched"] == 3
    assert stats1["parsed"] == 2
    assert stats1["upserted"] == 2

    actions = mock_db.get_collection("corporate_actions").find()
    assert len(actions) == 2

    tembo = mock_db.get_collection("corporate_actions").find_one({"symbol": "TEMBO"})
    assert tembo["action_type"] == "split"
    assert tembo["ratio"] == 10.0
    assert tembo["ex_date"] == "2026-08-05"
    assert tembo["source"] == "NSE_OFFICIAL"

    goodluck = mock_db.get_collection("corporate_actions").find_one({"symbol": "GOODLUCK"})
    assert goodluck["action_type"] == "bonus"
    assert goodluck["ratio"] == 3.0
    assert goodluck["ex_date"] == "2026-08-21"

    # Second sync (same records) -> idempotent
    stats2 = sync_corporate_actions(mock_db, days_back=7, days_ahead=30, client=mock_client)
    assert len(mock_db.get_collection("corporate_actions").find()) == 2


def test_end_to_end_paper_manager_applies_synced_split():
    mock_db = MockDatabase()

    cfg = PaperTradingConfig(strategy_id="alpha_semivol_pit", initial_capital=100000.0)
    strat = get_strategy("alpha_semivol_pit", cfg)

    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)

    # 1. Establish open position in TCS (100 shares at Rs 1000)
    mock_db.positions.insert_one({
        "strategy_id": "alpha_semivol_pit",
        "mode": "paper",
        "symbol": "TCS",
        "shares": 100,
        "entry_price": 1000.0,
        "entry_fill_price": 1000.0,
        "entry_date": "2026-11-01",
        "applied_actions": [],
    })

    # 2. Insert 1:2 split (ratio 2.0) with ex_date 2026-11-10
    mock_db.corporate_actions.insert_one({
        "symbol": "TCS",
        "ex_date": "2026-11-10",
        "action_type": "split",
        "ratio": 2.0,
        "source": "NSE_OFFICIAL",
    })

    # 3. Simulate post-market run on ex-date 2026-11-10
    open_positions = {"TCS": mock_db.positions.find_one({"symbol": "TCS"})}
    mgr._apply_corporate_actions(open_positions, as_of_date="2026-11-10")

    # 4. Verify position was adjusted
    pos = mock_db.positions.find_one({"symbol": "TCS"})
    assert pos["shares"] == 200
    assert pos["entry_price"] == pytest.approx(500.0)
    assert pos["entry_fill_price"] == pytest.approx(500.0)
    assert pos["applied_actions"] == ["TCS:2026-11-10:2.0"]

    # 5. Re-running on same date or later date does not apply again (idempotent)
    mgr._apply_corporate_actions(open_positions, as_of_date="2026-11-11")
    pos_after = mock_db.positions.find_one({"symbol": "TCS"})
    assert pos_after["shares"] == 200
    assert len(pos_after["applied_actions"]) == 1


# ---------------------------------------------------------------- combined / partial announcements (parser audit fix)
from nse_quant_lab.corporate_actions import needs_review


def test_combined_bonus_and_split_applies_the_product_not_half():
    r = parse_action_subject("Fv Spl-Rs5 To Rs2/Bon 1:1")
    assert r["action_type"] == "split_bonus"
    assert r["ratio"] == pytest.approx(2.5 * 2.0)          # split 5->2 (x2.5) AND bonus 1:1 (x2)

    r2 = parse_action_subject("Bonus 1:1 / Face Value Split From Rs 10 To Rs 5")
    assert r2["ratio"] == pytest.approx(4.0)


def test_glued_tokens_from_nse_subjects_are_repaired():
    assert parse_action_subject("Fv Spl-Rs10tors2/Bon-1:1")["ratio"] == pytest.approx(10.0)
    assert parse_action_subject("Bon 1:1/Fv Spl Rs.5tors.2")["ratio"] == pytest.approx(5.0)


def test_half_parseable_combined_subject_is_never_applied_and_is_flagged():
    subj = "Agm/Spl/Bon-1:1/Div-20%"            # says split AND bonus, split amounts missing
    assert parse_action_subject(subj) is None
    assert needs_review(subj) is True


def test_bonus_ratio_ignores_rights_ratio_and_non_events_are_not_flagged():
    assert parse_action_subject("Bonus - 1:5/Rights - 1:2")["ratio"] == pytest.approx(1.2)
    for s in ("Interim Dividend - Rs 8 Per Share", "Scheme Of Arrangement - Bonus Ncrps 4:1", "Rights 19:25 @ Premium Rs 10/-"):
        assert parse_action_subject(s) is None and needs_review(s) is False


def test_sync_reports_unparsed_relevant_announcements_loudly(caplog):
    import logging
    mock_db = MockDatabase()
    client = MagicMock(spec=NSECorporateActionClient)
    client.fetch_actions.return_value = [
        {"symbol": "SUNTV", "series": "EQ", "exDate": "05-Aug-2026", "subject": "Agm/Spl/Bon-1:1/Div-20%"},
        {"symbol": "OKCO", "series": "EQ", "exDate": "06-Aug-2026", "subject": "Fv Spl-Rs5 To Rs2/Bon 1:1"},
    ]
    with caplog.at_level(logging.ERROR, logger="nse_quant_lab.corporate_actions"):
        stats = sync_corporate_actions(mock_db, client=client)
    assert stats["unparsed"] == 1 and stats["parsed"] == 1
    assert any("CORP_ACTION_UNPARSED" in r.message and "SUNTV" in r.message for r in caplog.records)
    assert mock_db.get_collection("corporate_actions").find_one({"symbol": "SUNTV"}) is None, "half action must not be stored"
    assert mock_db.get_collection("corporate_actions").find_one({"symbol": "OKCO"})["ratio"] == pytest.approx(5.0)


def test_parser_matches_historical_dataset_factors_when_available():
    """Consistency check against external/nse_archive/all_corporate_actions.parquet (itself of unverified provenance):
    every subject the parser accepts must reproduce the dataset's split*bonus price factor."""
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "external" / "nse_archive" / "all_corporate_actions.parquet"
    if not p.exists():
        pytest.skip("historical corporate actions parquet not present")
    df = pd.read_parquet(p)
    sb = df[df["kind"].isin(["split", "bonus"])]
    grp = sb.groupby(["symbol", "ex_date", "subject"])["price_factor"].prod().reset_index()
    bad = []
    for _, r in grp.iterrows():
        parsed = parse_action_subject(r["subject"])
        if parsed and abs(1.0 / parsed["ratio"] - r["price_factor"]) > 1e-3:
            bad.append((r["symbol"], str(r["ex_date"])[:10], r["subject"], r["price_factor"], round(1 / parsed["ratio"], 6)))
    assert not bad, f"parser disagrees with dataset on {len(bad)} events, e.g. {bad[:3]}"
