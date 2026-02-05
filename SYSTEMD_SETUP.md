# FSY Packet Console - Systemd Service Setup

This guide explains how to set up the FSY Packet Console to run as a systemd service that automatically starts a screen session on boot.

## Quick Installation

The easiest way to install the service is using the provided installation script:

```bash
./install-service.sh
```

The script will:
- Auto-detect your Python environment (virtual environment or system Python)
- Auto-detect paths and user settings
- **Prompt for TNC transport configuration** (BLE, Serial KISS, or KISS-over-TCP)
- Create a customized service file with your specific transport settings
- Install it to `/etc/systemd/system/fsy-console.service`
- Optionally enable auto-start on boot
- Optionally start the service immediately

### Installation Example

```bash
$ ./install-service.sh
==========================================
FSY Packet Console - Service Installer
==========================================

✓ Found virtual environment: ~/venv

TNC Transport Configuration
===========================
Select your TNC connection type:
  1) BLE (Bluetooth Low Energy) - default
  2) Serial KISS TNC (e.g., /dev/ttyUSB0)
  3) KISS-over-TCP (e.g., Direwolf)

Select option [1-3, default: 1]: 3

Enter KISS TNC host (e.g., localhost or 192.168.1.100): localhost
Enter KISS TNC port [default: 8001]: 8001
✓ Configured for KISS-over-TCP: localhost:8001

Configuration:
  User:              dave
  Home Directory:    /home/dave
  Project Directory: /home/dave/mnt/fsy-packet-console
  Python Executable: /home/dave/venv/bin/python
  Command Arguments: -l -k localhost:8001

Install service with these settings? [y/N] y

Creating service file...
Installing service file to /etc/systemd/system/fsy-console.service...
Reloading systemd daemon...
✓ Service installed successfully!

Enable auto-start on boot? [y/N] y
✓ Auto-start enabled

Start the service now? [y/N] y
✓ Service started

Service status:
● fsy-console.service - FSY Packet Console - APRS TNC and Web UI
     Loaded: loaded (/etc/systemd/system/fsy-console.service; enabled; preset: enabled)
     Active: active (running) since Wed 2026-02-05 14:30:22 UTC; 1s ago
   Main PID: 54321 (screen)
      Tasks: 3 (limit: 4915)
     Memory: 52.3M
     CPU: 0.145s
   CGroup: /system.slice/fsy-console.service
           └─54321 /usr/bin/screen -dmS fsy-console /home/dave/venv/bin/python main.py -l -k localhost:8001

==========================================
Installation Complete!
==========================================

Useful commands:
  Start:   sudo systemctl start fsy-console.service
  Stop:    sudo systemctl stop fsy-console.service
  Status:  systemctl status fsy-console.service
  Logs:    journalctl -u fsy-console.service -f
  Attach:  screen -r fsy-console

See SYSTEMD_SETUP.md for detailed documentation.
```

### Transport Configuration Options

The installer will prompt you to choose your TNC connection method:

#### Option 1: BLE (Bluetooth Low Energy) - Default
- Automatically discovers and connects to Bluetooth radio
- MAC address optional (can be configured later in console)
- Best for portable/mobile setups

#### Option 2: Serial KISS TNC
- Direct serial connection (e.g., /dev/ttyUSB0)
- Requires TNC hardware with KISS protocol support
- Specify baud rate (default: 9600)

#### Option 3: KISS-over-TCP
- Remote TNC via network (e.g., Direwolf on another machine)
- Specify host and port
- Best for fixed installations with remote TNC server

## Basic Usage

### Check Service Status

```bash
systemctl status fsy-console.service
```

Output example:
```
● fsy-console.service - FSY Packet Console - APRS TNC and Web UI
     Loaded: loaded (/etc/systemd/system/fsy-console.service; enabled; preset: enabled)
     Active: active (running) since Mon 2026-02-03 10:15:00 UTC; 5s ago
   Main PID: 12345 (screen)
      Tasks: 2 (limit: 4915)
     Memory: 45.2M
     CPU: 1.234s
   CGroup: /system.slice/fsy-console.service
           └─12345 /usr/bin/screen -dmS fsy-console python main.py
```

