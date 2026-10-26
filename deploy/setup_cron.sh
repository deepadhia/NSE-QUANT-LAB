#!/usr/bin/env bash
# ==============================================================================
# NSE Quant Lab - Alternative Crontab Installer (For Cron Mode)
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "--> Installing crontab for user $(whoami)..."
mkdir -p "${APP_DIR}/logs"

# Replace %APP_DIR% in crontab.txt and install
sed "s|%APP_DIR%|${APP_DIR}|g" "${SCRIPT_DIR}/crontab.txt" | crontab -

echo "--> Crontab successfully installed! Active schedule:"
crontab -l
