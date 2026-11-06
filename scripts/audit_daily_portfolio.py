"""
audit_daily_portfolio.py -- Daily Portfolio Health & Execution Slippage Audit
=============================================================================
Usage:
    python scripts/audit_daily_portfolio.py                 # Audit today's session (IST)
    python scripts/audit_daily_portfolio.py --date 2026-11-06
    python scripts/audit_daily_portfolio.py --backfill      # Backfill all dates from daily_nav
    python scripts/audit_daily_portfolio.py --slippage-only # Display slippage analytics only

Stores structured audit snapshots in MongoDB collection: 'daily_portfolio_audit'.
Zero Telegram noise: records telemetry strictly to database and stdout logs.
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
    sys.path.insert(0, PROJECT_ROOT)

from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.db.models import DailyPortfolioAuditModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("portfolio_audit")

IST = timezone(timedelta(hours=5, minutes=30))


def compute_slippage_metrics(fills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate average modeled slippage vs. live observed half-spreads from fill_audit records."""
    if not fills:
        return {
            "fill_count": 0,
            "avg_model_slip_bps": 0.0,
            "avg_observed_half_spread_bps": 0.0,
            "avg_quote_vs_open_bps": 0.0,
            "verdict": "NO_FILLS_RECORDED",
        }

    model_slips = [f["model_slip_bps"] for f in fills if f.get("model_slip_bps") is not None]
    half_spreads = [f["half_spread_bps"] for f in fills if f.get("half_spread_bps") is not None]
    quote_diffs = [f["quote_vs_open_bps"] for f in fills if f.get("quote_vs_open_bps") is not None]

    avg_model = round(sum(model_slips) / len(model_slips), 2) if model_slips else 0.0
    avg_spread = round(sum(half_spreads) / len(half_spreads), 2) if half_spreads else 0.0
    avg_quote = round(sum(quote_diffs) / len(quote_diffs), 2) if quote_diffs else 0.0

    if avg_spread > 0:
        verdict = "CONSERVATIVE" if avg_model >= avg_spread else "OPTIMISTIC_UNDERESTIMATED"
    else:
        verdict = "SPREAD_DATA_UNAVAILABLE"

    return {
        "fill_count": len(fills),
        "avg_model_slip_bps": avg_model,
        "avg_observed_half_spread_bps": avg_spread,
        "avg_quote_vs_open_bps": avg_quote,
        "verdict": verdict,
    }