### View Live Logs

```bash
# Follow logs in real-time
journalctl -u fsy-console.service -f

# View last 50 lines
journalctl -u fsy-console.service -n 50

# View logs since last boot
journalctl -u fsy-console.service -b
```

### Attach to Screen Session

To interact with the console directly:

```bash
screen -r fsy-console
```

Once attached, you can:
- View console output
- Type commands
- Use keyboard shortcuts (Ctrl+A for screen commands)

To detach from screen without stopping it:
```
Ctrl+A, then D
```

### Control Service

```bash
# Start the service
sudo systemctl start fsy-console.service

# Stop the service (graceful shutdown with 15-second timeout)
sudo systemctl stop fsy-console.service

# Restart the service
sudo systemctl restart fsy-console.service

# Enable auto-start on boot
sudo systemctl enable fsy-console.service

# Disable auto-start (but keep installed)
sudo systemctl disable fsy-console.service
```

## Graceful Shutdown

The service is configured with advanced graceful shutdown handling. Here's how it works:

### Shutdown Sequence

1. **systemctl stop** is called
2. **ExecStop** script runs:
   - Finds the Python process (child of screen)
   - Sends SIGTERM signal to Python (not screen)
3. **Python receives SIGTERM**:
   - Converts to SIGINT internally
   - Triggers KeyboardInterrupt handler
   - Initiates graceful shutdown
4. **Graceful shutdown performs** (30-second timeout):
   - **Bluetooth cleanup**: Properly disconnects from radio
   - **Database saving**: Flushes APRS station data to disk
   - **Frame buffer**: Saves frame history to file
   - **Port cleanup**: Releases AGWPE, TNC, and Web UI ports
   - **Resource cleanup**: Closes all connections and files
5. **Screen session persists** (important):
   - Screen itself is NOT terminated
   - Only the Python process exits
   - This prevents abrupt loss of connectivity
6. **Timeout enforcement** (`TimeoutStopSec=30`):
   - After 30 seconds, systemd forcibly kills remaining processes
   - Normally not needed (Python finishes in <5 seconds)
   - Acts as safety net for stuck cleanup operations

### Why This Approach?

The previous design (`ExecStop=/usr/bin/screen -S fsy-console -X quit`) would:
- ❌ Send SIGTERM to screen, not Python
- ❌ Force screen to exit immediately
- ❌ Abruptly kill Python without cleanup
- ❌ Leave ports bound, frame buffer unflushed

The new design:
- ✅ Sends signals to Python directly
- ✅ Allows SIGINT handler to run
- ✅ Completes all cleanup operations
- ✅ Safely releases all resources
- ✅ Screen exits cleanly after Python finishes

## Advanced Configuration

### If Service Fails to Start

Check the detailed error:
```bash
journalctl -u fsy-console.service -n 100
```

Common issues:
- **Working directory doesn't exist**: Re-run `./install-service.sh` from the correct location
- **Python not found**: Check that virtual environment still exists at the configured path
- **Screen not installed**: `sudo apt install screen`
- **Port conflicts**: Check if AGWPE (8000), TNC (8001), or Web UI (8002) ports are in use

### Reinstalling or Updating Service

If you move the project directory, change Python environments, or need to change your TNC transport configuration, simply re-run the installation script:

```bash
./install-service.sh
```

The script will:
1. Detect the new configuration (Python env, paths)
2. Prompt for transport mode again (BLE, Serial, TCP)
3. Create an updated service file
4. Reload systemd and restart the service

**Note**: The old service file will be replaced with the new configuration.

### Manual Service File Editing

If you need to make manual changes to the service file:

```bash
sudo systemctl edit --full fsy-console.service
```

After editing:
```bash
sudo systemctl daemon-reload
sudo systemctl restart fsy-console.service
```

### Running with Environment Variables

To add custom environment variables, edit the service file and add to the `[Service]` section:
```ini
Environment="PYTHONUNBUFFERED=1"
Environment="TZ=America/New_York"
```

## Screen Session Quick Reference

### List Active Screen Sessions

```bash
screen -ls
```

