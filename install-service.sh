#!/usr/bin/env bash
set -euo pipefail

# FSY Packet Console - Systemd Service Installer
# This script creates a customized systemd service file for your system

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="fsy-console"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=========================================="
echo "FSY Packet Console - Service Installer"
echo "=========================================="
echo ""

# Detect current user and paths
CURRENT_USER="${USER}"
CURRENT_HOME="${HOME}"
PROJECT_DIR="${SCRIPT_DIR}"

# Try to detect Python virtual environment
PYTHON_EXEC=""
if [[ -f "${CURRENT_HOME}/venv/bin/python" ]]; then
    PYTHON_EXEC="${CURRENT_HOME}/venv/bin/python"
    echo "✓ Found virtual environment: ~/venv"
elif [[ -f "${PROJECT_DIR}/venv/bin/python" ]]; then
    PYTHON_EXEC="${PROJECT_DIR}/venv/bin/python"
    echo "✓ Found virtual environment: venv"
elif [[ -f "${CURRENT_HOME}/console-venv/bin/python" ]]; then
    PYTHON_EXEC="${CURRENT_HOME}/console-venv/bin/python"
    echo "✓ Found virtual environment: ~/console-venv"
else
    PYTHON_EXEC="$(which python3)"
    echo "⚠ No virtual environment found, using system Python: ${PYTHON_EXEC}"
    echo "  (This may not work if dependencies aren't installed system-wide)"
fi

# Confirm with user
echo ""
echo "Configuration:"
echo "  User:              ${CURRENT_USER}"
echo "  Home Directory:    ${CURRENT_HOME}"
echo "  Project Directory: ${PROJECT_DIR}"
echo "  Python Executable: ${PYTHON_EXEC}"
echo ""
read -p "Install service with these settings? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Installation cancelled."
    exit 1
fi

# Create customized service file
echo ""
echo "Creating service file..."

cat > /tmp/${SERVICE_NAME}.service <<EOF
[Unit]
Description=FSY Packet Console - APRS TNC and Web UI
After=network.target
Wants=network-online.target

[Service]
Type=forking
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}

# Start the console in a screen session
ExecStart=/usr/bin/screen -dmS ${SERVICE_NAME} ${PYTHON_EXEC} main.py

# Stop gracefully - send SIGTERM to allow cleanup
ExecStop=/usr/bin/screen -S ${SERVICE_NAME} -X quit

# Allow up to 15 seconds for graceful shutdown (important for Bluetooth cleanup)
TimeoutStopSec=15

# Restart on failure
Restart=on-failure
RestartSec=10

# Cleanup on stop
KillMode=process

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

# Process limits
LimitNOFILE=65536
LimitNPROC=4096

[Install]
WantedBy=multi-user.target
EOF

# Install the service
echo "Installing service file to ${SERVICE_FILE}..."
sudo cp /tmp/${SERVICE_NAME}.service ${SERVICE_FILE}
sudo chmod 644 ${SERVICE_FILE}
rm /tmp/${SERVICE_NAME}.service

# Reload systemd
echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

# Offer to enable and start
echo ""
echo "✓ Service installed successfully!"
echo ""
read -p "Enable auto-start on boot? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    sudo systemctl enable ${SERVICE_NAME}.service
    echo "✓ Auto-start enabled"
fi

echo ""
read -p "Start the service now? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    sudo systemctl start ${SERVICE_NAME}.service
    echo "✓ Service started"
    echo ""
    sleep 2
    echo "Service status:"
    systemctl status ${SERVICE_NAME}.service --no-pager
fi

echo ""
echo "=========================================="
echo "Installation Complete!"
echo "=========================================="
echo ""
echo "Useful commands:"
echo "  Start:   sudo systemctl start ${SERVICE_NAME}.service"
echo "  Stop:    sudo systemctl stop ${SERVICE_NAME}.service"
echo "  Status:  systemctl status ${SERVICE_NAME}.service"
echo "  Logs:    journalctl -u ${SERVICE_NAME}.service -f"
echo "  Attach:  screen -r ${SERVICE_NAME}"
echo ""
echo "See SYSTEMD_SETUP.md for detailed documentation."
echo ""
