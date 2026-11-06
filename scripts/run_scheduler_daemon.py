"""
NSE Quant Lab - Production Background Scheduler Daemon
Runs 24/7 as a systemd service on Oracle Cloud VM.
Dispatches scheduled jobs at exact IST market times with automated holiday checks.
Adheres to Zero-Spam Policy: completely silent on holidays, weekends, and routine operations.
"""

import os
import sys
import time
import html
import signal
import logging
import argparse
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from dotenv import load_dotenv

# Ensure environment variables loaded from .env
load_dotenv()

# Ensure project root in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
    sys.path.insert(0, PROJECT_ROOT)

from nse_quant_lab.holidays import is_trading_day
from nse_quant_lab.notifications.telegram import TelegramNotifier

# Setup logging
os.makedirs(os.path.join(PROJECT_ROOT, "logs"), exist_ok=True)
log_file = os.path.join(PROJECT_ROOT, "logs", "scheduler.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("scheduler")

IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    """Return current datetime in Indian Standard Time (IST)."""
    return datetime.now(IST)

class QuantLabScheduler:
    """
    Autonomous Scheduler Daemon for NSE Quant Lab.
    Handles exact-time job dispatching, holiday avoidance, and failure notifications.
    Zero-Spam: Operates silently. Alerts sent ONLY for actual trade fills, trade signals, or critical failures.
    """

    def __init__(self):
        self.running = True
        self.completed_today = set()      # job finished OR was given up on / skipped for today
        self.succeeded_today = set()      # job finished with exit code 0 (the ONLY set safety guards may trust)
        self.retry_counts = {}
        self.next_retry_time = {}
        self.current_date = get_ist_now().strftime("%Y-%m-%d")
        self.notifier = TelegramNotifier()
        self._load_state(self.current_date)
        
        # Define schedule (IST times) - Actionable trading jobs and periodic digests
        self.jobs = [
            {
                "id": "morning_execution",
                "time": "09:16",
                "max_time": "15:25",
                "description": "Morning Market Open Order Execution",
                "script": os.path.join(PROJECT_ROOT, "scripts", "execute_paper_orders.py"),
                "args": ["--strategy", "all"]
            },
            {
                "id": "fulltier_ingestion",
                "time": "17:45",
                "description": "EOD Upstox Full-Tier Candle Ingestion",
                "script": os.path.join(PROJECT_ROOT, "scripts", "ingest_upstox_fulltier.py"),
                "args": []
            },
            {
                "id": "benchmark_ingestion",
                "time": "17:55",
                "description": "Daily Benchmark Ingestion (^NSEI and ^CRSLDX)",
                "script": os.path.join(PROJECT_ROOT, "scripts", "ingest_benchmarks.py"),
                "args": []
            },
            {
                "id": "daily_pipeline",
                "time": "18:00",
                "description": "Daily Strategy Signal Generation and Portfolio Rebalance",
                "script": os.path.join(PROJECT_ROOT, "scripts", "run_daily_pipeline.py"),
                "args": ["--strategy", "all"]
            },
            {
                "id": "daily_portfolio_audit",
                "time": "18:05",
                "description": "Daily Portfolio & Slippage Audit Snapshot (MongoDB)",
                "script": os.path.join(PROJECT_ROOT, "scripts", "audit_daily_portfolio.py"),
                "args": []
            },
            {
                "id": "weekly_report",
                "time": "18:15",
                "description": "Weekly Portfolio Performance Digest (Fridays Only)",
                "script": os.path.join(PROJECT_ROOT, "scripts", "send_periodic_report.py"),
                "args": ["--period", "weekly"],
                "day_condition": "FRIDAY_ONLY",
            },
            {
                "id": "monthly_report",
                "time": "18:20",
                "description": "Monthly Performance & Rebalance Report (Month-End Only)",
                "script": os.path.join(PROJECT_ROOT, "scripts", "send_periodic_report.py"),
                "args": ["--period", "monthly"],
                "day_condition": "MONTH_END_ONLY",
            },
            {
                "id": "semi_annual_sync",
                "time": "06:00",
                "description": "Semi-Annual NSE Index Constituent Sync & Warm-Up (April & October)",
                "script": os.path.join(PROJECT_ROOT, "scripts", "sync_universe_constituents.py"),
                "args": ["--index", "nifty500", "--fetch-history"],
                "day_condition": "SEMI_ANNUAL_ONLY",
            },
            {
                "id": "nightly_catchup_ingestion",
                "time": "23:00",
                "description": "Nightly Settled EOD Candle Sync & Catch-Up (11:00 PM IST)",
                "script": os.path.join(PROJECT_ROOT, "scripts", "ingest_upstox_fulltier.py"),
                "args": [],
            },
            {
                "id": "nightly_catchup_pipeline",
                "time": "23:05",
                "description": "Nightly Final Signal Verification & Catch-Up (11:05 PM IST)",
                "script": os.path.join(PROJECT_ROOT, "scripts", "run_daily_pipeline.py"),
                "args": ["--strategy", "all"],
            }
        ]

    def _load_state(self, today_str: str):
        state_file = os.path.join(PROJECT_ROOT, "logs", "scheduler_state.json")
        if os.path.exists(state_file):
            try:
                import json
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("date") == today_str:
                    self.completed_today = set(data.get("completed", []))
                    self.succeeded_today = set(data.get("succeeded", []))
                    jobs_str = ", ".join(sorted(self.completed_today)) if self.completed_today else "none"
                    logger.info(f"Restored scheduler state from file: {len(self.completed_today)} jobs already completed today ({jobs_str})")
            except Exception as e:
                logger.warning(f"Could not load scheduler state from {state_file}: {e}")

    def _save_state(self, today_str: str):
        state_file = os.path.join(PROJECT_ROOT, "logs", "scheduler_state.json")
        try:
            import json
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({"date": today_str, "completed": list(self.completed_today), "succeeded": list(self.succeeded_today)}, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save scheduler state to {state_file}: {e}")

    def handle_signal(self, signum, frame):
        """Gracefully terminate daemon on SIGINT or SIGTERM."""
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.info(f"Received signal {sig_name}. Stopping scheduler daemon gracefully...")
        self.running = False
        self._save_state(self.current_date)

    def execute_job(self, job: Dict[str, Any], today_str: str):
        """Execute a scheduled task with full error handling and Telegram alerting."""
        job_id = job["id"]

        # Check if today is a trading day first.
        # Zero-Spam: Silently skip weekends and official holidays without sending any messages.
        if not is_trading_day(today_str):
            logger.info(f"Date {today_str} is weekend or holiday. Skipping {job_id} silently.")
            self.completed_today.add(job_id)
            return

        # Check day-specific condition if specified
        day_cond = job.get("day_condition")
        if day_cond == "FRIDAY_ONLY":
            now_dt = get_ist_now()
            if now_dt.weekday() != 4:  # 4 is Friday
                logger.info(f"Skipping {job_id}: today is not Friday.")
                self.completed_today.add(job_id)
                return
        elif day_cond == "MONTH_END_ONLY":
            now_dt = get_ist_now()
            test_dt = now_dt + timedelta(days=1)
            is_month_end = test_dt.month != now_dt.month
            if not is_month_end:
                curr_month = now_dt.month
                remaining_trading = False
                while test_dt.month == curr_month:
                    if is_trading_day(test_dt.strftime("%Y-%m-%d")):
                        remaining_trading = True
                        break
                    test_dt += timedelta(days=1)
                is_month_end = not remaining_trading
            if not is_month_end:
                logger.info(f"Skipping {job_id}: today is not the final trading session of the month.")
                self.completed_today.add(job_id)
                return
        elif day_cond == "SEMI_ANNUAL_ONLY":
            now_dt = get_ist_now()
            if now_dt.month not in (4, 10):
                logger.info(f"Skipping {job_id}: month {now_dt.month} is not April or October.")
                self.completed_today.add(job_id)
                return
            
            # Verify if today is the first trading session of April / October
            cur = now_dt.replace(day=1)
            is_first_trading_day = True
            while cur.date() < now_dt.date():
                if is_trading_day(cur.strftime("%Y-%m-%d")):
                    is_first_trading_day = False
                    break
                cur += timedelta(days=1)
            
            if not is_first_trading_day:
                logger.info(f"Skipping {job_id}: today is not the first trading day of April/October.")
                self.completed_today.add(job_id)
                return

        logger.info(f"========== DISPATCHING JOB: {job_id} ({job['description']}) ==========")
        cmd = [sys.executable, job["script"]] + job.get("args", [])
        start_t = time.time()
        
        try:
            res = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=1800  # 30 minutes max
            )
            elapsed = time.time() - start_t

            if res.returncode == 0:
                logger.info(f"SUCCESS: Job {job_id} finished in {elapsed:.1f}s (Exit code: 0)")
                self.completed_today.add(job_id)
                self.succeeded_today.add(job_id)
                self.retry_counts.pop(job_id, None)
                self.next_retry_time.pop(job_id, None)
                self._save_state(today_str)
            else:
                logger.error(f"FAILED: Job {job_id} exited with code {res.returncode}")
                logger.error(f"STDERR:\n{res.stderr[-1000:]}")
                logger.error(f"STDOUT:\n{res.stdout[-1000:]}")
                
                attempt = self.retry_counts.get(job_id, 0) + 1
                self.retry_counts[job_id] = attempt
                if attempt < 3:
                    retry_in = 15
                    self.next_retry_time[job_id] = get_ist_now() + timedelta(minutes=retry_in)
                    logger.warning(f"Job {job_id} failed on attempt {attempt}/3. Rescheduling non-blocking retry in {retry_in}m at {self.next_retry_time[job_id].strftime('%H:%M:%S IST')}.")
                else:
                    logger.error(f"Job {job_id} failed persistently after {attempt} attempts. Marking completed for today and alerting.")
                    self.completed_today.add(job_id)
                    self._save_state(today_str)
                    err_snippet = html.escape((res.stderr or res.stdout)[-400:].strip())
                    fail_msg = (
                        f"🚨 <b>ALERT: Persistent Job Failure in NSE Quant Lab</b>\n"
                        f"Job: <code>{html.escape(job_id)}</code>\n"
                        f"Exit Code: <code>{res.returncode}</code> (Failed all 3 attempts)\n"
                        f"Time: <code>{get_ist_now().strftime('%H:%M:%S IST')}</code>\n\n"
                        f"<b>Error Snippet:</b>\n<pre>{err_snippet}</pre>"
                    )
                    self.notifier.send_message(fail_msg)

        except subprocess.TimeoutExpired:
            logger.error(f"TIMEOUT: Job {job_id} exceeded 30 minutes execution limit!")
            t_msg = (
                f"🚨 <b>ALERT: Job Timeout in NSE Quant Lab</b>\n"
                f"Job: <code>{html.escape(job_id)}</code>\n"
                f"Status: Timed out after 30 minutes (will retry; order/fill writes are idempotent)"
            )
            self.notifier.send_message(t_msg)
            attempt = self.retry_counts.get(job_id, 0) + 1
            self.retry_counts[job_id] = attempt
            if attempt < 3:
                self.next_retry_time[job_id] = get_ist_now() + timedelta(minutes=15)
            else:
                # Given up for today, but NOT counted as a success: the nightly catch-up must still run.
                self.completed_today.add(job_id)
                self._save_state(today_str)
        except Exception as e:
            logger.error(f"Unexpected exception executing {job_id}: {e}", exc_info=True)
            err_str = html.escape(str(e)[:200])
            e_msg = (
                f"🚨 <b>ALERT: Unexpected Error in NSE Quant Lab</b>\n"
                f"Job: <code>{html.escape(job_id)}</code>\n"
                f"Error: <pre>{err_str}</pre>"
            )
            self.notifier.send_message(e_msg)
            self.completed_today.add(job_id)
            self._save_state(today_str)

    def run(self):
        """Main scheduler daemon loop."""
        logger.info("""
===========================================================
      NSE QUANT LAB - PRODUCTION SCHEDULER DAEMON
===========================================================
""")
        logger.info(f"Current IST Time: {get_ist_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
        logger.info(f"Monitoring schedule for date: {self.current_date}")
        for j in self.jobs:
            logger.info(f"  • [{j['time']} IST] {j['id']}: {j['description']}")

        while self.running:
            now_ist = get_ist_now()
            today_str = now_ist.strftime("%Y-%m-%d")
            time_str = now_ist.strftime("%H:%M")

            # Day rollover
            if today_str != self.current_date:
                logger.info(f"Calendar day changed from {self.current_date} to {today_str}. Resetting job trackers.")
                self.current_date = today_str
                self.completed_today.clear()
                self.succeeded_today.clear()
                self.retry_counts.clear()
                self.next_retry_time.clear()
                self._save_state(today_str)

            # Check jobs
            for job in self.jobs:
                job_id = job["id"]
                scheduled_time = job["time"]

                if job_id not in self.completed_today:
                    # Defense-in-depth: Skip nightly catchup pipeline if daily_pipeline succeeded today
                    if job_id == "nightly_catchup_pipeline" and "daily_pipeline" in self.succeeded_today:
                        logger.info("Skipping nightly_catchup_pipeline: daily_pipeline already completed successfully today.")
                        self.completed_today.add(job_id)
                        self._save_state(today_str)
                        continue

                    if job_id == "daily_pipeline" and "fulltier_ingestion" in self.next_retry_time:
                        continue
                    retry_at = self.next_retry_time.get(job_id)
                    if retry_at:
                        if now_ist >= retry_at:
                            self.next_retry_time.pop(job_id, None)
                            self.execute_job(job, today_str)
                    elif time_str >= scheduled_time:
                        max_t = job.get("max_time")
                        if max_t and time_str > max_t:
                            logger.warning(f"Skipping {job_id}: current time {time_str} IST exceeds max execution window {max_t} IST.")
                            self.completed_today.add(job_id)
                            self._save_state(today_str)
                            continue
                        self.execute_job(job, today_str)

            # Sleep 25 seconds between ticks
            time.sleep(25)

        logger.info("Scheduler daemon has stopped.")

def main():
    parser = argparse.ArgumentParser(description="NSE Quant Lab Background Scheduler")
    parser.add_argument("--test-job", choices=["morning_execution", "fulltier_ingestion", "benchmark_ingestion", "daily_pipeline", "weekly_report", "monthly_report", "semi_annual_sync", "nightly_catchup_ingestion", "nightly_catchup_pipeline"],
                        help="Immediately test run a specific job without waiting for its scheduled time.")
    args = parser.parse_args()

    scheduler = QuantLabScheduler()

    # Register OS signals
    signal.signal(signal.SIGINT, scheduler.handle_signal)
    signal.signal(signal.SIGTERM, scheduler.handle_signal)

    if args.test_job:
        target = next((j for j in scheduler.jobs if j["id"] == args.test_job), None)
        if not target:
            print(f"Unknown job: {args.test_job}")
            sys.exit(1)
        today_str = get_ist_now().strftime("%Y-%m-%d")
        print(f"Executing immediate test run of: {args.test_job}")
        scheduler.execute_job(target, today_str)
        sys.exit(0)

    scheduler.run()

if __name__ == "__main__":
    main()
