"""
FSY Packet Console - Command Processor and Main Application
"""

import asyncio
import base64
import gzip
import hashlib
import html
import json
import os
import re
import signal
import socket
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

# Try to use ujson for faster serialization (3-5x speedup)
try:
    import ujson
    HAS_UJSON = True
except ImportError:
    HAS_UJSON = False

from bleak import BleakClient, BleakScanner
from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from src import constants
from src.aprs_manager import APRSManager
from src.aprs.geo_utils import maidenhead_to_latlon
from src.aprs.formatters import APRSFormatters
from src.aprs.duplicate_detector import DuplicateDetector
from src.ax25_adapter import AX25Adapter
from src.device_id import get_device_identifier
from src.constants import *
from src.digipeater import Digipeater
from src.frame_analyzer import (
    decode_ax25_address,
    decode_control_byte,
    decode_aprs_info,
    decode_kiss_frame,
    format_frame_detailed,
    sanitize_for_xml as fa_sanitize_for_xml,
)
from src.commands.tnc_commands import TNCCommandHandler
from src.commands.beacon_commands import BeaconCommandHandler
from src.commands.weather_commands import WeatherCommandHandler
from src.commands.aprs_console_commands import APRSConsoleCommandHandler
from src.commands.debug_commands import DebugCommandHandler
from src.commands.radio_commands import RadioCommandHandler
from src.protocol import (
    build_iframe,
    decode_kiss_aprs,
    kiss_unwrap,
    parse_ax25_addresses_and_control,
    wrap_kiss,
)
from src.radio import RadioController
from src.tnc_bridge import TNCBridge
from src.utils import *
from src.web_server import WebServer

# Use sanitize_for_xml from frame_analyzer
sanitize_for_xml = fa_sanitize_for_xml


# Use decode_ax25_address from frame_analyzer (adds 'raw' key but otherwise compatible)
decode_ax25_address_field = decode_ax25_address


def decode_control_field(control):
    """
    Wrapper for decode_control_byte from frame_analyzer.
    Adapts the return value to match console.py's expected structure.
    """
    result = decode_control_byte(control)
    # Adapt frame_analyzer structure to console structure
    # frame_analyzer uses 'frame_type' and 'description', console expects 'type' and 'desc'
    result['type'] = result['frame_type']
    result['desc'] = result['description']
    return result


def decode_aprs_packet_type(info_str, dest_addr=None):
    """
    Wrapper for decode_aprs_info from frame_analyzer.
    Adapts the return value from nested structure to flat structure expected by console.

    frame_analyzer returns: {'type': 'X', 'details': {fields...}}
    console expects: {'type': 'X', fields...}
    """
    result = decode_aprs_info(info_str, dest_addr)

    # Flatten the structure by merging details into top level
    if 'details' in result:
        details = result.pop('details')
        result.update(details)

    return result




def format_detailed_frame(frame, index=1):
    """
    Wrapper for format_frame_detailed from frame_analyzer.
    Converts FrameHistoryEntry to the structure expected by format_frame_detailed,
    then adds device identification logic specific to console.py.

    Args:
        frame: FrameHistoryEntry object
        index: Frame number in sequence

    Returns:
        List of HTML-formatted strings for display
    """
    # Decode the KISS frame using frame_analyzer
    decoded = decode_kiss_frame(frame.raw_bytes)

    # Format timestamp
    time_str = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]

    # Use format_frame_detailed from frame_analyzer with HTML output
    lines = format_frame_detailed(
        decoded=decoded,
        frame_num=index,
        timestamp=time_str,
        direction=frame.direction,
        output_format='html'
    )

    # Add console-specific device identification if APRS data is present
    # Insert device info before hex dump section
    if decoded.get('aprs') and not 'error' in decoded:
        aprs = decoded['aprs']
        dest = decoded.get('ax25', {}).get('destination', {})
        dest_call = dest.get('callsign') if dest else None

        device_info = None
        try:
            device_id = get_device_identifier()

            # Try to identify by tocall (destination address) for normal APRS
            if dest_call:
                device_info = device_id.identify_by_tocall(dest_call)

            # For MIC-E, try to identify by comment suffix
            details = aprs.get('details', {})
            if not device_info and aprs.get('type') == 'APRS MIC-E Position' and 'comment' in details:
                device_info = device_id.identify_by_mice(details.get('comment', ''))

            if device_info:
                # Find where to insert (before hex dump)
                insert_index = len(lines)
                for i, line in enumerate(lines):
                    line_text = str(line) if hasattr(line, '__str__') else ''
                    if 'Hex Dump' in line_text:
                        insert_index = i
                        break

                # Create device info lines
                device_lines = [
                    HTML(f"\n<cyan><b>Device Identification</b></cyan>"),
                    HTML(f"  Vendor: <green>{sanitize_for_xml(device_info.vendor)}</green>"),
                    HTML(f"  Model: <green>{sanitize_for_xml(device_info.model)}</green>")
                ]

                if device_info.version:
                    device_lines.append(HTML(f"  Version: <green>{sanitize_for_xml(device_info.version)}</green>"))
                if device_info.class_type:
                    device_lines.append(HTML(f"  Class: <yellow>{sanitize_for_xml(device_info.class_type)}</yellow>"))

                # Show detection method
                if aprs.get('type') == 'APRS MIC-E Position':
                    device_lines.append(HTML(f"  <gray>(detected from MIC-E comment suffix)</gray>"))
                else:
                    device_lines.append(HTML(f"  <gray>(detected from destination: {sanitize_for_xml(dest_call)})</gray>"))

                # Insert at found position
                lines = lines[:insert_index] + device_lines + lines[insert_index:]

        except Exception:
            # Silently fail device detection - not critical
            pass

    return lines


def calculate_hop_count(addresses: List[str]) -> int:
    """Calculate hop count from AX.25 address path.

    Counts the number of digipeaters that have been used (marked with asterisk).
    In AX.25, digipeaters beyond the destination and source (addresses[2:])
    represent the path. Each used digipeater adds 1 hop.

    Args:
        addresses: List of callsigns from AX.25 header (includes destination, source, digipeaters)

    Returns:
        Number of hops (0 = direct RF, higher = more digipeaters used)
    """
    # Direct RF if no digipe path or only dst/src
    if len(addresses) <= 2:
        return 0

    # Count USED digipeaters in path (those marked with asterisk)
    # addresses[2:] contains the digipeater path
    # Only count digipeaters with * (the "used" bit set in AX.25)
    used_count = sum(1 for digi in addresses[2:] if digi.endswith('*'))
    return used_count


@dataclass
class FrameHistoryEntry:
    """Represents a captured frame for debugging."""

    timestamp: datetime
    direction: str  # 'RX' or 'TX'
    raw_bytes: bytes
    frame_number: int  # Sequential frame number

    def format_hex(self) -> str:
        """Format frame as hex dump."""
        hex_str = self.raw_bytes.hex()
        # Format as groups of 2 hex chars (bytes)
        formatted = " ".join(
            hex_str[i : i + 2] for i in range(0, len(hex_str), 2)
        )
        return formatted

    def format_ascii(self, chunk: bytes) -> str:
        """Format bytes as ASCII (printable chars or dots)."""
        return "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)

    def format_hex_lines(self) -> List[str]:
        """Format frame as hex editor style lines (hex + ASCII)."""
        lines = []
        for i in range(0, len(self.raw_bytes), 16):
            chunk = self.raw_bytes[i : i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            # Pad hex part to align ASCII column (16 bytes * 3 chars - 1 space = 47 chars)
            hex_part = hex_part.ljust(47)
            ascii_part = self.format_ascii(chunk)
            lines.append(f"  {hex_part}  {ascii_part}")
        return lines


class FrameHistory:
    """Tracks recent frames for debugging."""

    # File path for persistent storage
    BUFFER_FILE = os.path.expanduser("~/.console_frame_buffer.json.gz")
    AUTO_SAVE_INTERVAL = 100  # Save every N frames

    def __init__(self, max_size_mb: int = 10, buffer_mode: bool = True):
        self.buffer_mode = buffer_mode  # True = MB-based, False = simple 10-frame buffer
        self.frame_counter = 0  # Global frame counter (never resets)
        self.frames_since_save = 0  # Track frames added since last save

        if buffer_mode:
            self.frames = deque()  # No maxlen
            self.max_size_bytes = max_size_mb * 1024 * 1024  # Convert MB to bytes
            self.current_size_bytes = 0
        else:
            # Simple mode: just keep last 10 frames
            self.frames = deque(maxlen=10)
            self.max_size_bytes = 0
            self.current_size_bytes = 0

        # Async save lock to prevent concurrent saves
        self._save_lock = asyncio.Lock()
        self._last_save_time = 0  # Track last save for monitoring

        # Note: load_from_disk() called explicitly after creation to display load info

    def add_frame(self, direction: str, raw_bytes: bytes):
        """Add a frame to history.

        Args:
            direction: 'RX' or 'TX'
            raw_bytes: Complete KISS frame bytes
        """
        self.frame_counter += 1
        entry = FrameHistoryEntry(
            timestamp=datetime.now().astimezone(),  # Timezone-aware in local timezone
            direction=direction,
            raw_bytes=raw_bytes,
            frame_number=self.frame_counter
        )
        self.frames.append(entry)

        if self.buffer_mode:
            self.current_size_bytes += len(raw_bytes)
            # Remove old frames if we exceed size limit
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)

        # Auto-save periodically (async to avoid blocking)
        self.frames_since_save += 1
        if self.frames_since_save >= self.AUTO_SAVE_INTERVAL:
            # Trigger async save without blocking
            asyncio.create_task(self.save_to_disk_async())
            self.frames_since_save = 0

    def get_recent(self, count: int = None) -> List[FrameHistoryEntry]:
        """Get recent frames.

        Args:
            count: Number of frames to return (None = all)

        Returns:
            List of frames (most recent last)
        """
        if count is None:
            return list(self.frames)
        else:
            # Return last N frames
            return list(self.frames)[-count:]

    def get_by_number(self, frame_number: int) -> FrameHistoryEntry:
        """Get a specific frame by its number.

        Args:
            frame_number: Frame number to retrieve

        Returns:
            FrameHistoryEntry or None if not found
        """
        for frame in self.frames:
            if frame.frame_number == frame_number:
                return frame
        return None

    def set_buffer_mode(self, buffer_mode: bool, size_mb: int = 10):
        """Switch between buffer modes.

        Args:
            buffer_mode: True = MB-based, False = simple 10-frame
            size_mb: Size in MB for buffer mode
        """
        self.buffer_mode = buffer_mode

        if buffer_mode:
            # Convert to MB-based mode
            self.max_size_bytes = size_mb * 1024 * 1024
            # Recreate deque without maxlen
            old_frames = list(self.frames)
            self.frames = deque()
            for frame in old_frames:
                self.frames.append(frame)
            # Calculate current size
            self.current_size_bytes = sum(len(f.raw_bytes) for f in self.frames)
            # Trim if needed
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)
        else:
            # Convert to simple mode
            old_frames = list(self.frames)[-10:]  # Keep last 10
            self.frames = deque(old_frames, maxlen=10)
            self.max_size_bytes = 0
            self.current_size_bytes = 0

    def set_max_size_mb(self, size_mb: int):
        """Change buffer size limit.

        Args:
            size_mb: New size in MB
        """
        if self.buffer_mode:
            self.max_size_bytes = size_mb * 1024 * 1024
            # Trim if needed
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)

    async def save_to_disk_async(self):
        """Save frame buffer to disk asynchronously (non-blocking).

        Uses asyncio.to_thread to run the blocking save operation in a thread pool,
        preventing event loop blocking. Includes lock to prevent concurrent saves.
        """
        # Prevent concurrent saves
        if self._save_lock.locked():
            print_debug("Frame buffer save already in progress, skipping", level=3)
            return

        async with self._save_lock:
            save_start = time.time()
            try:
                # Run blocking save in thread pool
                await asyncio.to_thread(self.save_to_disk)
                save_duration = time.time() - save_start
                self._last_save_time = time.time()
                print_debug(f"Frame buffer saved asynchronously in {save_duration:.2f}s", level=3)
            except Exception as e:
                print_error(f"Async frame buffer save failed: {e}")

    def save_to_disk(self):
        """Save frame buffer to disk (compressed JSON format).

        Saves frames to ~/.console_frame_buffer.json.gz for persistence
        across restarts.

        Note: This is a blocking operation. Use save_to_disk_async() for non-blocking saves.
        """
        try:
            # Serialize frames to JSON-compatible format
            frames_data = []
            for frame in self.frames:
                frames_data.append({
                    'timestamp': frame.timestamp.isoformat(),
                    'direction': frame.direction,
                    'raw_bytes': base64.b64encode(frame.raw_bytes).decode('ascii'),
                    'frame_number': frame.frame_number
                })

            data = {
                'frame_counter': self.frame_counter,
                'buffer_mode': self.buffer_mode,
                'max_size_mb': self.max_size_bytes // (1024 * 1024) if self.buffer_mode else 0,
                'frames': frames_data,
                'saved_at': datetime.now(timezone.utc).isoformat()
            }

            # Write compressed JSON
            temp_file = self.BUFFER_FILE + ".tmp"
            with gzip.open(temp_file, 'wt', encoding='utf-8') as f:
                # Use ujson for 3-5x faster serialization if available
                if HAS_UJSON:
                    f.write(ujson.dumps(data, ensure_ascii=False))
                else:
                    json.dump(data, f)

            # Atomic rename
            os.replace(temp_file, self.BUFFER_FILE)

        except Exception as e:
            # Log error but don't disrupt operation
            print_error(f"Failed to save frame buffer: {type(e).__name__}: {e}")
            import traceback
            print_debug(traceback.format_exc(), level=3)

    def load_from_disk(self) -> dict:
        """Load frame buffer from disk if available.

        Restores frames from ~/.console_frame_buffer.json.gz to maintain
        debugging history across restarts.

        Returns:
            dict with keys: loaded (bool), frame_count (int), start_frame (int),
            file_size_kb (float), corrupted_frames (int)
        """
        result = {
            'loaded': False,
            'frame_count': 0,
            'start_frame': 0,
            'file_size_kb': 0.0,
            'corrupted_frames': 0
        }

        if not os.path.exists(self.BUFFER_FILE):
            return result

        try:
            # Get file size before loading
            result['file_size_kb'] = os.path.getsize(self.BUFFER_FILE) / 1024

            with gzip.open(self.BUFFER_FILE, 'rt', encoding='utf-8') as f:
                # Use ujson for faster deserialization if available
                if HAS_UJSON:
                    data = ujson.loads(f.read())
                else:
                    data = json.load(f)

            # Restore frame counter (important to maintain sequential numbering)
            self.frame_counter = data.get('frame_counter', 0)

            # Pre-compute local timezone once (optimization for legacy naive timestamps)
            local_tz = datetime.now(timezone.utc).astimezone().tzinfo

            # Restore frames
            corrupted = 0
            for frame_data in data.get('frames', []):
                try:
                    # Load timestamp and make timezone-aware if needed
                    ts = datetime.fromisoformat(frame_data['timestamp'])
                    if ts.tzinfo is None:
                        # Naive timestamp from old data - treat as local time, make aware
                        ts = ts.replace(tzinfo=local_tz)

                    entry = FrameHistoryEntry(
                        timestamp=ts,
                        direction=frame_data['direction'],
                        raw_bytes=base64.b64decode(frame_data['raw_bytes']),
                        frame_number=frame_data['frame_number']
                    )
                    self.frames.append(entry)

                    # Update size tracking for buffer mode
                    if self.buffer_mode:
                        self.current_size_bytes += len(entry.raw_bytes)

                except Exception:
                    # Skip corrupted frames but continue loading others
                    corrupted += 1
                    continue

            # Trim to current buffer size if needed
            trimmed = 0
            if self.buffer_mode:
                while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                    removed = self.frames.popleft()
                    self.current_size_bytes -= len(removed.raw_bytes)
                    trimmed += 1

            # Calculate start frame number (lowest frame in buffer)
            start_frame = self.frames[0].frame_number if self.frames else self.frame_counter

            result['loaded'] = True
            result['frame_count'] = len(self.frames)
            result['start_frame'] = start_frame
            result['corrupted_frames'] = corrupted

            return result

        except Exception as e:
            # If load fails (corrupted file, format change, etc.), start fresh
            print_error(f"Failed to load frame buffer: {type(e).__name__}: {e}")
            print_warning("Starting with empty frame buffer")
            import traceback
            print_debug(traceback.format_exc(), level=3)
            self.frames.clear()
            self.frame_counter = 0
            self.current_size_bytes = 0
            return result


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
                    import shutil
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
                from src.aprs_manager import APRSManager
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


