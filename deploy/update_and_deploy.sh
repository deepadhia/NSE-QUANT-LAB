#!/usr/bin/env bash
# ==============================================================================
# NSE Quant Lab - Smart Post-Push Update & Deployment Script
# Safely pulls git updates, conditionally installs packages only when needed,
# runs safety tests, restarts the daemon, and verifies live logs.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$APP_DIR"

FORCE_DEPS=false
if [[ "$1" == "--install-deps" || "$1" == "--force" ]]; then
    FORCE_DEPS=true
fi

echo "==========================================================="
echo "   NSE QUANT LAB - UPDATING & DEPLOYING LATEST CODE"
echo "==========================================================="
echo "Current Time: $(date)"
echo "Directory: $APP_DIR"

# 1. Pull Latest Git Code
echo "--> Fetching latest updates from origin/main..."
git fetch origin main
CURRENT_COMMIT=$(git rev-parse --short HEAD)
git reset --hard origin/main
NEW_COMMIT=$(git rev-parse --short HEAD)
chmod +x "$SCRIPT_DIR"/*.sh 2>/dev/null || true
echo "Commit status: $CURRENT_COMMIT -> $NEW_COMMIT"

# 2. Conditional Python Package Installation
DEP_DIFF=""
if [ "$CURRENT_COMMIT" != "$NEW_COMMIT" ]; then
    DEP_DIFF=$(git diff --name-only "$CURRENT_COMMIT" "$NEW_COMMIT" -- requirements.txt pyproject.toml setup.py 2>/dev/null || true)
fi

if [ "$FORCE_DEPS" = true ] || [ -n "$DEP_DIFF" ]; then
    echo "--> Dependency file changes detected ($DEP_DIFF) or --install-deps specified."
    echo "--> Updating virtual environment packages..."
    ./venv/bin/python -m pip install --upgrade pip --quiet
    ./venv/bin/pip install -r requirements.txt --quiet
    ./venv/bin/pip install -e . --quiet
    echo "--> Packages updated successfully."
else
    echo "--> [SKIPPED] No dependency changes detected in requirements.txt/pyproject.toml."
    echo "    (To force package update, run: ./deploy/update_and_deploy.sh --install-deps)"
fi

# 3. Run Pre-Restart Tests
echo "--> Running safety verification tests..."
if ./venv/bin/pytest tests -q; then
    echo "--> All safety tests passed cleanly!"
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
    echo "--> Note: Systemd service $SERVICE_NAME is not active."
    echo "    To install/enable it, run: ./deploy/manage_daemon.sh install-service"
fi

echo "==========================================================="
echo "   DEPLOYMENT SUCCEEDED (Commit: $NEW_COMMIT)!"
echo "==========================================================="
echo ""
echo "--> Recent daemon log entries:"
sudo journalctl -u "$SERVICE_NAME" -n 12 --no-pager || true
