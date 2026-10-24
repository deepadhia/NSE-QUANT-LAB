"""
NSE Quant Lab - Production Background Scheduler Daemon
Runs 24/7 as a systemd service on Oracle Cloud VM.
Dispatches scheduled jobs at exact IST market times with automated holiday checks.
"""

import os
import sys
import time
import signal
import logging
import argparse
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

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
    """

    def __init__(self):
        self.running = True
        self.completed_today = set()
        self.current_date = get_ist_now().strftime("%Y-%m-%d")
        self.notifier = TelegramNotifier()
        
        # Define schedule (IST times)
        self.jobs = [
            {
                "id": "morning_heartbeat",
                "time": "08:30",
                "description": "Pre-Market Status & Heartbeat Check",
                "type": "internal_heartbeat",
            },
            {
                "id": "morning_execution",
                "time": "09:16",
                "description": "Morning Market Open Order Execution",
                "type": "command",
                "script": os.path.join(PROJECT_ROOT, "scripts", "execute_paper_orders.py"),
                "args": []
            },
            {
                "id": "eod_ingestion",
                "time": "15:35",
                "description": "EOD Daily Candle Ingestion from Upstox",
                "type": "command",
                "script": os.path.join(PROJECT_ROOT, "scripts", "ingest_daily_upstox_candles.py"),
                "args": []
            },
            {
                "id": "daily_pipeline",
                "time": "15:45",
                "description": "Daily Strategy Signal Generation & Portfolio Rebalance",
                "type": "command",
                "script": os.path.join(PROJECT_ROOT, "scripts", "run_daily_pipeline.py"),
                "args": []
            }
        ]

    def handle_signal(self, signum, frame):
        """Gracefully terminate daemon on SIGINT or SIGTERM."""
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.info(f"Received signal {sig_name}. Stopping scheduler daemon gracefully...")
        self.running = False

    def send_heartbeat(self, today_str: str):
        """Send daily morning Telegram heartbeat."""
        trading = is_trading_day(today_str)
        if trading:
            msg = (
                f"🟢 *NSE Quant Lab Online - {today_str}*\n"
                f"Market Status: *TRADING SESSION OPEN*\n\n"
                f"⏰ *Schedule for Today:*\n"
                f"• `09:16 IST`: Morning Order Execution\n"
                f"• `15:35 IST`: Daily Candle Ingestion\n"
                f"• `15:45 IST`: Strategy Scanning & Signals\n\n"
                f"_System running on Oracle Cloud VM._"
            )
        else:
            msg = (
                f"⏸️ *NSE Quant Lab Notice - {today_str}*\n"
                f"Market Status: *CLOSED (Weekend / NSE Holiday)*\n\n"
                f"Automated execution pipelines are paused for today.\n"
                f"Next check tomorrow morning."
            )
        logger.info(f"Sending daily heartbeat for {today_str} (Trading Day: {trading})")
        self.notifier.send_message(msg)

    def execute_job(self, job: Dict[str, Any], today_str: str):
        """Execute a scheduled task with full error handling and Telegram alerting."""
        job_id = job["id"]
        logger.info(f"========== DISPATCHING JOB: {job_id} ({job['description']}) ==========")

        if job["type"] == "internal_heartbeat":
            try:
                self.send_heartbeat(today_str)
                self.completed_today.add(job_id)
                logger.info(f"Job {job_id} completed successfully.")
            except Exception as e:
                logger.error(f"Error sending heartbeat: {e}")
            return

        # For command jobs, check if today is trading day first
        if not is_trading_day(today_str):
            logger.info(f"Date {today_str} is weekend or holiday. Skipping {job_id}.")
            self.completed_today.add(job_id)
            return

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
            else:
                logger.error(f"FAILED: Job {job_id} exited with code {res.returncode}")
                logger.error(f"STDERR:\n{res.stderr[-1000:]}")
                logger.error(f"STDOUT:\n{res.stdout[-1000:]}")
                
                # Send failure alert to Telegram
                err_snippet = (res.stderr or res.stdout)[-400:].strip()
                fail_msg = (
                    f"🚨 *ALERT: Job Failure in NSE Quant Lab*\n"
                    f"Job: `{job_id}`\n"
                    f"Exit Code: `{res.returncode}`\n"
                    f"Time: `{get_ist_now().strftime('%H:%M:%S IST')}`\n\n"
                    f"*Error Snippet:*\n`{err_snippet}`"
                )
                self.notifier.send_message(fail_msg)
                # Still mark completed to prevent rapid retry loops
                self.completed_today.add(job_id)

        except subprocess.TimeoutExpired:
            logger.error(f"TIMEOUT: Job {job_id} exceeded 30 minutes execution limit!")
            self.notifier.send_message(f"🚨 *ALERT: Job {job_id} TIMED OUT after 30 minutes!*")
            self.completed_today.add(job_id)
        except Exception as e:
            logger.error(f"Unexpected exception executing {job_id}: {e}", exc_info=True)
            self.notifier.send_message(f"🚨 *ALERT: Unexpected error executing {job_id}:* {str(e)[:200]}")
            self.completed_today.add(job_id)

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

        # Startup notification
        try:
            startup_msg = (
                f"🚀 *NSE Quant Lab Daemon Started*\n"
                f"Host: Oracle Cloud VM\n"
                f"Time: `{get_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')}`\n"
                f"Scheduler is active and monitoring market sessions."
            )
            self.notifier.send_message(startup_msg)
        except Exception as e:
            logger.warning(f"Could not send startup notification: {e}")

        while self.running:
            now_ist = get_ist_now()
            today_str = now_ist.strftime("%Y-%m-%d")
            time_str = now_ist.strftime("%H:%M")

            # Day rollover
            if today_str != self.current_date:
                logger.info(f"Calendar day changed from {self.current_date} to {today_str}. Resetting job trackers.")
                self.current_date = today_str
                self.completed_today.clear()

            # Check jobs
            for job in self.jobs:
                job_id = job["id"]
                scheduled_time = job["time"]

                if job_id not in self.completed_today:
                    if time_str >= scheduled_time:
                        self.execute_job(job, today_str)

            # Sleep 25 seconds between ticks
            time.sleep(25)

        logger.info("Scheduler daemon has stopped.")

def main():
    parser = argparse.ArgumentParser(description="NSE Quant Lab Background Scheduler")
    parser.add_argument("--test-job", choices=["morning_heartbeat", "morning_execution", "eod_ingestion", "daily_pipeline"],
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
