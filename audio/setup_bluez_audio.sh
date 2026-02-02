#!/bin/bash
# Setup script for BlueZ audio support on Raspberry Pi
# This recreates the venv with access to system packages (for PyGObject)

set -e  # Exit on error

echo "======================================================================"
echo "BlueZ Audio Support Setup for UV-50PRO Console"
echo "======================================================================"
echo ""

# Step 1: Install system packages
echo "[1/5] Installing system packages..."
sudo apt install -y python3-gi python3-dev bluez pipewire

# Step 2: Backup requirements
echo "[2/5] Saving current pip packages..."
if [ -d "venv" ]; then
    source venv/bin/activate
    pip freeze > requirements_backup.txt 2>/dev/null || echo "# No packages" > requirements_backup.txt
    deactivate
fi

# Step 3: Recreate venv with system packages
echo "[3/5] Recreating virtual environment..."
rm -rf venv
python3 -m venv --system-site-packages venv

# Step 4: Reinstall packages
echo "[4/5] Reinstalling packages..."
source venv/bin/activate
pip install --upgrade pip

# Install pydbus (not available as system package)
pip install pydbus

# Reinstall other packages (if requirements exist)
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
elif [ -f "requirements_backup.txt" ]; then
    # Filter out packages that are now available from system
    grep -v "PyGObject\|gi\|cairo" requirements_backup.txt > requirements_filtered.txt || true
    if [ -s requirements_filtered.txt ]; then
        pip install -r requirements_filtered.txt
    fi
fi

# Step 5: Test
echo "[5/5] Testing pydbus..."
python3 -c "from pydbus import SystemBus; bus = SystemBus(); print('[SUCCESS] pydbus is working!')"

echo ""
echo "======================================================================"
echo "Setup complete!"
echo "======================================================================"
echo ""
echo "Test BlueZ audio discovery:"
echo "  python3 test_bluez_audio.py discover"
echo ""