class TNCCompleter(Completer):
    """Tab completion for TNC mode commands."""

    def get_completions(self, document, complete_event):
        """Generate completions for TNC commands.

        Args:
            document: Current document (input text)
            complete_event: Completion event

        Yields:
            Completion objects for matching TNC commands
        """
        text = document.text_before_cursor.upper()
        words = text.split()

        # TNC-2 commands
        tnc_commands = [
            "CONNECT",
            "DISCONNECT",
            "CONVERSE",
            "MYCALL",
            "MYALIAS",
            "MYLOCATION",
            "UNPROTO",
            "MONITOR",
            "AUTO_ACK",
            "BEACON",
            "DIGIPEATER",
            "DIGI",
            "RETRY",
            "RETRY_FAST",
            "RETRY_SLOW",
            "DISPLAY",
            "STATUS",
            "RESET",
            "HARDRESET",
            "POWERCYCLE",
            "DEBUGFRAMES",
            "AGWPE_HOST",
            "AGWPE_PORT",
            "TNC_HOST",
            "TNC_PORT",
            "WEBUI_HOST",
            "WEBUI_PORT",
            "WX_ENABLE",
            "WX_BACKEND",
            "WX_ADDRESS",
            "WX_PORT",
            "WX_INTERVAL",
            "WX_AVERAGE_WIND",
            "QUIT",
            "EXIT",
        ]

        if not words or (len(words) == 1 and not text.endswith(" ")):
            word = words[0] if words else ""
            for cmd in tnc_commands:
                if cmd.startswith(word):
                    yield Completion(
                        cmd,
                        start_position=-len(word),
                        display=cmd,
                        display_meta=self._get_tnc_help(cmd),
                    )

    def _get_tnc_help(self, cmd):
        """Get brief help for TNC command.

        Args:
            cmd: TNC command name

        Returns:
            Brief help string
        """
        help_text = {
            "CONNECT": "Connect to station",
            "DISCONNECT": "Disconnect from station",
            "CONVERSE": "Enter conversation mode",
            "MYCALL": "Set/show my callsign",
            "MYALIAS": "Set/show my alias",
            "MYLOCATION": "Set manual position (Maidenhead grid, e.g., FN31pr)",
            "RADIO_MAC": "Set Bluetooth MAC address for BLE radio (e.g., 38:D2:00:01:62:C2)",
            "UNPROTO": "Set unproto destination",
            "MONITOR": "Toggle monitor mode",
            "AUTO_ACK": "Auto-acknowledge APRS messages (ON/OFF)",
            "BEACON": "GPS beacon (ON/OFF/INTERVAL/PATH/SYMBOL/COMMENT/NOW)",
            "DIGIPEATER": "Digipeater mode (ON/OFF/STATUS) - repeats direct packets only",
            "DIGI": "Digipeater mode (ON/OFF/STATUS) - short alias",
            "RETRY": "Set max retry attempts (1-10)",
            "RETRY_FAST": "Fast retry timeout in seconds (5-300) for non-digipeated messages",
            "RETRY_SLOW": "Slow retry timeout in seconds (60-86400) for digipeated messages",
            "DISPLAY": "Toggle display mode",
            "STATUS": "Show TNC status",
            "RESET": "Reset TNC settings",
            "HARDRESET": "Hard reset radio",
            "POWERCYCLE": "Power cycle radio",
            "DEBUGFRAMES": "Toggle frame debugging",
            "AGWPE_HOST": "Set AGWPE bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "AGWPE_PORT": "Set AGWPE server port (default: 8000)",
            "TNC_HOST": "Set TNC bridge bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "TNC_PORT": "Set TNC bridge port (default: 8001)",
            "WEBUI_HOST": "Set Web UI bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "WEBUI_PORT": "Set Web UI port (default: 8002)",
            "WX_ENABLE": "Enable/disable weather station (ON/OFF)",
            "WX_BACKEND": "Set weather station backend (ecowitt, davis, etc.)",
            "WX_ADDRESS": "Set weather station IP or serial port",
            "WX_PORT": "Set weather station port (blank = auto)",
            "WX_INTERVAL": "Set update interval in seconds (30-3600)",
            "WX_AVERAGE_WIND": "Average wind over beacon interval (ON/OFF)",
            "QUIT": "Exit TNC mode",
            "EXIT": "Exit TNC mode",
        }
        return help_text.get(cmd, "")


