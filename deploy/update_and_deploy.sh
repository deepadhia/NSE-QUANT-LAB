#!/usr/bin/env bash
# ==============================================================================
# NSE Quant Lab - Post-Push Update & Deployment Script
# Safely pulls git updates, updates dependencies, runs tests, and restarts daemon.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$APP_DIR"

echo "==========================================================="
echo "   NSE QUANT LAB - UPDATING & DEPLOYING LATEST CODE"
echo "==========================================================="
echo "Current Time: $(date)"
echo "Directory: $APP_DIR"

# 1. Pull Latest Git Code
echo "--> Pulling latest code from origin/main..."
git fetch origin main
CURRENT_COMMIT=$(git rev-parse --short HEAD)
git reset --hard origin/main
NEW_COMMIT=$(git rev-parse --short HEAD)
echo "Updated: $CURRENT_COMMIT -> $NEW_COMMIT"

# 2. Update Python Packages
echo "--> Updating virtual environment packages..."
./venv/bin/python -m pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install -e .

# 3. Run Pre-Restart Tests
echo "--> Running safety verification tests..."
if ./venv/bin/pytest tests -q; then
    echo "--> All tests passed!"
else
    echo "❌ ERROR: Tests failed after git pull! Aborting daemon restart to protect production."
    exit 1
fi

# 4. Restart Daemon Service if Active
SERVICE_NAME="nse-quant-lab.service"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "--> Restarting $SERVICE_NAME..."
    sudo systemctl restart "$SERVICE_NAME"
    echo "--> Service restarted successfully."
elif systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "--> Starting $SERVICE_NAME..."
    sudo systemctl start "$SERVICE_NAME"
else
    echo "--> Note: Systemd service $SERVICE_NAME is not active or not installed."
    echo "    To install it, run: ./deploy/manage_daemon.sh install-service"
fi

echo "==========================================================="
echo "   DEPLOYMENT SUCCEEDED (Commit: $NEW_COMMIT)!"
echo "==========================================================="