Output example:
```
There are screens on:
	12345.fsy-console	(Attached)
	67890.other		(Detached)
2 screens in total.
```

### Attach to Session

```bash
screen -r fsy-console
```

### Send Command to Screen Session (without attaching)

```bash
screen -S fsy-console -X stuff "help\n"
```

### Kill Screen Session

```bash
screen -S fsy-console -X quit
```

Or via systemd (recommended):
```bash
sudo systemctl stop fsy-console.service
```

## Troubleshooting

### Service Won't Start

1. Check status:
   ```bash
   systemctl status fsy-console.service
   ```

2. View logs:
   ```bash
   journalctl -u fsy-console.service -n 50
   ```

3. Verify the service file syntax:
   ```bash
   systemd-analyze verify fsy-console.service
   ```

4. Check file permissions:
   ```bash
   ls -la /etc/systemd/system/fsy-console.service
   ```

### Screen Session Not Found

The service creates a screen session named `fsy-console`. If it's not appearing:

1. Check if process is running:
   ```bash
   ps aux | grep python
   ```

2. Check service status:
   ```bash
   systemctl status fsy-console.service
   ```

3. Check if screen is installed:
   ```bash
   which screen
   ```

### Can't Attach to Screen

If you get "Cannot open your terminal '/dev/pts/X'" error:

1. Check your terminal:
   ```bash
   tty
   ```

2. Try attaching as the same user that runs the service:
   ```bash
   screen -r fsy-console
   ```

### Graceful Shutdown Takes Too Long

If you see timeout warnings during shutdown:

1. Check logs to see what's delaying shutdown:
   ```bash
   journalctl -u fsy-console.service | tail -50
   ```

2. If Bluetooth disconnection is slow, you may need to increase the timeout by editing the service file:
   ```ini
   TimeoutStopSec=30
   ```

## Logs and Monitoring

### Logging Features

The service is configured with automatic logging enabled (`-l` flag):
- Console output is sent to **both** the screen session AND a log file
- Log file location: `~/.local/share/fsy-packet-console/logs/` (by default)
- Journalctl captures all systemd/stdio output separately
- Dual logging provides redundancy and searchability

### Check Last 5 Minutes of Logs

```bash
journalctl -u fsy-console.service --since "5 minutes ago"
```

### Check Local Log File

```bash
# View logs from the local log file (with -l flag)
tail -f ~/.local/share/fsy-packet-console/logs/fsy-packet-console.log
```

### Combine Journalctl and Local Logs

For comprehensive debugging:
```bash
# Follow journalctl
journalctl -u fsy-console.service -f

# In another terminal, follow local logs
tail -f ~/.local/share/fsy-packet-console/logs/fsy-packet-console.log
```

### Monitor CPU and Memory Usage

```bash
# Real-time monitoring
watch -n 1 'systemctl status fsy-console.service'

# Or using ps
ps aux | grep [p]ython
```

### Export Logs to File

```bash
journalctl -u fsy-console.service > fsy-console-logs.txt
```

## Benefits of Using Systemd Service

✅ **Auto-start on boot**: Console starts automatically after system reboot
✅ **Auto-restart**: Restarts if process crashes (10-second delay prevents thrashing)
✅ **Graceful shutdown**: 30-second timeout with SIGINT-based cleanup
✅ **Transport flexibility**: Configure BLE, Serial KISS, or KISS-over-TCP at install time
✅ **Smart signal handling**: Sends SIGTERM directly to Python (not screen)
✅ **Logging enabled**: Console runs with `-l` flag for automatic log file output
✅ **Centralized logging**: All output goes to journalctl (searchable, timestamped)
✅ **Easy control**: Standard systemctl commands work seamlessly
✅ **Screen session**: Interactive access via `screen -r fsy-console`
✅ **System integration**: Works with system boot order (After=network.target)
✅ **Auto-configuration**: Installation script detects paths, Python env, and transport settings
✅ **Resource limits**: Pre-configured for high file descriptor and process counts
✅ **Process isolation**: Logging and syslog identification for easy troubleshooting

## Testing the Setup

### Test 1: Manual Start and Stop