class CommandCompleter(Completer):
    """Tab completion for radio console commands."""

    def __init__(self, command_processor):
        """Initialize with reference to command processor.

        Args:
            command_processor: CommandProcessor instance to get available commands
        """
        self.command_processor = command_processor

    def get_completions(self, document, complete_event):
        """Generate completions for the current input.

        Args:
            document: Current document (input text)
            complete_event: Completion event

        Yields:
            Completion objects for matching commands
        """
        text = document.text_before_cursor
        words = text.split()

        # If empty or just whitespace, show all commands
        if not words or (len(words) == 1 and not text.endswith(" ")):
            # Completing the first word (command)
            word = words[0] if words else ""

            # Get base commands
            commands = sorted(self.command_processor.commands.keys())

            # Mode-specific filtering
            if self.command_processor.console_mode == "aprs":
                # APRS mode: add APRS subcommands as top-level, hide radio commands
                aprs_subcommands = ["message", "msg", "station", "wx", "weather"]
                commands = sorted(set(commands + aprs_subcommands))

                # Hide radio-specific commands (keep "radio" for mode switching if BLE)
                radio_commands = ["status", "health", "vfo", "setvfo", "active", "dual",
                                "scan", "squelch", "volume", "channel", "list", "power",
                                "freq", "bss", "setbss", "poweron", "poweroff", "scan_ble",
                                "notifications"]
                commands = [c for c in commands if c not in radio_commands]

                # In serial mode, also hide the "radio" command (can't switch to radio mode)
                if self.command_processor.serial_mode:
                    commands = [c for c in commands if c != "radio"]

            elif self.command_processor.console_mode == "radio":
                # Radio mode: don't show APRS subcommands as top-level (keep "aprs" for mode switching)
                pass  # APRS subcommands stay hidden, full commands shown normally

            # Filter and yield matching commands
            for cmd in commands:
                if cmd.startswith(word.lower()):
                    yield Completion(
                        cmd,
                        start_position=-len(word),
                        display=cmd,
                        display_meta=self._get_command_help(cmd),
                    )

        # Special completion for multi-word commands
        elif len(words) >= 1:
            first_word = words[0].lower()

            # APRS command completions
            if first_word == "aprs":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    # Complete aprs subcommands
                    subcommands = [
                        "message",
                        "msg",
                        "wx",
                        "weather",
                        "position",
                        "pos",
                        "station",
                        "database",
                        "db",
                    ]
                    word = words[1] if len(words) == 2 else ""
                    for sub in subcommands:
                        if sub.startswith(word):
                            yield Completion(
                                sub, start_position=-len(word), display=sub
                            )
                elif len(words) >= 2:
                    subcmd = words[1].lower()
                    if subcmd in ("message", "msg"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete message actions
                            actions = ["read", "send", "clear", "monitor"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3:
                            action = words[2].lower()
                            if action == "monitor":
                                if len(words) == 3 or (
                                    len(words) == 4 and not text.endswith(" ")
                                ):
                                    # Complete monitor subactions
                                    subactions = ["list"]
                                    word = words[3] if len(words) == 4 else ""
                                    for subaction in subactions:
                                        if subaction.startswith(word):
                                            yield Completion(
                                                subaction,
                                                start_position=-len(word),
                                                display=subaction,
                                            )
                    elif subcmd in ("wx", "weather"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete wx actions
                            actions = ["list"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3:
                            # Complete sort options for "aprs wx list"
                            action = words[2].lower()
                            if action == "list":
                                if len(words) == 3 or (
                                    len(words) == 4 and not text.endswith(" ")
                                ):
                                    sort_options = [
                                        "last",
                                        "name",
                                        "temp",
                                        "humidity",
                                        "pressure",
                                    ]
                                    word = words[3] if len(words) == 4 else ""
                                    for option in sort_options:
                                        if option.startswith(word):
                                            # Add descriptive meta text
                                            meta = {
                                                "last": "Most recent first",
                                                "name": "Alphabetically by callsign",
                                                "temp": "Highest temperature first",
                                                "humidity": "Highest humidity first",
                                                "pressure": "Highest pressure first",
                                            }.get(option, "")
                                            yield Completion(
                                                option,
                                                start_position=-len(word),
                                                display=option,
                                                display_meta=meta,
                                            )
                    elif subcmd in ("position", "pos"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete position actions
                            actions = ["list"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                    elif subcmd == "station":
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete station actions
                            actions = ["list", "show"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3 and words[2].lower() == "show":
                            # Complete with known station callsigns
                            if len(words) == 3 or (
                                len(words) == 4 and not text.endswith(" ")
                            ):
                                word = words[3] if len(words) == 4 else ""
                                stations = (
                                    self.command_processor.aprs_manager.get_all_stations()
                                )
                                for station in stations:
                                    if station.callsign.lower().startswith(
                                        word.lower()
                                    ):
                                        yield Completion(
                                            station.callsign,
                                            start_position=-len(word),
                                            display=station.callsign,
                                        )
                        elif len(words) >= 3 and words[2].lower() == "list":
                            # Complete sort order options for station list
                            if len(words) == 3 or (
                                len(words) == 4 and not text.endswith(" ")
                            ):
                                word = words[3] if len(words) == 4 else ""
                                sort_options = [
                                    (
                                        "name",
                                        "Sort alphabetically by callsign",
                                    ),
                                    (
                                        "packets",
                                        "Sort by packet count (highest first)",
                                    ),
                                    (
                                        "last",
                                        "Sort by last heard (most recent first)",
                                    ),
                                    (
                                        "hops",
                                        "Sort by hop count (direct RF first)",
                                    ),
                                ]
                                for option, meta in sort_options:
                                    if option.startswith(word.lower()):
                                        yield Completion(
                                            option,
                                            start_position=-len(word),
                                            display=option,
                                            display_meta=meta,
                                        )
                    elif subcmd in ("database", "db"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete database actions
                            actions = ["clear", "prune"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )

            # APRS subcommands as top-level commands (in APRS mode)
            # Handle "message ?", "station ?", etc. when used without "aprs" prefix
            elif self.command_processor.console_mode == "aprs" and first_word in ("message", "msg", "station", "wx", "weather"):
                # Redirect to the same logic as "aprs <subcommand>"
                # Treat first_word as if it were the second word after "aprs"
                subcmd = first_word

                if subcmd in ("message", "msg"):
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete message actions
                        actions = ["read", "send", "clear", "monitor"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                meta = {
                                    "read": "Read messages addressed to you",
                                    "send": "Send APRS message to callsign",
                                    "clear": "Clear read messages",
                                    "monitor": "View all monitored messages"
                                }.get(action, "")
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta=meta,
                                )
                    elif len(words) >= 2:
                        action = words[1].lower()
                        if action == "monitor":
                            if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                                # Complete monitor subactions
                                subactions = ["list"]
                                word = words[2] if len(words) == 3 else ""
                                for subaction in subactions:
                                    if subaction.startswith(word):
                                        yield Completion(
                                            subaction,
                                            start_position=-len(word),
                                            display=subaction,
                                            display_meta="List all monitored messages",
                                        )

                elif subcmd in ("wx", "weather"):
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete wx actions
                        actions = ["list"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta="List weather stations",
                                )
                    elif len(words) >= 2:
                        # Complete sort options for "wx list"
                        action = words[1].lower()
                        if action == "list":
                            if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                                sort_options = ["last", "name", "temp", "humidity", "pressure"]
                                word = words[2] if len(words) == 3 else ""
                                for option in sort_options:
                                    if option.startswith(word):
                                        meta = {
                                            "last": "Most recent first",
                                            "name": "Alphabetically by callsign",
                                            "temp": "Highest temperature first",
                                            "humidity": "Highest humidity first",
                                            "pressure": "Highest pressure first",
                                        }.get(option, "")
                                        yield Completion(
                                            option,
                                            start_position=-len(word),
                                            display=option,
                                            display_meta=meta,
                                        )

                elif subcmd == "station":
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete station actions
                        actions = ["list", "show"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                meta = {
                                    "list": "List all heard stations",
                                    "show": "Show detailed station info",
                                }.get(action, "")
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta=meta,
                                )
                    elif len(words) >= 2 and words[1].lower() == "show":
                        # Complete with known station callsigns
                        if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                            word = words[2] if len(words) == 3 else ""
                            stations = self.command_processor.aprs_manager.get_all_stations()
                            for station in stations:
                                if station.callsign.lower().startswith(word.lower()):
                                    yield Completion(
                                        station.callsign,
                                        start_position=-len(word),
                                        display=station.callsign,
                                    )
                    elif len(words) >= 2 and words[1].lower() == "list":
                        # Complete sort options for "station list"
                        if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                            sort_options = ["last", "name", "packets", "hops"]
                            word = words[2] if len(words) == 3 else ""
                            for option in sort_options:
                                if option.startswith(word):
                                    meta = {
                                        "last": "Most recent first",
                                        "name": "Alphabetically by callsign",
                                        "packets": "Most packets first",
                                        "hops": "Fewest hops first",
                                    }.get(option, "")
                                    yield Completion(
                                        option,
                                        start_position=-len(word),
                                        display=option,
                                        display_meta=meta,
                                    )

            # VFO completions
            elif first_word in ("vfo", "setvfo"):
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    vfos = ["a", "b"]
                    word = words[1] if len(words) == 2 else ""
                    for vfo in vfos:
                        if vfo.startswith(word.lower()):
                            yield Completion(
                                vfo.upper(),
                                start_position=-len(word),
                                display=vfo.upper(),
                            )

            # Power completions
            elif first_word == "power":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    levels = ["high", "medium", "low"]
                    word = words[1] if len(words) == 2 else ""
                    for level in levels:
                        if level.startswith(word.lower()):
                            yield Completion(
                                level, start_position=-len(word), display=level
                            )

            # Debug level completions
            elif first_word == "debug":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    level_meta = {
                        "0": "Off (no debug output)",
                        "1": "TNC monitor",
                        "2": "Critical errors and events",
                        "3": "Connection state changes",
                        "4": "Frame transmission/reception",
                        "5": "Protocol details, retransmissions",
                        "6": "Everything (BLE, config, hex dumps)",
                        "dump": "Dump frame history",
                        "filter": "Show/set station-specific debug filters",
                    }
                    # Add 'dump' and 'filter' to completions
                    options = ["0", "1", "2", "3", "4", "5", "6", "dump", "filter"]
                    word = words[1] if len(words) == 2 else ""
                    for option in options:
                        if option.startswith(word.lower()):
                            yield Completion(
                                option,
                                start_position=-len(word),
                                display=option,
                                display_meta=level_meta[option],
                            )
                elif len(words) >= 2 and words[1].lower() == "dump":
                    # After "debug dump", suggest "brief", "detail", or "watch"
                    if len(words) == 2 or (
                        len(words) >= 3 and not text.endswith(" ")
                    ):
                        word = words[-1] if len(words) >= 3 else ""
                        if "brief".startswith(word.lower()):
                            yield Completion(
                                "brief",
                                start_position=-len(word),
                                display="brief",
                                display_meta="compact hex output",
                            )
                        if "detail".startswith(word.lower()):
                            yield Completion(
                                "detail",
                                start_position=-len(word),
                                display="detail",
                                display_meta="Wireshark-style protocol analysis",
                            )
                        if "watch".startswith(word.lower()):
                            yield Completion(
                                "watch",
                                start_position=-len(word),
                                display="watch",
                                display_meta="live frame analysis (ESC to exit)",
                            )
                elif len(words) >= 2 and words[1].lower() == "filter":
                    # After "debug filter", suggest "clear"
                    if len(words) == 2 or (
                        len(words) == 3 and not text.endswith(" ")
                    ):
                        word = words[2] if len(words) == 3 else ""
                        if "clear".startswith(word.lower()):
                            yield Completion(
                                "clear",
                                start_position=-len(word),
                                display="clear",
                                display_meta="Clear all station filters",
                            )

            # PWS (Personal Weather Station) completions
            elif first_word == "pws":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    subcommands_meta = {
                        "show": "Display current weather data",
                        "fetch": "Fetch fresh weather data now",
                        "connect": "Connect to weather station",
                        "disconnect": "Disconnect from weather station",
                        "test": "Test connection to weather station",
                    }
                    word = words[1] if len(words) == 2 else ""
                    for subcmd, meta in subcommands_meta.items():
                        if subcmd.startswith(word.lower()):
                            yield Completion(
                                subcmd,
                                start_position=-len(word),
                                display=subcmd,
                                display_meta=meta,
                            )

            # TNC command completions
            elif first_word == "tnc":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    # TNC-2 configuration commands
                    subcommands_meta = {
                        "display": "Show all TNC parameters",
                        "mycall": "Set your callsign",
                        "myalias": "Set your alias",
                        "mylocation": "Set Maidenhead grid square",
                        "connect": "Connect to station",
                        "disconnect": "Disconnect current connection",
                        "conv": "Enter conversation mode",
                        "unproto": "Set unproto destination",
                        "monitor": "Enable/disable packet monitoring",
                        "auto_ack": "Enable/disable auto ACK",
                        "retry": "Set retry count",
                        "retry_fast": "Set fast retry timeout",
                        "retry_slow": "Set slow retry timeout",
                        "digipeater": "Enable/disable digipeater",
                        "debug_buffer": "Set debug buffer size",
                        "status": "Show TNC status",
                        "reset": "Reset TNC settings",
                        "hardreset": "Hard reset (factory defaults)",
                        "powercycle": "Power cycle radio",
                        "tncsend": "Send raw hex to TNC",
                    }
                    word = words[1] if len(words) == 2 else ""
                    for subcmd, meta in subcommands_meta.items():
                        if subcmd.startswith(word.lower()):
                            yield Completion(
                                subcmd,
                                start_position=-len(word),
                                display=subcmd,
                                display_meta=meta,
                            )

    def _get_command_help(self, cmd):
        """Get brief help text for a command.

        Args:
            cmd: Command name

        Returns:
            Brief help string
        """
        help_text = {
            "help": "Show available commands",
            "status": "Show radio status",
            "health": "Show radio health",
            "notifications": "Toggle notifications",
            "vfo": "Select VFO (A/B)",
            "setvfo": "Set VFO frequency",
            "active": "Set active channel",
            "dual": "Toggle dual watch",
            "scan": "Toggle scan mode",
            "squelch": "Set squelch level",
            "volume": "Set volume level",
            "bss": "Show BSS status",
            "setbss": "Set BSS user ID",
            "poweron": "Power on radio",
            "poweroff": "Power off radio",
            "power": "Set TX power",
            "channel": "Show channel info",
            "list": "List channels",
            "freq": "Show/set frequency",
            "dump": "Dump config/status",
            "debug": "Set debug level (0-6), filter by station, or dump frames (dump/filter)",
            "tncsend": "Send TNC data",
            "aprs": "APRS commands / Switch to APRS mode",
            "radio": "Radio commands / Switch to radio mode",
            "scan_ble": "Scan BLE characteristics",
            "tnc": "Enter TNC mode",
            "quit": "Exit console",
            "exit": "Exit console",
            # APRS subcommands (when shown as top-level in APRS mode)
            "message": "APRS messaging",
            "msg": "APRS messaging (alias for message)",
            "station": "Station database",
            "wx": "Weather stations",
            "weather": "Weather stations (alias for wx)",
            "pws": "Personal Weather Station",
        }
        return help_text.get(cmd, "")


class CommandProcessor:
    def __init__(self, radio, serial_mode=False):
        self.radio = radio
        self.serial_mode = serial_mode  # True if using serial TNC (no radio control)
        self.console_mode = "aprs" if serial_mode else "radio"  # Start in APRS mode for serial

        self.commands = {
            "help": self.cmd_help,
            "status": self._dispatch_radio_command,  # Uses radio handler
            "health": self._dispatch_radio_command,  # Uses radio handler
            "notifications": self._dispatch_radio_command,  # Uses radio handler
            "vfo": self._dispatch_radio_command,  # Uses radio handler
            "setvfo": self._dispatch_radio_command,  # Uses radio handler
            "active": self._dispatch_radio_command,  # Uses radio handler
            "dual": self._dispatch_radio_command,  # Uses radio handler
            "scan": self._dispatch_radio_command,  # Uses radio handler
            "squelch": self._dispatch_radio_command,  # Uses radio handler
            "volume": self._dispatch_radio_command,  # Uses radio handler
            "bss": self._dispatch_radio_command,  # Uses radio handler
            "setbss": self._dispatch_radio_command,  # Uses radio handler
            "poweron": self._dispatch_radio_command,  # Uses radio handler
            "poweroff": self._dispatch_radio_command,  # Uses radio handler
            "channel": self._dispatch_radio_command,  # Uses radio handler
            "list": self._dispatch_radio_command,  # Uses radio handler
            "power": self._dispatch_radio_command,  # Uses radio handler
            "freq": self._dispatch_radio_command,  # Uses radio handler
            "dump": self._dispatch_radio_command,  # Uses radio handler
            "debug": self._dispatch_debug_command,  # Uses debug handler
            "tncsend": self._dispatch_tnc_command,  # Uses TNC handler
            "aprs": self._dispatch_aprs_command,  # Uses APRS handler
            "pws": self._dispatch_pws_command,  # Uses weather handler
            "scan_ble": self._dispatch_radio_command,  # Uses radio handler
            "tnc": self.cmd_tnc,
            "quit": self.cmd_quit,
            "exit": self.cmd_quit,
        }
        # TNC configuration and state
        self.tnc_config = TNCConfig()
        self.tnc_connected_to = None
        self.tnc_mode = False
        self.tnc_conversation_mode = (
            False  # Track if in conversation mode (vs command mode)
        )
        self.tnc_debug_frames = False
        self._original_debug_state = (
            None  # Save original DEBUG state for restoration
        )
        self._tnc_text_buffer = (
            ""  # Buffer for accumulating text across frames
        )

        # APRS message and weather tracking
        # Use existing APRS manager if already created (e.g., by web server)
        # Otherwise create a new one
        if hasattr(self.radio, 'aprs_manager') and self.radio.aprs_manager:
            self.aprs_manager = self.radio.aprs_manager
            # Update retry config from TNC config if it changed
            retry_count = int(self.tnc_config.get("RETRY") or "3")
            retry_fast = int(self.tnc_config.get("RETRY_FAST") or "20")
            retry_slow = int(self.tnc_config.get("RETRY_SLOW") or "600")
            self.aprs_manager.max_retries = retry_count
            self.aprs_manager.retry_fast = retry_fast
            self.aprs_manager.retry_slow = retry_slow
        else:
            mycall = self.tnc_config.get("MYCALL") or "NOCALL"
            retry_count = int(self.tnc_config.get("RETRY") or "3")
            retry_fast = int(self.tnc_config.get("RETRY_FAST") or "20")
            retry_slow = int(self.tnc_config.get("RETRY_SLOW") or "600")
            self.aprs_manager = APRSManager(mycall, max_retries=retry_count,
                                           retry_fast=retry_fast, retry_slow=retry_slow)
            # Attach to radio so tnc_monitor() can access it
            self.radio.aprs_manager = self.aprs_manager

        # Frame history for debugging
        debug_buffer_setting = self.tnc_config.get("DEBUG_BUFFER") or "10"
        if debug_buffer_setting.upper() == "OFF":
            self.frame_history = FrameHistory(buffer_mode=False)
            load_info = self.frame_history.load_from_disk()
            if load_info['loaded']:
                print_info(f"Frame buffer: Simple mode (last 10 frames), loaded {load_info['frame_count']} frames")
                print_info(f"  Starting at frame #{load_info['start_frame']}, next frame will be #{self.frame_history.frame_counter + 1}")
            else:
                print_info("Frame buffer: Simple mode (last 10 frames), starting fresh at frame #1")
        else:
            debug_buffer_mb = int(debug_buffer_setting)
            self.frame_history = FrameHistory(max_size_mb=debug_buffer_mb, buffer_mode=True)
            load_info = self.frame_history.load_from_disk()
            if load_info['loaded']:
                size_kb = load_info['file_size_kb']
                print_info(f"Frame buffer: {debug_buffer_mb} MB buffer, loaded {load_info['frame_count']} frames ({size_kb:.1f} KB)")
                print_info(f"  Starting at frame #{load_info['start_frame']}, next frame will be #{self.frame_history.frame_counter + 1}")
                if load_info['corrupted_frames'] > 0:
                    print_warning(f"  Skipped {load_info['corrupted_frames']} corrupted frames during load")
            else:
                print_info(f"Frame buffer: {debug_buffer_mb} MB buffer, starting fresh at frame #1")

        # Run database migrations (after APRS manager and frame history are initialized)
        from src.migrations import run_startup_migrations
        run_startup_migrations(self.aprs_manager, self)

        # GPS state
        self.gps_position = None  # Current GPS position from radio
        self.gps_locked = False  # GPS lock status

        # Initialize weather station manager
        from src.weather_manager import WeatherStationManager
        self.weather_manager = WeatherStationManager()

        # Configure from saved settings
        backend = self.tnc_config.get("WX_BACKEND")
        address = self.tnc_config.get("WX_ADDRESS")
        port_str = self.tnc_config.get("WX_PORT")
        interval_str = self.tnc_config.get("WX_INTERVAL") or "300"
        enabled = self.tnc_config.get("WX_ENABLE") == "ON"

        port = int(port_str) if port_str else None
        interval = int(interval_str) if interval_str else 300

        self.weather_manager.configure(
            backend=backend if backend else None,
            address=address if address else None,
            port=port,
            enabled=enabled,
            update_interval=interval
        )

        # Configure wind averaging
        average_wind = self.tnc_config.get("WX_AVERAGE_WIND") == "ON"
        self.weather_manager.average_wind = average_wind

        # Load last beacon time from config
        last_beacon_str = self.tnc_config.get("LAST_BEACON")
        if last_beacon_str:
            try:
                self.last_beacon_time = datetime.fromisoformat(last_beacon_str)
                print_debug(f"Loaded last beacon time: {self.last_beacon_time}", level=6)
            except (ValueError, TypeError):
                self.last_beacon_time = None
        else:
            self.last_beacon_time = None

        self.gps_poll_task = None  # Background GPS polling task
        # Attach to radio so frame hooks can access it
        self.radio.cmd_processor = self

        # AX.25 adapter - use shared adapter if available, otherwise create new one
        try:
            if hasattr(self.radio, "shared_ax25") and self.radio.shared_ax25:
                # Use the shared adapter (created in main() for AGWPE compatibility)
                self.ax25 = self.radio.shared_ax25
                print_debug(
                    "CommandProcessor: Using shared AX25Adapter instance",
                    level=6,
                )
            else:
                # Create new adapter (fallback for standalone use)
                self.ax25 = AX25Adapter(
                    self.radio,
                    get_mycall=lambda: self.tnc_config.get("MYCALL"),
                    get_txdelay=lambda: self.tnc_config.get("TXDELAY"),
                )
                print_debug(
                    "CommandProcessor: Created new AX25Adapter instance",
                    level=6,
                )

            # Register callback to display received data
            self.ax25.register_callback(self._tnc_receive_callback)
            try:
                self.radio.register_kiss_callback(self.ax25.handle_incoming)
            except Exception:
                pass
        except Exception as e:
            print_error(f"Failed to initialize AX25Adapter: {e}")
            sys.exit(1)

        # Initialize command handlers
        self.tnc_handler = TNCCommandHandler(self)
        self.beacon_handler = BeaconCommandHandler(self)
        self.weather_handler = WeatherCommandHandler(self)
        self.aprs_console_handler = APRSConsoleCommandHandler(self)
        self.debug_handler = DebugCommandHandler(self)
        self.radio_handler = RadioCommandHandler(self)

    async def _broadcast_mylocation_to_web(self, grid_square: str) -> bool:
        """
        Broadcast MYLOCATION grid square to web UI as GPS position.

        Converts Maidenhead grid square to lat/lon and sends GPS update
        to connected web clients via WebSocket.

        Args:
            grid_square: Maidenhead grid square (e.g., 'FN42pr')

        Returns:
            True if broadcast succeeded, False otherwise
        """
        try:
            from src.aprs_manager import APRSManager
            lat, lon = maidenhead_to_latlon(grid_square)
            if self.aprs_manager._web_broadcast:
                await self.aprs_manager._web_broadcast('gps_update', {
                    'latitude': lat,
                    'longitude': lon,
                    'altitude': None,
                    'locked': True,
                    'source': 'MYLOCATION'
                })
            return True
        except Exception as e:
            print_debug(f"Failed to broadcast MYLOCATION: {e}", level=6)
            return False

    async def _dispatch_aprs_command(self, args):
        """Dispatch APRS command to handler."""
        await self.aprs_console_handler.aprs(args)

    async def _dispatch_debug_command(self, args):
        """Dispatch DEBUG command to handler."""
        await self.debug_handler.debug(args)

    async def _dispatch_pws_command(self, args):
        """Dispatch PWS (Personal Weather Station) command to handler."""
        await self.weather_handler.pws(args)

    async def _dispatch_radio_command(self, args):
        """Dispatch radio control command to handler."""
        # Get the command name from the call stack
        import inspect
        frame = inspect.currentframe()
        # Walk up the stack to find the command dispatcher
        caller_frame = frame.f_back
        # Get the command being executed from process() method
        # The command name will be in the local variables of process()
        while caller_frame:
            if 'cmd' in caller_frame.f_locals:
                cmd = caller_frame.f_locals['cmd']
                break
            caller_frame = caller_frame.f_back

        # Dispatch to radio handler
        await self.radio_handler.dispatch(cmd.upper(), args)

    async def _dispatch_tnc_command(self, args):
        """Dispatch TNCSEND command to TNC handler."""
        await self.tnc_handler.tncsend(args)

    async def process(self, line):
        """Process a command line with mode-aware dispatching."""
        parts = line.strip().split()
        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        # Handle mode switching commands
        if cmd == "aprs":
            if not args:
                # Switch to APRS mode
                self.console_mode = "aprs"
                print_info("Switched to APRS mode (APRS commands no longer need 'aprs' prefix)")
                return
            # Fall through to handle "aprs" command with subcommands

        elif cmd == "radio":
            if not args:
                # Switch to radio mode
                if self.serial_mode:
                    print_error("Radio mode not available in serial mode (no radio control)")
                    return
                self.console_mode = "radio"
                print_info("Switched to radio mode (radio commands no longer need 'radio' prefix)")
                return
            # Fall through to handle "radio" prefix in APRS mode

        # Mode-aware command routing
        if self.console_mode == "aprs":
            # In APRS mode:
            # - APRS subcommands work without "aprs" prefix
            # - Radio commands need "radio" prefix (if not in serial mode)

            # Check if it's an APRS subcommand without prefix
            aprs_subcommands = ["message", "msg", "station", "wx", "weather"]
            if cmd in aprs_subcommands:
                # Rewrite as "aprs <subcommand> ..."
                cmd = "aprs"
                args = [parts[0]] + args  # Prepend original command as first arg

            # Handle "radio" prefix for radio commands
            elif cmd == "radio" and args:
                if self.serial_mode:
                    print_error("Radio commands not available in serial mode")
                    return
                # Remove "radio" prefix and dispatch
                cmd = args[0].lower()
                args = args[1:]

        elif self.console_mode == "radio":
            # In radio mode:
            # - Radio commands work without prefix
            # - "aprs" prefix required for APRS commands (handled normally)
            pass

        # Dispatch command
        if cmd in self.commands:
            try:
                await self.commands[cmd](args)
            except Exception as e:
                print_error(f"Command failed: {e}")
                import traceback
                traceback.print_exc()
        else:
            if self.console_mode == "aprs":
                print_error(
                    f"Unknown command: {cmd}. Type 'help' for available commands."
                )
            else:
                print_error(
                    f"Unknown command: {cmd}. Type 'help' for available commands."
                )

    async def show_startup_status(self):
        """Display startup status screen with VFO info."""
        # In serial mode, skip radio status and show APRS mode info
        if self.serial_mode:
            print_header("Console Ready")
            print_pt("")
            print_info(f"Mode: APRS (serial KISS TNC)")
            print_info(f"MYCALL: {self.tnc_config.get('MYCALL')}")
            mylocation = self.tnc_config.get('MYLOCATION')
            if mylocation:
                print_info(f"MYLOCATION: {mylocation}")
                # Broadcast MYLOCATION position to web UI on startup
                await self._broadcast_mylocation_to_web(mylocation)

            print_pt("")
            print_pt(HTML("<gray>Type 'help' for available commands</gray>"))
            print_pt(HTML(f"<gray>Console mode: <b>{self.console_mode}</b> (use APRS commands without prefix)</gray>"))
            print_pt("")
            return

        try:
            # Get radio settings and status
            settings = await self.radio.get_settings()
            status = await self.radio.get_status()
            volume = await self.radio.get_volume()

            if not settings:
                print_error("Unable to read radio settings")
                return

            # Get channel details for both VFOs
            ch_a = await self.radio.read_channel(settings["channel_a"])
            ch_b = await self.radio.read_channel(settings["channel_b"])

            print_header("Radio Status")
            print_pt("")

            # Determine active VFO: prefer `double_channel` (radio's dual-watch
            # mode) when present, otherwise fall back to `vfo_x`.
            mode = settings.get("double_channel", None)
            if mode is not None and mode in (1, 2):
                active_vfo = "A" if mode != 2 else "B"
            else:
                vfo_x = settings.get("vfo_x", None)
                active_vfo = "A" if (vfo_x is None or vfo_x == 0) else "B"
            active_a = "●" if active_vfo == "A" else "○"
            active_b = "●" if active_vfo == "B" else "○"

            if ch_a:
                print_pt(
                    HTML(
                        f"<b>VFO A {active_a}</b>  CH{settings['channel_a']:3d}  {ch_a['tx_freq_mhz']:.4f} MHz  {ch_a['power']:4s}  {ch_a['name']}"
                    )
                )
            else:
                print_pt(
                    HTML(
                        f"<b>VFO A {active_a}</b>  CH{settings['channel_a']:3d}"
                    )
                )

            if ch_b:
                print_pt(
                    HTML(
                        f"<b>VFO B {active_b}</b>  CH{settings['channel_b']:3d}  {ch_b['tx_freq_mhz']:.4f} MHz  {ch_b['power']:4s}  {ch_b['name']}"
                    )
                )
            else:
                print_pt(
                    HTML(
                        f"<b>VFO B {active_b}</b>  CH{settings['channel_b']:3d}"
                    )
                )

            print_pt("")

            # Radio state
            if status:
                power_state = "ON" if status["is_power_on"] else "OFF"
                power_color = "green" if status["is_power_on"] else "red"
                print_pt(
                    HTML(
                        f"Power: <{power_color}>{power_state}</{power_color}>"
                    )
                )

            # Dual watch
            dual_mode = settings.get("double_channel", 0)
            if dual_mode == 1:
                print_pt("Dual Watch: A+B")
            elif dual_mode == 2:
                print_pt("Dual Watch: B+A")
            else:
                print_pt("Dual Watch: Off")

            # Volume and squelch
            squelch = settings.get("squelch_level", 0)
            if volume is not None:
                print_pt(f"Volume: {volume}/15    Squelch: {squelch}/15")
            else:
                print_pt(f"Squelch: {squelch}/15")

            print_pt("")
            print_pt(HTML("<gray>Type 'help' for commands</gray>"))
            print_pt(HTML(f"<gray>Console mode: <b>{self.console_mode}</b> (type 'aprs' to switch to APRS mode)</gray>"))
            print_pt("")

        except Exception as e:
            print_error(f"Failed to read status: {e}")

    async def cmd_help(self, args):
        """Show mode-aware help."""
        print_header(f"Available Commands (Mode: {self.console_mode.upper()})")

        if self.console_mode == "aprs":
            # APRS mode help
            print_pt(HTML("<b>Mode Switching:</b>"))
            if not self.serial_mode:
                print_pt(HTML("  <b>radio</b>             - Switch to radio mode"))
            print_pt("")

            print_pt(HTML("<b>APRS Commands (no prefix needed):</b>"))
            print_pt(HTML("  <b>message read</b>       - Read APRS messages"))
            print_pt(HTML("  <b>message send &lt;call&gt; &lt;text&gt;</b> - Send APRS message"))
            print_pt(HTML("  <b>message clear</b>      - Clear all messages"))
            print_pt(HTML("  <b>station list [N]</b>   - List last N heard stations"))
            print_pt(HTML("  <b>station info &lt;call&gt;</b> - Show station details"))
            print_pt(HTML("  <b>wx list [sort]</b>     - List weather stations"))
            print_pt("")

            if not self.serial_mode:
                print_pt(HTML("<b>Radio Commands:</b>"))
                print_pt(HTML("  <gray>Radio commands require 'radio' prefix in APRS mode</gray>"))
                print_pt(HTML("  <gray>(Type 'radio' to switch modes for direct access)</gray>"))
                print_pt("")

            print_pt(HTML("<b>TNC Configuration:</b>"))
            print_pt(HTML("  <b>tnc</b>               - Enter TNC-2 terminal mode"))
            print_pt(HTML("  <b>tnc display</b>       - Show TNC parameters"))
            print_pt(HTML("  <b>tnc mycall &lt;call&gt;</b> - Set your callsign"))
            print_pt(HTML("  <b>tnc monitor &lt;on|off&gt;</b> - Enable/disable monitoring"))
            print_pt("")

        elif self.console_mode == "radio":
            # Radio mode help
            print_pt(HTML("<b>Mode Switching:</b>"))
            print_pt(HTML("  <b>aprs</b>              - Switch to APRS mode"))
            print_pt("")

            print_pt(HTML("<b>Radio Control:</b>"))
            print_pt(
                HTML("  <b>status</b>            - Show current radio status")
            )
            print_pt(HTML("  <b>health</b>            - Show connection health"))
            print_pt(
                HTML("  <b>notifications</b>     - Check BLE notification status")
            )
            print_pt(
                HTML("  <b>vfo</b>               - Show VFO A/B configuration")
            )
            print_pt(
                HTML(
                    "  <b>setvfo &lt;a|b&gt; &lt;ch&gt;</b>  - Set VFO to channel 1-256"
                )
            )
            print_pt(HTML("  <b>active &lt;a|b&gt;</b>      - Switch active VFO"))
            print_pt(
                HTML(
                    "  <b>dual &lt;off|ab|ba&gt;</b> - Set dual watch (off/A+B/B+A)"
                )
            )
            print_pt(HTML("  <b>scan &lt;on|off&gt;</b>    - Enable/disable scan"))
            print_pt(HTML("  <b>squelch &lt;0-15&gt;</b>   - Set squelch level"))
            print_pt(
                HTML("  <b>volume &lt;0-15&gt;</b>    - Get/set volume level")
            )
            print_pt("")
            print_pt(HTML("<b>Channel Management:</b>"))
            print_pt(
                HTML("  <b>channel &lt;id&gt;</b>     - Show channel details")
            )
            print_pt(HTML("  <b>list [start] [end]</b> - List channels"))
            print_pt(
                HTML(
                    "  <b>power &lt;id&gt; &lt;lvl&gt;</b>  - Set power (low/med/high)"
                )
            )
            print_pt(
                HTML(
                    "  <b>freq &lt;id&gt; &lt;tx&gt; &lt;rx&gt;</b> - Set frequencies"
                )
            )
            print_pt("")
            print_pt(HTML("<b>BSS Settings:</b>"))
            print_pt(
                HTML("  <b>bss</b>                    - Show BSS/APRS settings")
            )
            print_pt(
                HTML(
                    "  <b>setbss &lt;param&gt; &lt;val&gt;</b>  - Set BSS parameter"
                )
            )
            print_pt("")

            print_pt(HTML("<b>APRS Commands:</b>"))
            print_pt(HTML("  <gray>APRS commands require 'aprs' prefix in radio mode</gray>"))
            print_pt(HTML("  <gray>(Type 'aprs' to switch modes for direct access)</gray>"))
            print_pt("")

            print_pt(HTML("<b>TNC Configuration:</b>"))
            print_pt(HTML("  <b>tnc</b>               - Enter TNC terminal mode"))
            print_pt(HTML("  <b>tnc display</b>       - Show TNC parameters"))
            print_pt(HTML("  <b>tnc mycall &lt;call&gt;</b> - Set your callsign"))
            print_pt(HTML("  <b>tnc tncsend &lt;hex&gt;</b> - Send raw hex to TNC"))
            print_pt("")

        # Common commands (both modes)
        print_pt(HTML("<b>Utility:</b>"))
        print_pt(HTML("  <b>dump</b>              - Dump raw settings"))
        print_pt(HTML("  <b>debug</b>             - Toggle debug output"))
        print_pt(HTML("  <b>pws [show|fetch]</b>  - Personal Weather Station"))
        print_pt(HTML("  <b>help</b>              - Show this help"))
        print_pt(HTML("  <b>quit</b> / <b>exit</b>      - Exit application"))
        print_pt("")

        # Show server ports from TNC config
        tnc_port = self.tnc_config.get("TNC_PORT") or "8001"
        agwpe_port = self.tnc_config.get("AGWPE_PORT") or "8000"
        webui_port = self.tnc_config.get("WEBUI_PORT") or "8002"
        print_pt(HTML(f"<b>TNC TCP Bridge:</b> Port {tnc_port} (bidirectional)"))
        print_pt(HTML(f"<b>AGWPE Bridge:</b> Port {agwpe_port}"))
        print_pt(HTML(f"<b>Web UI:</b> Port {webui_port}"))
        print_pt("")


    async def cmd_quit(self, args):
        """Quit the application."""
        print_info("Exiting...")

        # Trigger async saves for both database and frame buffer
        # This allows instant exit while saves complete in background
        save_tasks = []

        # Save APRS station database
        save_tasks.append(self.aprs_manager.save_database_async())

        # Save frame buffer
        if hasattr(self, 'frame_history'):
            save_tasks.append(self.frame_history.save_to_disk_async())

        # Wait for both saves to complete (runs concurrently)
        print_info("Saving database and frame buffer...")
        results = await asyncio.gather(*save_tasks, return_exceptions=True)

        # Report results
        if len(results) >= 1 and isinstance(results[0], int) and results[0] > 0:
            print_info(f"Saved {results[0]} station(s) to APRS database")

        self.radio.running = False

    async def cmd_tnc(self, args, auto_connect=None):
        """TNC commands and terminal mode.

        Usage:
            tnc                     - Enter TNC terminal mode
            tnc <command> [args]    - Execute TNC command from any mode

        Examples:
            tnc display             - Show TNC parameters
            tnc mycall N0CALL       - Set your callsign
            tnc monitor on          - Enable packet monitoring
        """
        # If args provided, dispatch to TNC handler for command execution
        if args:
            await self.tnc_handler.dispatch(args[0].upper(), args[1:])
            return

        # No args - enter TNC terminal mode
        print_header("TNC Terminal Mode")
        print_pt(
            HTML(
                f"<gray>Current MYCALL: <b>{self.tnc_config.get('MYCALL')}</b></gray>"
            )
        )
        print_pt(
            HTML(
                "<gray>Use '~~~' to toggle between conversation and command mode</gray>"
            )
        )
        print_pt("")

        self.tnc_mode = True

        # Flag to trigger auto-connect after setup
        do_auto_connect = auto_connect
        # Disable tnc_monitor display - AX25Adapter callback handles everything
        self.radio.tnc_mode_active = True
        # Add keybinding for Ctrl+] to toggle between conversation and command mode

        kb = KeyBindings()

        @kb.add("c-]")
        def _escape_tnc(event):
            # Toggle conversation mode and exit prompt to refresh
            print_debug("Ctrl+] keybinding triggered", level=6)
            self.tnc_conversation_mode = not self.tnc_conversation_mode
            # Exit with special marker to trigger mode change message
            event.app.exit(result="<<<TOGGLE_MODE>>>")

        @kb.add("?")
        def _show_tnc_help(event):
            """Show context-sensitive help when '?' is pressed (IOS-style)."""
            from prompt_toolkit.completion import CompleteEvent
            from prompt_toolkit.document import Document
            from prompt_toolkit.formatted_text import to_plain_text

            buffer = event.current_buffer
            text_before_cursor = buffer.text[: buffer.cursor_position]

            # IOS-style context help: "command ?" shows options for next token
            # If text ends with space, show completions. Otherwise insert "?" literally
            if text_before_cursor.strip() and not text_before_cursor.endswith(' '):
                # Not asking for help - insert ? as regular character
                buffer.insert_text('?')
                return

            # Get completions at current position
            document = Document(
                text=buffer.text, cursor_position=buffer.cursor_position
            )
            completions = list(
                tnc_completer.get_completions(document, CompleteEvent())
            )

            # Display available options
            if completions:
                print_pt("\n<Available options>")
                for comp in completions:
                    if comp.display_meta:
                        # Convert FormattedText to plain string
                        meta_text = (
                            to_plain_text(comp.display_meta)
                            if hasattr(comp.display_meta, "__iter__")
                            else str(comp.display_meta)
                        )
                        print_pt(f"  {comp.text:<15} {meta_text}")
                    else:
                        print_pt(f"  {comp.text}")
                print_pt("")  # Blank line after help
            else:
                # No completions - show general TNC help
                print_pt(
                    "\n<TNC Commands: CONNECT, DISCONNECT, CONVERSE, STATUS, K/UNPROTO, MONITOR, DIGIPEATER, DISPLAY, RESET>"
                )
                print_pt("")

            # Redisplay the prompt with current text intact
            # This is done automatically by not calling validate_and_handle()

        # Create TNC completer for command mode
        tnc_completer = TNCCompleter()

        session = PromptSession(
            completer=tnc_completer,
            complete_while_typing=False,
            key_bindings=kb,
        )
        # initialize pyax25 AX25 instance for TNC mode
        try:
            if getattr(self, "ax25", None) is not None:
                try:
                    self.ax25.init_ax25()
                except Exception as e:
                    print_error(
                        f"Failed to initialize pyax25 AX25 instance: {e}"
                    )
                    return
        except Exception:
            pass
        # Apply DEBUGFRAMES setting from TNC config and register debug callback
        try:
            # Save original DEBUG_LEVEL state before entering TNC mode
            if self._original_debug_state is None:
                self._original_debug_state = constants.DEBUG_LEVEL

            df = (self.tnc_config.get("DEBUGFRAMES") or "").upper()
            self.tnc_debug_frames = df in ("ON", "1", "YES", "TRUE")

            # Set DEBUG_LEVEL based on DEBUGFRAMES and console debug mode
            # If DEBUGFRAMES is ON, enable at least level 1 (frame debugging)
            # If console debug is already higher, keep the higher level
            if self.tnc_debug_frames:
                # Enable frame debugging (level 1) at minimum
                if constants.DEBUG_LEVEL < 1:
                    constants.DEBUG_LEVEL = 1
            else:
                # Restore original level
                constants.DEBUG_LEVEL = self._original_debug_state

            if getattr(self, "ax25", None) is not None:
                if self.tnc_debug_frames:
                    try:
                        self.ax25.register_frame_debug(
                            self._tnc_frame_debug_cb
                        )
                    except Exception:
                        pass
                else:
                    try:
                        self.ax25.register_frame_debug(None)
                    except Exception:
                        pass
        except Exception:
            pass

        with patch_stdout():
            while self.tnc_mode and self.radio.running:
                try:
                    # Handle auto-connect on first iteration
                    if do_auto_connect:
                        await asyncio.sleep(
                            0.5
                        )  # Give TNC mode time to initialize
                        print_info(f"Auto-connecting to {do_auto_connect}...")
                        await self._process_tnc_command(
                            f"connect {do_auto_connect}"
                        )
                        do_auto_connect = None  # Only do this once
                        continue  # Skip to next iteration to show connected prompt

                    # Sync tnc_connected_to with actual link state
                    # If adapter reports link is down, clear our connected state
                    if self.tnc_connected_to and not getattr(
                        self.radio, "tnc_link_established", True
                    ):
                        print_pt("")  # New line to clear prompt
                        print_info(
                            f"*** DISCONNECTED from {self.tnc_connected_to}"
                        )
                        self.tnc_connected_to = None
                        self.tnc_conversation_mode = (
                            False  # Exit conversation mode on disconnect
                        )
                        continue  # Restart loop immediately to show updated prompt

                    # Show different prompt based on connection state and conversation mode
                    if self.tnc_connected_to:
                        if self.tnc_conversation_mode:
                            # No prompt in conversation mode - let BBS/node prompts be visible
                            prompt_text = ""
                        else:
                            prompt_text = f"<b><cyan>TNC({self.tnc_connected_to}:CMD)&gt;</cyan></b> "
                    else:
                        if self.tnc_conversation_mode:
                            prompt_text = (
                                "<b><yellow>TNC(CONV)&gt;</yellow></b> "
                            )
                        else:
                            prompt_text = "<b><cyan>TNC&gt;</cyan></b> "

                    # Create prompt task and disconnect watcher task
                    prompt_task = asyncio.create_task(
                        session.prompt_async(
                            HTML(prompt_text), key_bindings=kb
                        )
                    )

                    async def watch_disconnect():
                        """Monitor connection state and return when disconnected."""
                        initial_connection = self.tnc_connected_to
                        if not initial_connection:
                            # Not connected - wait forever (will be cancelled by prompt)
                            await asyncio.Event().wait()
                            return False
                        # Connected - monitor for disconnect
                        while self.tnc_connected_to == initial_connection:
                            await asyncio.sleep(0.1)  # Check every 100ms
                            if not getattr(
                                self.radio, "tnc_link_established", True
                            ):
                                return True  # Disconnected
                        return False  # Connection changed or cleared

                    watcher_task = asyncio.create_task(watch_disconnect())

                    # Wait for either prompt completion or disconnect detection
                    done, pending = await asyncio.wait(
                        {prompt_task, watcher_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Cancel the pending task
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                    # Check which task completed
                    if watcher_task in done and watcher_task.result():
                        # Disconnected while waiting for input
                        print_pt("")  # New line
                        print_info(
                            f"*** DISCONNECTED from {self.tnc_connected_to}"
                        )
                        self.tnc_connected_to = None
                        self.tnc_conversation_mode = (
                            False  # Exit conversation mode on disconnect
                        )
                        continue  # Show updated prompt

                    # Prompt completed normally, get the result
                    line = prompt_task.result()

                    # Debug: show what we received (only in debug mode)
                    if line and constants.DEBUG_LEVEL >= 1:
                        print_debug(f"Received input: {repr(line)}", level=6)

                    if not line:
                        continue

                    # Check for mode toggle marker from Ctrl+] keybinding
                    if line == "<<<TOGGLE_MODE>>>":
                        # Mode was already toggled in keybinding, just show feedback
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Check for text-based escape sequence: ~~~
                    if line.strip() == "~~~":
                        print_debug("Escape sequence '~~~' detected", level=6)
                        self.tnc_conversation_mode = (
                            not self.tnc_conversation_mode
                        )
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Check for escape sequence (fallback if keybinding doesn't work)
                    if line == "\x1d":  # ^]
                        print_debug(
                            "Ctrl+] received as input character (keybinding fallback)",
                            level=6,
                        )
                        self.tnc_conversation_mode = (
                            not self.tnc_conversation_mode
                        )
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Process based on conversation mode
                    if self.tnc_conversation_mode:
                        # In conversation mode - send text (either connected or UI frames)
                        await self._tnc_send_text(line)
                    else:
                        # In command mode - process as TNC command
                        await self._process_tnc_command(line)

                except EOFError:
                    print_pt("")
                    break
                except KeyboardInterrupt:
                    print_pt("")
                    print_info("Interrupt received - cleaning up...")
                    if self.tnc_connected_to:
                        try:
                            await self._tnc_disconnect()
                        except Exception:
                            pass
                    # Exit TNC mode on keyboard interrupt
                    break
                except Exception as e:
                    print_error(f"TNC error: {e}")

        self.tnc_mode = False
        # Re-enable tnc_monitor display for regular console mode
        self.radio.tnc_mode_active = False
        # Restore original DEBUG_LEVEL when exiting TNC mode
        if self._original_debug_state is not None:
            constants.DEBUG_LEVEL = self._original_debug_state
            self._original_debug_state = None
        # close pyax25 AX25 instance when leaving TNC mode
        try:
            if getattr(self, "ax25", None) is not None:
                try:
                    await self.ax25.close_ax25()
                except Exception as e:
                    print_error(f"Error closing AX25 adapter: {e}")
        except Exception:
            pass
        print_info("Exited TNC mode")
        print_pt("")

    def _tnc_frame_debug_cb(self, direction, kiss_frame: bytes):
        try:
            if not self.tnc_debug_frames:
                return
            if direction == "tx":
                print_debug(
                    f"TNC TX KISS ({len(kiss_frame)} bytes): {kiss_frame.hex()}",
                    level=4,
                )
                try:
                    ascii = "".join(
                        chr(b) if 32 <= b <= 126 else "." for b in kiss_frame
                    )
                    print_debug(f"TNC TX ASCII: {ascii}", level=4)
                except Exception:
                    pass
            elif direction == "rx":
                print_debug(
                    f"TNC RX KISS ({len(kiss_frame)} bytes): {kiss_frame.hex()}",
                    level=4,
                )
                try:
                    ascii = "".join(
                        chr(b) if 32 <= b <= 126 else "." for b in kiss_frame
                    )
                    print_debug(f"TNC RX ASCII: {ascii}", level=4)
                except Exception:
                    pass
        except Exception:
            pass

    async def _process_tnc_command(self, line):
        """Process TNC command using handler dispatch."""
        parts = line.strip().split()
        if not parts:
            return

        cmd = parts[0].upper()
        args = parts[1:]

        # Try TNC protocol commands
        if await self.tnc_handler.dispatch(cmd, args):
            return

        # Try beacon commands
        if await self.beacon_handler.dispatch(cmd, args):
            return

        # Try weather commands
        if await self.weather_handler.dispatch(cmd, args):
            return

        # Handle generic TNC-2 parameters
        if cmd in self.tnc_config.settings:
            if not args:
                # Display current value
                print_pt(f"{cmd}: {self.tnc_config.get(cmd)}")
            else:
                # Set new value
                value = " ".join(args)
                self.tnc_config.set(cmd, value)
                print_info(f"{cmd} set to {value}")
                # Apply DEBUGFRAMES immediately if changed
                try:
                    if cmd == "DEBUGFRAMES":
                        v = value.upper()
                        self.tnc_debug_frames = v in (
                            "ON",
                            "1",
                            "YES",
                            "TRUE",
                        )

                        # Enable/disable DEBUG_LEVEL based on DEBUGFRAMES
                        if self.tnc_debug_frames:
                            constants.DEBUG_LEVEL = 1  # Frame debugging
                        else:
                            constants.DEBUG_LEVEL = 0  # No debugging

                        if getattr(self, "ax25", None) is not None:
                            if self.tnc_debug_frames:
                                self.ax25.register_frame_debug(
                                    self._tnc_frame_debug_cb
                                )
                            else:
                                self.ax25.register_frame_debug(None)
                except Exception:
                    pass
        else:
            print_error(f"Unknown TNC command: {cmd}")
            print_pt("Type DISPLAY for available parameters")

    async def _tnc_connect(self, callsign, path=None):
        """Connect to a station."""
        if path is None:
            path = []

        if self.tnc_connected_to:
            print_error(f"Already connected to {self.tnc_connected_to}")
            print_pt("DISCONNECT first")
            return

        mycall = self.tnc_config.get("MYCALL")
        if mycall == "NOCALL":
            print_error("Set MYCALL first")
            return

        print_info(f"Connecting to {callsign}...")
        if path:
            print_pt(f"Via: {', '.join(path)}")

        try:
            # If AX25 adapter exists, use it exclusively — no fallback.
            if getattr(self, "ax25", None) is not None:
                # Use 3 second timeout per attempt, 5 retries = 15 seconds total
                ok = await self.ax25.connect(
                    callsign, path or [], timeout=3.0, max_retries=5
                )
                if ok:
                    self.tnc_connected_to = callsign
                    self.tnc_conversation_mode = (
                        True  # Automatically enter conversation mode
                    )
                    self._tnc_text_buffer = (
                        ""  # Clear text buffer for new connection
                    )
                    print_info(f"*** LINK ESTABLISHED with {callsign}")
                    print_pt(
                        HTML(
                            "<gray>Type text to send, type '~~~' for command mode</gray>"
                        )
                    )
                else:
                    print_error("Connect failed: no response after 5 attempts")
                return

            # No AX25 adapter — continue with UI fallback behavior
            from src.ax25_adapter import build_ui_kiss_frame

            frame = build_ui_kiss_frame(
                source=mycall,
                dest=callsign,
                path=path or [],
                info=b"*** CONNECTED\r",
            )

            await self.radio.send_tnc_data(frame)

            self.tnc_connected_to = callsign
            self.tnc_conversation_mode = (
                True  # Automatically enter conversation mode
            )
            print_info(f"*** CONNECTED to {callsign}")
            print_pt(
                HTML(
                    "<gray>Type text to send, type '~~~' for command mode</gray>"
                )
            )

        except Exception as e:
            print_error(f"Connect failed: {e}")

    async def _tnc_disconnect(self):
        """Disconnect from current station."""
        if not self.tnc_connected_to:
            print_warning("Not connected")
            return

        callsign = self.tnc_connected_to
        mycall = self.tnc_config.get("MYCALL")

        try:
            # If AX25 adapter exists, use it exclusively
            if getattr(self, "ax25", None) is not None:
                await self.ax25.disconnect()
                print_info(f"*** LINK TEARDOWN requested for {callsign}")
            else:
                # Send disconnect message as UI fallback
                from src.ax25_adapter import build_ui_kiss_frame

                frame = build_ui_kiss_frame(
                    source=mycall,
                    dest=callsign,
                    path=[],
                    info=b"*** DISCONNECTED\r",
                )
                await self.radio.send_tnc_data(frame)

                print_info(f"*** DISCONNECTED from {callsign}")
            # Clear link-layer state if set
            try:
                self.radio.tnc_link_established = False
                self.radio.tnc_connected_callsign = None
                self.radio.tnc_pending_connect = None
                self.radio.tnc_connect_event.clear()
            except Exception:
                pass
            self.tnc_connected_to = None
            self.tnc_conversation_mode = (
                False  # Exit conversation mode on disconnect
            )
            self._tnc_text_buffer = ""  # Clear text buffer on disconnect

        except Exception as e:
            print_error(f"Disconnect failed: {e}")
            self.tnc_connected_to = None
            self.tnc_conversation_mode = (
                False  # Exit conversation mode on disconnect
            )
            self._tnc_text_buffer = ""  # Clear text buffer on disconnect

    async def _tnc_send_text(self, text):
        """Send text to connected station or as UI frame if disconnected."""
        mycall = self.tnc_config.get("MYCALL")

        # Handle disconnected conversation mode - send UI frames
        if not self.tnc_connected_to:
            # Parse UNPROTO setting: "DEST VIA PATH1,PATH2"
            unproto = self.tnc_config.get("UNPROTO") or "CQ"
            parts = unproto.split()
            dest = parts[0] if parts else "CQ"
            path = []
            if len(parts) > 2 and parts[1].upper() == "VIA":
                path = [p.strip() for p in " ".join(parts[2:]).split(",")]

            try:
                from src.ax25_adapter import build_ui_kiss_frame

                payload = (text + "\r").encode("ascii", errors="replace")
                kiss_frame = build_ui_kiss_frame(mycall, dest, path, payload)
                await self.radio.send_tnc_data(kiss_frame)
                print_pt(HTML(f"<green>&gt;</green> {text}"))
            except Exception as e:
                print_error(f"Send failed: {e}")
            return

        try:
            # Build UI frame with text (append CR to follow TNC-2 line convention)
            payload = (text + "\r").encode("ascii", errors="replace")

            # If AX25Adapter exists, use it exclusively (no fallback)
            if getattr(self, "ax25", None) is not None:
                ok = await self.ax25.send_info(
                    mycall, self.tnc_connected_to, [], payload
                )
                if not ok:
                    print_error(
                        "Send failed: link not established or pyax25 send error"
                    )
                    return
            else:
                # Fallback to existing radio behavior when adapter is not present
                if getattr(self.radio, "tnc_link_established", False):
                    # Use RadioController helper to send linked I-frame (handles queuing)
                    await self.radio.send_linked_info(
                        mycall, self.tnc_connected_to, [], payload
                    )
                else:
                    from src.ax25_adapter import build_ui_kiss_frame

                    kiss_frame = build_ui_kiss_frame(
                        mycall, self.tnc_connected_to, [], payload
                    )
                    await self.radio.send_tnc_data(kiss_frame)

            # Echo locally
            print_pt(HTML(f"<green>&gt;</green> {text}"))

        except Exception as e:
            print_error(f"Send failed: {e}")

    def _tnc_receive_callback(self, parsed_frame):
        """Callback for received AX.25 frames from the adapter.

        Args:
            parsed_frame: Dict with keys: src, dst, path, pid, info
        """
        try:
            # Only display if in TNC mode
            if not self.tnc_mode:
                return

            src = parsed_frame.get("src", "")
            dst = parsed_frame.get("dst", "")
            info = parsed_frame.get("info", b"")

            # If connected and this is from our connected station, display the data
            if self.tnc_connected_to and src == self.tnc_connected_to and info:
                try:
                    # Decode text (keep \r as-is for now to properly handle line continuation)
                    text = info.decode("ascii", errors="replace")

                    # Add to buffer
                    self._tnc_text_buffer += text

                    # Split by \r to find complete lines
                    lines = self._tnc_text_buffer.split("\r")

                    # Last element is incomplete (no \r at end), keep it in buffer
                    self._tnc_text_buffer = lines[-1]

                    # Display all complete lines (all but the last)
                    for line in lines[:-1]:
                        if line:  # Only display non-empty lines
                            print_pt(line)
                except Exception:
                    pass
            # If MONITOR is ON, display all frames
            elif self.tnc_config.get("MONITOR") == "ON":
                try:
                    if info:
                        # Convert \r to \n for proper display
                        text = (
                            info.decode("ascii", errors="replace")
                            .replace("\r", "\n")
                            .rstrip("\n")
                        )
                        if text:
                            header = (
                                f"{src}>{dst}"
                                if src and dst
                                else (src or dst or "")
                            )
                            path = parsed_frame.get("path", [])
                            if path:
                                header += "," + ",".join(path)
                            print_pt(HTML(f"<gray>[{header}] {text}</gray>"))
                except Exception:
                    pass
        except Exception as e:
            if constants.DEBUG:
                print_debug(f"_tnc_receive_callback error: {e}")

    async def _tnc_status(self):
        """Show TNC status."""
        mycall = self.tnc_config.get("MYCALL")
        myalias = self.tnc_config.get("MYALIAS")

        print_pt(HTML(f"<b>MYCALL:</b> {mycall}"))
        if myalias:
            print_pt(HTML(f"<b>MYALIAS:</b> {myalias}"))

        print_pt(HTML(f"<b>UNPROTO:</b> {self.tnc_config.get('UNPROTO')}"))
        print_pt(HTML(f"<b>MONITOR:</b> {self.tnc_config.get('MONITOR')}"))

        if self.tnc_connected_to:
            print_pt(HTML(f"<b>Connected to:</b> {self.tnc_connected_to}"))
        else:
            print_pt(HTML("<gray>Not connected</gray>"))

        # Show internal state for debugging
        if getattr(self, "ax25", None) is not None:
            tx_worker_status = (
                "running"
                if (self.ax25._tx_task and not self.ax25._tx_task.done())
                else "STOPPED"
            )
            tx_queue_len = len(self.ax25._tx_queue)
            print_pt(HTML(f"<b>TX Worker:</b> {tx_worker_status}"))
            print_pt(HTML(f"<b>TX Queue:</b> {tx_queue_len} frame(s)"))
            print_pt(
                HTML(f"<b>N(S)/N(R):</b> {self.ax25._ns}/{self.ax25._nr}")
            )

    async def gps_poll_and_beacon_task(self):
        """Background task to poll GPS and send beacons when enabled."""

        while self.radio.running:
            try:
                # Poll GPS every 5 seconds
                await asyncio.sleep(5)

                # Check GPS lock
                gps_locked = await self.radio.check_gps_lock()

                # Try getting position anyway (for debugging - lock check may be inaccurate)
                position = await self.radio.get_gps_position()

                if position:
                    # We got valid position data - update lock status
                    self.gps_position = position
                    self.gps_locked = True  # Override lock check if we got valid data
                    print_debug(f"GPS: {position['latitude']:.6f}, {position['longitude']:.6f} (lock_check={gps_locked})", level=6)

                    # Broadcast position update to web clients
                    if self.aprs_manager._web_broadcast:
                        await self.aprs_manager._web_broadcast('gps_update', {
                            'latitude': position['latitude'],
                            'longitude': position['longitude'],
                            'altitude': position.get('altitude'),
                            'locked': True
                        })

                    # Check if beacon is enabled and due
                    if self.tnc_config.get("BEACON") == "ON":
                        beacon_interval = int(self.tnc_config.get("BEACON_INTERVAL") or "10")

                        # Check if it's time to beacon
                        now = datetime.now()
                        should_beacon = False

                        if self.last_beacon_time is None:
                            should_beacon = True  # First beacon
                        else:
                            elapsed = (now - self.last_beacon_time).total_seconds()
                            if elapsed >= (beacon_interval * 60):
                                should_beacon = True

                        if should_beacon:
                            await self._send_position_beacon(position)
                else:
                    # No GPS position data
                    self.gps_position = None
                    self.gps_locked = False
                    print_debug(f"GPS: No position data (lock_check={gps_locked})", level=6)

                    # Check if beacon is enabled with manual location (MYLOCATION)
                    if self.tnc_config.get("BEACON") == "ON" and self.tnc_config.get("MYLOCATION"):
                        beacon_interval = int(self.tnc_config.get("BEACON_INTERVAL") or "10")

                        # Check if it's time to beacon
                        now = datetime.now()
                        should_beacon = False

                        if self.last_beacon_time is None:
                            should_beacon = True  # First beacon
                        else:
                            elapsed = (now - self.last_beacon_time).total_seconds()
                            if elapsed >= (beacon_interval * 60):
                                should_beacon = True

                        if should_beacon:
                            await self._send_position_beacon(None)  # Use MYLOCATION

            except Exception as e:
                print_error(f"GPS poll task error: {e}")
                await asyncio.sleep(10)  # Back off on error

    async def _send_position_beacon(self, position=None):
        """Send APRS position beacon.

        Args:
            position: GPS position dict with latitude, longitude, altitude, etc.
                     If None, will use MYLOCATION grid square if configured.
        """

        try:
            # Get beacon settings
            mycall = self.tnc_config.get("MYCALL")
            symbol = self.tnc_config.get("BEACON_SYMBOL") or "/["
            comment = self.tnc_config.get("BEACON_COMMENT") or ""
            path_str = self.tnc_config.get("BEACON_PATH") or "WIDE1-1"

            # Parse path
            path = [p.strip() for p in path_str.split(",")]

            # Determine position source
            lat = None
            lon = None
            alt = None
            source = None

            if position:
                # Use GPS position
                lat = position['latitude']
                lon = position['longitude']
                alt = position.get('altitude')
                source = "GPS"
            else:
                # Try manual location (Maidenhead grid square)
                mylocation = self.tnc_config.get("MYLOCATION")
                if mylocation:
                    try:
                        lat, lon = maidenhead_to_latlon(mylocation)
                        source = f"Grid {mylocation.upper()}"
                    except ValueError as e:
                        print_error(f"Invalid MYLOCATION '{mylocation}': {e}")
                        return

            if lat is None or lon is None:
                print_error("No position available (GPS unavailable and MYLOCATION not set)")
                return

            # Convert to APRS lat/lon format (DDMM.HH N/S, DDDMM.HH E/W)
            lat_deg = int(abs(lat))
            lat_min = (abs(lat) - lat_deg) * 60
            lat_dir = 'N' if lat >= 0 else 'S'
            lat_str = f"{lat_deg:02d}{lat_min:05.2f}{lat_dir}"

            lon_deg = int(abs(lon))
            lon_min = (abs(lon) - lon_deg) * 60
            lon_dir = 'E' if lon >= 0 else 'W'
            lon_str = f"{lon_deg:03d}{lon_min:05.2f}{lon_dir}"

            # Symbol table and code
            symbol_table = symbol[0] if len(symbol) >= 1 else '/'
            symbol_code = symbol[1] if len(symbol) >= 2 else '['

            # Check if weather data is available
            wx_string = ""
            wx_source = None
            if hasattr(self, 'weather_manager') and self.weather_manager.enabled:
                # Get beacon interval for wind averaging
                beacon_interval_min = int(self.tnc_config.get("BEACON_INTERVAL") or "10")
                beacon_interval_sec = beacon_interval_min * 60

                # Get weather data with wind averaging over beacon interval
                wx_data = self.weather_manager.get_beacon_weather(beacon_interval_sec)
                if wx_data:
                    # Format weather data in APRS Complete Weather Report format
                    # Format: _DIR/SPDgGUSTtTEMPrRAINpRAIN24PhHUMbPRESSURE
                    # where _ is the weather symbol (underscore)
                    wx_parts = []

                    # Wind direction (3 digits, degrees)
                    if wx_data.wind_direction is not None:
                        wx_parts.append(f"{int(wx_data.wind_direction):03d}")
                    else:
                        wx_parts.append("...")

                    wx_parts.append("/")

                    # Wind speed (3 digits, mph)
                    if wx_data.wind_speed is not None:
                        wx_parts.append(f"{int(wx_data.wind_speed):03d}")
                    else:
                        wx_parts.append("...")

                    # Wind gust (g = gust, 3 digits, mph)
                    if wx_data.wind_gust is not None:
                        wx_parts.append(f"g{int(wx_data.wind_gust):03d}")

                    # Temperature (t = temp, 3 digits, °F, can be negative)
                    if wx_data.temperature_outdoor is not None:
                        temp_int = int(wx_data.temperature_outdoor)
                        if temp_int < 0:
                            # Negative temps: -01 to -99
                            wx_parts.append(f"t{temp_int:03d}")
                        else:
                            wx_parts.append(f"t{temp_int:03d}")

                    # Rain last hour (r = rain 1h, 3 digits, hundredths of inch)
                    if wx_data.rain_hourly is not None:
                        rain_hundredths = int(wx_data.rain_hourly * 100)
                        wx_parts.append(f"r{rain_hundredths:03d}")

                    # Rain last 24h (p = rain 24h, 3 digits, hundredths of inch)
                    if wx_data.rain_daily is not None:
                        rain_hundredths = int(wx_data.rain_daily * 100)
                        wx_parts.append(f"p{rain_hundredths:03d}")

                    # Rain since midnight (P = rain midnight, 3 digits, hundredths of inch)
                    if wx_data.rain_event is not None:
                        rain_hundredths = int(wx_data.rain_event * 100)
                        wx_parts.append(f"P{rain_hundredths:03d}")

                    # Humidity (h = humidity, 2 digits, %, 00 = 100%)
                    if wx_data.humidity_outdoor is not None:
                        humidity = wx_data.humidity_outdoor
                        if humidity == 100:
                            wx_parts.append("h00")
                        else:
                            wx_parts.append(f"h{humidity:02d}")

                    # Barometric pressure (b = pressure, 5 digits, tenths of mbar)
                    if wx_data.pressure_relative is not None:
                        pressure_tenths = int(wx_data.pressure_relative * 10)
                        wx_parts.append(f"b{pressure_tenths:05d}")

                    wx_string = "".join(wx_parts)
                    wx_source = "wx"
                    symbol_code = "_"  # Use weather symbol

            # Build position report (! = position without timestamp)
            # Format: !DDMM.HHN/DDDMM.HHW_WEATHER/A=ALTCOMMENT
            info = f"!{lat_str}/{lon_str}{symbol_code}"

            # Add weather data if available
            if wx_string:
                info += wx_string

            # Add altitude if available (format: /A=XXXXXX feet)
            if alt is not None:
                alt_feet = int(alt * 3.28084)  # Convert meters to feet
                info += f"/A={alt_feet:06d}"

            if comment:
                info += comment

            # Send via APRS
            await self.radio.send_aprs(mycall, info, to_call="APRS", path=path)

            # Update timestamp (both in-memory and persisted to config)
            now = datetime.now()
            self.last_beacon_time = now
            self.tnc_config.set("LAST_BEACON", now.isoformat())

            # Show beacon info
            if wx_source:
                print_info(f"📡 Beacon sent ({source} + weather): {lat:.6f}, {lon:.6f}")
            else:
                print_info(f"📡 Beacon sent ({source}): {lat:.6f}, {lon:.6f}")

        except Exception as e:
            print_error(f"Failed to send position beacon: {e}")

    async def _send_aprs_message(self, to_call: str, message_text: str):
        """Send an APRS message with automatic tracking and retry.

        Args:
            to_call: Destination callsign
            message_text: Message content (max 67 characters)

        This method:
        1. Generates a unique message ID
        2. Formats the APRS message packet
        3. Transmits via radio
        4. Adds to APRS manager for tracking/retry
        """
        try:
            # Generate message ID (1-5 alphanumeric characters)
            import random
            import string
            message_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))

            # Format APRS message: :CALLSIGN :message{ID
            # Pad callsign to 9 characters
            to_padded = to_call.upper().ljust(9)
            info = f":{to_padded}:{message_text}{{{message_id}"

            # Get my callsign
            mycall = self.tnc_config.get("MYCALL") or "NOCALL"

            # Send via radio
            await self.radio.send_aprs(mycall, info, to_call="APRS", path=None)

            # Track in APRS manager for retry/ack handling
            self.aprs_manager.add_sent_message(to_call.upper(), message_text, message_id)

            print_info(f"📤 Message sent to {to_call}: {message_text}")

        except Exception as e:
            print_error(f"Failed to send APRS message: {e}")

    async def _send_aprs_ack(self, to_call: str, message_id: str):
        """Send an APRS acknowledgment for a received message.

        Args:
            to_call: Callsign to send ACK to
            message_id: Message ID being acknowledged
        """
        try:
            # Format APRS ACK: :CALLSIGN :ack{ID
            to_padded = to_call.upper().ljust(9)
            info = f":{to_padded}:ack{message_id}"

            # Get my callsign
            mycall = self.tnc_config.get("MYCALL") or "NOCALL"

            # Send via radio
            await self.radio.send_aprs(mycall, info, to_call="APRS", path=None)

            print_debug(f"📥 ACK sent to {to_call} for message {message_id}", level=5)

        except Exception as e:
            print_error(f"Failed to send APRS ACK: {e}")


# === Monitor Tasks ===

# Duplicate packet detection cache
# Tracks (packet_hash, timestamp) to suppress digipeater duplicates
# Format: {packet_hash: timestamp}
_duplicate_cache = {}
_DUPLICATE_WINDOW = 30  # seconds to consider packets as duplicates


def is_duplicate_packet(src_call: str, info: str) -> bool:
    """Check if packet is a duplicate based on source and content.

    Packets from the same source with identical content within the
    duplicate window (30 seconds) are considered duplicates.

    This suppresses multiple digipeater copies of the same packet
    while still allowing new packets from the same station.

    Args:
        src_call: Source callsign
        info: Packet information field content

    Returns:
        True if packet is a duplicate, False otherwise
    """
    global _duplicate_cache

    # Create hash of source + content
    packet_key = f"{src_call}:{info}"
    packet_hash = hashlib.md5(packet_key.encode()).hexdigest()

    current_time = time.time()

    # Clean old entries from cache (older than duplicate window)
    expired = [
        h
        for h, ts in _duplicate_cache.items()
        if current_time - ts > _DUPLICATE_WINDOW
    ]
    for h in expired:
        del _duplicate_cache[h]

    # Check if this packet hash exists in cache
    if packet_hash in _duplicate_cache:
        # Duplicate found - update timestamp and return True
        _duplicate_cache[packet_hash] = current_time
        return True

    # Not a duplicate - add to cache
    _duplicate_cache[packet_hash] = current_time
    return False


def parse_and_track_aprs_frame(complete_frame, radio, timestamp=None, frame_number=None):
    """Parse APRS frame and update database tracking.

    This function runs in ALL modes (TNC and non-TNC) to ensure
    database tracking happens regardless of display mode.

    Args:
        complete_frame: Complete KISS frame bytes
        radio: RadioController instance
        timestamp: Optional timestamp for the frame (used by migrations to preserve historical timestamps)
        frame_number: Optional frame buffer reference number

    Returns:
        dict with:
            is_aprs: bool - Whether this is an APRS frame
            is_duplicate: bool - Whether this is a duplicate packet
            src_call: str - Source callsign
            dst_call: str - Destination callsign
            info_str: str - Decoded info field
            relay: str - Relay call if third-party packet
            hop_count: int - Number of digipeater hops
            digipeater_path: list - List of digipeater callsigns
            aprs_types: dict - Parsed APRS data:
                mic_e: MICEPosition or None
                object: ObjectPosition or None
                item: ItemPosition or None
                status: StatusReport or None
                telemetry: Telemetry or None
                message: Message or None
                weather: Weather or None
                position: Position or None
    """
    result = {
        'is_aprs': False,
        'is_duplicate': False,
        'src_call': None,
        'dst_call': None,
        'info_str': '',
        'relay': None,
        'hop_count': 999,
        'digipeater_path': [],
        'aprs_types': {
            'mic_e': None,
            'object': None,
            'item': None,
            'status': None,
            'telemetry': None,
            'message': None,
            'weather': None,
            'position': None,
        }
    }

    try:
        # Parse AX.25 frame
        payload = kiss_unwrap(complete_frame)
        addresses, control_byte, offset = parse_ax25_addresses_and_control(payload)

        # Extract PID and info field
        # NOTE: offset points to PID byte (after control byte)
        if offset < len(payload):
            pid = payload[offset]
            info_bytes = payload[offset + 1:]  # Info starts after PID
        else:
            return result  # No PID/info

        # Extract basic frame info
        if len(addresses) >= 2:
            result['src_call'] = addresses[1]
            result['dst_call'] = addresses[0]
            raw_path = addresses[2:] if len(addresses) > 2 else []

            # Filter out Q constructs (APRS-IS server metadata that should never appear in RF)
            # Badly configured iGates sometimes encode these into AX.25 paths when gating from APRS-IS
            # Q constructs are exactly 3 chars: q + two uppercase (qAC, qAO, qAR, qAS, qAX, qAZ, qAU)
            # Reference: http://www.aprs-is.net/q.aspx
            Q_CONSTRUCTS = {'QAC', 'QAO', 'QAR', 'QAS', 'QAX', 'QAZ', 'QAU'}

            # Filter Q constructs first
            filtered_path = [
                digi for digi in raw_path
                if digi.upper().rstrip('*') not in Q_CONSTRUCTS
            ]

            # Filter iGate trace callsigns (appear AFTER unused WIDE/RELAY aliases)
            # Proper path order: used digis (*), then unused aliases (WIDE2-1)
            # Trace callsigns appear AFTER unused aliases (bad iGate behavior)
            # Find first unused WIDE/RELAY and truncate path there
            final_path = []
            for i, digi in enumerate(filtered_path):
                digi_upper = digi.upper().rstrip('*')
                is_unused_alias = (
                    not digi.endswith('*') and
                    (digi_upper.startswith('WIDE') or digi_upper.startswith('RELAY'))
                )
                if is_unused_alias:
                    # Include the unused alias, but drop everything after it
                    final_path.append(digi)
                    break
                else:
                    final_path.append(digi)

            result['digipeater_path'] = final_path

            # Log when Q constructs or traces are filtered (indicates misbehaving iGate)
            filtered_q = [d for d in raw_path if d.upper().rstrip('*') in Q_CONSTRUCTS]
            filtered_trace = filtered_path[len(final_path):] if len(final_path) < len(filtered_path) else []

            if filtered_q and constants.DEBUG:
                print_debug(
                    f"Filtered Q construct(s) from path: {filtered_q} (bad iGate behavior)",
                    level=2
                )
            if filtered_trace and constants.DEBUG:
                print_debug(
                    f"Filtered iGate trace callsign(s) from path: {filtered_trace} (bad iGate behavior)",
                    level=2
                )

            result['hop_count'] = calculate_hop_count(addresses)
        else:
            return result  # Invalid frame

        # Check if this looks like APRS
        if not info_bytes:
            return result

        first_byte = info_bytes[0]
        first_char = chr(first_byte) if first_byte < 128 else None
        first_two = info_bytes[:2].decode("ascii", errors="ignore") if len(info_bytes) >= 2 else ""

        # APRS marker detection
        aprs_marker = bool(
            first_char and (
                first_char in ("!", "/", "@", ":", "=", "}", "'", "`", ";", ")", ">")
                or first_two.startswith("T#")
                or first_two.startswith("p")
                or first_byte in (0x1C, 0x1D, 0x1E, 0x1F)
            )
        )

        # Only treat as APRS if marker present (reduces false positives)
        if pid == 0xF0 and aprs_marker:
            result['is_aprs'] = True
        elif pid != 0xF0 and aprs_marker:
            result['is_aprs'] = True  # Heuristic match
        else:
            return result  # Not APRS

        # Decode info field
        info_str = info_bytes.decode("ascii", errors="replace")
        result['info_str'] = info_str

        # Check for third-party packet
        third_party = radio.aprs_manager.parse_third_party(result['src_call'], info_str)
        if third_party:
            source_call, relay_call, inner_info = third_party
            parse_call = source_call
            parse_info = inner_info
            result['relay'] = relay_call
            # Third-party packets (igated from APRS-IS) should NEVER count as zero-hop
            # Override hop_count to 999 (unknown/igated) regardless of RF path from iGate
            result['hop_count'] = 999
        else:
            parse_call = result['src_call']
            parse_info = info_str

        # Check for duplicate packet (suppresses digipeater copies)
        # Convert datetime timestamp to unix timestamp for duplicate detection
        timestamp_float = timestamp.timestamp() if timestamp else None
        result['is_duplicate'] = radio.aprs_manager.duplicate_detector.is_duplicate(parse_call, parse_info, timestamp_float)

        # Record digipeater paths even for duplicates (improves coverage accuracy)
        # Pass relay information to correctly mark third-party duplicates
        if result['is_duplicate'] and result['digipeater_path']:
            radio.aprs_manager.duplicate_detector.record_path(parse_call, result['digipeater_path'], timestamp=timestamp_float, frame_number=frame_number, relay_call=result.get('relay'))

        # Parse all APRS types (updates database in aprs_manager)
        # This happens even for duplicates to ensure tracking
        if not result['is_duplicate']:
            # MIC-E
            result['aprs_types']['mic_e'] = radio.aprs_manager.parse_aprs_mice(
                parse_call, result['dst_call'], parse_info, result['relay'],
                result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
            )

            # Object
            if not result['aprs_types']['mic_e']:
                result['aprs_types']['object'] = radio.aprs_manager.parse_aprs_object(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Item
            if not result['aprs_types']['object']:
                result['aprs_types']['item'] = radio.aprs_manager.parse_aprs_item(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Status
            if not result['aprs_types']['item']:
                result['aprs_types']['status'] = radio.aprs_manager.parse_aprs_status(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Telemetry
            if not result['aprs_types']['status']:
                result['aprs_types']['telemetry'] = radio.aprs_manager.parse_aprs_telemetry(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Message
            if not result['aprs_types']['telemetry']:
                result['aprs_types']['message'] = radio.aprs_manager.parse_aprs_message(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Weather and Position (can coexist)
            if not result['aprs_types']['message']:
                result['aprs_types']['weather'] = radio.aprs_manager.parse_aprs_weather(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )
                result['aprs_types']['position'] = radio.aprs_manager.parse_aprs_position(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'],
                    result['dst_call'], timestamp=timestamp, frame_number=frame_number
                )

    except Exception as e:
        if constants.DEBUG:
            print_debug(f"APRS parsing exception: {e}", level=2)

    return result


async def tnc_monitor(tnc_queue, radio):
    """Monitor TNC data and display/forward to TCP."""
    frame_buffer = bytearray()

    while True:
        try:
            data = await tnc_queue.get()

            # Update activity tracker
            radio.update_tnc_activity()

            # Show raw data in debug mode
            if constants.DEBUG:
                print_debug(
                    f"TNC RX ({len(data)} bytes): {data.hex()}", level=4
                )
                ascii_str = "".join(
                    chr(b) if 32 <= b <= 126 else "." for b in data
                )
                if ascii_str.strip("."):
                    print_debug(f"TNC ASCII: {ascii_str}", level=5)

            # Add data to buffer
            frame_buffer.extend(data)
            if constants.DEBUG:
                print_debug(f"Buffer now {len(frame_buffer)} bytes", level=5)

            # Process complete KISS frames in buffer
            frames_processed = 0
            while True:
                # Look for frame start (0xC0)
                if len(frame_buffer) == 0:
                    if constants.DEBUG and frames_processed > 0:
                        print_debug(
                            f"Buffer empty after processing {frames_processed} frames",
                            level=5,
                        )
                    break

                # If buffer doesn't start with KISS frame delimiter, find it
                if frame_buffer[0] != 0xC0:
                    try:
                        start_idx = frame_buffer.index(0xC0)
                        if constants.DEBUG:
                            discarded = frame_buffer[:start_idx]
                            print_debug(
                                f"Discarded {len(discarded)} bytes of non-KISS data: {bytes(discarded).hex()}",
                                level=4,
                            )
                        frame_buffer = frame_buffer[start_idx:]
                    except ValueError:
                        if constants.DEBUG:
                            print_debug(
                                f"No KISS frame start found, discarding {len(frame_buffer)} bytes",
                                level=5,
                            )
                        frame_buffer.clear()
                        break

                # Now we have a frame that starts with 0xC0
                if len(frame_buffer) < 2:
                    if constants.DEBUG:
                        print_debug(
                            f"Buffer too small ({len(frame_buffer)} bytes), waiting for more data",
                            level=5,
                        )
                    break

                try:
                    # Find next 0xC0 after the first one
                    end_idx = frame_buffer.index(0xC0, 1)

                    # Collapse immediate duplicate FENDs introduced by
                    # chunk boundaries. If the next fence is at index 1
                    # and there are more bytes, drop the first fence and
                    # continue parsing so we don't interpret a 2-byte
                    # c0,c0 sequence as an empty frame and lose the real
                    # payload that follows.
                    if end_idx == 1 and len(frame_buffer) > 2:
                        if DEBUG:
                            print_debug(
                                "Collapsing duplicate leading FEND (0xC0); skipping one"
                            )
                        frame_buffer = frame_buffer[1:]
                        continue

                    # Extract complete frame
                    complete_frame = bytes(frame_buffer[: end_idx + 1])
                    frame_buffer = frame_buffer[end_idx + 1 :]

                    # Capture frame for history (if processor available)
                    frame_num = None
                    if hasattr(radio, "cmd_processor") and radio.cmd_processor:
                        radio.cmd_processor.frame_history.add_frame(
                            "RX", complete_frame
                        )
                        # Get the frame number that was just assigned
                        frame_num = radio.cmd_processor.frame_history.frame_counter

                    if constants.DEBUG:
                        print_debug(
                            f"Processing complete frame of {len(complete_frame)} bytes",
                            level=5,
                        )

                    # CRITICAL: Invoke AX25Adapter callback for link-layer processing
                    # This must happen BEFORE display code so adapter can process UA, I-frames, etc.
                    try:
                        if (
                            hasattr(radio, "_kiss_callback")
                            and radio._kiss_callback
                        ):
                            if asyncio.iscoroutinefunction(
                                radio._kiss_callback
                            ):
                                await radio._kiss_callback(complete_frame)
                            else:
                                radio._kiss_callback(complete_frame)
                    except Exception as e:
                        if constants.DEBUG:
                            print_debug(f"KISS callback error: {e}", level=2)

                    # Parse APRS and update database (works in all modes)
                    parsed_aprs = parse_and_track_aprs_frame(complete_frame, radio)

                    # Digipeat if enabled and criteria met
                    if parsed_aprs['is_aprs'] and not parsed_aprs['is_duplicate'] and hasattr(radio, 'digipeater'):
                        try:
                            # Check if source is a known digipeater
                            src_call_upper = parsed_aprs['src_call'].upper().rstrip('*')
                            is_source_digi = radio.aprs_manager.stations.get(src_call_upper, None)
                            is_source_digipeater = is_source_digi.is_digipeater if is_source_digi else False

                            # Debug: Show digipeater evaluation
                            if constants.DEBUG_LEVEL >= 4:
                                print_debug(
                                    f"Digipeater eval: {parsed_aprs['src_call']} "
                                    f"hop={parsed_aprs['hop_count']} "
                                    f"path={parsed_aprs['digipeater_path']} "
                                    f"enabled={radio.digipeater.enabled}",
                                    level=4
                                )

                            # Check if we should digipeat
                            if radio.digipeater.should_digipeat(
                                parsed_aprs['src_call'],
                                parsed_aprs['hop_count'],
                                parsed_aprs['digipeater_path'],
                                is_source_digipeater
                            ):
                                # Create digipeated frame
                                digi_frame, path_type = radio.digipeater.digipeat_frame(complete_frame, parsed_aprs)
                                if digi_frame:
                                    # Transmit the digipeated frame via radio
                                    await radio.write_kiss_frame(digi_frame, response=False)
                                    print_info(
                                        f"🔁 Digipeated {parsed_aprs['src_call']} "
                                        f"({radio.digipeater.packets_digipeated} total)"
                                    )

                                    # Track digipeater statistics
                                    if hasattr(radio, 'aprs_manager') and radio.aprs_manager:
                                        try:
                                            radio.aprs_manager.record_digipeater_activity(
                                                station_call=parsed_aprs['src_call'],
                                                path_type=path_type,
                                                original_path=parsed_aprs.get('digipeater_path', []),
                                                frame_number=frame_num
                                            )
                                        except AttributeError:
                                            # record_digipeater_activity method not yet implemented
                                            pass
                                        except Exception as e:
                                            if constants.DEBUG_LEVEL >= 3:
                                                print_debug(f"Digipeater stats error: {e}", level=3)
                        except Exception as e:
                            if constants.DEBUG_LEVEL >= 2:
                                print_debug(f"Digipeater error: {e}", level=2)
                                import traceback
                                print_debug(traceback.format_exc(), level=3)

                    # Display ASCII-decoded frame at debug level 1 (all modes)
                    if constants.DEBUG_LEVEL >= 1 and not radio.tnc_mode_active:
                        try:
                            from src.protocol import parse_ax25_addresses_and_control
                            payload = complete_frame[1:-1]  # Remove KISS delimiters
                            if len(payload) > 0 and payload[0] == 0x00:  # Data frame
                                payload = payload[1:]  # Remove KISS command byte
                                addresses, control_byte, offset = parse_ax25_addresses_and_control(payload)

                                if addresses and len(addresses) >= 2:
                                    # addresses is a list: [dest, src, digi1, digi2, ...]
                                    dst = addresses[0]
                                    src = addresses[1]
                                    path = addresses[2:] if len(addresses) > 2 else []

                                    # Get info field if present
                                    if offset < len(payload):
                                        pid = payload[offset]
                                        if pid == 0xF0 and offset + 1 < len(payload):  # No layer 3
                                            info_bytes = payload[offset + 1:]
                                            # Try to decode as ASCII
                                            info_text = info_bytes.decode('ascii', errors='replace')

                                            # Build path string
                                            path_str = ','.join(path) if path else ''
                                            path_display = f',{path_str}' if path_str else ''

                                            # Display in gray (monitor style) with frame number
                                            header = f"{src}>{dst}{path_display}"
                                            print_tnc(f"{header}:{info_text}", frame_num=frame_num)
                        except Exception:
                            pass  # Silent fail for malformed frames

                    # Display emoji pins (console mode only, not for duplicates)
                    if parsed_aprs['is_aprs'] and not parsed_aprs['is_duplicate'] and not radio.tnc_mode_active:
                        buffer_mode = hasattr(radio, "cmd_processor") and radio.cmd_processor and radio.cmd_processor.frame_history.buffer_mode
                        aprs = parsed_aprs['aprs_types']
                        relay = parsed_aprs['relay']
                        
                        # MIC-E
                        if aprs['mic_e']:
                            mice_pos = aprs['mic_e']
                            cleaned_comment = APRSFormatters.clean_position_comment(mice_pos.comment)
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            if cleaned_comment:
                                print_info(
                                    f"📍 MIC-E from {mice_pos.station}{relay_part}: {mice_pos.grid_square} - {cleaned_comment}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                            else:
                                print_info(
                                    f"📍 MIC-E from {mice_pos.station}{relay_part}: {mice_pos.grid_square}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                        
                        # Object
                        elif aprs['object']:
                            obj_pos = aprs['object']
                            cleaned_comment = APRSFormatters.clean_position_comment(obj_pos.comment)
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            if cleaned_comment:
                                print_info(
                                    f"📍 Object {obj_pos.station}{relay_part}: {obj_pos.grid_square} - {cleaned_comment}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                            else:
                                print_info(
                                    f"📍 Object {obj_pos.station}{relay_part}: {obj_pos.grid_square}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                        
                        # Item
                        elif aprs['item']:
                            item_pos = aprs['item']
                            cleaned_comment = APRSFormatters.clean_position_comment(item_pos.comment)
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            if cleaned_comment:
                                print_info(
                                    f"📦 Item {item_pos.station}{relay_part}: {item_pos.grid_square} - {cleaned_comment}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                            else:
                                print_info(
                                    f"📦 Item {item_pos.station}{relay_part}: {item_pos.grid_square}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                        
                        # Status
                        elif aprs['status']:
                            status = aprs['status']
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            print_info(
                                f"💬 Status from {status.station}{relay_part}: {status.status_text}",
                                frame_num=frame_num,
                                buffer_mode=buffer_mode
                            )
                        
                        # Telemetry
                        elif aprs['telemetry']:
                            telemetry = aprs['telemetry']
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            analog_str = ",".join(str(v) for v in telemetry.analog)
                            print_info(
                                f"📊 Telemetry from {telemetry.station}{relay_part}: seq={telemetry.sequence} analog=[{analog_str}] digital={telemetry.digital}",
                                frame_num=frame_num,
                                buffer_mode=buffer_mode
                            )
                        
                        # Message
                        elif aprs['message']:
                            msg = aprs['message']
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            print_info(
                                f"📨 New APRS message from {msg.from_call}{relay_part}",
                                frame_num=frame_num,
                                buffer_mode=buffer_mode
                            )
                            
                            # Send automatic ACK if message has ID and AUTO_ACK is enabled
                            if msg.message_id and radio.cmd_processor.tnc_config.get("AUTO_ACK") == "ON":
                                try:
                                    await radio.cmd_processor._send_aprs_ack(msg.from_call, msg.message_id)
                                except Exception as e:
                                    print_debug(f"Failed to send ACK: {e}", level=2)
                        
                        # Weather and/or Position
                        else:
                            wx = aprs['weather']
                            pos = aprs['position']
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            
                            if wx and pos:
                                # Combined
                                combined = radio.aprs_manager.format_combined_notification(pos, wx, relay)
                                print_info(f"📍🌤️  {combined}", frame_num=frame_num, buffer_mode=buffer_mode)
                            elif wx:
                                # Weather only
                                print_info(f"🌤️  Weather update from {wx.station}{relay_part}", frame_num=frame_num, buffer_mode=buffer_mode)
                            elif pos:
                                # Position only
                                cleaned_comment = APRSFormatters.clean_position_comment(pos.comment)
                                if cleaned_comment:
                                    print_info(
                                        f"📍 Position from {pos.station}{relay_part}: {pos.grid_square} - {cleaned_comment}",
                                        frame_num=frame_num,
                                        buffer_mode=buffer_mode
                                    )
                                else:
                                    print_info(
                                        f"📍 Position from {pos.station}{relay_part}: {pos.grid_square}",
                                        frame_num=frame_num,
                                        buffer_mode=buffer_mode
                                    )

                    # Forward to bridges (all modes)
                    if radio.tnc_bridge:
                        try:
                            await radio.tnc_bridge.send_to_client(complete_frame)
                        except Exception as e:
                            print_error(f"TCP bridge error: {e}")

                    if getattr(radio, "agwpe_bridge", None):
                        try:
                            await radio.agwpe_bridge.send_monitored_frame(complete_frame)
                        except Exception as e:
                            print_error(f"AGWPE bridge error: {e}")

                    frames_processed += 1

                except ValueError:
                    # No closing delimiter found yet
                    if len(frame_buffer) > 2048:
                        if constants.DEBUG:
                            print_debug(
                                f"Buffer overflow ({len(frame_buffer)} bytes), discarding",
                                level=5,
                            )
                        frame_buffer.clear()
                    else:
                        if constants.DEBUG:
                            print_debug(
                                f"Incomplete frame in buffer ({len(frame_buffer)} bytes), waiting for more data",
                                level=5,
                            )
                    break

        except Exception as e:
            print_error(f"TNC monitor error: {e}")
            import traceback

            traceback.print_exc()
            # Clear buffer to prevent corruption from cascading
            frame_buffer.clear()
            if constants.DEBUG:
                print_debug("Buffer cleared due to error", level=2)


async def heartbeat_monitor(radio):
    """Periodic connection health check."""
    while radio.running:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            if not radio.running:
                break

            healthy = await radio.check_connection_health()

            if not healthy:
                print_error(
                    "Connection health check failed - consider restarting"
                )

        except Exception as e:
            print_error(f"Heartbeat monitor error: {e}")


async def autosave_monitor(radio):
    """Periodic auto-save of APRS database and frame buffer every 2 minutes.

    Uses async saves to avoid blocking the event loop. Saves run in thread pool.
    """
    AUTOSAVE_INTERVAL = 120  # 2 minutes (increased frequency for better data safety)

    while radio.running:
        try:
            await asyncio.sleep(AUTOSAVE_INTERVAL)

            if not radio.running:
                break

            # Save both database and frame buffer asynchronously (non-blocking)
            save_tasks = []

            # Save APRS database
            if hasattr(radio, 'aprs_manager') and radio.aprs_manager:
                save_tasks.append(radio.aprs_manager.save_database_async())

            # Save frame buffer
            if hasattr(radio, 'cmd_processor') and radio.cmd_processor and hasattr(radio.cmd_processor, 'frame_history'):
                save_tasks.append(radio.cmd_processor.frame_history.save_to_disk_async())

            # Run both saves concurrently
            if save_tasks:
                results = await asyncio.gather(*save_tasks, return_exceptions=True)
                # Check results (first is station count, second is None)
                if len(results) >= 1 and isinstance(results[0], int) and results[0] > 0:
                    print_debug(f"Auto-saved APRS database and frame buffer", level=3)

        except Exception as e:
            print_error(f"Auto-save monitor error: {e}")


async def gps_monitor(radio):
    """Monitor GPS and send beacons when enabled."""
    # Wait for command processor to be initialized
    while radio.running:
        if hasattr(radio, "cmd_processor") and radio.cmd_processor:
            break
        await asyncio.sleep(1)

    if not radio.running:
        return

    # Run GPS polling and beacon task
    await radio.cmd_processor.gps_poll_and_beacon_task()


async def message_retry_monitor(radio):
    """Monitor sent messages and retry those that haven't been acknowledged."""

    while radio.running:
        try:
            await asyncio.sleep(5)  # Check every 5 seconds

            if not radio.running:
                break

            # Get command processor if available
            if (
                not hasattr(radio, "cmd_processor")
                or radio.cmd_processor is None
            ):
                continue

            aprs_mgr = radio.cmd_processor.aprs_manager

            # Check for messages that have expired after final attempt
            expired = aprs_mgr.check_expired_messages()
            for msg in expired:
                aprs_mgr.mark_message_failed(msg)
                print_warning(
                    f"Message to {msg.to_call} failed after {msg.retry_count} attempts"
                )

            # Get messages that need retry
            pending = aprs_mgr.get_pending_retries()

            for msg in pending:
                # Format the APRS message
                padded_to = msg.to_call.ljust(9)

                # Check if this is an ACK (no message ID) or regular message
                if msg.message_id is None:
                    # ACK message - format as :CALL___:ackXXXXX (no message ID on ACK itself)
                    aprs_message = f":{padded_to}:{msg.message}"
                    print_debug(
                        f"Retrying ACK to {msg.to_call} (attempt {msg.retry_count + 1}/{aprs_mgr.max_retries}): {msg.message}",
                        level=5,
                    )
                else:
                    # Regular message - format with message ID
                    aprs_message = f":{padded_to}:{msg.message}{{{msg.message_id}"
                    print_debug(
                        f"Retrying message to {msg.to_call} (attempt {msg.retry_count + 1}/{aprs_mgr.max_retries}): {msg.message[:30]}...",
                        level=5,
                    )

                # Resend the message
                await radio.send_aprs(
                    aprs_mgr.my_callsign, aprs_message, to_call="APRS"
                )

                # Update retry tracking
                aprs_mgr.update_message_retry(msg)

        except Exception as e:
            print_debug(f"Message retry monitor error: {e}", level=2)


async def connection_watcher(radio):
    """Aggressively monitor BLE connection state."""
    while radio.running:
        try:
            await asyncio.sleep(5)  # Check every 5 seconds

            if not radio.running:
                break

            # Check if we're still actually connected (BLE mode only)
            if radio.client and not radio.client.is_connected:
                print_error("Connection watcher: BLE disconnected!")
                radio.running = False
                break

        except Exception as e:
            print_error(f"Connection watcher error: {e}")


async def command_loop(radio, auto_tnc=False, auto_connect=None, serial_mode=False):
    """Command input loop with pinned prompt."""
    processor = CommandProcessor(radio, serial_mode=serial_mode)

    # Register command processor with APRS manager for GPS access
    if hasattr(radio, 'aprs_manager') and radio.aprs_manager:
        radio.aprs_manager._cmd_processor = processor

    # Auto-connect to weather station if enabled
    if hasattr(processor, 'weather_manager') and processor.weather_manager.enabled:
        wx_address = processor.tnc_config.get("WX_ADDRESS")
        if wx_address:
            await processor.weather_manager.connect()

    # Display initial status screen
    await processor.show_startup_status()

    # Auto-enter TNC mode if requested
    if auto_tnc:
        await processor.cmd_tnc([], auto_connect=auto_connect)
        # After TNC mode exits, continue to regular command loop
        print_pt("")  # Add a blank line for spacing

    # Create tab completer
    completer = CommandCompleter(processor)

    # Create key bindings for "?" to show help
    kb = KeyBindings()

    @kb.add("?")
    def show_help(event):
        """Show context-sensitive help when '?' is pressed (IOS-style)."""

        buffer = event.current_buffer
        text_before_cursor = buffer.text[: buffer.cursor_position]

        # IOS-style context help: "command ?" shows options for next token
        # If text ends with space, show completions. Otherwise insert "?" literally
        # This allows: "debug ?" (show help) vs "msg K1MAL are you there?" (literal ?)
        if text_before_cursor.strip() and not text_before_cursor.endswith(' '):
            # Not asking for help - insert ? as regular character
            buffer.insert_text('?')
            return

        # Get completions at current position
        document = Document(
            text=buffer.text, cursor_position=buffer.cursor_position
        )
        completions = list(
            completer.get_completions(document, CompleteEvent())
        )

        # Display available options
        if completions:
            print_pt("\n<Available options>")
            for comp in completions:
                if comp.display_meta:
                    # Convert FormattedText to plain string
                    meta_text = (
                        to_plain_text(comp.display_meta)
                        if hasattr(comp.display_meta, "__iter__")
                        else str(comp.display_meta)
                    )
                    print_pt(f"  {comp.text:<20} {meta_text}")
                else:
                    print_pt(f"  {comp.text}")
            print_pt("")  # Blank line after help
        else:
            # No completions - show general help
            print_pt("\n<Type 'help' for command list>")
            print_pt("")

        # Redisplay the prompt with current text intact
        # This is done automatically by not calling validate_and_handle()

    session = PromptSession(
        completer=completer,
        complete_while_typing=False,  # Only complete on Tab
        key_bindings=kb,
    )

    with patch_stdout():
        while radio.running:
            try:
                # Build prompt with mode and unread message indicator
                mode_name = processor.console_mode
                unread = processor.aprs_manager.get_unread_count()
                if unread > 0:
                    prompt_html = f"<b><green>{mode_name}</green><yellow>({unread} msg)</yellow><green>&gt;</green></b> "
                else:
                    prompt_html = f"<b><green>{mode_name}&gt;</green></b> "

                line = await session.prompt_async(HTML(prompt_html))

                if line:
                    await processor.process(line)

            except EOFError:
                print_pt("")
                await processor.cmd_quit([])
                break
            except KeyboardInterrupt:
                print_pt("")
                await processor.cmd_quit([])
                break
            except Exception as e:
                print_error(f"Input error: {e}")


# === Main Application ===


async def main(auto_tnc=False, auto_connect=None, auto_debug=False,
               serial_port=None, serial_baud=9600, init_kiss=False,
               tcp_host=None, tcp_port=8001, radio_mac=None):
    # Enable debug mode if requested via command line
    if auto_debug:
        constants.DEBUG_LEVEL = 2
        constants.DEBUG = True
        print_info("Debug mode enabled at startup")

    print_header(f"FSY Packet Console v{constants.VERSION}")

    rx_queue = asyncio.Queue()
    tnc_queue = asyncio.Queue()

    # Transport and client setup
    transport = None
    client = None
    is_shutting_down = False

    # Load TNC config early to get RADIO_MAC if needed for BLE mode
    tnc_config_early = None
    if not serial_port and not tcp_host:
        # BLE mode - need to determine MAC address
        tnc_config_early = TNCConfig()

        # Determine MAC address: command-line overrides config
        if radio_mac:
            ble_mac = radio_mac
        elif tnc_config_early.settings.get("RADIO_MAC"):
            ble_mac = tnc_config_early.settings.get("RADIO_MAC")
        else:
            print_error("No radio MAC address configured")
            print_error("Set via command line: -r/--radio-mac MAC_ADDRESS")
            print_error("Or in TNC mode: RADIO_MAC 38:D2:00:01:62:C2")
            return

    # Serial mode
    if serial_port:
        print_pt(HTML(f"<gray>Serial KISS Mode: {serial_port} @ {serial_baud} baud...</gray>\n"))

        try:
            from src.transport import SerialTransport

            transport = SerialTransport(serial_port, serial_baud, tnc_queue)
            await transport.connect()
            print_info(f"Serial port connected: {serial_port}")

            # Initialize KISS mode if requested
            if init_kiss:
                print_info("Initializing TNC into KISS mode...")
                success = await transport.initialize_kiss_mode()
                if success:
                    print_info("TNC is in KISS mode and ready")
                else:
                    print_warning("KISS mode initialization may have failed - continuing anyway")

        except Exception as e:
            print_error(f"Failed to open serial port: {e}")
            return

    # TCP KISS TNC Client Mode
    elif tcp_host:
        print_pt(HTML(f"<gray>TCP KISS Mode: {tcp_host}:{tcp_port}...</gray>\n"))

        try:
            from src.transport import TCPTransport

            transport = TCPTransport(
                host=tcp_host,
                port=tcp_port,
                tnc_queue=tnc_queue
            )

            if not await transport.connect():
                print_error("Failed to connect to KISS TNC server")
                print_error("Verify Direwolf or remote TNC is running")
                return

            print_info(f"TCP KISS client ready")

        except Exception as e:
            print_error(f"Failed to connect to TCP TNC: {e}")
            return

    # BLE mode
    else:
        print_pt(HTML(f"<gray>Connecting to {ble_mac}...</gray>\n"))

        device = await BleakScanner.find_device_by_address(
            ble_mac, timeout=10.0
        )
        if not device:
            print_error(f"Device not found: {ble_mac}")
            print_error("Use 'bluetoothctl scan on' to find your radio's MAC address")
            return

        print_info(f"Found: {device.name}")

        async def handle_indication(sender, data):
            if constants.DEBUG:
                print_debug(
                    f"Radio notification from {sender.uuid}: {len(data)} bytes",
                    level=6,
                )
            await rx_queue.put(data)

        async def handle_tnc(sender, data):
            if constants.DEBUG:
                print_debug(
                    f"TNC notification from {sender.uuid}: {len(data)} bytes",
                    level=6,
                )
            await tnc_queue.put(data)

        def disconnected_callback(ble_client):
            """Called when BLE disconnects."""
            if not is_shutting_down:
                print_error("BLE disconnected!")

        try:
            client = BleakClient(
                device, timeout=20.0, disconnected_callback=disconnected_callback
            )
            await client.connect()
            print_info("Connected")

            await client.start_notify(RADIO_INDICATE_UUID, handle_indication)
            await client.start_notify(TNC_RX_UUID, handle_tnc)

            print_info("Notifications enabled")

            await asyncio.sleep(0.5)
            while not rx_queue.empty():
                await rx_queue.get()

            # Create BLE transport
            from src.transport import BLETransport
            transport = BLETransport(client, rx_queue, tnc_queue)

            # Auto-save the working MAC address to config (if different or not set)
            if tnc_config_early:
                stored_mac = tnc_config_early.settings.get("RADIO_MAC")
                if stored_mac != ble_mac:
                    print_info(f"Saving radio MAC {ble_mac} to config")
                    tnc_config_early.set("RADIO_MAC", ble_mac)
                    tnc_config_early.save()

        except Exception as e:
            print_error(f"BLE connection failed: {e}")
            return

    # Create radio controller with transport
    try:
        radio = RadioController(transport, rx_queue, tnc_queue)

        # Create shared AX25Adapter that will be used by both CommandProcessor and AGWPE
        # This prevents the two-adapter conflict where one sets _pending_connect
        # but the other receives the UA frames

        # Reuse the config from BLE setup if available, otherwise create new one
        tnc_config = tnc_config_early if tnc_config_early else TNCConfig()
        shared_ax25 = AX25Adapter(
            radio,
            get_mycall=lambda: tnc_config.get("MYCALL"),
            get_txdelay=lambda: tnc_config.get("TXDELAY"),
        )
        # Store on radio object so CommandProcessor can access it
        radio.shared_ax25 = shared_ax25

        # Create APRS manager early so web server can use it
        mycall = tnc_config.get("MYCALL") or "NOCALL"
        retry_count = int(tnc_config.get("RETRY") or "3")
        retry_fast = int(tnc_config.get("RETRY_FAST") or "20")
        retry_slow = int(tnc_config.get("RETRY_SLOW") or "600")
        radio.aprs_manager = APRSManager(mycall, max_retries=retry_count,
                                        retry_fast=retry_fast, retry_slow=retry_slow)

        # Create digipeater (read state from TNC config)
        digipeat_value = tnc_config.get("DIGIPEAT") or ""
        digipeat_enabled = digipeat_value.upper() == "ON"
        myalias = tnc_config.get("MYALIAS") or ""
        radio.digipeater = Digipeater(mycall, my_alias=myalias, enabled=digipeat_enabled)

        # Only start TNC/AGWPE bridges if NOT in TCP client mode
        # (TCP mode acts as client to external TNC, not a server for other apps)
        if not tcp_host:
            # Start TNC TCP bridge (with error handling to survive bind failures)
            try:
                tnc_host = tnc_config.get("TNC_HOST") or "0.0.0.0"
                tnc_port = int(tnc_config.get("TNC_PORT") or "8001")
                radio.tnc_bridge = TNCBridge(radio, port=tnc_port)
                await radio.tnc_bridge.start(host=tnc_host)
            except OSError as e:
                print_error(f"TNC bridge failed to bind to {tnc_host}:{tnc_port} - {e}")
                print_error(f"  Port may be in use or address unavailable. Fix with: TNC_HOST/TNC_PORT")
                radio.tnc_bridge = None
            except Exception as e:
                print_error(f"Failed to start TNC bridge: {e}")
                radio.tnc_bridge = None

            # Start AGWPE-compatible bridge (with error handling to survive bind failures)
            try:
                from src.agwpe_bridge import AGWPEBridge

                agwpe_host = tnc_config.get("AGWPE_HOST") or "0.0.0.0"
                agwpe_port = int(tnc_config.get("AGWPE_PORT") or "8000")
                radio.agwpe_bridge = AGWPEBridge(
                    radio,
                    get_mycall=lambda: tnc_config.get("MYCALL"),
                    get_txdelay=lambda: tnc_config.get("TXDELAY"),
                    ax25_adapter=shared_ax25,  # Use the shared adapter
                )
                started = await radio.agwpe_bridge.start(host=agwpe_host, port=agwpe_port)
                if not started:
                    print_error("AGWPE bridge failed to start")
                    radio.agwpe_bridge = None
            except OSError as e:
                print_error(f"AGWPE bridge failed to bind to {agwpe_host}:{agwpe_port} - {e}")
                print_error(f"  Port may be in use or address unavailable. Fix with: AGWPE_HOST/AGWPE_PORT")
                radio.agwpe_bridge = None
            except Exception as e:
                print_error(f"Failed to start AGWPE bridge: {e}")
                radio.agwpe_bridge = None
        else:
            print_info("TNC/AGWPE bridges disabled (TCP client mode)")

        # Start Web UI Server (with error handling to survive bind failures)
        try:
            webui_host = tnc_config.get("WEBUI_HOST") or "0.0.0.0"
            webui_port = int(tnc_config.get("WEBUI_PORT") or "8002")
            print_info(f"Starting Web UI server on port {webui_port}...")

            radio.web_server = WebServer(
                radio=radio,
                aprs_manager=radio.aprs_manager,
                get_mycall=lambda: tnc_config.get("MYCALL"),
                get_mylocation=lambda: tnc_config.get("MYLOCATION"),
                get_wxtrend=lambda: tnc_config.get("WXTREND")
            )

            started = await radio.web_server.start(host=webui_host, port=webui_port)
            if started:
                print_info(f"Web UI started on http://{webui_host}:{webui_port}")
            else:
                print_error("Web UI failed to start")
                radio.web_server = None
        except OSError as e:
            print_error(f"Web UI failed to bind to {webui_host}:{webui_port} - {e}")
            print_error(f"  Port may be in use or address unavailable. Fix with: WEBUI_HOST/WEBUI_PORT")
            radio.web_server = None
        except Exception as e:
            print_error(f"Failed to start Web UI: {e}")
            radio.web_server = None

        # Auto-connect to weather station if enabled
        # Wait until CommandProcessor is created (happens in processor creation below)
        # We'll connect after command_loop starts

        print_info("Monitoring TNC traffic...")

        # Create background task list
        tasks = [
            asyncio.create_task(tnc_monitor(tnc_queue, radio)),
            asyncio.create_task(message_retry_monitor(radio)),
            asyncio.create_task(autosave_monitor(radio)),
            asyncio.create_task(gps_monitor(radio)),  # Runs in both BLE and serial modes
        ]

        # Add BLE-only monitors
        if not serial_port and not tcp_host:
            tasks.extend([
                asyncio.create_task(connection_watcher(radio)),
                asyncio.create_task(heartbeat_monitor(radio)),
            ])

        # Add command loop
        tasks.append(
            asyncio.create_task(
                command_loop(
                    radio, auto_tnc=auto_tnc, auto_connect=auto_connect,
                    serial_mode=(serial_port is not None or tcp_host is not None)
                )
            )
        )

        # Wait for command loop to finish (last task)
        await tasks[-1]

        # Mark as shutting down to suppress disconnect error
        is_shutting_down = True

        # Cancel other tasks
        for task in tasks[:-1]:  # All except command_loop
            task.cancel()

        # Stop TNC bridge (if started)
        if hasattr(radio, 'tnc_bridge') and radio.tnc_bridge:
            await radio.tnc_bridge.stop()

        # Stop AGWPE bridge (if started)
        if hasattr(radio, 'agwpe_bridge') and radio.agwpe_bridge:
            await radio.agwpe_bridge.stop()

        # Shutdown web server
        if hasattr(radio, 'web_server') and radio.web_server:
            print_info("Shutting down Web UI...")
            await radio.web_server.stop()

        # Save frame buffer to disk (async for faster shutdown)
        if hasattr(radio, 'cmd_processor') and radio.cmd_processor:
            print_info("Saving frame buffer...")
            await radio.cmd_processor.frame_history.save_to_disk_async()

        print_info("Disconnecting...")

        # Close transport
        if transport:
            await transport.close()

    except Exception as e:
        print_error(f"{type(e).__name__}: {e}")

        traceback.print_exc()


def run(auto_tnc=False, auto_connect=None, auto_debug=False,
        serial_port=None, serial_baud=9600, tcp_host=None, tcp_port=8001,
        radio_mac=None):
    """Entry point for the console application."""
    def sigterm_handler(signum, frame):
        """Handle SIGTERM by raising SIGINT to interrupt the prompt."""
        # Raise SIGINT to trigger KeyboardInterrupt in the blocking prompt
        signal.raise_signal(signal.SIGINT)

    # Register SIGTERM handler
    signal.signal(signal.SIGTERM, sigterm_handler)

    try:
        asyncio.run(
            main(
                auto_tnc=auto_tnc,
                auto_connect=auto_connect,
                auto_debug=auto_debug,
                serial_port=serial_port,
                serial_baud=serial_baud,
                tcp_host=tcp_host,
                tcp_port=tcp_port,
                radio_mac=radio_mac,
            )
        )
    except KeyboardInterrupt:
        print_pt(HTML("\n<yellow>Interrupted by user</yellow>"))

    print_pt(HTML("<gray>Goodbye!</gray>"))