def audit_strategy_for_date(db, strategy_id: str, date_str: str, mode: str = "paper") -> Optional[Dict[str, Any]]:
    """Generates and persists a DailyPortfolioAuditModel document for a specific strategy and date."""
    # 1. Fetch NAV record for this date
    nav_doc = db.daily_nav.find_one({"strategy_id": strategy_id, "mode": mode, "date": date_str})
    if not nav_doc:
        logger.warning(f"No daily_nav record found for {strategy_id} on {date_str}. Skipping.")
        return None

    nav = float(nav_doc.get("nav", 0.0))
    cash = float(nav_doc.get("cash", 0.0))
    holdings_val = float(nav_doc.get("holdings_val", 0.0))

    # 2. Fetch open positions
    pos_cursor = db.positions.find({"strategy_id": strategy_id, "mode": mode})
    positions_list = []
    total_unrealized_pnl = 0.0

    for p in pos_cursor:
        shares = int(p.get("shares", 0))
        entry_px = float(p.get("entry_price", 0.0))
        last_close = float(p.get("last_close", entry_px))
        market_val = float(p.get("market_value", shares * last_close))
        unrealized = float(p.get("unrealized_pnl", market_val - (shares * entry_px)))
        total_unrealized_pnl += unrealized

        pnl_pct = round((last_close / entry_px - 1.0) * 100, 2) if entry_px > 0 else 0.0
        stop_loss_px = p.get("stop_loss_price")
        stop_dist_pct = None
        if stop_loss_px and last_close > 0:
            stop_dist_pct = round((last_close - float(stop_loss_px)) / last_close * 100, 2)

        positions_list.append({
            "symbol": p.get("symbol"),
            "shares": shares,
            "entry_price": entry_px,
            "last_close": last_close,
            "market_value": round(market_val, 2),
            "unrealized_pnl": round(unrealized, 2),
            "pnl_pct": pnl_pct,
            "stop_loss_price": stop_loss_px,
            "stop_distance_pct": stop_dist_pct,
            "days_held": p.get("days_held", 0),
        })

    # Sort positions by P&L % descending
    positions_list.sort(key=lambda x: x["pnl_pct"], reverse=True)
    unrealized_pnl_pct = round((total_unrealized_pnl / nav) * 100, 2) if nav > 0 else 0.0

    # 3. Gather Fills executed on this date
    fill_docs = list(db.fill_audit.find({"strategy_id": strategy_id, "mode": mode, "date": date_str}))
    daily_fills = []
    for f in fill_docs:
        daily_fills.append({
            "symbol": f.get("symbol"),
            "side": f.get("side"),
            "shares": f.get("shares"),
            "open_price": f.get("open_price"),
            "fill_price": f.get("fill_price"),
            "model_slip_bps": f.get("model_slip_bps"),
            "half_spread_bps": f.get("half_spread_bps"),
            "quote_vs_open_bps": f.get("quote_vs_open_bps"),
        })

    day_slip_metrics = compute_slippage_metrics(fill_docs)

    # 4. Gather Cumulative Slippage metrics across all historical fills to date
    all_strat_fills = list(db.fill_audit.find({
        "strategy_id": strategy_id,
        "mode": mode,
        "date": {"$lte": date_str}
    }))
    cum_slip_metrics = compute_slippage_metrics(all_strat_fills)

    slippage_summary = {
        "session": day_slip_metrics,
        "cumulative": cum_slip_metrics,
    }

    # 5. Build Audit Document
    audit_model = DailyPortfolioAuditModel(
        strategy_id=strategy_id,
        mode=mode,
        date=date_str,
        nav=round(nav, 2),
        cash=round(cash, 2),
        holdings_val=round(holdings_val, 2),
        open_positions_count=len(positions_list),
        unrealized_pnl=round(total_unrealized_pnl, 2),
        unrealized_pnl_pct=unrealized_pnl_pct,
        positions=positions_list,
        daily_fills=daily_fills,
        slippage_summary=slippage_summary,
    )

    # Upsert into daily_portfolio_audit
    doc_dict = audit_model.to_dict()
    db.daily_portfolio_audit.replace_one(
        {"strategy_id": strategy_id, "mode": mode, "date": date_str},
        doc_dict,
        upsert=True
    )
    return doc_dict


