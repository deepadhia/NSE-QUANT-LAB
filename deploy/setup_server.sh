#!/usr/bin/env bash
# ==============================================================================
# NSE Quant Lab - Oracle Cloud VM Server Setup & Environment Provisioner
# Supports: Ubuntu 22.04/24.04 LTS and Debian / Oracle Linux 8/9
# ==============================================================================

set -e

echo "==========================================================="
echo "   NSE QUANT LAB - ORACLE CLOUD SERVER PROVISIONING"
echo "==========================================================="

# 1. Ensure root or sudo
if [ "$EUID" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

# 2. Set Timezone to Asia/Kolkata (Indian Standard Time)
echo "--> Configuring timezone to Asia/Kolkata (IST)..."
$SUDO timedatectl set-timezone Asia/Kolkata || {
    echo "Warning: timedatectl failed, setting via /etc/localtime..."
    $SUDO ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime
}
echo "Current Time: $(date)"

# 3. Detect OS & Install Dependencies
echo "--> Installing system packages (Python 3, pip, venv, git, curl, build tools)..."
if [ -f /etc/debian_version ]; then
    $SUDO apt-get update -y
    $SUDO apt-get install -y python3 python3-pip python3-venv git curl cron tzdata build-essential
elif [ -f /etc/redhat-release ]; then
    $SUDO dnf update -y
    $SUDO dnf install -y python3 python3-pip git curl cronie tzdata gcc python3-devel
    $SUDO systemctl enable crond || true
    $SUDO systemctl start crond || true
fi

# 4. Project Directory Discovery
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$APP_DIR"
echo "Project Directory: $APP_DIR"

# 5. Create Virtual Environment
if [ ! -d "venv" ]; then
    echo "--> Creating Python virtual environment in $APP_DIR/venv..."
    python3 -m venv venv
else
    echo "--> Existing virtual environment found."
fi

# 6. Install & Upgrade Python Dependencies
echo "--> Upgrading pip and installing requirements..."
./venv/bin/python -m pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install -e .

# 7. Setup .env file
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo "--> Creating .env from .env.example..."
        cp .env.example .env
        chmod 600 .env
        echo "IMPORTANT: Please edit .env with your actual MONGO_URI, TELEGRAM_BOT_TOKEN, etc."
    fi
else
    chmod 600 .env
    echo "--> .env already exists. Permissions set to 600."
fi

# 8. Create logs and results directories
mkdir -p "$APP_DIR/logs"
mkdir -p "$APP_DIR/results"

# 9. Ensure executable permissions on deploy scripts
chmod +x "$APP_DIR"/deploy/*.sh || true

# 10. Run Tests to Verify Environment
echo "--> Running verification tests..."
./venv/bin/pytest tests -q

echo "==========================================================="
echo "   SERVER PROVISIONING COMPLETE!"
echo "==========================================================="
echo "Next Steps:"
echo "1. Verify/Edit your credentials: nano $APP_DIR/.env"
echo "2. Install the systemd daemon:   $APP_DIR/deploy/manage_daemon.sh install-service"
echo "3. Monitor service logs:        $APP_DIR/deploy/manage_daemon.sh logs"
echo "==========================================================="
