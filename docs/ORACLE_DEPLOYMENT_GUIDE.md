# Oracle Cloud Infrastructure (OCI) Deployment Guide

This guide walks you through setting up, deploying, and maintaining **NSE Quant Lab** on a free or standard **Oracle Cloud Compute VM** (Always Free Tier eligible).

---

## Architecture Overview

```
                                      ┌────────────────────────────────┐
                                      │       Oracle Cloud VM          │
                                      │    (Ubuntu 24.04 / IST Zone)   │
                                      │                                │
                                      │  ┌──────────────────────────┐  │
                                      │  │ nse-quant-lab.service    │  │
                                      │  │ (Scheduler Daemon)       │  │
                                      │  └─────────────┬────────────┘  │
                                      │                │               │
                                      │     09:16 IST  │ Morning Fills │
                                      │     15:35 IST  │ EOD Ingest    │
                                      │     15:45 IST  │ Strategy Scan │
                                      └────────────────┼───────────────┘
                                                       │
                      ┌────────────────────────────────┼───────────────────────────────┐
                      │                                │                               │
                      ▼                                ▼                               ▼
        ┌───────────────────────────┐    ┌───────────────────────────┐   ┌───────────────────────────┐
        │       MongoDB Atlas       │    │      Upstox V2 API        │   │    Telegram Bot Alerts    │
        │  • Daily OHLCV Candles    │    │  • Daily EOD Candles      │   │  • Trade Signals & NAV    │
        │  • Portfolios & Orders    │    │  • Market Holidays        │   │  • System Health Alerts   │
        └───────────────────────────┘    └───────────────────────────┘   └───────────────────────────┘
```

---

## Part 1: Provisioning Oracle Cloud VM (Free Tier)

1. **Log in to Oracle Cloud Console**: Navigate to [cloud.oracle.com](https://cloud.oracle.com).
2. **Create a Compute Instance**:
   - Go to **Compute** > **Instances** > **Create Instance**.
   - **Name**: `nse-quant-lab-prod`
   - **Placement**: Default AD.
   - **Image and Shape**:
     - *Image*: **Ubuntu 24.04 LTS** (or Ubuntu 22.04 LTS).
     - *Shape*: **VM.Standard.A1.Flex** (Ampere ARM - 1 to 4 OCPUs, 6 to 24 GB RAM free) OR **VM.Standard.E2.1.Micro** (AMD x86_64 - 1 OCPU, 1 GB RAM free).
   - **Networking**: Select your default VCN & public subnet, ensure **Assign a public IPv4 address** is checked.
   - **Add SSH Keys**: Generate and download a private key, or paste your public SSH key (`~/.ssh/id_rsa.pub`).
   - Click **Create**.
3. **Connect to your VM via SSH**:
   ```bash
   ssh -i /path/to/your-private-key ubuntu@<YOUR_ORACLE_PUBLIC_IP>
   ```

---

## Part 2: First-Time Server Setup (Automated)

Run the following commands on your Oracle VM:

```bash
# 1. Clone the repository
git clone https://github.com/deepadhia/NSE-QUANT-LAB.git
cd NSE-QUANT-LAB

# 2. Run the automated server provisioner
# (Installs Python 3, venv, git, build tools, sets timezone to Asia/Kolkata, sets up venv & dependencies)
chmod +x deploy/*.sh
./deploy/setup_server.sh
```

---

## Part 3: Configure Environment Variables

1. Open `.env` to configure your credentials:
   ```bash
   nano .env
   ```

2. Verify that your `.env` contains:
   ```ini
   # Environment Mode
   APP_ENV=production

   # MongoDB Atlas Connection
   MONGO_URI=mongodb+srv://<username>:<password>@<your_cluster>.mongodb.net/?retryWrites=true&w=majority
   MONGO_DB_NAME=stock_scanner

   # Upstox API
   UPSTOX_API_KEY=your_upstox_api_key
   UPSTOX_API_SECRET=your_upstox_api_secret
   UPSTOX_REDIRECT_URI=https://127.0.0.1
   UPSTOX_ACCESS_TOKEN=your_access_token_if_available

   # Telegram Notifications
   TELEGRAM_ENABLED=true
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token
   TELEGRAM_CHAT_ID=your_numeric_chat_id
   ```
   Save with `Ctrl+O`, then `Ctrl+X`.

3. Secure file permissions:
   ```bash
   chmod 600 .env
   ```

---

## Part 4: Start Background Daemon (Systemd)

We provide a systemd service wrapper so that the scheduler daemon runs 24/7 in the background, automatically starts on server reboot, and restarts if ever interrupted.

```bash
# 1. Install and start the service with 1 command:
./deploy/manage_daemon.sh install-service

# 2. Check service status:
./deploy/manage_daemon.sh status

# 3. View live logs:
./deploy/manage_daemon.sh logs
```

Upon starting, the daemon will send an initial verification alert to your Telegram:
> 🚀 **NSE Quant Lab Daemon Started**  
> Host: Oracle Cloud VM  
> Time: `YYYY-MM-DD HH:MM:SS IST`  
> Scheduler is active and monitoring market sessions.

---

## Part 5: Pushing Updates & Redeploying

Whenever you make code improvements on your local machine and push to GitHub:

```bash
# On your local machine:
git add .
git commit -m "feat: added new feature"
git push origin main
```

To update the running Oracle server, simply SSH in and run:

```bash
# On the Oracle Cloud VM:
cd ~/NSE-QUANT-LAB
./deploy/update_and_deploy.sh
```

What `update_and_deploy.sh` does automatically:
1. Pulls the latest code from `origin/main`.
2. Updates and installs any new Python packages from `requirements.txt`.
3. Runs the test suite (`pytest tests`). **If tests fail, it cancels restart to protect live operations.**
4. Restarts `nse-quant-lab.service` cleanly.

---

## Part 6: How Holidays and Weekends are Handled

The system is equipped with **zero-maintenance holiday & weekend protection**:

1. **Weekend Guard**: Automatically detects Saturdays and Sundays.
2. **Official NSE Calendar**: Includes official NSE trading holiday calendars for 2024, 2025, 2026, and 2027.
3. **Dynamic API & Cache**: Syncs with Upstox `/v2/market/holidays` API when available and caches holidays in MongoDB.
4. **Daemon Behavior on Non-Trading Days**:
   - All pipelines safely skip execution completely silently on weekends and official exchange holidays.
   - Zero spam: No messages or alerts are sent when markets are closed.
   - Zero unnecessary API queries or corrupted holiday data.

To force run a script on a holiday (for manual backtesting or testing), use the `--force` flag:
```bash
./venv/bin/python scripts/run_daily_pipeline.py --force
```

---

## Part 7: Daemon Management Cheat Sheet

| Command | Action |
|---|---|
| `./deploy/manage_daemon.sh status` | Check if scheduler is currently running |
| `./deploy/manage_daemon.sh logs` | View live systemd and stdout/stderr logs |
| `./deploy/manage_daemon.sh restart` | Restart the scheduler daemon |
| `./deploy/manage_daemon.sh stop` | Stop the scheduler daemon |
| `./deploy/manage_daemon.sh start` | Start the scheduler daemon |
| `./deploy/manage_daemon.sh test-job eod_ingestion` | Run candle ingestion immediately |
| `./deploy/manage_daemon.sh test-job daily_pipeline` | Run post-market signal scan immediately |
| `./deploy/manage_daemon.sh test-job morning_execution` | Run morning order fills immediately |
| `./deploy/update_and_deploy.sh` | Pull latest code, install packages, run tests, and restart |
