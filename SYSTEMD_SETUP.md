# FSY Packet Console - Systemd Service Setup

This guide explains how to set up the FSY Packet Console to run as a systemd service that automatically starts a screen session on boot.

## Installation Steps

### 1. Copy Service File to Systemd Directory

```bash
sudo cp fsy-console.service /etc/systemd/system/
```

### 2. Reload Systemd Daemon

```bash
sudo systemctl daemon-reload
```

### 3. Enable Service (Auto-start on Boot)

```bash
sudo systemctl enable fsy-console.service
```

### 4. Start the Service

```bash
sudo systemctl start fsy-console.service
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
     Active: active (running) since Mon 2026-02-02 10:15:00 UTC; 5s ago
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

### Stop the Service

```bash
sudo systemctl stop fsy-console.service
```

### Restart the Service

```bash
sudo systemctl restart fsy-console.service
```

### Disable Auto-start (but keep installed)

```bash
sudo systemctl disable fsy-console.service
```

## Advanced Configuration

### If Service Fails to Start

Check the detailed error:
```bash
journalctl -u fsy-console.service -n 100
```

Common issues:
- **Working directory doesn't exist**: Check `/home/dave/mnt/fsy-packet-console` path
- **User doesn't exist**: Change `User=dave` to your username
- **Python not found**: Use full path `/usr/bin/python3` instead of `python`
- **Screen not installed**: `sudo apt install screen`

### Running as Different User

Edit `/etc/systemd/system/fsy-console.service`:
```ini
User=your_username
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl restart fsy-console.service
```

### Running with Environment Variables

If you need custom environment variables (e.g., `PYTHONUNBUFFERED=1`):

Add to the `[Service]` section:
```ini
Environment="PYTHONUNBUFFERED=1"
Environment="TZ=America/New_York"
```

### Custom Python Environment

If using a virtual environment:

```ini
ExecStart=/usr/bin/screen -dmS fsy-console /home/dave/mnt/fsy-packet-console/venv/bin/python main.py
```

## Useful System Commands

### Auto-restart on Failure

The service is already configured to restart on failure:
```ini
Restart=on-failure
RestartSec=10
```

This means if the console crashes, systemd will restart it after 10 seconds.

### View System Boot Messages

```bash
dmesg | grep fsy-console
```

### List All Active Services

```bash
systemctl list-units --type=service --state=running
```

### Edit Service (Advanced)

To edit the service file directly:
```bash
sudo systemctl edit fsy-console.service
```

This opens an editor for custom modifications that persist over updates.

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

Or via systemd:
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

2. Try attaching from same user that started the service:
   ```bash
   sudo su - dave
   screen -r fsy-console
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
✅ **Centralized logging**: Logs go to journalctl (searchable, timestamped)
✅ **Easy control**: Standard systemctl commands
✅ **Screen session**: Interactive access via `screen -r`
✅ **Graceful shutdown**: Service manager handles cleanup
✅ **System integration**: Works with system boot order (After=network.target)

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

## Notes

- The service runs as user `dave`. Change this if using a different user.
- Working directory is `/home/dave/mnt/fsy-packet-console`. Update if path is different.
- The service file uses `Type=forking` because screen creates a background process.
- Logs are sent to systemd journal (viewable with `journalctl`).
- The service automatically restarts on failure with a 10-second delay.
