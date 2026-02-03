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

# Ask about TNC transport mode
echo ""
echo "TNC Transport Configuration"
echo "==========================="
echo "Select your TNC connection type:"
echo "  1) BLE (Bluetooth Low Energy) - default"
echo "  2) Serial KISS TNC (e.g., /dev/ttyUSB0)"
echo "  3) KISS-over-TCP (e.g., Direwolf)"
echo ""
read -p "Select option [1-3, default: 1]: " TRANSPORT_CHOICE

EXEC_ARGS="-l"  # Always enable logging

case "${TRANSPORT_CHOICE}" in
    2)
        echo ""
        read -p "Enter serial port (e.g., /dev/ttyUSB0): " SERIAL_PORT
        if [[ -n "${SERIAL_PORT}" ]]; then
            read -p "Enter baud rate [default: 9600]: " BAUD_RATE
            BAUD_RATE=${BAUD_RATE:-9600}
            EXEC_ARGS="${EXEC_ARGS} -s ${SERIAL_PORT} -b ${BAUD_RATE}"
            echo "✓ Configured for Serial KISS: ${SERIAL_PORT} @ ${BAUD_RATE} baud"
        else
            echo "⚠ No serial port specified, falling back to BLE mode"
        fi
        ;;
    3)
        echo ""
        read -p "Enter KISS TNC host (e.g., localhost or 192.168.1.100): " KISS_HOST
        if [[ -n "${KISS_HOST}" ]]; then
            read -p "Enter KISS TNC port [default: 8001]: " KISS_PORT
            KISS_PORT=${KISS_PORT:-8001}
            EXEC_ARGS="${EXEC_ARGS} -k ${KISS_HOST}:${KISS_PORT}"
            echo "✓ Configured for KISS-over-TCP: ${KISS_HOST}:${KISS_PORT}"
        else
            echo "⚠ No KISS host specified, falling back to BLE mode"
        fi
        ;;
    1|*)
        echo ""
        read -p "Enter Bluetooth MAC address (optional, can configure later): " BLE_MAC
        if [[ -n "${BLE_MAC}" ]]; then
            EXEC_ARGS="${EXEC_ARGS} -r ${BLE_MAC}"
            echo "✓ Configured for BLE: ${BLE_MAC}"
        else
            echo "✓ Configured for BLE (MAC address will be read from config)"
        fi
        ;;
esac

# Confirm with user
echo ""
echo "Configuration:"
echo "  User:              ${CURRENT_USER}"
echo "  Home Directory:    ${CURRENT_HOME}"
echo "  Project Directory: ${PROJECT_DIR}"
echo "  Python Executable: ${PYTHON_EXEC}"
echo "  Command Arguments: ${EXEC_ARGS}"
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
ExecStart=/usr/bin/screen -dmS ${SERVICE_NAME} ${PYTHON_EXEC} main.py ${EXEC_ARGS}

# Stop gracefully - send SIGINT (Ctrl+C) for immediate clean shutdown
ExecStop=/usr/bin/screen -S ${SERVICE_NAME} -X quit
KillSignal=SIGINT

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
