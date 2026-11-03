"""
verify_production_health.py -- Strict Pre-Flight Health Check for Deployment & Cron
===================================================================================
Fails LOUDLY with Exit Code 1 if any critical production invariant, data artifact,
or configuration is missing. Must be executed in CI and before starting daemon services.
"""

import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("production_health_check")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

load_dotenv(REPO_ROOT / ".env")


def check_production_health() -> bool:
    logger.info("=" * 70)
    logger.info("STARTING PRODUCTION PRE-FLIGHT HEALTH CHECK")
    logger.info("=" * 70)

    has_failures = False

    # 1. Check MongoDB Connectivity
    logger.info("--> [1/5] Checking MongoDB Atlas Connection...")
    try:
        from nse_quant_lab.db.mongo_client import MongoDatabase
        mongo_uri = os.getenv("MONGO_URI")
        if not mongo_uri:
            logger.error("❌ CRITICAL: MONGO_URI is not set in environment or .env!")
            has_failures = True
        else:
            db = MongoDatabase.get_instance(mongo_uri, "stock_scanner")
            if not db.ping():
                logger.error("❌ CRITICAL: Cannot ping MongoDB Atlas! Check IP whitelist or credentials.")
                has_failures = True
            else:
                logger.info("✅ MongoDB Atlas ping successful.")
    except Exception as e:
        logger.error(f"❌ CRITICAL: MongoDB connection error: {e}")
        has_failures = True

    # 2. Check Active Strategies and Required Production Datasets
    logger.info("--> [2/5] Checking Active Strategies & Data Artifacts...")
    try:
        active_strats = list(db.strategies.find({"status": "ACTIVE"}))
        active_ids = [s["strategy_id"] for s in active_strats]
        logger.info(f"   Active strategies found in DB: {active_ids}")

        pit_strategies = {"trend_momentum_pit", "minervini_trend_template", "alpha_semivol_pit"}
        active_pit = [s for s in active_ids if s in pit_strategies]

        if active_pit:
            logger.info(f"   Active PIT strategies in DB: {active_pit}")
            p1 = REPO_ROOT / "data" / "external" / "nse_archive" / "clean_panel.parquet"
            p2 = REPO_ROOT / "external" / "nse_archive" / "clean_panel.parquet"
            resolved_panel = p1 if p1.exists() else (p2 if p2.exists() else None)

            if resolved_panel:
                file_size_mb = resolved_panel.stat().st_size / (1024 * 1024)
                logger.info(f"✅ clean_panel.parquet found at: {resolved_panel} ({file_size_mb:.1f} MB)")
            else:
                # Approach A: Verify MongoDB live data store has sufficient recent candles
                mongo_bars_count = 0
                try:
                    mongo_bars_count = db.db["upstox_daily_ohlcv"].count_documents({"date": {"$gte": "2025-01-01"}})
                except Exception:
                    pass

                if mongo_bars_count >= 1000:
                    logger.info(
                        f"✅ MongoDB market data store active: {mongo_bars_count:,} recent candles found in 'upstox_daily_ohlcv'. "
                        f"(Running in MongoDB-backed mode, clean_panel.parquet not required on server)."
                    )
                else:
                    logger.error("=" * 70)
                    logger.error("❌ CRITICAL DATA ARTIFACT MISSING: clean_panel.parquet not found and MongoDB data is insufficient!")
                    logger.error(f"   Searched locations:")
                    logger.error(f"     1. {p1}")
                    logger.error(f"     2. {p2}")
                    logger.error(f"   MongoDB 'upstox_daily_ohlcv' recent bars: {mongo_bars_count} (< 1,000)")
                    logger.error("   Remedy: Either transfer clean_panel.parquet or ingest recent bars via Upstox.")
                    logger.error("=" * 70)
                    has_failures = True
    except Exception as e:
        logger.error(f"❌ Failed to verify strategy data artifacts: {e}")
        has_failures = True

    # 3. Check Upstox API Configuration
    logger.info("--> [3/5] Checking Broker API Configuration...")
    upstox_token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not upstox_token:
        logger.warning("⚠️  WARNING: UPSTOX_ACCESS_TOKEN is not set in .env. Live quotes will fall back.")
    else:
        logger.info("✅ Upstox access token detected.")

    # 4. Check Telegram Alerting Configuration
    logger.info("--> [4/5] Checking Telegram Alert Configuration...")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    if not tg_token or not tg_chat:
        logger.warning("⚠️  WARNING: Telegram bot credentials not fully set. Alerts will be logged to console only.")
    else:
        logger.info("✅ Telegram credentials configured.")

    # 5. Check Morning Execution Resilience (Dry-Run Check)
    logger.info("--> [5/5] Checking Pipeline Script Integrity...")
    try:
        from nse_quant_lab.registry import list_strategies
        strats = list_strategies()
        logger.info(f"✅ Registry contains {len(strats)} registered strategies: {strats}")
    except Exception as e:
        logger.error(f"❌ Strategy registry check failed: {e}")
        has_failures = True

    logger.info("=" * 70)
    if has_failures:
        logger.error("❌ PRODUCTION PRE-FLIGHT CHECK FAILED! Fix the issues above before running jobs.")
        logger.info("=" * 70)
        return False
    else:
        logger.info("✅ ALL PRODUCTION PRE-FLIGHT CHECKS PASSED CLEANLY.")
        logger.info("=" * 70)
        return True


if __name__ == "__main__":
    success = check_production_health()
    sys.exit(0 if success else 1)

