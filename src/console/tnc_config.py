"""TNC-2 style configuration management."""

import json
import os
import shutil

from prompt_toolkit import HTML

from src.aprs.geo_utils import maidenhead_to_latlon
from src.utils import (
    print_debug,
    print_error,
    print_header,
    print_info,
    print_pt,
)


class TNCConfig:
    """TNC-2 style configuration management."""

    def __init__(self, config_file=None):
        # Default to user's home directory
        if config_file is None:
            config_file = os.path.expanduser("~/.tnc_config.json")

        self.config_file = config_file
        self.legacy_file = "tnc_config.json"  # Old location in project directory

        self.settings = {
            "MYCALL": "NOCALL",
            "MYALIAS": "",
            "MYLOCATION": "",  # Maidenhead grid square (2-10 chars) for manual position
            "RADIO_MAC": "",  # Bluetooth MAC address for BLE radio (e.g., 38:D2:00:01:62:C2)
            "UNPROTO": "CQ",
            "DIGIPEAT": "OFF",
            "MONITOR": "ON",
            "DEBUGFRAMES": "OFF",
            "TXDELAY": "30",
            "RETRY": "3",
            "RETRY_FAST": "20",  # Fast retry timeout (seconds) for non-digipeated messages
            "RETRY_SLOW": "600",  # Slow retry timeout (seconds) for digipeated but not ACKed - 10 minutes
            "AUTO_ACK": "ON",  # Automatic ACK for APRS messages with IDs
            "BEACON": "OFF",
            "BEACON_INTERVAL": "10",
            "BEACON_PATH": "WIDE1-1",
            "BEACON_SYMBOL": "/[",
            "BEACON_COMMENT": "FSY Packet Console",
            "LAST_BEACON": "",  # Timestamp of last beacon sent (ISO format)
            "DEBUG_BUFFER": "10",  # Frame history buffer size in MB (or "OFF" for simple 10-frame mode)
            "AGWPE_HOST": "0.0.0.0",  # AGWPE bind address (0.0.0.0=all, 127.0.0.1=localhost)
            "AGWPE_PORT": "8000",  # AGWPE-compatible server port
            "TNC_HOST": "0.0.0.0",    # TNC bridge bind address (0.0.0.0=all, 127.0.0.1=localhost)
            "TNC_PORT": "8001",    # TNC TCP bridge port
            "WEBUI_HOST": "0.0.0.0",  # Web UI bind address (0.0.0.0=all, 127.0.0.1=localhost)
            "WEBUI_PORT": "8002",  # Web UI HTTP server port
            "WEBUI_PASSWORD": "",  # Password for POST API endpoints (empty = disabled)
            "WX_ENABLE": "OFF",  # Enable weather station integration
            "WX_BACKEND": "ecowitt",  # Backend type: ecowitt, davis, ambient, etc.
            "WX_ADDRESS": "",  # IP address (http) or serial port path (serial)
            "WX_PORT": "",  # Port number for network stations (blank = auto)
            "WX_INTERVAL": "300",  # Update interval in seconds (300 = 5 minutes)
            "WX_AVERAGE_WIND": "ON",  # Average wind over beacon interval (ON/OFF)
            "WXTREND": "0.3",  # Pressure tendency threshold in mb/hr for Zambretti (0.3 = ~1.0 mb in 3 hours)
        }
        self.load()

    def load(self):
        """Load configuration from file, with migration from legacy location."""
        try:
            # Check if we need to migrate from old location
            if not os.path.exists(self.config_file) and os.path.exists(self.legacy_file):
                print_info(f"Migrating config from {self.legacy_file} to {self.config_file}")
                try:
                    # Copy the file to new location
                    shutil.copy2(self.legacy_file, self.config_file)
                    print_info(f"Migration complete. You can safely delete {self.legacy_file}")
                except Exception as e:
                    print_error(f"Could not migrate config file: {e}")
                    print_info(f"Will use legacy file at {self.legacy_file}")
                    # Fall back to legacy file
                    self.config_file = self.legacy_file

            # Load from config file
            if os.path.exists(self.config_file):
                with open(self.config_file, "r") as f:
                    saved = json.load(f)
                    self.settings.update(saved)
                print_debug(
                    f"Loaded TNC config from {self.config_file}", level=6
                )
        except Exception as e:
            print_debug(f"Could not load TNC config: {e}", level=6)

    def save(self):
        """Save configuration to file."""
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.settings, f, indent=2)
            print_debug(f"Saved TNC config to {self.config_file}", level=6)
        except Exception as e:
            print_error(f"Could not save TNC config: {e}")

    def set(self, key, value):
        """Set a configuration value."""
        key = key.upper()
        if key in self.settings:
            # Validate MYLOCATION (Maidenhead grid square)
            if key == "MYLOCATION" and value:
                try:
                    # Test if valid grid square by converting to lat/lon
                    lat, lon = maidenhead_to_latlon(value)
                    # Verify we got sensible coordinates
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        print_error(f"Invalid grid square '{value}': coordinates out of range")
                        return False
                    print_info(f"MYLOCATION set to {value.upper()} ({lat:.6f}, {lon:.6f})")
                except ValueError as e:
                    print_error(f"Invalid grid square '{value}': {e}")
                    return False
                # Store in uppercase
                value = value.upper()

            # Validate port numbers
            if key in ["AGWPE_PORT", "TNC_PORT", "WEBUI_PORT"]:
                try:
                    port = int(value)
                    if not (1 <= port <= 65535):
                        print_error(f"Invalid port '{value}': must be between 1 and 65535")
                        return False
                    print_info(f"{key} set to {port} (restart required to take effect)")
                    value = str(port)
                except ValueError:
                    print_error(f"Invalid port '{value}': must be a number")
                    return False

            # Validate weather station backend
            if key == "WX_BACKEND":
                from src.weather_manager import WeatherStationManager
                backends = WeatherStationManager.list_backends()
                if value.lower() not in backends:
                    valid = ', '.join(backends.keys())
                    print_error(f"Invalid backend '{value}'. Valid: {valid}")
                    return False
                value = value.lower()

            # Validate weather station interval
            if key == "WX_INTERVAL":
                try:
                    interval = int(value)
                    if not (30 <= interval <= 3600):
                        print_error(f"Invalid interval '{value}': must be 30-3600 seconds")
                        return False
                except ValueError:
                    print_error(f"Invalid interval '{value}': must be a number")
                    return False

            # Validate weather station port
            if key == "WX_PORT" and value:
                try:
                    port = int(value)
                    if not (1 <= port <= 65535):
                        print_error(f"Invalid port '{value}': must be 1-65535")
                        return False
                except ValueError:
                    print_error(f"Invalid port '{value}': must be a number")
                    return False

            self.settings[key] = value
            self.save()
            return True
        return False

    def get(self, key):
        """Get a configuration value."""
        return self.settings.get(key.upper(), "")

    def display(self):
        """Display all settings."""
        print_header("TNC-2 Configuration")
        for key in sorted(self.settings.keys()):
            value = self.settings[key]
            if value:
                print_pt(HTML(f"<b>{key:12s}</b> {value}"))
            else:
                print_pt(HTML(f"<gray>{key:12s} (not set)</gray>"))
        print_pt("")

