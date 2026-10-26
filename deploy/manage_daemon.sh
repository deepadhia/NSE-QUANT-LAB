#!/usr/bin/env bash
# ==============================================================================
# NSE Quant Lab - Systemd Daemon Manager Utility
# Easy management of the background trading daemon.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_NAME="nse-quant-lab.service"
SYSTEMD_PATH="/etc/systemd/system/${SERVICE_NAME}"

action="${1:-status}"

case "$action" in
    install-service)
        echo "--> Installing ${SERVICE_NAME} to ${SYSTEMD_PATH}..."
        CURRENT_USER=$(whoami)
        # Substitute placeholders
        sed -e "s|%USER%|${CURRENT_USER}|g" -e "s|%APP_DIR%|${APP_DIR}|g" "${SCRIPT_DIR}/service/${SERVICE_NAME}" | sudo tee "${SYSTEMD_PATH}" > /dev/null
        sudo systemctl daemon-reload
        sudo systemctl enable "${SERVICE_NAME}"
        sudo systemctl restart "${SERVICE_NAME}"
        echo "--> Service installed, enabled on boot, and started!"
        sudo systemctl status "${SERVICE_NAME}" --no-pager
        ;;

    start)
        echo "--> Starting ${SERVICE_NAME}..."
        sudo systemctl start "${SERVICE_NAME}"
        sudo systemctl status "${SERVICE_NAME}" --no-pager
        ;;

    stop)
        echo "--> Stopping ${SERVICE_NAME}..."
        sudo systemctl stop "${SERVICE_NAME}"
        echo "--> Service stopped."
        ;;

    restart)
        echo "--> Restarting ${SERVICE_NAME}..."
        sudo systemctl restart "${SERVICE_NAME}"
        sudo systemctl status "${SERVICE_NAME}" --no-pager
        ;;

    status)
        sudo systemctl status "${SERVICE_NAME}" --no-pager || true
        ;;

    logs)
        echo "--> Tailing live logs (Ctrl+C to exit)..."
        sudo journalctl -u "${SERVICE_NAME}" -f -n 50
        ;;

    test-job)
        job="${2:-eod_ingestion}"
        echo "--> Running immediate test of job: ${job}..."
        "${APP_DIR}/venv/bin/python" "${APP_DIR}/scripts/run_scheduler_daemon.py" --test-job "${job}"
        ;;

    uninstall-service)
        echo "--> Uninstalling ${SERVICE_NAME}..."
        sudo systemctl stop "${SERVICE_NAME}" || true
        sudo systemctl disable "${SERVICE_NAME}" || true
        sudo rm -f "${SYSTEMD_PATH}"
        sudo systemctl daemon-reload
        echo "--> Service removed."
        ;;

    *)
        echo "Usage: $0 {install-service|start|stop|restart|status|logs|test-job <job_name>|uninstall-service}"
        echo ""
        echo "Available test jobs:"
        echo "  • morning_execution"
        echo "  • eod_ingestion"
        echo "  • daily_pipeline"
        exit 1
        ;;
esac
