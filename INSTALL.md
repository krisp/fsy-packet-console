# FSY Packet Console - Installation Guide

This guide covers installation on Raspberry Pi OS (Debian-based systems). For other platforms, adapt the system package installation accordingly.

## Prerequisites

FSY Packet Console requires Python 3.7 or later and is designed to run on Raspberry Pi for APRS packet radio operations.

## Installation Methods

### Method 1: System Packages + Virtual Environment (Recommended for Raspberry Pi)

This method uses pre-compiled system packages where possible, avoiding slow compilation on the Pi's ARM processor.

#### Step 1: Install System Packages

```bash
# Update package lists
sudo apt-get update

# Install Python and core development tools
sudo apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    build-essential

# Install Python library system packages
sudo apt-get install -y \
    python3-bleak \
    python3-dbus-fast \
    python3-prompt-toolkit \
    python3-aiohttp \
    python3-yaml \
    python3-serial

# Install D-Bus development libraries (for Bluetooth support)
sudo apt-get install -y \
    libdbus-1-dev \
    bluez
```

**Note:** Some packages (like `python3-bleak` or `python3-dbus-fast`) may not be available on older Raspberry Pi OS versions. If installation fails, these will be installed via pip in Step 3.

#### Step 2: Create Virtual Environment with System Packages

```bash
# Navigate to the project directory
cd /path/to/fsy-packet-console

# Create virtual environment that inherits system packages
python3 -m venv ~/console-venv --system-site-packages

# Activate the virtual environment
source ~/console-venv/bin/activate
```

The `--system-site-packages` flag allows the venv to use pre-compiled system packages, significantly reducing installation time and avoiding compilation.

#### Step 3: Install Remaining Requirements

```bash
# Upgrade pip to latest version
pip install --upgrade pip

# Install any packages not available via apt
# This will skip packages already installed via system packages
pip install -r requirements.txt
```

#### Step 4: Verify Installation

```bash
# Check that all packages are available
python3 -c "import bleak; import prompt_toolkit; import aiohttp; import yaml; import serial_asyncio; print('All dependencies installed successfully!')"
```

### Method 2: Pure pip Installation (Development/Non-Pi Systems)

For development on x86_64 systems or if you prefer pip-only installation:

```bash
# Create a standard virtual environment
python3 -m venv ~/venv

# Activate the virtual environment
source ~/venv/bin/activate

# Install all dependencies via pip
pip install --upgrade pip
pip install -r requirements.txt
```

**Warning:** On Raspberry Pi, this method will compile `dbus-fast` from source, which can take 2-5 minutes.

## Configuration

### TNC Configuration

The console uses `~/.tnc_config.json` for TNC configuration. On first run, a default configuration will be created.

### KISS-over-TCP Mode (for testing on development machines)

You can run the console in KISS-over-TCP mode to connect to Direwolf or other virtual TNCs:

```bash
source ~/console-venv/bin/activate
python3 main.py
```

## Running the Console

```bash
# Activate the virtual environment (if not already active)
source onyx-venv/bin/activate

# Run the console
python3 main.py
```

On first startup, the console will:
- Create default configuration in `~/.tnc_config.json`
- Scan for Bluetooth TNC devices (BLE mode)
- Start the web UI server (default: http://localhost:8080)
- Start the AGWPE bridge (if enabled)

## Running as a System Service (Optional)

For production deployments, you can run the console as a systemd service that automatically starts on boot and runs in a screen session.

### Automated Setup (Recommended)

Use the installation script that automatically configures everything for your system:

```bash
# Run the installer (auto-detects user, paths, Python environment)
./install-service.sh
```

The installer will:
- Auto-detect your username and home directory
- Find your Python virtual environment (onyx-venv, venv, or console-venv)
- Create a customized service file with correct paths
- Install to /etc/systemd/system/
- Optionally enable auto-start and start the service

### Manual Setup

If you need to customize the service configuration:

```bash
# Edit the template service file
nano fsy-console.service

# Copy to systemd directory
sudo cp fsy-console.service /etc/systemd/system/

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable fsy-console.service
sudo systemctl start fsy-console.service
```

### Accessing the Console

When running as a service, the console runs in a screen session that you can attach to:

```bash
# Attach to the running console
screen -r fsy-console

# Detach without stopping (Ctrl+A, then D)
```

### Managing the Service

```bash
# Stop the service
sudo systemctl stop fsy-console.service

# Restart the service
sudo systemctl restart fsy-console.service

# View logs
journalctl -u fsy-console.service -f

# Disable auto-start
sudo systemctl disable fsy-console.service
```

**Benefits:**
- ✅ Auto-start on boot
- ✅ Auto-restart if the console crashes
- ✅ Centralized logging via journalctl
- ✅ Still interactive via screen session

See [SYSTEMD_SETUP.md](SYSTEMD_SETUP.md) for detailed configuration options and troubleshooting.

## Troubleshooting

### "Building wheel for dbus-fast" takes too long

If you see `dbus-fast` building from source despite following the system package method:

```bash
# Try installing the system package directly
sudo apt-get install python3-dbus-fast

# If that fails, install build dependencies to speed up compilation
sudo apt-get install python3-dev libdbus-1-dev build-essential
```

### ImportError for bleak or other packages

If you get import errors after installation:

```bash
# Verify system packages are installed
dpkg -l | grep python3-bleak
dpkg -l | grep python3-dbus-fast

# If missing, install via pip instead
pip install bleak dbus-fast
```

### Bluetooth permissions

If you encounter Bluetooth permission errors:

```bash
# Add your user to the bluetooth group
sudo usermod -a -G bluetooth $USER

# Log out and log back in for group changes to take effect
```

## Package Mapping Reference

This table shows the mapping between pip packages and Debian/Raspberry Pi OS system packages:

| pip Package          | System Package              | Notes                              |
|---------------------|-----------------------------|------------------------------------|
| `bleak`             | `python3-bleak`             | May not exist on older OS versions |
| `dbus-fast`         | `python3-dbus-fast`         | Dependency of bleak                |
| `prompt-toolkit`    | `python3-prompt-toolkit`    | Available on most versions         |
| `aiohttp`           | `python3-aiohttp`           | Available on most versions         |
| `PyYAML`            | `python3-yaml`              | Available on most versions         |
| `pyserial-asyncio`  | `python3-serial`            | Base serial package (close enough) |

## Next Steps

After installation:
- Run `python3 main.py` to start the console
- Use `help` command to see available commands
- Access the Web UI at http://localhost:8002
- Set your callsign with `mycall YOUR-CALL`
- Configure TNC settings via console commands
