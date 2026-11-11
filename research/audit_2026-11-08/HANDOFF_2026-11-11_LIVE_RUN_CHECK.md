# Handoff 2026-11-11: live-run check (morning 09:16 run, evening run still to test)

Scope: only what was checked on 2026-11-11 about the production scheduler on the Oracle server (`ubuntu@nse-quant`, `~/NSE-QUANT-LAB`). No code or data was changed. Nothing was committed or pushed.

## Question asked
Was this morning's Learning-Lab execution proper, or is tonight's evening run the one to test?

## VERIFIED (from server output pasted by the user)
- Production runs as the systemd daemon `nse-quant-lab.service` (`scripts/run_scheduler_daemon.py`), not cron. `logs/cron.log` does not exist on the server; the daemon logs to journald under `nse-quant-lab`. (`deploy/crontab.txt` is only used by `setup_cron.sh`.)
- Daemon was restarted 2026-11-10 20:27:22 IST (active, PID 183142, still running at the time of the check). Likely a deploy; not confirmed which commit.
- 2026-11-11 09:16:09 `morning_execution` dispatched; 09:16:14 "SUCCESS ... finished in 4.2s (Exit code: 0)".
- The daemon captures script output and logs it only on failure (`scripts/run_scheduler_daemon.py:256-275`), so no per-order lines in the journal is expected.
- The daemon logged 2026-11-10 as "weekend or holiday" (skipped `nightly_catchup_ingestion`). No evening jobs ran on Nov 10 (grep of 17:30-19:00 was empty). Not checked against the NSE calendar.
- `scripts/report_fill_audit.py --since 2026-11-11` on the server: "No fill_audit records", so no order was filled this morning.

## NOT ESTABLISHED
- Whether the morning run was a correct no-op (nothing pending) or orders were pending and not filled. Repo invariant 4 (AGENTS.md) says an unpriced order must exit non-zero, so a no-op is more likely, but that is inference.
- Whether the Nov 9 evening pipeline staged any orders (they would have waited through the Nov 10 holiday for Nov 11).
- Which commit the 20:27 restart deployed, and so whether the 2026-11-09 benchmark change (`ingest_benchmarks.py`, Upstox primary) is live.

## Checks still to run on the server (all read-only)
1. Pending orders:
   ```bash
   ./venv/bin/python - <<'EOF'
   import sys; sys.path.insert(0, "src")
   from dotenv import load_dotenv; load_dotenv(".env")
   from nse_quant_lab.db.mongo_client import MongoDatabase
   db = MongoDatabase.get_instance().db
   print("orders by status:", list(db.orders.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])))
   for d in db.orders.find({}, {"_id": 0}).sort("_id", -1).limit(8): print(d)
   EOF
   ```
   Any `PENDING_OPEN` BUY/SELL left from Nov 9 means the morning exited 0 without filling: investigate.
2. Nov 9 evening pipeline:
   ```bash
   sudo journalctl -u nse-quant-lab --since "2026-11-09 17:30" --until "2026-11-09 19:00" --no-pager | grep -i -E "dispatch|finished|failed|error|order|signal"
   ```
3. Tonight, after 18:15 IST (17:45 Upstox ingest, 17:55 benchmarks, 18:00 pipeline):
   ```bash
   sudo journalctl -u nse-quant-lab --since "today 17:40" --no-pager | grep -i -E "dispatch|finished|failed|error|degraded"
   ```
   Expect each job to finish with exit code 0. A benchmark job exit 1 means no bar for the latest session (the scheduler retries and holds the 18:00 pipeline); a Telegram "degraded" alert means Upstox outage, no token, or a Yahoo/Upstox close mismatch above 0.1%.
4. First morning run that can fill: tomorrow, 2026-11-12 09:16, if tonight stages orders. Then run `report_fill_audit.py --since 2026-11-12` and confirm fills have quotes and exit code 0.

## Reading of the result
This morning's run is not a meaningful test of fills: it ran on time and exited 0, but with no fills it exercised little. Tonight's evening chain is the first live exercise of anything deployed on Nov 10, and tomorrow morning is the first fill test.

## Local-environment note
The laptop's Mongo defaults to `localhost` (dev), so local `report_fill_audit.py` output says nothing about the server. Local `logs/scheduler.log` stops at 2026-10-25.
