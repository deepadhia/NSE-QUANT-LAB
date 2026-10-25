"""
send_periodic_report.py -- Weekly and Monthly Automated Telegram Reporting
========================================================================
Usage:
  python scripts/send_periodic_report.py --period weekly --strategy trend_momentum
  python scripts/send_periodic_report.py --period monthly --strategy trend_momentum
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timedelta
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.db.mongo_client import MongoDatabase
from nse_quant_lab.notifications.telegram import TelegramNotifier
from nse_quant_lab.holidays import is_trading_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("periodic_report")


def generate_weekly_report(db: MongoDatabase, strategy_id: str, mode: str = "paper"):
    notifier = TelegramNotifier()
    # 1. Fetch NAV history for strategy
    nav_cursor = db.daily_nav.find({"strategy_id": strategy_id, "mode": mode}).sort("date", 1)
    nav_docs = list(nav_cursor)
    if not nav_docs:
        logger.warning(f"No NAV records found in DB for {strategy_id}.")
        return False

    latest_doc = nav_docs[-1]
    end_date = latest_doc["date"]
    end_nav = float(latest_doc["nav"])
    cash = float(latest_doc["cash"])
    holdings_val = float(latest_doc.get("holdings_val", 0.0))

    # Determine start of week (up to 5 trading records back)
    start_doc = nav_docs[-min(5, len(nav_docs))]
    start_date = start_doc["date"]
    start_nav = float(start_doc["nav"])

    # 2. Fetch trades closed this week
    dt_end = pd.to_datetime(end_date)
    dt_start = dt_end - pd.Timedelta(days=7)
    trades_cursor = db.trades.find({
        "strategy_id": strategy_id,
        "mode": mode,
        "exit_date": {"$gte": dt_start.strftime("%Y-%m-%d"), "$lte": end_date}
    })
    closed_trades = list(trades_cursor)

    # 3. Fetch active open positions
    pos_cursor = db.positions.find({"strategy_id": strategy_id, "mode": mode})
    open_positions = list(pos_cursor)

    logger.info(f">> Sending Weekly Telegram Summary for {strategy_id} ({start_date} to {end_date})...")
    return notifier.send_weekly_summary(
        strategy_id=strategy_id,
        start_date=start_date,
        end_date=end_date,
        start_nav=start_nav,
        end_nav=end_nav,
        cash=cash,
        holdings_val=holdings_val,
        open_positions=open_positions,
        closed_trades=closed_trades,
    )


def generate_monthly_report(db: MongoDatabase, strategy_id: str, mode: str = "paper"):
    notifier = TelegramNotifier()
    strat_doc = db.strategies.find_one({"strategy_id": strategy_id, "mode": mode})
    initial_cap = float(strat_doc.get("initial_capital", 1_000_000.0)) if strat_doc else 1_000_000.0

    nav_cursor = db.daily_nav.find({"strategy_id": strategy_id, "mode": mode}).sort("date", 1)
    nav_docs = list(nav_cursor)
    if not nav_docs:
        logger.warning(f"No NAV records found in DB for {strategy_id}.")
        return False

    latest_doc = nav_docs[-1]
    latest_dt = pd.to_datetime(latest_doc["date"])
    curr_month_str = latest_dt.strftime("%Y-%m")
    month_name = latest_dt.strftime("%B %Y")

    month_docs = [d for d in nav_docs if str(d["date"]).startswith(curr_month_str)]
    start_doc = month_docs[0] if month_docs else latest_doc
    start_nav = float(start_doc["nav"])
    end_nav = float(latest_doc["nav"])
    cash = float(latest_doc["cash"])
    holdings_val = float(latest_doc.get("holdings_val", 0.0))

    # YTD calculation (records from Jan 1 of current year)
    curr_year_str = latest_dt.strftime("%Y")
    year_docs = [d for d in nav_docs if str(d["date"]).startswith(curr_year_str)]
    ytd_start_nav = float(year_docs[0]["nav"]) if year_docs else initial_cap
    ytd_ret_pct = ((end_nav - ytd_start_nav) / ytd_start_nav * 100.0) if ytd_start_nav > 0 else 0.0

    # Monthly closed trades
    trades_cursor = db.trades.find({
        "strategy_id": strategy_id,
        "mode": mode,
        "exit_date": {"$regex": f"^{curr_month_str}"}
    })
    closed_trades = list(trades_cursor)

    logger.info(f">> Sending Monthly Telegram Summary for {strategy_id} ({month_name})...")
    return notifier.send_monthly_summary(
        strategy_id=strategy_id,
        month_str=month_name,
        start_nav=start_nav,
        end_nav=end_nav,
        initial_capital=initial_cap,
        cash=cash,
        holdings_val=holdings_val,
        closed_trades=closed_trades,
        ytd_ret_pct=ytd_ret_pct,
    )


def main():
    parser = argparse.ArgumentParser(description="Send Periodic (Weekly/Monthly) Telegram Reports")
    parser.add_argument("--period", choices=["weekly", "monthly"], required=True, help="Report cadence")
    parser.add_argument("--strategy", default="all", help="Strategy ID or 'all'")
    parser.add_argument("--mode", default="paper", choices=["paper", "real"])
    args = parser.parse_args()

    cfg = PaperTradingConfig(strategy_id="system")
    db = MongoDatabase.get_instance(cfg.mongo_uri, cfg.db_name)
    if not db.ping():
        logger.error("Cannot connect to MongoDB. Skipping periodic report.")
        sys.exit(1)

    if args.strategy.lower() == "all":
        strats = list(db.strategies.find({"status": "ACTIVE", "mode": args.mode}))
        strat_ids = [s["strategy_id"] for s in strats] if strats else ["trend_momentum"]
    else:
        strat_ids = [args.strategy]

    for s_id in strat_ids:
        try:
            if args.period == "weekly":
                generate_weekly_report(db, s_id, args.mode)
            else:
                generate_monthly_report(db, s_id, args.mode)
        except Exception as e:
            logger.error(f"Error generating {args.period} report for {s_id}: {e}", exc_info=True)


if __name__ == "__main__":
    main()