```bash
sudo systemctl start fsy-console.service
sleep 2
systemctl status fsy-console.service
screen -ls | grep fsy-console
sudo systemctl stop fsy-console.service
```

### Test 2: Attach and Verify

```bash
sudo systemctl start fsy-console.service
screen -r fsy-console
# You should see the console output
# Press Ctrl+A then D to detach
```

### Test 3: Restart and Check Logs

```bash
sudo systemctl restart fsy-console.service
journalctl -u fsy-console.service -f
```

### Test 4: Graceful Shutdown

```bash
sudo systemctl stop fsy-console.service
journalctl -u fsy-console.service -n 20
# Should see messages about saving database, frame buffer, and disconnecting
```

## Removing the Service

If you want to completely remove the service:

```bash
# Stop the service
sudo systemctl stop fsy-console.service

# Disable auto-start
sudo systemctl disable fsy-console.service

# Remove the service file
sudo rm /etc/systemd/system/fsy-console.service

# Reload systemd
sudo systemctl daemon-reload
```

## Service Configuration Details

The installation script creates a service with these key features:

```ini
[Unit]
Description=FSY Packet Console - APRS TNC and Web UI
After=network.target
Wants=network-online.target

[Service]
Type=forking
User=<your-user>
WorkingDirectory=<project-directory>

# Start the console in a screen session with configured transport options
ExecStart=/usr/bin/screen -dmS fsy-console <python-path> main.py <options>

# Graceful shutdown: Find Python process (child of screen) and send SIGTERM
# Python's SIGTERM handler converts it to SIGINT internally for graceful cleanup
ExecStop=/bin/bash -c 'PYTHON_PID=$(pgrep --parent $MAINPID); [ -n "$PYTHON_PID" ] && kill -TERM $PYTHON_PID'

# Don't let systemd send signals - ExecStop handles it
# This prevents screen from receiving SIGTERM (which causes immediate exit)
KillMode=none

# Allow 30 seconds for graceful shutdown before SIGKILL
TimeoutStopSec=30

# Restart on failure
Restart=on-failure
RestartSec=10

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fsy-console

# Process limits
LimitNOFILE=65536
LimitNPROC=4096

[Install]
WantedBy=multi-user.target
```

### Configuration Details

**Type=forking**: Screen creates a background daemon process

**ExecStart with Transport Options**:
- `-l`: Always enables logging to file
- `-r <MAC>`: BLE mode with optional Bluetooth MAC address
- `-s <PORT> -b <BAUD>`: Serial KISS TNC mode (e.g., `/dev/ttyUSB0` @ 9600 baud)
- `-k <HOST>:<PORT>`: KISS-over-TCP mode (e.g., `localhost:8001` for Direwolf)

**ExecStop with Graceful Shutdown**:
- Finds Python child process of screen
- Sends SIGTERM (converted to SIGINT internally)
- Allows ordered shutdown without abrupt termination
- No screen-level signals sent (prevents forced exit)

**KillMode=none**:
- Systemd doesn't send any signals to the process group
- Only ExecStop command is used for shutdown
- Prevents screen from being forcibly terminated mid-cleanup

**TimeoutStopSec=30**:
- 30-second timeout for graceful shutdown (up from 15)
- Allows time for:
  - Bluetooth disconnect sequence
  - Database/frame buffer saving
  - Network connection cleanup
  - Port/resource cleanup

**Restart=on-failure**: Auto-restarts if console crashes with 10-second delay

**StandardOutput=journal & StandardError=journal**: All output goes to systemd journal (searchable)

**Process Limits**:
- `LimitNOFILE=65536`: Allow up to 65k open file descriptors (for frame buffer)
- `LimitNPROC=4096`: Allow up to 4k processes/threads per user

**After=network.target**: Waits for network before starting (important for KISS-over-TCP mode)

## Notes

- The service is created with your current user and paths automatically detected
- Working directory is set to the project location where you ran `install-service.sh`
- Python executable is auto-detected (prefers virtual environments)
- Logs are sent to systemd journal (viewable with `journalctl`)
- The service automatically restarts on failure with a 10-second delay
- Graceful shutdown includes 15-second timeout for clean Bluetooth disconnection