def run_portfolio_audit(target_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """Runs the audit for all active strategies for target_date (or today IST)."""
    db = MongoDatabase.get_instance().db

    # Ensure index exists
    db.daily_portfolio_audit.create_index(
        [("strategy_id", 1), ("mode", 1), ("date", -1)],
        unique=True
    )

    if not target_date:
        target_date = datetime.now(IST).strftime("%Y-%m-%d")

    strat_docs = list(db.strategies.find({"status": "ACTIVE"}))
    if not strat_docs:
        strat_docs = [{"strategy_id": "trend_momentum_pit", "mode": "paper"}]

    audits = []
    print("\n" + "=" * 80)
    print(f" DAILY PORTFOLIO & EXECUTION SLIPPAGE AUDIT: {target_date} ")
    print("=" * 80)

    for s in strat_docs:
        sid = s["strategy_id"]
        mode = s.get("mode", "paper")
        res = audit_strategy_for_date(db, sid, target_date, mode)
        if res:
            audits.append(res)
            print_strategy_summary(res)

    print_global_slippage_verdict(db)
    print("=" * 80 + "\n")
    return audits


def print_strategy_summary(audit: Dict[str, Any]):
    """Pretty prints an audit document to stdout."""
    sid = audit["strategy_id"].upper()
    nav = audit["nav"]
    cash = audit["cash"]
    holdings = audit["holdings_val"]
    pnl = audit["unrealized_pnl"]
    pnl_pct = audit["unrealized_pnl_pct"]
    pos_count = audit["open_positions_count"]
    fills = audit["daily_fills"]
    slip_day = audit["slippage_summary"]["session"]
    slip_cum = audit["slippage_summary"]["cumulative"]

    print(f"\n>> STRATEGY: {sid} (NAV: INR {nav:,.2f} | Cash: INR {cash:,.2f} | Unrealized P&L: INR {pnl:,.2f} ({pnl_pct:+.2f}%))")
    print(f"   Holdings: {pos_count} stocks (Val: INR {holdings:,.2f}) | Fills Today: {len(fills)}")

    if fills:
        print(f"   [TODAY'S FILLS] ({slip_day['fill_count']} fills):")
        print(f"      Avg Modeled Slippage:    {slip_day['avg_model_slip_bps']:.2f} bps")
        print(f"      Avg Observed Half-Spread:{slip_day['avg_observed_half_spread_bps']:.2f} bps")
        print(f"      Realism Verdict:         {slip_day['verdict']}")
    else:
        print("   [TODAY'S FILLS] None (Session was holding / no trades executed)")

    print(f"   [CUMULATIVE SLIPPAGE] ({slip_cum['fill_count']} total fills to date):")
    print(f"      Cumulative Modeled Slippage:    {slip_cum['avg_model_slip_bps']:.2f} bps")
    print(f"      Cumulative Observed Half-Spread:{slip_cum['avg_observed_half_spread_bps']:.2f} bps")
    print(f"      Accounting Status:              {slip_cum['verdict']}")

    # Display Top 3 Gainers & Losers
    positions = audit.get("positions", [])
    if positions:
        print("   [TOP GAINERS / MOVERS]:")
        for p in positions[:3]:
            sl_info = f" | Stop Dist: {p['stop_distance_pct']:.1f}%" if p.get('stop_distance_pct') is not None else ""
            print(f"      * {p['symbol']:<12} {p['pnl_pct']:+6.2f}% (Close: INR {p['last_close']:,.2f} | Entry: INR {p['entry_price']:,.2f}{sl_info})")

        print("   [MOST DEFENSIVE / NEARING STOP]:")
        for p in positions[-3:]:
            sl_info = f" | Stop Dist: {p['stop_distance_pct']:.1f}%" if p.get('stop_distance_pct') is not None else ""
            print(f"      * {p['symbol']:<12} {p['pnl_pct']:+6.2f}% (Close: INR {p['last_close']:,.2f} | Entry: INR {p['entry_price']:,.2f}{sl_info})")


def print_global_slippage_verdict(db):
    """Summarizes slippage realism across all strategies combined."""
    all_fills = list(db.fill_audit.find())
    if not all_fills:
        return

    metrics = compute_slippage_metrics(all_fills)
    print("\n" + "-" * 80)
    print(" ALL-STRATEGY COMBINED SLIPPAGE AUDIT")
    print("-" * 80)
    print(f" Total Audited Fills in DB:     {metrics['fill_count']}")
    print(f" Model Slippage Applied (Avg):   {metrics['avg_model_slip_bps']:.2f} bps")
    print(f" Live L1 Half-Spread Observed:   {metrics['avg_observed_half_spread_bps']:.2f} bps")
    print(f" Live Ask vs. Open Price Diff:   {metrics['avg_quote_vs_open_bps']:+.2f} bps")
    print(f" Quantitative Conclusion:        {metrics['verdict']}")
    if metrics["avg_model_slip_bps"] >= metrics["avg_observed_half_spread_bps"]:
        print(" >> Model is CONSERVATIVE: Backtest & paper deduct MORE friction than live market imposes.")
    else:
        print(" >> WARNING: Model is OPTIMISTIC: Real market spreads exceed modeled deductions.")


def main():
    parser = argparse.ArgumentParser(description="NSE Quant Lab Daily Portfolio & Slippage Audit")
    parser.add_argument("--date", default=None, help="Specific date to audit (YYYY-MM-DD)")
    parser.add_argument("--backfill", action="store_true", help="Backfill audit snapshots for all historical dates in daily_nav")
    parser.add_argument("--slippage-only", action="store_true", help="Display only global slippage analysis")
    args = parser.parse_args()

    db = MongoDatabase.get_instance().db

    if args.slippage_only:
        print_global_slippage_verdict(db)
        return

    if args.backfill:
        dates = sorted(list(db.daily_nav.distinct("date")))
        logger.info(f"Backfilling daily portfolio audits for dates: {dates}")
        for d in dates:
            run_portfolio_audit(d)
        return

    run_portfolio_audit(args.date)


if __name__ == "__main__":
    main()

