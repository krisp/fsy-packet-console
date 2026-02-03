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
- Create a customized service file for your system
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

Configuration:
  User:              dave
  Home Directory:    /home/dave
  Project Directory: /home/dave/mnt/fsy-packet-console
  Python Executable: /home/dave/venv/bin/python

Install service with these settings? [y/N] y

Creating service file...
Installing service file to /etc/systemd/system/fsy-console.service...
Reloading systemd daemon...
✓ Service installed successfully!

Enable auto-start on boot? [y/N] y
✓ Auto-start enabled

Start the service now? [y/N] y
✓ Service started
```

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

The service is configured with a 15-second timeout for graceful shutdown (`TimeoutStopSec=15`). This is important for:
- **Bluetooth cleanup**: Allows time to properly disconnect from the radio
- **Database saving**: Ensures APRS station data is saved
- **Frame buffer**: Saves frame history to disk

When you stop the service, the console receives a SIGTERM signal and performs orderly shutdown before the connection is terminated.

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

If you move the project directory or change Python environments, simply re-run the installation script:

```bash
./install-service.sh
```

The script will detect the new configuration and update the service file automatically.

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

### Check Last 5 Minutes of Logs

```bash
journalctl -u fsy-console.service --since "5 minutes ago"
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
✅ **Auto-restart**: Restarts if process crashes
✅ **Graceful shutdown**: 15-second timeout for clean Bluetooth disconnection
✅ **Centralized logging**: Logs go to journalctl (searchable, timestamped)
✅ **Easy control**: Standard systemctl commands
✅ **Screen session**: Interactive access via `screen -r`
✅ **System integration**: Works with system boot order (After=network.target)
✅ **Auto-configuration**: Installation script detects paths and Python environment

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
ExecStart=/usr/bin/screen -dmS fsy-console <python-path> main.py
ExecStop=/usr/bin/screen -S fsy-console -X quit
TimeoutStopSec=15         # 15 seconds for graceful shutdown
Restart=on-failure
RestartSec=10
KillMode=process

[Install]
WantedBy=multi-user.target
```

Key points:
- **Type=forking**: Screen creates a background daemon process
- **TimeoutStopSec=15**: Allows time for Bluetooth cleanup before forced termination
- **Restart=on-failure**: Auto-restarts if console crashes
- **KillMode=process**: Only kills the main process (screen), not child processes
- **After=network.target**: Waits for network before starting (important for KISS-over-TCP mode)

## Notes

- The service is created with your current user and paths automatically detected
- Working directory is set to the project location where you ran `install-service.sh`
- Python executable is auto-detected (prefers virtual environments)
- Logs are sent to systemd journal (viewable with `journalctl`)
- The service automatically restarts on failure with a 10-second delay
- Graceful shutdown includes 15-second timeout for clean Bluetooth disconnection
