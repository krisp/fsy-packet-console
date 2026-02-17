"""APRS Message and Weather Tracking Manager.

Tracks APRS messages sent to our station and weather reports from other stations.
"""

import asyncio
import gzip
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# Try to use ujson for faster serialization (3-5x speedup)
try:
    import ujson
    HAS_UJSON = True
except ImportError:
    HAS_UJSON = False

import src.constants as constants
from src.device_id import get_device_identifier
from src.utils import print_debug, print_error, print_info

# Import models and utilities from the modular package
from .models import (
    APRSMessage, APRSPosition, APRSWeather, APRSStatus,
    APRSTelemetry, APRSStation
)
from .duplicate_detector import DuplicateDetector, DUPLICATE_WINDOW
from .geo_utils import latlon_to_maidenhead, maidenhead_to_latlon, calculate_dew_point
from .formatters import APRSFormatters
from .weather_forecast import calculate_zambretti_code, adjust_pressure_to_sea_level, ZAMBRETTI_FORECASTS
from .digipeater_stats import DigipeaterStats, DigipeaterActivity

# Note: Models are imported from src/aprs/models.py to ensure consistency
# across the codebase. The dataclass definitions below were removed to avoid
# duplicate definitions and potential isinstance() failures.

# Message retry configuration
MESSAGE_RETRY_FAST = 20  # seconds between fast retry attempts (not digipeated)
MESSAGE_RETRY_SLOW = 600  # seconds between slow retry attempts (digipeated but not ACKed) - 10 minutes
MESSAGE_MAX_RETRIES = (
    3  # maximum number of transmission attempts (original + 2 retries)
)

def ensure_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert naive datetime to UTC-aware datetime.

    Args:
        dt: Datetime object (may be naive or timezone-aware)

    Returns:
        UTC-aware datetime, or None if input is None
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Naive datetime - assume UTC
        return dt.replace(tzinfo=timezone.utc)
    elif dt.tzinfo != timezone.utc:
        # Different timezone - convert to UTC
        return dt.astimezone(timezone.utc)
    else:
        # Already UTC-aware
        return dt



class APRSManager:
    """Manages APRS messages and weather tracking."""

    def __init__(self, my_callsign: str, max_retries: int = MESSAGE_MAX_RETRIES,
                 retry_fast: int = MESSAGE_RETRY_FAST, retry_slow: int = MESSAGE_RETRY_SLOW):
        """Initialize APRS manager.

        Args:
            my_callsign: Our callsign (without SSID, or with SSID)
            max_retries: Maximum number of message retry attempts (default: 3)
            retry_fast: Fast retry timeout in seconds for non-digipeated messages (default: 20)
            retry_slow: Slow retry timeout in seconds for digipeated but not ACKed messages (default: 600)
        """
        self.my_callsign = my_callsign.upper()
        # Support both with and without SSID
        self.my_callsign_base = my_callsign.split("-")[0].upper()

        # Message retry configuration
        self.max_retries = max_retries
        self.retry_fast = retry_fast  # Timeout for messages not yet digipeated
        self.retry_slow = retry_slow  # Timeout for messages digipeated but not ACKed

        # Migration mode flag (disables expensive operations during bulk replay)
        self._migration_mode = False

        # Storage
        self.messages: List[APRSMessage] = []  # Messages addressed to us
        self.monitored_messages: List[APRSMessage] = (
            []
        )  # All messages (monitoring)
        self.weather_reports: Dict[str, APRSWeather] = (
            {}
        )  # station -> latest weather
        self.position_reports: Dict[str, APRSPosition] = (
            {}
        )  # station -> latest position
        self.stations: Dict[str, APRSStation] = (
            {}
        )  # station -> comprehensive info

        # Duplicate packet detection
        self.duplicate_detector = DuplicateDetector()
        self.duplicate_detector.set_stations_reference(self.stations)
        self.duplicate_detector.set_manager_reference(self)

        # Digipeater statistics
        self.digipeater_stats = DigipeaterStats(
            session_start=datetime.now(timezone.utc)
        )

        # Command processor reference (for GPS access via web API)
        self._cmd_processor = None

        # Web broadcast callback for real-time updates
        self._web_broadcast = None

        # Database file location (GZIP compressed for efficiency)
        self.db_file = os.path.expanduser("~/.aprs_stations.json.gz")
        self.db_file_legacy = os.path.expanduser("~/.aprs_stations.json")

        # Migration state (populated by load_database or migration system)
        self.migrations = {}

        # Async save lock to prevent concurrent saves
        self._save_lock = asyncio.Lock()
        self._last_save_time = 0  # Track last save for monitoring

        # Note: Database will be loaded explicitly with load_database()
        # or load_database_async() after initialization

    def set_web_broadcast_callback(self, callback):
        """Register callback for web UI real-time updates.

        Args:
            callback: Async function(event_type: str, data: dict) to broadcast events
        """
        self._web_broadcast = callback

    def _broadcast_update(self, event_type: str, data):
        """Broadcast update to web clients if callback is registered.

        Args:
            event_type: Type of event (station_update, weather_update, message_received)
            data: Event data (station object, message object, etc.)
        """
        if self._web_broadcast:
            try:
                # Serialize data using late import to avoid circular dependency
                # Import is cached after first call, so performance impact is minimal
                if event_type in ('station_update', 'weather_update'):
                    from src.web_api import serialize_station
                    serialized = serialize_station(data)
                elif event_type == 'message_received':
                    from src.web_api import serialize_message
                    serialized = serialize_message(data)
                else:
                    serialized = data

                # Create task to run broadcast without blocking
                asyncio.create_task(self._web_broadcast(event_type, serialized))
            except Exception:
                # Silently ignore broadcast errors to not disrupt normal operation
                pass

    async def save_database_async(self):
        """Save APRS station database to disk asynchronously (non-blocking).

        Uses asyncio.to_thread to run the blocking save operation in a thread pool,
        preventing event loop blocking. Includes lock to prevent concurrent saves.

        Returns:
            Number of stations saved, or 0 on error
        """
        # Prevent concurrent saves
        if self._save_lock.locked():
            print_debug("Database save already in progress, skipping", level=3)
            return 0

        async with self._save_lock:
            save_start = time.time()
            try:
                # Run blocking save in thread pool
                count = await asyncio.to_thread(self.save_database)
                save_duration = time.time() - save_start
                self._last_save_time = time.time()
                print_debug(f"Database saved asynchronously in {save_duration:.2f}s ({count} stations)", level=3)
                return count
            except Exception as e:
                print_error(f"Async database save failed: {e}")
                return 0

    def save_database(self):
        """Save APRS station database to disk (blocking).

        Saves the stations dictionary and monitored messages to GZIP-compressed
        JSON format with datetime serialization. Uses atomic write to prevent
        corruption.

        Note: This is a blocking operation. Use save_database_async() for non-blocking saves.

        Returns:
            Number of stations saved, or 0 on error
        """
        try:
            # Check directory write access first
            db_dir = os.path.dirname(self.db_file)
            if not os.path.exists(db_dir):
                try:
                    os.makedirs(db_dir, exist_ok=True)
                except Exception as e:
                    print_error(f"Cannot create database directory {db_dir}: {e}")
                    return 0

            if not os.access(db_dir, os.W_OK):
                print_error(f"No write permission for database directory {db_dir}")
                return 0

            # Check if existing file is writable
            if os.path.exists(self.db_file) and not os.access(self.db_file, os.W_OK):
                print_error(f"No write permission for database file {self.db_file}")
                return 0

            # Create snapshots of data structures to prevent "dictionary changed size during iteration"
            # These can be modified by the event loop while save runs in thread pool
            stations_snapshot = dict(self.stations)
            messages_snapshot = list(self.monitored_messages)

            # Prepare data for serialization
            data = {
                "stations": {},
                "messages": [],
                "migrations": getattr(self, 'migrations', {}),  # Migration state
                "digipeater_stats": self.digipeater_stats.to_dict(),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }

            # Convert stations to JSON-serializable format
            for callsign, station in stations_snapshot.items():
                station_data = {
                    "callsign": station.callsign,
                    "first_heard": station.first_heard.isoformat(),
                    "last_heard": station.last_heard.isoformat(),
                    "messages_received": station.messages_received,
                    "messages_sent": station.messages_sent,
                    "packets_heard": station.packets_heard,
                    "device": station.device,
                    "digipeaters_heard_by": station.digipeaters_heard_by,
                    "is_digipeater": station.is_digipeater,
                    # NOTE: The following are @property fields computed from receptions:
                    # - zero_hop_packet_count, relay_paths, heard_direct, hop_count
                    # - heard_zero_hop, last_heard_zero_hop, digipeater_path, digipeater_paths
                    # They are NOT saved to reduce database size and prevent inconsistencies
                }

                # Add position data if present
                if station.last_position:
                    pos = station.last_position
                    station_data["last_position"] = {
                        "timestamp": pos.timestamp.isoformat(),
                        "station": pos.station,
                        "latitude": pos.latitude,
                        "longitude": pos.longitude,
                        "altitude": pos.altitude,
                        "symbol_table": pos.symbol_table,
                        "symbol_code": pos.symbol_code,
                        "comment": pos.comment,
                        "grid_square": pos.grid_square,
                        "device": pos.device,
                    }

                # Add position history if present (use list comprehension)
                if station.position_history:
                    station_data["position_history"] = [
                        {
                            "timestamp": pos.timestamp.isoformat(),
                            "station": pos.station,
                            "latitude": pos.latitude,
                            "longitude": pos.longitude,
                            "altitude": pos.altitude,
                            "symbol_table": pos.symbol_table,
                            "symbol_code": pos.symbol_code,
                            "comment": pos.comment,
                            "grid_square": pos.grid_square,
                            "device": pos.device,
                        }
                        for pos in station.position_history
                    ]

                # Add weather data if present
                if station.last_weather:
                    wx = station.last_weather
                    station_data["last_weather"] = {
                        "timestamp": wx.timestamp.isoformat(),
                        "station": wx.station,
                        "latitude": wx.latitude,
                        "longitude": wx.longitude,
                        "temperature": wx.temperature,
                        "humidity": wx.humidity,
                        "pressure": wx.pressure,
                        "wind_speed": wx.wind_speed,
                        "wind_direction": wx.wind_direction,
                        "wind_gust": wx.wind_gust,
                        "rain_1h": wx.rain_1h,
                        "rain_24h": wx.rain_24h,
                        "rain_since_midnight": wx.rain_since_midnight,
                        "raw_data": wx.raw_data,
                    }

                # Add weather history if present (use list comprehension)
                if station.weather_history:
                    station_data["weather_history"] = [
                        {
                            "timestamp": wx.timestamp.isoformat(),
                            "station": wx.station,
                            "latitude": wx.latitude,
                            "longitude": wx.longitude,
                            "temperature": wx.temperature,
                            "humidity": wx.humidity,
                            "pressure": wx.pressure,
                            "wind_speed": wx.wind_speed,
                            "wind_direction": wx.wind_direction,
                            "wind_gust": wx.wind_gust,
                            "rain_1h": wx.rain_1h,
                            "rain_24h": wx.rain_24h,
                            "rain_since_midnight": wx.rain_since_midnight,
                            "raw_data": wx.raw_data,
                        }
                        for wx in station.weather_history
                    ]

                # Add status data if present
                if station.last_status:
                    status = station.last_status
                    station_data["last_status"] = {
                        "timestamp": status.timestamp.isoformat(),
                        "station": status.station,
                        "status_text": status.status_text,
                    }

                # Add telemetry data if present
                if station.last_telemetry:
                    telem = station.last_telemetry
                    station_data["last_telemetry"] = {
                        "timestamp": telem.timestamp.isoformat(),
                        "station": telem.station,
                        "sequence": telem.sequence,
                        "analog": telem.analog,
                        "digital": telem.digital,
                    }

                # Add telemetry sequence if present (use list comprehension)
                if station.telemetry_sequence:
                    station_data["telemetry_sequence"] = [
                        {
                            "timestamp": telem.timestamp.isoformat(),
                            "station": telem.station,
                            "sequence": telem.sequence,
                            "analog": telem.analog,
                            "digital": telem.digital,
                        }
                        for telem in station.telemetry_sequence
                    ]

                # Add reception events (NEW: single source of truth)
                # Use list comprehension for faster serialization
                if station.receptions:
                    station_data["receptions"] = [
                        {
                            "timestamp": r.timestamp.isoformat(),
                            "hop_count": r.hop_count,
                            "direct_rf": r.direct_rf,
                            "relay_call": r.relay_call,
                            "digipeater_path": r.digipeater_path,
                            "packet_type": r.packet_type,
                            "frame_number": r.frame_number,
                        }
                        for r in station.receptions
                    ]

                data["stations"][callsign] = station_data

            # Save monitored messages
            for msg in messages_snapshot:
                msg_data = {
                    "timestamp": msg.timestamp.isoformat(),
                    "from_call": msg.from_call,
                    "to_call": msg.to_call,
                    "message": msg.message,
                    "message_id": msg.message_id,
                    "direction": msg.direction,
                    "ack_received": msg.ack_received,
                    "failed": msg.failed,
                    "retry_count": msg.retry_count,
                    "last_sent": (
                        msg.last_sent.isoformat() if msg.last_sent else None
                    ),
                    "read": msg.read,
                }
                data["messages"].append(msg_data)

            # Write to GZIP compressed file (fast compression for quick saves)
            # Use atomic write: write to temp file, then rename
            temp_file = self.db_file + ".tmp"

            try:
                # Write to temporary file with fast compression (level 1 is 10-20x faster than level 6)
                with gzip.open(temp_file, "wt", encoding="utf-8", compresslevel=1) as f:
                    # Use ujson for 3-5x faster serialization if available
                    if HAS_UJSON:
                        f.write(ujson.dumps(data, ensure_ascii=False))
                    else:
                        json.dump(data, f, separators=(',', ':'))  # Compact format

                # Atomic rename (overwrites existing file safely)
                os.replace(temp_file, self.db_file)

            except Exception as write_error:
                # Clean up temp file on failure
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass
                raise write_error

            # Return count for confirmation message
            return len(data["stations"])

        except PermissionError as e:
            print_error(f"Permission denied writing APRS database: {e}")
            print_error(f"Check file permissions on {self.db_file}")
            return 0
        except IOError as e:
            print_error(f"I/O error writing APRS database: {e}")
            print_error(f"Check disk space and file system")
            return 0
        except Exception as e:
            # Don't crash on save errors, just log with details
            print_error(f"Failed to save APRS database: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return 0

    async def load_database_async(self):
        """Load APRS station database from disk asynchronously (non-blocking).

        Uses asyncio.to_thread to run the blocking load operation in a thread pool,
        allowing parallel loading with other startup tasks.
        """
        print_info("Loading APRS database...")
        await asyncio.to_thread(self.load_database)

    def load_database(self):
        """Load APRS station database from disk (blocking).

        Loads previously saved stations, positions, and weather data.
        If file doesn't exist or is corrupt, starts with empty database.

        Supports both GZIP compressed (.json.gz) and legacy plain JSON (.json) formats.
        Automatically migrates from .json to .json.gz on first save.

        Note: For async loading during startup, use load_database_async().
        """
        load_start = time.time()

        # Try GZIP compressed file first (new format)
        if os.path.exists(self.db_file):
            try:
                decompress_start = time.time()
                with gzip.open(self.db_file, "rt", encoding="utf-8") as f:
                    # Use ujson for faster deserialization if available
                    if HAS_UJSON:
                        data = ujson.loads(f.read())
                    else:
                        data = json.load(f)
                decompress_time = time.time() - decompress_start
                print_info(f"Database decompression: {decompress_time:.2f}s")
            except Exception as e:
                print_info(f"Warning: Failed to load GZIP database: {e}")
                return
        # Fall back to legacy plain JSON file (backward compatibility)
        elif os.path.exists(self.db_file_legacy):
            try:
                with open(self.db_file_legacy, "r") as f:
                    # Use ujson for faster deserialization if available
                    if HAS_UJSON:
                        data = ujson.loads(f.read())
                    else:
                        data = json.load(f)
                print_info(f"Loaded legacy JSON database (will migrate to GZIP on next save)")
            except Exception as e:
                print_info(f"Warning: Failed to load legacy database: {e}")
                return
        else:
            return  # No saved database, start fresh

        # Initialize migration state from database
        self.migrations = data.get('migrations', {})
        if 'migrations_applied' not in self.migrations:
            self.migrations['migrations_applied'] = {}

        try:
            parse_start = time.time()

            station_count = len(data.get("stations", {}))
            total_positions = 0
            total_weather = 0
            total_telemetry = 0

            # Restore stations
            for callsign, station_data in data.get("stations", {}).items():
                # Create station object with only the new fields
                station = APRSStation(
                    callsign=station_data["callsign"],
                    first_heard=ensure_utc_aware(
                        datetime.fromisoformat(station_data["first_heard"])
                    ),
                    last_heard=ensure_utc_aware(
                        datetime.fromisoformat(station_data["last_heard"])
                    ),
                    messages_received=station_data.get("messages_received", 0),
                    messages_sent=station_data.get("messages_sent", 0),
                    packets_heard=station_data.get("packets_heard", 0),
                    device=station_data.get("device"),
                    is_digipeater=station_data.get("is_digipeater", False),
                    digipeaters_heard_by=station_data.get("digipeaters_heard_by", []),
                )

                # Restore position if present
                if "last_position" in station_data:
                    pos_data = station_data["last_position"]
                    station.last_position = APRSPosition(
                        timestamp=ensure_utc_aware(
                            datetime.fromisoformat(pos_data["timestamp"])
                        ),
                        station=pos_data["station"],
                        latitude=pos_data["latitude"],
                        longitude=pos_data["longitude"],
                        altitude=pos_data.get("altitude"),
                        symbol_table=pos_data.get("symbol_table", "/"),
                        symbol_code=pos_data.get("symbol_code", ">"),
                        comment=pos_data.get("comment", ""),
                        grid_square=pos_data.get("grid_square", ""),
                        device=pos_data.get("device"),
                    )
                    # Also add to position_reports dict
                    self.position_reports[callsign] = station.last_position

                # Restore weather if present
                if "last_weather" in station_data:
                    wx_data = station_data["last_weather"]
                    station.last_weather = APRSWeather(
                        timestamp=ensure_utc_aware(datetime.fromisoformat(wx_data["timestamp"])),
                        station=wx_data["station"],
                        latitude=wx_data.get("latitude"),
                        longitude=wx_data.get("longitude"),
                        temperature=wx_data.get("temperature"),
                        humidity=wx_data.get("humidity"),
                        pressure=wx_data.get("pressure"),
                        wind_speed=wx_data.get("wind_speed"),
                        wind_direction=wx_data.get("wind_direction"),
                        wind_gust=wx_data.get("wind_gust"),
                        rain_1h=wx_data.get("rain_1h"),
                        rain_24h=wx_data.get("rain_24h"),
                        rain_since_midnight=wx_data.get("rain_since_midnight"),
                        raw_data=wx_data.get("raw_data", ""),
                    )

                    # Migration: Fix invalid pressure values from old parsing bug
                    if station.last_weather.pressure is not None:
                        if station.last_weather.pressure < 900 or station.last_weather.pressure > 1100:
                            # Invalid pressure, try to reparse from raw_data
                            corrected = _parse_pressure_from_raw(station.last_weather.raw_data)
                            if corrected is not None:
                                print_info(f"Migrated pressure for {callsign}: {station.last_weather.pressure:.1f} → {corrected:.1f} mb")
                                station.last_weather.pressure = corrected

                    # Also add to weather_reports dict
                    self.weather_reports[callsign] = station.last_weather

                # Restore weather history if present
                if "weather_history" in station_data:
                    station.weather_history = []
                    for wx_data in station_data["weather_history"]:
                        wx = APRSWeather(
                            timestamp=ensure_utc_aware(datetime.fromisoformat(wx_data["timestamp"])),
                            station=wx_data["station"],
                            latitude=wx_data.get("latitude"),
                            longitude=wx_data.get("longitude"),
                            temperature=wx_data.get("temperature"),
                            humidity=wx_data.get("humidity"),
                            pressure=wx_data.get("pressure"),
                            wind_speed=wx_data.get("wind_speed"),
                            wind_direction=wx_data.get("wind_direction"),
                            wind_gust=wx_data.get("wind_gust"),
                            rain_1h=wx_data.get("rain_1h"),
                            rain_24h=wx_data.get("rain_24h"),
                            rain_since_midnight=wx_data.get("rain_since_midnight"),
                            raw_data=wx_data.get("raw_data", ""),
                        )

                        # Migration: Fix invalid pressure values in history
                        if wx.pressure is not None:
                            if wx.pressure < 900 or wx.pressure > 1100:
                                corrected = _parse_pressure_from_raw(wx.raw_data)
                                if corrected is not None:
                                    wx.pressure = corrected

                        station.weather_history.append(wx)
                        total_weather += 1

                # Restore position history if present
                if "position_history" in station_data:
                    station.position_history = []
                    for pos_data in station_data["position_history"]:
                        pos = APRSPosition(
                            timestamp=ensure_utc_aware(datetime.fromisoformat(pos_data["timestamp"])),
                            station=pos_data["station"],
                            latitude=pos_data["latitude"],
                            longitude=pos_data["longitude"],
                            altitude=pos_data.get("altitude"),
                            symbol_table=pos_data.get("symbol_table", "/"),
                            symbol_code=pos_data.get("symbol_code", ">"),
                            comment=pos_data.get("comment", ""),
                            grid_square=pos_data.get("grid_square", ""),
                            device=pos_data.get("device"),
                        )
                        station.position_history.append(pos)
                        total_positions += 1

                # Restore status if present
                if "last_status" in station_data:
                    status_data = station_data["last_status"]
                    station.last_status = APRSStatus(
                        timestamp=ensure_utc_aware(
                            datetime.fromisoformat(status_data["timestamp"])
                        ),
                        station=status_data["station"],
                        status_text=status_data["status_text"],
                    )

                # Restore telemetry if present
                if "last_telemetry" in station_data:
                    telem_data = station_data["last_telemetry"]
                    station.last_telemetry = APRSTelemetry(
                        timestamp=ensure_utc_aware(
                            datetime.fromisoformat(telem_data["timestamp"])
                        ),
                        station=telem_data["station"],
                        sequence=telem_data["sequence"],
                        analog=telem_data["analog"],
                        digital=telem_data["digital"],
                    )

                # Restore telemetry sequence if present
                if "telemetry_sequence" in station_data:
                    station.telemetry_sequence = []
                    for telem_data in station_data["telemetry_sequence"]:
                        telem = APRSTelemetry(
                            timestamp=ensure_utc_aware(
                                datetime.fromisoformat(telem_data["timestamp"])
                            ),
                            station=telem_data["station"],
                            sequence=telem_data["sequence"],
                            analog=telem_data["analog"],
                            digital=telem_data["digital"],
                        )
                        station.telemetry_sequence.append(telem)
                        total_telemetry += 1

                # Restore reception events (NEW: single source of truth)
                if "receptions" in station_data:
                    from src.aprs.models import ReceptionEvent
                    for rx_data in station_data["receptions"]:
                        reception = ReceptionEvent(
                            timestamp=ensure_utc_aware(
                                datetime.fromisoformat(rx_data["timestamp"])
                            ),
                            hop_count=rx_data["hop_count"],
                            direct_rf=rx_data["direct_rf"],
                            relay_call=rx_data.get("relay_call"),
                            digipeater_path=rx_data.get("digipeater_path", []),
                            packet_type=rx_data.get("packet_type", "unknown"),
                            frame_number=rx_data.get("frame_number"),
                        )
                        station.receptions.append(reception)

                # Add station to dictionary
                self.stations[callsign] = station

            # Restore messages
            for msg_data in data.get("messages", []):
                # Parse last_sent timestamp if present
                last_sent = None
                if msg_data.get("last_sent"):
                    try:
                        last_sent = ensure_utc_aware(
                            datetime.fromisoformat(msg_data["last_sent"])
                        )
                    except Exception:
                        pass

                msg = APRSMessage(
                    timestamp=ensure_utc_aware(
                        datetime.fromisoformat(msg_data["timestamp"])
                    ),
                    from_call=msg_data["from_call"],
                    to_call=msg_data["to_call"],
                    message=msg_data["message"],
                    message_id=msg_data.get("message_id"),
                    direction=msg_data.get(
                        "direction", "received"
                    ),  # Default to 'received' for old data
                    ack_received=msg_data.get("ack_received", False),
                    failed=msg_data.get("failed", False),
                    retry_count=msg_data.get("retry_count", 0),
                    last_sent=last_sent,
                    read=msg_data.get("read", False),
                )
                self.monitored_messages.append(msg)
                # Add to personal messages if addressed to us (received) or from us (sent)
                if msg.direction == "sent" or self.is_message_for_me(
                    msg.to_call
                ):
                    self.messages.append(msg)

            # Note: migration state already loaded at line ~510 above
            # (not reassigned here to avoid overwriting mutations)

            # Restore digipeater stats
            if "digipeater_stats" in data:
                self.digipeater_stats = DigipeaterStats.from_dict(
                    data["digipeater_stats"]
                )
                # Ensure timestamps are UTC-aware
                self.digipeater_stats.session_start = ensure_utc_aware(
                    self.digipeater_stats.session_start
                )
                # Ensure activity timestamps are UTC-aware
                for activity in self.digipeater_stats.activities:
                    activity.timestamp = ensure_utc_aware(activity.timestamp)
            else:
                # Initialize if missing (backwards compatibility)
                self.digipeater_stats = DigipeaterStats(
                    session_start=datetime.now(timezone.utc)
                )

            # Recompute aggregates after loading
            self._recompute_digipeater_aggregates()

            # Success message
            parse_time = time.time() - parse_start
            load_time = time.time() - load_start

            message_count = len(data.get("messages", []))
            if station_count > 0 or message_count > 0:
                saved_at = data.get("saved_at", "unknown time")
                print_info(
                    f"Loaded {station_count} station(s), {message_count} message(s) from APRS database (saved {saved_at})"
                )
                print_info(
                    f"  Histories: {total_positions} positions, {total_weather} weather, {total_telemetry} telemetry"
                )
                print_info(
                    f"  Parse time: {parse_time:.2f}s, Total load time: {load_time:.2f}s"
                )

        except Exception as e:
            # Don't crash on load errors, just start fresh
            print_info(f"Warning: Failed to load APRS database: {e}")
            self.stations.clear()
            self.position_reports.clear()
            self.weather_reports.clear()

    def is_message_for_me(self, to_call: str) -> bool:
        """Check if a message is addressed to our callsign.

        Args:
            to_call: Destination callsign

        Returns:
            True if message is for us
        """
        to_call_upper = to_call.upper().strip()

        # Normalize callsigns: K1FSY and K1FSY-0 are equivalent (SSID 0 is implicit)
        # All other SSIDs are distinct stations
        def normalize_ssid(callsign):
            """Add explicit -0 if no SSID present."""
            return callsign if "-" in callsign else callsign + "-0"

        to_call_normalized = normalize_ssid(to_call_upper)
        my_call_normalized = normalize_ssid(self.my_callsign)

        # Exact match (with SSID normalization)
        result = (to_call_normalized == my_call_normalized)

        # Also match if the to_call is just the base callsign (no SSID)
        # In APRS, a message addressed to "K1FSY" should be received by
        # K1FSY-5, K1FSY-9, etc.
        if not result and "-" not in to_call_upper:
            result = (to_call_upper == self.my_callsign_base)

        print_debug(
            f"is_message_for_me: to_call='{to_call}' -> '{to_call_normalized}', my_callsign='{my_call_normalized}', result={result}",
            level=5,
        )

        return result

    def record_digipeater_path(self, callsign: str, digipeater_path: List[str]):
        """Record digipeater paths for a station (used even for duplicate packets).

        This lightweight method ONLY updates digipeater tracking without full packet
        processing. This ensures digipeater coverage data is accurate even when
        duplicate suppression is active.

        Stores:
        - Complete digipeater path (for analysis)
        - First hop only in digipeaters_heard_by (for coverage circles)

        Args:
            callsign: Station callsign
            digipeater_path: List of digipeater callsigns from AX.25 path
        """
        if not digipeater_path:
            return  # No digipeaters to record

        # Strip asterisk from callsign (APRS path marker, not part of callsign)
        callsign_upper = callsign.upper().rstrip('*')
        now = datetime.now(timezone.utc)

        # Create station if it doesn't exist
        if callsign_upper not in self.stations:
            self.stations[callsign_upper] = APRSStation(
                callsign=callsign_upper,
                first_heard=now,
                last_heard=now,
                packets_heard=0,
            )

        # Update last_heard timestamp (don't increment packet count for duplicates)
        self.stations[callsign_upper].last_heard = now

        # NOTE: The following fields are now @property methods computed from receptions:
        # - digipeater_path, digipeater_paths, heard_zero_hop, last_heard_zero_hop
        # This function is obsolete and only used by legacy tests.
        # The duplicate_detector.record_path() method should be used instead,
        # which creates proper ReceptionEvents.

        # Mark all stations in the digipeater path as digipeaters
        # Only mark stations we've actually heard (don't create phantom entries)
        for digi_call in digipeater_path:
            digi_upper = digi_call.upper().rstrip('*')
            if digi_upper and digi_upper in self.stations:
                if not self.stations[digi_upper].is_digipeater:
                    self.stations[digi_upper].is_digipeater = True

        # Track only FIRST digipeater for coverage mapping
        # (the one that heard the station directly over RF)
        first_digi = digipeater_path[0].upper().rstrip('*')
        if first_digi and first_digi not in self.stations[callsign_upper].digipeaters_heard_by:
            self.stations[callsign_upper].digipeaters_heard_by.append(first_digi)

    def _get_or_create_station(
        self,
        callsign: str,
        relay_call: str = None,
        hop_count: int = 999,
        is_duplicate: bool = False,
        digipeater_path: List[str] = None,
        packet_type: str = "unknown",
        frame_number: int = None,
        timestamp: datetime = None,
    ) -> APRSStation:
        """Get or create a station entry and record reception event.

        Args:
            callsign: Station callsign
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops (0 = direct RF, 999 = unknown)
            is_duplicate: If True, don't increment packet count (duplicate suppression)
            digipeater_path: List of digipeater callsigns from AX.25 path
            packet_type: Type of APRS packet (position, weather, message, etc.)
            frame_number: Optional frame buffer reference number
            timestamp: Optional timestamp for reception (defaults to now, used by migrations)

        Returns:
            APRSStation object
        """
        # Strip asterisk from callsign (APRS path marker, not part of callsign)
        callsign_upper = callsign.upper().rstrip('*')

        # Use provided timestamp or current time
        # Convert to UTC for consistent storage
        if timestamp:
            if timestamp.tzinfo:
                # Already timezone-aware, convert to UTC
                reception_time = timestamp.astimezone(timezone.utc)
            else:
                # Naive timestamp - assume local time, make aware and convert to UTC
                local_tz = datetime.now(timezone.utc).astimezone().tzinfo
                reception_time = timestamp.replace(tzinfo=local_tz).astimezone(timezone.utc)
        else:
            reception_time = datetime.now(timezone.utc)

        if callsign_upper not in self.stations:
            self.stations[callsign_upper] = APRSStation(
                callsign=callsign_upper,
                first_heard=reception_time,
                last_heard=reception_time,
                packets_heard=0,
            )

        # Update last heard (and potentially first heard)
        if reception_time < self.stations[callsign_upper].first_heard:
            self.stations[callsign_upper].first_heard = reception_time
        if reception_time > self.stations[callsign_upper].last_heard:
            self.stations[callsign_upper].last_heard = reception_time

        # Increment packet count only for non-duplicates
        if not is_duplicate:
            self.stations[callsign_upper].packets_heard += 1

        # Create ReceptionEvent to record this packet reception
        # (even for duplicates, to track digipeater paths for coverage analysis)
        from src.aprs.models import ReceptionEvent

        # Normalize digipeater path
        norm_path = [d.upper() for d in digipeater_path] if digipeater_path else []

        event = ReceptionEvent(
            timestamp=reception_time,
            hop_count=hop_count,
            direct_rf=(relay_call is None),
            relay_call=relay_call.upper() if relay_call else None,
            digipeater_path=norm_path,
            packet_type=packet_type,
            frame_number=frame_number,
        )

        self.stations[callsign_upper].receptions.append(event)

        # Prune to last 200 receptions (keep memory bounded)
        if len(self.stations[callsign_upper].receptions) > 200:
            self.stations[callsign_upper].receptions = (
                self.stations[callsign_upper].receptions[-200:]
            )

        # Mark digipeater stations (for coverage mapping)
        # This happens even for duplicates to improve digipeater detection
        if digipeater_path:
            for digi_call in digipeater_path:
                digi_upper = digi_call.upper().rstrip('*')
                if digi_upper and digi_upper != callsign_upper and digi_upper in self.stations:
                    if not self.stations[digi_upper].is_digipeater:
                        self.stations[digi_upper].is_digipeater = True

            # Track digipeater coverage for the web UI
            # The first digipeater with an asterisk (*) is the one that heard the station directly
            # Multi-hop paths are fine - we just track the first hop
            # This is used by get_digipeater_coverage() for the web UI coverage circles
            if (not relay_call and  # Only direct RF (not iGate packets)
                len(digipeater_path) >= 1 and
                digipeater_path[0].endswith('*')):  # First digi has repeated the packet
                first_digi = digipeater_path[0].upper().rstrip('*')
                if first_digi and first_digi not in self.stations[callsign_upper].digipeaters_heard_by:
                    self.stations[callsign_upper].digipeaters_heard_by.append(first_digi)

        return self.stations[callsign_upper]

    def parse_third_party(
        self, relay_call: str, info: str
    ) -> Optional[Tuple[str, str, str]]:
        """Parse third-party APRS packet.

        Third-party format: }SOURCE>DEST,PATH:info_field

        Args:
            relay_call: Callsign of the relay station
            info: APRS information field

        Returns:
            Tuple of (source_call, relay_call, inner_info) if third-party, None otherwise
        """
        if not info.startswith("}"):
            return None

        try:
            # Remove leading }
            inner = info[1:]

            # Extract source callsign (before >)
            gt_pos = inner.find(">")
            if gt_pos == -1:
                return None

            source_call = inner[:gt_pos].strip()

            # Find the FIRST : after the > which separates header from info
            # (Can't use rfind because info field may contain colons, e.g., APRS messages)
            colon_pos = inner.find(":", gt_pos)
            if colon_pos == -1:
                return None

            inner_info = inner[colon_pos + 1 :]

            print_debug(
                f"parse_third_party: source={source_call}, relay={relay_call}, inner_info='{inner_info[:50]}...'",
                level=5,
            )

            return (source_call, relay_call, inner_info)

        except Exception as e:
            print_debug(f"parse_third_party: exception {e}", level=5)
            return None

    def parse_aprs_mice(
        self,
        station: str,
        dest_addr: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None,
    ) -> Optional[APRSPosition]:
        """Parse MIC-E format APRS position.

        MIC-E encodes position data in the destination address and first 9 bytes of info.

        Args:
            station: Station callsign
            dest_addr: Destination address (contains encoded latitude)
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops (0 = direct RF)
            digipeater_path: List of digipeater callsigns from AX.25 path

        Returns:
            APRSPosition if valid MIC-E, None otherwise
        """
        # MIC-E packets start with specific data types
        if not info or len(info) < 9:
            return None

        # Check for MIC-E indicator (', `, or 0x1c-0x1f)
        first_byte = ord(info[0]) if isinstance(info[0], str) else info[0]
        if first_byte not in (0x27, 0x60, 0x1C, 0x1D, 0x1E, 0x1F):
            return None

        try:
            print_debug(
                f"MIC-E parsing: {station} dest={dest_addr} info={repr(info[:20])}...",
                level=5,
                stations=[station]
            )

            # Remove SSID from dest_addr if present
            dest_call = (
                dest_addr.split("-")[0] if "-" in dest_addr else dest_addr
            )

            # Destination address must be 6 characters for MIC-E
            if len(dest_call) != 6:
                return None

            # Decode latitude from destination address
            # Each character encodes a digit plus message/position info
            lat_digits = []
            north_south = None
            msg_bits = []

            for i, ch in enumerate(dest_call):
                if "0" <= ch <= "9":
                    lat_digits.append(ch)
                    msg_bits.append(0)
                elif "A" <= ch <= "J":
                    lat_digits.append(str(ord(ch) - ord("A")))
                    msg_bits.append(1)
                elif "P" <= ch <= "Y":
                    lat_digits.append(str(ord(ch) - ord("P")))
                    msg_bits.append(1)
                elif ch == "K" or ch == "L" or ch == "Z":
                    # Space characters represent zero
                    lat_digits.append("0")
                    msg_bits.append(0 if ch == "L" else 1)
                else:
                    return None

            # Extract latitude
            if len(lat_digits) != 6:
                return None

            # Format: DDMM.HH (degrees, minutes, hundredths)
            lat_str = "".join(lat_digits)

            lat_deg = int(lat_str[0:2])
            lat_min = float(lat_str[2:4] + "." + lat_str[4:6])
            latitude = lat_deg + (lat_min / 60.0)

            # N/S is encoded in message bits (bit 3)
            # Per APRS spec: 0 = South, 1 = North
            if msg_bits[3] == 0:
                latitude = -latitude  # South

            # Decode longitude from info bytes 1-3
            lon_deg = ord(info[1]) - 28
            lon_min = ord(info[2]) - 28
            lon_min_frac = ord(info[3]) - 28

            # Longitude offset is in message bits (bit 4)
            if msg_bits[4] == 1:
                lon_deg += 100  # +100 for longitude >= 100 degrees

            # E/W is in message bits (bit 5)
            longitude = lon_deg + ((lon_min + lon_min_frac / 100.0) / 60.0)
            if msg_bits[5] == 1:
                longitude = -longitude  # West

            print_debug(
                f"MIC-E decoded position: {latitude:.6f}, {longitude:.6f}",
                level=5,
                stations=[station]
            )

            # Extract speed and course from bytes 4-6
            speed_course = ord(info[4]) - 28
            speed = ((ord(info[5]) - 28) * 10) + ((speed_course // 10) % 10)
            course = ((speed_course % 10) * 100) + (ord(info[6]) - 28)

            # Symbol table and code
            symbol_code = info[7] if len(info) > 7 else ">"
            symbol_table = info[8] if len(info) > 8 else "/"

            # Status text (everything after byte 8)
            # MIC-E status format: T......Mv where:
            #   T = Type indicator (1 byte): space, >, ], `, '
            #   . = Free text (7 bytes, can include altitude as "}aaa" base-91 encoding)
            #   M = Manufacturer code (1 byte)
            #   v = Version code (1 byte)
            raw_comment = info[9:] if len(info) > 9 else ""

            # Strip MIC-E type indicator (first byte)
            # Known type indicators: space (0x20), > (0x3E), ] (0x5D), ` (0x60), ' (0x27)
            if raw_comment and ord(raw_comment[0]) in (
                0x20,
                0x3E,
                0x5D,
                0x60,
                0x27,
            ):
                raw_comment = raw_comment[1:]

            # Keep only printable characters (0x20-0x7E = space through tilde)
            printable_comment = "".join(
                c for c in raw_comment if 0x20 <= ord(c) <= 0x7E
            )

            # Strip MIC-E altitude encoding if present: }xyz (base-91)
            # Altitude format: } followed by 2-3 base-91 characters
            if "}" in printable_comment:
                # Find the } and remove it plus following characters that look like altitude
                brace_idx = printable_comment.find("}")
                # Base-91 uses chars 0x21-0x7B (! through {)
                end_idx = brace_idx + 1
                while (
                    end_idx < len(printable_comment)
                    and end_idx < brace_idx + 4
                ):
                    ch = printable_comment[end_idx]
                    if 0x21 <= ord(ch) <= 0x7B:  # Base-91 character range
                        end_idx += 1
                    else:
                        break
                # Remove the altitude encoding
                printable_comment = (
                    printable_comment[:brace_idx] + printable_comment[end_idx:]
                )

            # Identify device type from MIC-E comment suffix BEFORE stripping
            # MIC-E devices encode type in last 2 characters (new-style) or prefix+suffix (legacy)
            device_str = None
            try:
                device_id = get_device_identifier()
                device_info = device_id.identify_by_mice(printable_comment)
                if device_info:
                    device_str = str(device_info)
                    print_debug(
                        f"MIC-E device identified: {device_str} (comment: {repr(printable_comment[-10:])})",
                        level=3,
                        stations=[station]
                    )
                else:
                    print_debug(
                        f"MIC-E device not found for comment: {repr(printable_comment[-10:])}",
                        level=4,
                        stations=[station]
                    )
            except Exception as e:
                print_debug(f"MIC-E device ID error: {e}", level=3, stations=[station])

            # Strip trailing manufacturer/version codes (last 1-2 chars)
            # These are typically symbols (non-alphanumeric except space)
            while (
                len(printable_comment) > 0
                and printable_comment[-1]
                in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
            ):
                printable_comment = printable_comment[:-1]

            # Strip trailing whitespace
            printable_comment = printable_comment.rstrip()

            # If result is mostly symbols/gibberish (>60% non-alphanumeric), suppress it
            if printable_comment:
                alphanumeric_count = sum(
                    1 for c in printable_comment if c.isalnum() or c == " "
                )
                if (
                    len(printable_comment) > 0
                    and (alphanumeric_count / len(printable_comment)) < 0.4
                ):
                    printable_comment = ""  # Suppress gibberish

            # Apply standard APRS comment cleaning to remove data fields (PHG, weather codes, etc.)
            comment = self.clean_position_comment(printable_comment)

            print_debug(
                f"MIC-E symbol: {symbol_table}{symbol_code}, comment: {repr(comment)}",
                level=5,
                stations=[station]
            )

            # Convert to Maidenhead grid
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Filter out invalid "Null Island" coordinates (0.0, 0.0)
            if latitude == 0.0 and longitude == 0.0:
                print_debug(
                    f"parse_mice_position: Rejecting Null Island coordinates from {station}",
                    level=5,
                    stations=[station]
                )
                return None

            # Create position object
            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
                device=device_str,
            )

            # Store position
            self.position_reports[station.upper()] = pos

            # Track station
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="mic_e", timestamp=timestamp, frame_number=frame_number)
            sta.last_position = pos
            if device_str:
                sta.device = device_str
            self._add_position_to_history(sta, pos)

            # Broadcast station update to web clients
            self._broadcast_update('station_update', sta)

            print_debug(
                f"MIC-E position stored: {station} @ {grid_square} ({latitude:.6f}, {longitude:.6f})",
                level=5,
                stations=[station]
            )

            return pos

        except Exception as e:
            print_debug(f"parse_aprs_mice exception for {station}: {e}", level=5, stations=[station])
            import traceback
            print_debug(traceback.format_exc(), level=6, stations=[station])
            return None

    def parse_aprs_message(
        self,
        from_call: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSMessage]:
        """Parse APRS message format.

        APRS message format: :CALLSIGN :message text{12345
        Where CALLSIGN is 9 chars padded with spaces, {12345 is optional message ID

        Args:
            from_call: Source callsign
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops (0 = direct RF)
            digipeater_path: List of digipeater callsigns from AX.25 path

        Returns:
            APRSMessage if this is a message, None otherwise
        """
        if not info.startswith(":"):
            return None

        print_debug(
            f"parse_aprs_message: from={from_call}, info='{info[:50]}...'",
            level=5,
        )

        try:
            # Format: :CALLSIGN :message{id
            # CALLSIGN is 9 chars (padded with spaces)
            if len(info) < 11:  # Minimum: ":" + 9-char call + ":"
                print_debug(
                    f"parse_aprs_message: info too short ({len(info)} chars)",
                    level=5,
                )
                return None

            to_call = info[1:10].strip()  # Extract 9-char callsign field
            if info[10] != ":":
                print_debug(
                    f"parse_aprs_message: missing colon at position 10",
                    level=5,
                )
                return None

            message_part = info[11:]

            print_debug(
                f"parse_aprs_message: to_call='{to_call}', message='{message_part[:30]}...'",
                level=5,
            )

            # Check for message ID: {12345
            message_id = None
            message_text = message_part
            if "{" in message_part:
                parts = message_part.split("{", 1)
                message_text = parts[0]
                if len(parts) > 1:
                    message_id = parts[1].strip()

            # Filter out telemetry definition messages (not user messages)
            # These are configuration broadcasts: PARM., UNIT., EQNS., BITS.
            if message_text.startswith(("PARM.", "UNIT.", "EQNS.", "BITS.")):
                # Track station activity but don't treat as a message
                sender_station = self._get_or_create_station(
                    from_call, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="telemetry_config", timestamp=timestamp, frame_number=frame_number
                )
                # Note: packets_heard incremented by _get_or_create_station
                print_debug(
                    f"parse_aprs_message: filtered out telemetry config message",
                    level=5,
                )
                return None  # Don't notify - these are telemetry config, not messages

            # Handle ACK/REJ messages (protocol acknowledgments)
            # Format: "ack12345" or "rej12345"
            if message_text.lower().startswith(("ack", "rej")):
                # Track station activity
                sender_station = self._get_or_create_station(
                    from_call, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="message_ack", timestamp=timestamp, frame_number=frame_number
                )
                # Note: packets_heard incremented by _get_or_create_station

                # Check if this ACK is for one of our sent messages
                if message_text.lower().startswith("ack"):
                    # Extract ID after "ack", handling multi-line format: ack12345}line_num
                    acked_msg_id = message_text[3:].strip()
                    if "}" in acked_msg_id:
                        acked_msg_id = acked_msg_id.split("}")[0]
                    print_debug(
                        f"parse_aprs_message: received ACK for message ID '{acked_msg_id}' from {from_call}",
                        level=5,
                    )

                    # Extract base callsign from ACK sender (strip SSID)
                    from_call_base = from_call.upper().split("-")[0]

                    # Find and mark our sent message as acknowledged
                    found = False
                    for sent_msg in self.messages:
                        if sent_msg.direction == "sent":
                            print_debug(
                                f"  Checking sent msg: to={sent_msg.to_call}, msg_id={sent_msg.message_id}, ack={sent_msg.ack_received}",
                                level=6,
                            )

                        # Match on message ID and base callsign (to handle different SSIDs)
                        sent_to_base = sent_msg.to_call.upper().split("-")[0]
                        if (
                            sent_msg.direction == "sent"
                            and sent_msg.message_id == acked_msg_id
                            and (sent_msg.to_call.upper() == from_call.upper()
                                 or sent_to_base == from_call_base)
                        ):
                            sent_msg.ack_received = True
                            found = True
                            print_debug(
                                f"parse_aprs_message: ✓ MATCHED - marked message ID '{acked_msg_id}' to {sent_msg.to_call} as acknowledged (ACK from {from_call})",
                                level=5,
                            )
                            break

                    if not found:
                        print_debug(
                            f"parse_aprs_message: ACK for '{acked_msg_id}' from {from_call} - no matching sent message found",
                            level=5,
                        )

                return None  # Don't notify or add ACK messages to list

            # Check if this is our own message digipeated back to us
            # If so, treat it as proof of successful transmission (implicit ACK)
            # NOTE: Only match exact callsign (with SSID). Different SSIDs are different
            # stations (e.g., K1MAL-5 is HT, K1MAL-6 is console) and should communicate.
            is_our_message = from_call.upper() == self.my_callsign

            if is_our_message and digipeater_path:
                # This is our own message coming back via digipeater(s)
                # Could be a regular message (with message_id) or an ACK (without message_id)

                if message_id:
                    # Regular message with message ID
                    print_debug(
                        f"parse_aprs_message: received our own message via digipeater (ID={message_id}, path={digipeater_path})",
                        level=5,
                    )

                    # Find and mark our sent message as digipeated
                    found = False
                    for sent_msg in self.messages:
                        if (
                            sent_msg.direction == "sent"
                            and sent_msg.message_id == message_id
                            and not sent_msg.digipeated  # Don't re-mark if already digipeated
                        ):
                            sent_msg.digipeated = True
                            found = True
                            print_debug(
                                f"parse_aprs_message: ✓ DIGIPEATED - marked message ID '{message_id}' as digipeated (heard via {','.join(digipeater_path)})",
                                level=5,
                            )
                            break

                    if not found:
                        print_debug(
                            f"parse_aprs_message: Digipeated message ID '{message_id}' - no matching sent message found",
                            level=5,
                        )
                else:
                    # No message ID - could be an ACK we sent
                    # ACKs have message text like "ackXXXXX" and are sent to the original sender
                    print_debug(
                        f"parse_aprs_message: received our own message via digipeater (no ID, to={to_call}, msg='{message_text}', path={digipeater_path})",
                        level=5,
                    )

                    # Find matching ACK by to_call and message text
                    found = False
                    for sent_msg in self.messages:
                        if (
                            sent_msg.direction == "sent"
                            and sent_msg.message_id is None  # ACKs don't have message IDs
                            and sent_msg.to_call.upper() == to_call.upper()
                            and sent_msg.message == message_text
                            and not sent_msg.digipeated  # Don't re-mark if already digipeated
                        ):
                            sent_msg.digipeated = True
                            # ACKs are considered "acknowledged" once digipeated (no ACK for ACKs)
                            sent_msg.ack_received = True
                            found = True
                            print_debug(
                                f"parse_aprs_message: ✓ DIGIPEATED - marked ACK to {to_call} as digipeated (heard via {','.join(digipeater_path)})",
                                level=5,
                            )
                            break

                    if not found:
                        print_debug(
                            f"parse_aprs_message: Digipeated message to {to_call} (no ID) - no matching sent ACK found",
                            level=5,
                        )

                return None  # Don't add our own messages to the received list

            # Create message object
            msg = APRSMessage(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                from_call=from_call.upper(),
                to_call=to_call.upper(),
                message=message_text,
                message_id=message_id,
                read=False,
            )

            # Track station activity
            sender_station = self._get_or_create_station(
                from_call, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="message", timestamp=timestamp, frame_number=frame_number
            )
            sender_station.messages_sent += 1

            # Track receiver if it's us
            to_call_upper = to_call.upper()
            if self.is_message_for_me(to_call):
                # Message is to us - track as received by the sender
                sender_station.messages_received += 1

            # Always add to monitored messages (for monitoring all traffic)
            self.monitored_messages.append(msg)

            # Add to personal messages if addressed to us, ALL, or BSS callsign
            is_for_me = self.is_message_for_me(to_call)
            is_all = to_call_upper == "ALL"
            is_bss = to_call_upper.startswith("BSS")
            is_base = to_call_upper == self.my_callsign_base

            print_debug(
                f"parse_aprs_message: filtering - is_for_me={is_for_me}, is_all={is_all}, is_bss={is_bss}, is_base={is_base}",
                level=5,
            )

            if is_for_me or is_all or is_bss or is_base:
                # Check for duplicate before adding
                is_duplicate = False
                for existing_msg in self.messages:
                    # Check if duplicate: same sender + (same message_id OR same content OR fuzzy match)
                    if existing_msg.from_call == msg.from_call:
                        if (
                            message_id
                            and existing_msg.message_id == message_id
                        ):
                            # Same sender, same message ID = duplicate
                            is_duplicate = True
                            print_debug(
                                f"parse_aprs_message: duplicate detected (same message_id={message_id})",
                                level=5,
                            )
                            break
                        elif existing_msg.message == msg.message:
                            # Same sender, same content = duplicate (for messages without IDs)
                            is_duplicate = True
                            print_debug(
                                f"parse_aprs_message: duplicate detected (same content)",
                                level=5,
                            )
                            break
                        else:
                            # Fuzzy duplicate detection: catches corrupted iGate packets
                            # Check if message content is similar (one starts with the other)
                            # and within a time window (30 seconds)
                            time_diff = abs((msg.timestamp - existing_msg.timestamp).total_seconds())
                            min_match_len = 20  # Minimum characters to match

                            if time_diff < 30:  # Within 30 seconds
                                # Check if messages have enough content to compare
                                if len(existing_msg.message) >= min_match_len and len(msg.message) >= min_match_len:
                                    # Check if one message starts with the other (fuzzy match)
                                    if (existing_msg.message.startswith(msg.message[:min_match_len]) or
                                        msg.message.startswith(existing_msg.message[:min_match_len])):
                                        is_duplicate = True
                                        print_debug(
                                            f"parse_aprs_message: duplicate detected (fuzzy match, time_diff={time_diff:.1f}s)",
                                            level=5,
                                        )
                                        break

                if not is_duplicate:
                    self.messages.append(msg)
                    print_debug(
                        f"parse_aprs_message: added to personal messages (count={len(self.messages)})",
                        level=5,
                    )

                    # Broadcast message received to web clients
                    self._broadcast_update('message_received', msg)

                    return msg  # Return for notification
                else:
                    print_debug(
                        f"parse_aprs_message: skipped duplicate message",
                        level=5,
                    )
                    return None  # Don't notify for duplicates

            print_debug(
                f"parse_aprs_message: NOT added to personal messages (not for us)",
                level=5,
            )
            return None  # Don't notify for messages not to us

        except Exception:
            return None

    def parse_aprs_weather(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSWeather]:
        """Parse APRS weather data.

        Supports position-with-weather formats:
        - ! (position without timestamp)
        - @ (position with timestamp)
        - / (position with timestamp, no messaging)
        - _ (weather report without position)

        Args:
            station: Station callsign
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops
            digipeater_path: List of digipeater callsigns from AX.25 path

        Returns:
            APRSWeather if weather data found, None otherwise
        """
        if not info or info[0] not in ("!", "@", "/", "_"):
            return None

        try:
            wx = APRSWeather(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                raw_data=info,
            )

            # Check for weather data indicators
            has_weather = False

            # Look for weather fields (these are the typical indicators)
            # Wind: c...s... (direction/speed) or g... (gust)
            # Temp: t... (F)
            # Rain: r... (last hour), p... (last 24h), P... (since midnight)
            # Humidity: h... (00-99, 00 means 100%)
            # Pressure: b..... (tenths of mbar)

            # Simple pattern matching for common weather fields
            # Allow variable digit counts and negative signs for temperature (t-3, t003, etc.)
            if re.search(r"[csghpPb]\d{3}|t-?\d{1,3}|r\d{3}", info):
                has_weather = True

            if not has_weather:
                return None

            # Try to extract lat/lon (simplified - just check for valid position format)
            # Full parsing would require comprehensive APRS position decoding
            # For now, we'll extract what we can

            # Extract weather values using regex

            # Wind - two formats supported:
            # Format 1: _ddd/sss (underscore, direction/speed)
            match = re.search(r"_(\d{3})/(\d{3})", info)
            if match:
                wx.wind_direction = int(match.group(1))
                wx.wind_speed = int(match.group(2))
            else:
                # Format 2: cdddsddd (compact form)
                match = re.search(r"c(\d{3})s(\d{3})", info)
                if match:
                    wx.wind_direction = int(match.group(1))
                    wx.wind_speed = int(match.group(2))

            # Wind gust (g...) - mph
            match = re.search(r"g(\d{3})", info)
            if match:
                wx.wind_gust = int(match.group(1))

            # Temperature (t...) - Fahrenheit
            # Allow 1-3 digits with optional minus sign (e.g., t-3, t-03, t-003, t003)
            match = re.search(r"t(-?\d{1,3})", info)
            if match:
                temp = int(match.group(1))
                # Negative temps in standard APRS use two's complement (e.g., 253 = -3)
                # But some stations send explicit minus sign (e.g., -3)
                if temp > 200:
                    temp = temp - 256
                wx.temperature = temp

            # Rain last hour (r...) - hundredths of inches
            match = re.search(r"r(\d{3})", info)
            if match:
                wx.rain_1h = int(match.group(1)) / 100.0

            # Rain last 24h (p...) - hundredths of inches
            match = re.search(r"p(\d{3})", info)
            if match:
                wx.rain_24h = int(match.group(1)) / 100.0

            # Rain since midnight (P...) - hundredths of inches
            match = re.search(r"P(\d{3})", info)
            if match:
                wx.rain_since_midnight = int(match.group(1)) / 100.0

            # Humidity (h...) - percent (00 = 100%)
            match = re.search(r"h(\d{2})", info)
            if match:
                humidity = int(match.group(1))
                wx.humidity = 100 if humidity == 0 else humidity

            # Barometric pressure (b.....) - auto-detect format
            # Some stations use tenths of mb (b10130 = 1013.0 mb)
            # Others use hundredths of inHg (b02979 = 29.79 inHg)
            match = re.search(r"b(\d{5})", info)
            if match:
                raw_value = int(match.group(1))

                # Try as tenths of millibars first
                pressure_mb = raw_value / 10.0

                # Sanity check: valid atmospheric pressure is 900-1100 mb
                if 900 <= pressure_mb <= 1100:
                    # Valid as millibars, use directly
                    wx.pressure = pressure_mb
                else:
                    # Try as hundredths of inHg (US format)
                    pressure_inhg = raw_value / 100.0

                    # Sanity check: valid inHg range is 25-32 inHg
                    if 25 <= pressure_inhg <= 32:
                        # Valid as inHg, convert to millibars
                        wx.pressure = pressure_inhg * 33.8639
                    # else: invalid pressure, leave as None

            # Store latest weather for this station
            self.weather_reports[station.upper()] = wx

            # Track station activity
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="weather", timestamp=timestamp, frame_number=frame_number)
            sta.last_weather = wx
            self._add_weather_to_history(sta, wx)

            # Broadcast weather update to web clients
            self._broadcast_update('weather_update', sta)

            return wx

        except Exception:
            return None

    def _add_weather_to_history(
        self, station: APRSStation, weather: APRSWeather
    ) -> None:
        """Add weather report to station history with intelligent retention.

        Three-tier retention policy:
        - Last hour: ALL samples (full detail for current weather)
        - 1 hour to 1 day: one sample every 15 minutes (recent trends)
        - Older than 1 day: one sample per hour (long-term history)

        Args:
            station: Station to update
            weather: New weather report to add
        """
        now = weather.timestamp
        history = station.weather_history

        # Always append first
        history.append(weather)

        # Skip expensive operations during migration
        if self._migration_mode:
            return

        # Sort
        history.sort(key=lambda w: w.timestamp, reverse=True)

        # Only run retention policy when history exceeds threshold
        # This avoids O(n²) during migration
        if len(history) <= 250:
            return  # No pruning needed yet

        # Calculate pressure tendency (3-hour change)
        if weather.pressure is not None:
            # Find weather report from ~3 hours ago
            three_hours_ago = now - timedelta(hours=3)
            tolerance = timedelta(minutes=30)  # ±30 min tolerance

            for old_wx in reversed(history):
                age = abs((old_wx.timestamp - three_hours_ago).total_seconds())
                if age <= tolerance.total_seconds() and old_wx.pressure is not None:
                    change = weather.pressure - old_wx.pressure
                    weather.pressure_change_3h = change

                    if change > 0.5:
                        weather.pressure_tendency = 'rising'
                    elif change < -0.5:
                        weather.pressure_tendency = 'falling'
                    else:
                        weather.pressure_tendency = 'steady'
                    break

        # Build retention list with three-tier policy
        retained = []
        last_15min = None
        last_hour = None

        for wx in history:
            age = now - wx.timestamp

            # Tier 1: Keep ALL reports from the last hour (full detail)
            if age <= timedelta(hours=1):
                retained.append(wx)
            # Tier 2: 1 hour to 1 day - keep one sample every 15 minutes
            elif age <= timedelta(days=1):
                # Keep if no 15-min sample yet, or if 15+ min since last kept
                if last_15min is None or (
                    last_15min - wx.timestamp
                ) >= timedelta(minutes=15):
                    retained.append(wx)
                    last_15min = wx.timestamp
            # Tier 3: Older than 1 day - keep one sample per hour
            else:
                # Keep if no hourly sample yet, or if 1+ hour since last kept
                if last_hour is None or (
                    last_hour - wx.timestamp
                ) >= timedelta(hours=1):
                    retained.append(wx)
                    last_hour = wx.timestamp

        # Update history with retained samples
        station.weather_history = retained

    def _add_position_to_history(
        self, station: APRSStation, position: APRSPosition
    ) -> None:
        """Add position report to station history with intelligent retention.

        Retention policy optimized for tracking movement:
        - Last hour: ALL positions (full movement detail)
        - 1 hour to 1 day: Keep if position moved >100m OR 15+ min elapsed
        - Older than 1 day: Keep if position moved >500m OR 1+ hour elapsed
        - Maximum: 200 position points per station

        Args:
            station: Station to update
            position: New position report to add
        """
        def distance_meters(lat1, lon1, lat2, lon2):
            """Calculate distance between two coordinates in meters (Haversine formula)."""
            R = 6371000  # Earth radius in meters
            phi1 = math.radians(lat1)
            phi2 = math.radians(lat2)
            delta_phi = math.radians(lat2 - lat1)
            delta_lambda = math.radians(lon2 - lon1)

            a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

            return R * c

        now = position.timestamp
        history = station.position_history

        # Always append (O(1) - fast)
        history.append(position)

        # Skip expensive operations during migration
        if self._migration_mode:
            return

        # Sort before retention policy (Python's Timsort is O(n) for nearly-sorted lists)
        history.sort(key=lambda p: p.timestamp, reverse=True)

        # Only run retention policy when history exceeds threshold
        # This avoids O(n²) during migration (running policy on every frame)
        if len(history) <= 250:
            return  # No pruning needed yet, skip expensive retention policy

        # Build retention list with movement-based policy
        retained = []
        last_retained = None

        for pos in history:
            age = now - pos.timestamp

            # Tier 1: Keep ALL positions from the last hour (full movement detail)
            if age <= timedelta(hours=1):
                retained.append(pos)
                last_retained = pos
            # Tier 2: 1 hour to 1 day - keep if moved >100m OR 15+ min elapsed
            elif age <= timedelta(days=1):
                if last_retained is None:
                    retained.append(pos)
                    last_retained = pos
                else:
                    dist = distance_meters(
                        last_retained.latitude, last_retained.longitude,
                        pos.latitude, pos.longitude
                    )
                    time_diff = last_retained.timestamp - pos.timestamp

                    # Keep if significant movement OR enough time elapsed
                    if dist > 100 or time_diff >= timedelta(minutes=15):
                        retained.append(pos)
                        last_retained = pos
            # Tier 3: Older than 1 day - keep if moved >500m OR 1+ hour elapsed
            else:
                if last_retained is None:
                    retained.append(pos)
                    last_retained = pos
                else:
                    dist = distance_meters(
                        last_retained.latitude, last_retained.longitude,
                        pos.latitude, pos.longitude
                    )
                    time_diff = last_retained.timestamp - pos.timestamp

                    # Keep if significant movement OR enough time elapsed
                    if dist > 500 or time_diff >= timedelta(hours=1):
                        retained.append(pos)
                        last_retained = pos

        # Limit to maximum 200 points to prevent unbounded growth
        if len(retained) > 200:
            retained = retained[:200]

        # Update history with retained positions
        station.position_history = retained

    def get_unread_count(self) -> int:
        """Get count of unread received messages.

        Returns:
            Number of unread received messages
        """
        return sum(
            1
            for msg in self.messages
            if msg.direction == "received" and not msg.read
        )

    def mark_all_read(self) -> int:
        """Mark all received messages as read.

        Returns:
            Number of messages marked as read
        """
        count = 0
        for msg in self.messages:
            if msg.direction == "received" and not msg.read:
                msg.read = True
                count += 1
        return count

    def clear_messages(self) -> int:
        """Clear all messages (both sent and received).

        Returns:
            Number of messages cleared
        """
        count = len(self.messages)
        self.messages.clear()
        self.monitored_messages.clear()
        return count

    def add_sent_message(
        self, to_call: str, message: str, message_id: str
    ) -> APRSMessage:
        """Add a sent message to the message list.

        Args:
            to_call: Destination callsign
            message: Message text
            message_id: Message ID for tracking acknowledgments

        Returns:
            The created message object
        """
        now = datetime.now(timezone.utc)
        msg = APRSMessage(
            timestamp=now,
            from_call=self.my_callsign,
            to_call=to_call.upper(),
            message=message,
            message_id=message_id,
            direction="sent",
            ack_received=False,
            failed=False,
            retry_count=0,
            last_sent=now,  # Track when message was sent for retry logic
            read=True,  # Sent messages are always "read"
        )

        print_debug(
            f"add_sent_message: tracking message to {to_call} with ID '{message_id}' (ack_received=False)",
            level=5,
        )

        self.messages.append(msg)
        self.monitored_messages.append(
            msg
        )  # Also add to monitored for database persistence
        return msg

    def get_pending_retries(self) -> List[APRSMessage]:
        """Get messages that need to be retried using two-tier timeout system.

        Returns messages that:
        - Are sent messages
        - Haven't been acknowledged
        - Haven't failed
        - Have exceeded the appropriate retry timeout since last send
        - Haven't exceeded max retry count
        - Are NOT ACKs (ACKs are never retried per APRS spec)

        Two-tier retry system:
        - Fast retries: For messages not yet digipeated (trying to get on RF)
        - Slow retries: For messages digipeated but not ACKed (reminder to recipient)

        Returns:
            List of messages that should be retried
        """
        now = datetime.now(timezone.utc)
        pending = []

        for msg in self.messages:
            # Skip ACKs - they should never be retried (fire-and-forget per APRS spec)
            # ACKs have two definitive characteristics:
            # 1. message_id is None (ACKs don't have their own message IDs)
            # 2. Message text matches pattern: "ack" + original message ID (1-5 chars)
            # The message_id check is the strongest indicator since user messages ALWAYS have IDs
            is_ack = (
                msg.message_id is None  # Primary check: ACKs never have message IDs
                and msg.message.lower().startswith("ack")  # Secondary validation
                and len(msg.message) >= 4  # At minimum "ack" + 1 char
                and len(msg.message) <= 8  # At maximum "ack" + 5 chars (APRS msg ID limit)
            )

            if (
                msg.direction == "sent"
                and not msg.ack_received
                and not msg.failed
                and not is_ack  # Don't retry ACKs!
                and msg.last_sent is not None
                and msg.retry_count < self.max_retries
            ):

                # Check if timeout has elapsed based on digipeater status
                elapsed = (now - msg.last_sent).total_seconds()

                # Two-tier retry: fast if not digipeated, slow if digipeated
                if msg.digipeated:
                    # Message made it to RF, use slow retry (remind recipient)
                    timeout = self.retry_slow
                else:
                    # Message not heard digipeated yet, use fast retry (get on RF)
                    timeout = self.retry_fast

                if elapsed >= timeout:
                    pending.append(msg)

        return pending

    def mark_message_failed(self, msg: APRSMessage):
        """Mark a message as failed after max retries exceeded.

        Args:
            msg: Message to mark as failed
        """
        msg.failed = True

    def check_expired_messages(self) -> List[APRSMessage]:
        """Check for messages that have expired without acknowledgment.

        Returns messages that:
        - Are sent messages
        - Haven't been acknowledged
        - Haven't already been marked as failed
        - Have reached max retry count
        - Have exceeded the timeout period since last transmission

        Uses two-tier timeout: fast for non-digipeated, slow for digipeated.

        These messages should be marked as failed.

        Returns:
            List of expired messages
        """
        now = datetime.now(timezone.utc)
        expired = []

        for msg in self.messages:
            if (
                msg.direction == "sent"
                and not msg.ack_received
                and not msg.failed
                and msg.last_sent is not None
                and msg.retry_count >= self.max_retries
            ):
                # Check if timeout has elapsed since final attempt
                elapsed = (now - msg.last_sent).total_seconds()

                # Use appropriate timeout based on digipeater status
                timeout = self.retry_slow if msg.digipeated else self.retry_fast

                if elapsed >= timeout:
                    expired.append(msg)

        return expired

    def update_message_retry(self, msg: APRSMessage):
        """Update message retry tracking after retransmission.

        Args:
            msg: Message that was just retransmitted
        """
        msg.retry_count += 1
        msg.last_sent = datetime.now(timezone.utc)

        # Note: Do NOT mark as failed here - we need to wait for the timeout
        # period after the last transmission to see if an ACK arrives.
        # Failure is determined by check_expired_messages().

    def get_messages(self, unread_only: bool = False) -> List[APRSMessage]:
        """Get messages, optionally filtered.

        Args:
            unread_only: If True, only return unread messages

        Returns:
            List of messages
        """
        if unread_only:
            return [msg for msg in self.messages if not msg.read]
        return self.messages.copy()

    def get_monitored_messages(
        self, limit: Optional[int] = None
    ) -> List[APRSMessage]:
        """Get monitored messages (all APRS messages heard).

        Args:
            limit: Maximum number of messages to return (most recent), None for all

        Returns:
            List of monitored messages (most recent first if limited)
        """
        if limit:
            return self.monitored_messages[-limit:]
        return self.monitored_messages.copy()

    def get_weather_stations(self, sort_by: str = "last") -> List[APRSWeather]:
        """Get all weather reports with flexible sorting.

        Args:
            sort_by: Sort field - 'last' (default), 'name', 'temp', 'humidity', 'pressure'

        Returns:
            List of latest weather reports from each station
        """
        stations = list(self.weather_reports.values())

        if sort_by == "name":
            return sorted(stations, key=lambda x: x.station)
        elif sort_by == "temp" or sort_by == "temperature":
            # Sort by temperature, None values last
            return sorted(
                stations,
                key=lambda x: (
                    x.temperature is None,
                    x.temperature if x.temperature is not None else 0,
                ),
                reverse=True,
            )
        elif sort_by == "humidity":
            # Sort by humidity, None values last
            return sorted(
                stations,
                key=lambda x: (
                    x.humidity is None,
                    x.humidity if x.humidity is not None else 0,
                ),
                reverse=True,
            )
        elif sort_by == "pressure":
            # Sort by pressure, None values last
            return sorted(
                stations,
                key=lambda x: (
                    x.pressure is None,
                    x.pressure if x.pressure is not None else 0,
                ),
                reverse=True,
            )
        elif sort_by == "last":
            # Sort by timestamp (most recent first)
            return sorted(stations, key=lambda x: x.timestamp, reverse=True)
        else:
            # Default to last heard
            return sorted(stations, key=lambda x: x.timestamp, reverse=True)

    def get_zambretti_forecast(self, callsign: str, pressure_threshold: float = 0.3) -> Optional[Dict]:
        """Generate Zambretti weather forecast for a station.

        Args:
            callsign: Station callsign to generate forecast for
            pressure_threshold: Pressure tendency threshold in mb/hr (default: 0.3)

        Returns:
            Dictionary with forecast data or None if insufficient data:
            {
                'code': 'A-Z',
                'forecast': 'Forecast text',
                'pressure': float,
                'trend': 'rising/falling/steady',
                'confidence': 'high/medium/low',
                'wind_dir': int or None
            }
        """
        station = self.stations.get(callsign.upper())
        if not station or not station.last_weather:
            return None

        weather = station.last_weather

        # Need pressure for Zambretti
        if weather.pressure is None:
            return None

        # Calculate pressure trend from weather history
        trend = 'steady'
        confidence = 'low'

        if len(station.weather_history) >= 2:
            # Look for pressure readings in the last 3-6 hours
            now = datetime.now(timezone.utc)
            recent_readings = []

            for wx in station.weather_history:
                if wx.pressure is not None:
                    age_hours = (now - wx.timestamp).total_seconds() / 3600
                    if age_hours <= 6:  # Last 6 hours
                        recent_readings.append((wx.timestamp, wx.pressure))

            if len(recent_readings) >= 2:
                # Sort by timestamp
                recent_readings.sort(key=lambda x: x[0])

                # Compare oldest and newest in window
                old_pressure = recent_readings[0][1]
                new_pressure = recent_readings[-1][1]
                time_diff_hours = (recent_readings[-1][0] - recent_readings[0][0]).total_seconds() / 3600

                # Calculate trend (need at least 1 hour of data for reliable trend)
                if time_diff_hours >= 1:
                    pressure_change = new_pressure - old_pressure
                    hourly_rate = pressure_change / time_diff_hours

                    # Pressure tendency thresholds
                    # WMO/NOAA standard: ±0.17 mb/hr (0.5 mb in 3 hours)
                    # Default 0.30 mb/hr (~1.0 mb in 3 hours) for Zambretti because:
                    # - Zambretti (1915) doesn't account for air mass characteristics
                    # - Small pressure changes don't always indicate weather change
                    # - More conservative threshold prevents false "showery" forecasts
                    # - Better matches modern forecasting which uses humidity, temperature, etc.
                    # Configurable via WXTREND TNC command
                    if abs(hourly_rate) < pressure_threshold:
                        trend = 'steady'
                        confidence = 'high' if time_diff_hours >= 3 else 'medium'
                    elif hourly_rate > 0:
                        trend = 'rising'
                        confidence = 'high' if time_diff_hours >= 3 else 'medium'
                    else:
                        trend = 'falling'
                        confidence = 'high' if time_diff_hours >= 3 else 'medium'

        # Get current month for seasonal adjustment
        current_month = datetime.now(timezone.utc).month

        # Get wind direction (optional for Zambretti)
        wind_dir = weather.wind_direction

        # Calculate Zambretti code
        # Note: Pressures from PWS are already sea-level adjusted
        zambretti_code = calculate_zambretti_code(
            sea_level_pressure_mb=weather.pressure,
            pressure_trend=trend,
            wind_direction=wind_dir,
            month=current_month,
            hemisphere='N'  # TODO: Could be determined from station latitude
        )

        forecast_text = ZAMBRETTI_FORECASTS.get(zambretti_code, 'Unknown')

        return {
            'code': zambretti_code,
            'forecast': forecast_text,
            'pressure': weather.pressure,
            'trend': trend,
            'confidence': confidence,
            'wind_dir': wind_dir
        }

    def format_message(self, msg: APRSMessage, index: int = None) -> str:
        """Format message for display. Delegates to APRSFormatters."""
        return APRSFormatters.format_message(msg, index)

    def format_weather(self, wx: APRSWeather) -> Dict[str, str]:
        """Format weather report for display. Delegates to APRSFormatters."""
        return APRSFormatters.format_weather(wx)

    def _format_wind(self, wx: APRSWeather) -> str:
        """Format wind information. Delegates to APRSFormatters."""
        return APRSFormatters._format_wind(wx)

    @staticmethod
    def latlon_to_maidenhead(lat: float, lon: float) -> str:
        """Convert lat/lon to Maidenhead grid. Delegates to geo_utils."""
        return latlon_to_maidenhead(lat, lon)

    @staticmethod
    def maidenhead_to_latlon(grid: str) -> tuple:
        """Convert Maidenhead grid to lat/lon. Delegates to geo_utils."""
        return maidenhead_to_latlon(grid)

    @staticmethod
    def calculate_dew_point(temp_f: float, humidity: int) -> Optional[float]:
        """Calculate dew point. Delegates to geo_utils."""
        return calculate_dew_point(temp_f, humidity)

    def _parse_compressed_position(
        self,
        info: str,
        offset: int,
        station: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        dest_addr: str = None,
        timestamp: datetime = None,
        frame_number: int = None,
    ) -> Optional[APRSPosition]:
        r"""Parse APRS compressed position format.

        Compressed format: /YYYYXXXX$csT
        - / or \ = symbol table (1 byte)
        - YYYY = compressed latitude (4 bytes, base-91)
        - XXXX = compressed longitude (4 bytes, base-91)
        - $ = symbol code (1 byte)
        - cs = compressed course/speed or other data (1-2 bytes)
        - T = compression type byte (1 byte)

        Args:
            info: APRS info field
            offset: Start offset of position data
            station: Station callsign
            relay_call: Optional relay station
            hop_count: Number of digipeater hops
            digipeater_path: List of digipeater callsigns
            dest_addr: Destination address

        Returns:
            APRSPosition if valid, None otherwise
        """
        try:
            if len(info) < offset + 13:  # Minimum: symbol_table + lat(4) + lon(4) + symbol + cs + T
                return None

            # Extract components
            symbol_table = info[offset]
            lat_compressed = info[offset + 1:offset + 5]
            lon_compressed = info[offset + 5:offset + 9]
            symbol_code = info[offset + 9]

            # Optional: compressed course/speed and type byte
            # We'll extract the comment starting after the compression type byte
            # The compression type byte is typically at offset+10, but we'll be lenient

            # Decode base-91 coordinates
            # Base-91 uses ASCII 33-124 (! to |)
            def base91_decode(s):
                """Decode 4-character base-91 string to integer."""
                result = 0
                for i, c in enumerate(s):
                    val = ord(c) - 33
                    if val < 0 or val > 90:
                        return None
                    result = result * 91 + val
                return result

            lat_val = base91_decode(lat_compressed)
            lon_val = base91_decode(lon_compressed)

            if lat_val is None or lon_val is None:
                return None

            # Convert to decimal degrees
            # Latitude: 90 - (lat_val / 380926)
            # Longitude: -180 + (lon_val / 190463)
            latitude = 90.0 - (lat_val / 380926.0)
            longitude = -180.0 + (lon_val / 190463.0)

            # Validate coordinates
            if latitude < -90 or latitude > 90 or longitude < -180 or longitude > 180:
                return None

            # Filter out Null Island
            if latitude == 0.0 and longitude == 0.0:
                return None

            # Extract comment (skip compression type byte and optional data)
            # Typically comment starts at offset+13
            comment = info[offset + 13:].strip() if len(info) > offset + 13 else ""

            # Convert to Maidenhead grid square
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Identify device type
            device_str = None
            if dest_addr:
                try:
                    device_id = get_device_identifier()
                    device_info = device_id.identify_by_tocall(dest_addr)
                    if device_info:
                        device_str = str(device_info)
                except Exception:
                    pass

            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
                device=device_str,
            )

            # Store latest position
            self.position_reports[station.upper()] = pos

            # Track station activity
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="position", timestamp=timestamp, frame_number=frame_number)
            sta.last_position = pos
            if device_str:
                sta.device = device_str
            self._add_position_to_history(sta, pos)

            # Broadcast update
            self._broadcast_update('station_update', sta)

            return pos

        except Exception as e:
            # Silently fail for invalid compressed data
            return None

    def parse_aprs_position(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        dest_addr: str = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSPosition]:
        """Parse APRS position report.

        Supports formats:
        - ! (position without timestamp)
        - @ (position with timestamp)
        - / (position with timestamp, no messaging)
        - = (position without timestamp, with messaging)

        Supports both uncompressed and compressed position formats.

        Args:
            station: Station callsign
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops
            digipeater_path: List of digipeater callsigns from AX.25 path
            dest_addr: Destination address (for device identification)

        Returns:
            APRSPosition if position data found, None otherwise
        """
        if not info or info[0] not in ("!", "@", "/", "="):
            return None

        try:
            # Skip timestamp if present (@ and / formats have 7-char timestamp)
            offset = 0
            if info[0] in ("@", "/"):
                offset = 8  # 1 (type) + 7 (timestamp)
            else:
                offset = 1  # Just skip type character

            # Check if this is compressed format
            # Compressed format: symbol_table(1) + lat(4) + lon(4) + symbol(1) + compressed_type(1) = 11 bytes minimum
            # Uncompressed format: lat(8) + symbol_table(1) + lon(9) + symbol(1) = 19 bytes minimum
            if len(info) >= offset + 13:  # Minimum for compressed
                # Check for compressed format: symbol table char followed by base-91 chars
                symbol_table_char = info[offset]
                # Compressed format uses symbol tables / or \, followed by base-91 encoded data
                if symbol_table_char in ('/', '\\') and len(info) >= offset + 13:
                    # Try to parse as compressed
                    result = self._parse_compressed_position(
                        info, offset, station, relay_call, hop_count,
                        digipeater_path, dest_addr,
                        timestamp=timestamp, frame_number=frame_number)
                    if result:
                        return result
                    # If compressed parsing failed, fall through to try uncompressed

            if len(info) < offset + 19:  # Need at least lat/lon/symbol for uncompressed
                return None

            # Parse position data
            # Format: DDMMmmN$DDDMMmmW# where $ is symbol table, # is symbol code
            # Example: 4210.45N/07153.00W> (/ is symbol table, > is symbol code)
            # Symbol table can be / \ or any printable ASCII character
            lat_str = info[offset : offset + 8]  # DDMMmmN or DDMMmmS
            lon_str = info[offset + 9 : offset + 18]  # DDDMMmmW or DDDMMmmE

            # Extract symbol table and code
            symbol_table = info[offset + 8] if offset + 8 < len(info) else "/"
            symbol_code = info[offset + 18] if offset + 18 < len(info) else ">"

            # Parse latitude (DDMMmmN/S format)
            try:
                lat_deg = int(lat_str[0:2])
                lat_min = float(lat_str[2:7])
                lat_dir = lat_str[7]
                latitude = lat_deg + (lat_min / 60.0)
                if lat_dir in ("S", "s"):
                    latitude = -latitude
            except (ValueError, IndexError):
                return None

            # Parse longitude (DDDMMmmW/E format)
            try:
                lon_deg = int(lon_str[0:3])
                lon_min = float(lon_str[3:8])
                lon_dir = lon_str[8]
                longitude = lon_deg + (lon_min / 60.0)
                if lon_dir in ("W", "w"):
                    longitude = -longitude
            except (ValueError, IndexError):
                return None

            # Extract comment (everything after symbol code)
            comment = (
                info[offset + 19 :].strip() if len(info) > offset + 19 else ""
            )

            # Convert to Maidenhead grid square
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Filter out invalid "Null Island" coordinates (0.0, 0.0)
            if latitude == 0.0 and longitude == 0.0:
                print_debug(
                    f"parse_position: Rejecting Null Island coordinates from {station}",
                    level=5,
                )
                return None

            # Identify device type from destination callsign (tocall)
            device_str = None
            if dest_addr:
                try:
                    device_id = get_device_identifier()
                    device_info = device_id.identify_by_tocall(dest_addr)
                    if device_info:
                        device_str = str(device_info)
                except Exception:
                    pass  # Silently ignore device ID errors

            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
                device=device_str,
            )

            # Store latest position for this station
            self.position_reports[station.upper()] = pos

            # Track station activity
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="position", timestamp=timestamp, frame_number=frame_number)
            sta.last_position = pos
            if device_str:
                sta.device = device_str
            self._add_position_to_history(sta, pos)

            # Broadcast station update to web clients
            self._broadcast_update('station_update', sta)

            return pos

        except Exception:
            return None

    def parse_aprs_object(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSPosition]:
        """Parse APRS object report.

        Format: ;OBJECTNAM*DDHHMMzLATITUDEsLONGITUDEsCOMMENT
        Object name is 9 characters (padded with spaces)
        Status is * (live) or _ (killed)

        Args:
            station: Station that sent the object
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)

        Returns:
            APRSPosition if object contains position data, None otherwise
        """
        if not info or info[0] != ";":
            return None

        try:
            # Extract object name (9 chars) and status (* or _)
            if len(info) < 11:  # ; + 9-char name + *
                return None

            object_name = info[1:10].strip()  # 9-character object name
            status = info[10]  # * (live) or _ (killed)

            if status not in ("*", "_"):
                return None

            # Killed objects just announce removal, no position data needed
            if status == "_":
                return None

            # Parse timestamp (7 chars: DDHHMMz)
            if len(info) < 18:  # Need at least: ; + 9 + * + 7
                return None

            timestamp_str = info[11:18]  # DDHHMMz format
            offset = 18  # Start of position data

            if len(info) < offset + 19:  # Need at least lat/lon/symbol
                return None

            # Parse position data (same format as regular position reports)
            lat_str = info[offset : offset + 8]  # DDMMmmN or DDMMmmS
            lon_str = info[offset + 9 : offset + 18]  # DDDMMmmW or DDDMMmmE

            # Extract symbol table and code
            symbol_table = info[offset + 8] if offset + 8 < len(info) else "/"
            symbol_code = info[offset + 18] if offset + 18 < len(info) else ">"

            # Parse latitude (DDMMmmN/S format)
            try:
                lat_deg = int(lat_str[0:2])
                lat_min = float(lat_str[2:7])
                lat_dir = lat_str[7]
                latitude = lat_deg + (lat_min / 60.0)
                if lat_dir in ("S", "s"):
                    latitude = -latitude
            except (ValueError, IndexError):
                return None

            # Parse longitude (DDDMMmmW/E format)
            try:
                lon_deg = int(lon_str[0:3])
                lon_min = float(lon_str[3:8])
                lon_dir = lon_str[8]
                longitude = lon_deg + (lon_min / 60.0)
                if lon_dir in ("W", "w"):
                    longitude = -longitude
            except (ValueError, IndexError):
                return None

            # Extract comment (everything after symbol code)
            comment = (
                info[offset + 19 :].strip() if len(info) > offset + 19 else ""
            )

            # Convert to Maidenhead grid square
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Create position object using the OBJECT name as the station
            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=object_name.upper(),  # Use object name, not sender
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
            )

            # Store latest position for this object
            self.position_reports[object_name.upper()] = pos

            # Track as station (objects are tracked like stations)
            sta = self._get_or_create_station(
                object_name, relay_call, hop_count, packet_type="object", timestamp=timestamp, frame_number=frame_number
            )
            sta.last_position = pos
            self._add_position_to_history(sta, pos)

            # Broadcast station update to web clients
            self._broadcast_update('station_update', sta)

            return pos

        except Exception:
            return None

    def parse_aprs_status(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSStatus]:
        """Parse APRS status report.

        Status format: >Status text here

        Args:
            station: Station callsign
            info: APRS info field
            relay_call: Optional relay station callsign

        Returns:
            APRSStatus object if valid, None otherwise
        """
        try:
            if not info or info[0] != ">":
                return None

            # Extract status text (everything after >)
            status_text = info[1:].strip()

            if not status_text:
                return None

            # Create status object
            status = APRSStatus(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                status_text=status_text,
            )

            # Track as station
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="status", timestamp=timestamp, frame_number=frame_number)
            sta.last_status = status

            return status

        except Exception:
            return None

    def parse_aprs_item(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSPosition]:
        """Parse APRS item report.

        Item format: )NAME!lat/lonSymbol...
        Items are like objects but with 3-9 character names (not fixed at 9)

        Args:
            station: Station callsign that placed the item
            info: APRS info field
            relay_call: Optional relay station callsign

        Returns:
            APRSPosition object if valid, None otherwise
        """
        try:
            if not info or info[0] != ")":
                return None

            # Find the position marker (! or _)
            pos_marker_idx = -1
            for i, c in enumerate(info[1:], 1):
                if c in ("!", "_"):
                    pos_marker_idx = i
                    break

            if pos_marker_idx == -1:
                return None

            # Extract item name (3-9 chars between ) and !)
            item_name = info[1:pos_marker_idx].strip()
            if not item_name or len(item_name) < 3 or len(item_name) > 9:
                return None

            # Parse position (same format as standard position)
            # Position starts after the marker
            offset = pos_marker_idx + 1

            # Need at least lat (8) + symbol table (1) + lon (9) + symbol code (1) = 19 chars
            if len(info) < offset + 19:
                return None

            # Parse latitude: DDMM.MMN (8 chars)
            lat_str = info[offset : offset + 8]
            symbol_table = info[offset + 8]
            lon_str = info[offset + 9 : offset + 18]
            symbol_code = info[offset + 18]

            # Convert lat/lon
            try:
                lat_deg = int(lat_str[0:2])
                lat_min = float(lat_str[2:7])
                lat_dir = lat_str[7]
                latitude = lat_deg + (lat_min / 60.0)
                if lat_dir in ("S", "s"):
                    latitude = -latitude

                lon_deg = int(lon_str[0:3])
                lon_min = float(lon_str[3:8])
                lon_dir = lon_str[8]
                longitude = lon_deg + (lon_min / 60.0)
                if lon_dir in ("W", "w"):
                    longitude = -longitude
            except (ValueError, IndexError):
                return None

            # Extract comment (everything after symbol code)
            comment = (
                info[offset + 19 :].strip() if len(info) > offset + 19 else ""
            )

            # Convert to Maidenhead grid square
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Create position object using the ITEM name as the station
            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=item_name.upper(),  # Use item name, not sender
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
            )

            # Store latest position for this item
            self.position_reports[item_name.upper()] = pos

            # Track as station (items are tracked like stations)
            sta = self._get_or_create_station(item_name, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="item", timestamp=timestamp, frame_number=frame_number)
            sta.last_position = pos

            return pos

        except Exception:
            return None

    def parse_aprs_telemetry(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSTelemetry]:
        """Parse APRS telemetry packet.

        Telemetry format: T#SSS,A1,A2,A3,A4,A5,BBBBBBBB
        SSS = sequence number (000-999)
        A1-A5 = analog values (000-255)
        BBBBBBBB = 8 digital bits (0/1)

        Args:
            station: Station callsign
            info: APRS info field
            relay_call: Optional relay station callsign

        Returns:
            APRSTelemetry object if valid, None otherwise
        """
        try:
            if not info or not info.startswith("T#"):
                return None

            # Remove T# prefix
            data = info[2:].strip()

            # Split by comma
            parts = data.split(",")

            # Need exactly 7 parts: sequence + 5 analog + digital
            if len(parts) != 7:
                return None

            # Parse sequence number
            try:
                sequence = int(parts[0])
                if sequence < 0 or sequence > 999:
                    return None
            except ValueError:
                return None

            # Parse analog values (5 channels)
            analog = []
            for i in range(1, 6):
                try:
                    val = int(parts[i])
                    if val < 0 or val > 255:
                        return None
                    analog.append(val)
                except ValueError:
                    return None

            # Parse digital bits (8 bits)
            digital = parts[6].strip()
            if len(digital) != 8 or not all(c in "01" for c in digital):
                return None

            # Create telemetry object
            telemetry = APRSTelemetry(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                sequence=sequence,
                analog=analog,
                digital=digital,
            )

            # Track as station
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="telemetry", timestamp=timestamp, frame_number=frame_number)
            sta.last_telemetry = telemetry

            # Keep recent telemetry history (last 20 packets)
            sta.telemetry_sequence.append(telemetry)
            if len(sta.telemetry_sequence) > 20:
                sta.telemetry_sequence.pop(0)

            return telemetry

        except Exception:
            return None

    def get_position_reports(self) -> List[APRSPosition]:
        """Get all position reports, sorted by station.

        Returns:
            List of latest position reports from each station
        """
        return sorted(self.position_reports.values(), key=lambda x: x.station)

    def format_position(self, pos: APRSPosition) -> Dict[str, str]:
        """Format position report for display. Delegates to APRSFormatters."""
        return APRSFormatters.format_position(pos)

    @staticmethod
    def clean_position_comment(comment: str) -> str:
        """Clean position comment. Delegates to APRSFormatters."""
        return APRSFormatters.clean_position_comment(comment)

    def format_combined_notification(
        self, pos: APRSPosition, wx: APRSWeather, relay_call: str = None
    ) -> str:
        """Format combined notification. Delegates to APRSFormatters."""
        return APRSFormatters.format_combined_notification(pos, wx, relay_call)

    def get_all_stations(self, sort_by: str = "last") -> List[APRSStation]:
        """Get all tracked stations.

        Args:
            sort_by: Sort order - 'name', 'packets', 'last', or 'hops' (default: 'last')

        Returns:
            List of all stations sorted by specified order
        """
        if sort_by == "name":
            # Sort alphabetically by callsign
            return sorted(self.stations.values(), key=lambda x: x.callsign)
        elif sort_by == "packets":
            # Sort by packet count (highest first)
            return sorted(
                self.stations.values(),
                key=lambda x: x.packets_heard,
                reverse=True,
            )
        elif sort_by == "hops":
            # Sort by hop count (direct RF / 0 hops first)
            return sorted(self.stations.values(), key=lambda x: x.hop_count)
        else:  # 'last' or default
            # Sort by last heard timestamp (most recent first)
            return sorted(
                self.stations.values(),
                key=lambda x: x.last_heard,
                reverse=True,
            )

    def get_station(self, callsign: str) -> Optional[APRSStation]:
        """Get station information.

        Args:
            callsign: Station callsign

        Returns:
            APRSStation if found, None otherwise
        """
        return self.stations.get(callsign.upper())

    def get_zero_hop_stations(self) -> List[APRSStation]:
        """Get all stations heard with zero hops (direct RF, no digipeaters).

        Returns:
            List of APRSStation objects with heard_zero_hop=True and
            zero_hop_packet_count > 0 (filters out stations from before
            zero-hop packet counting was implemented)
        """
        return [station for station in self.stations.values()
                if station.heard_zero_hop and station.zero_hop_packet_count > 0]

    def get_network_digipeater_stats(
        self, hours: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get network-wide digipeater statistics from ReceptionEvents.

        Scans all stations' reception events to count how many packets each
        digipeater has relayed. This is computed on-demand from existing data.

        Args:
            hours: Only include receptions from last N hours (None = all time)

        Returns:
            List of digipeater statistics, sorted by packet count descending:
            [
                {
                    "callsign": "DIGI-CALL",
                    "packets_relayed": 150,
                    "unique_stations": 25,
                    "last_heard": "ISO timestamp",
                    "position": {...} or None
                },
                ...
            ]
        """
        from datetime import timedelta

        # Calculate cutoff time
        cutoff_time = None
        if hours is not None:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

        # Track digipeater activity
        digi_stats = {}  # callsign -> {packets, stations_set, last_heard}

        # Scan all stations' receptions
        for station in self.stations.values():
            for reception in station.receptions:
                # Skip if outside time window
                if cutoff_time and reception.timestamp < cutoff_time:
                    continue

                # Skip if not RF
                if not reception.direct_rf:
                    continue

                # Skip if no digipeater path
                if not reception.digipeater_path:
                    continue

                # Count each digipeater in the path
                for hop in reception.digipeater_path:
                    # Clean callsign (remove asterisk H-bit marker)
                    digi_call = hop.rstrip('*').upper()

                    # Skip empty or WIDEn-N aliases (not actual callsigns)
                    if not digi_call or digi_call.startswith('WIDE'):
                        continue

                    # Initialize if first time seeing this digipeater
                    if digi_call not in digi_stats:
                        digi_stats[digi_call] = {
                            'packets': 0,
                            'stations': set(),
                            'last_heard': reception.timestamp
                        }

                    # Update stats
                    digi_stats[digi_call]['packets'] += 1
                    digi_stats[digi_call]['stations'].add(station.callsign)

                    # Update last_heard if newer
                    if reception.timestamp > digi_stats[digi_call]['last_heard']:
                        digi_stats[digi_call]['last_heard'] = reception.timestamp

        # Convert to list format with positions
        result = []
        for callsign, stats in digi_stats.items():
            entry = {
                'callsign': callsign,
                'packets_relayed': stats['packets'],
                'unique_stations': len(stats['stations']),
                'last_heard': stats['last_heard'].isoformat(),
                'position': None
            }

            # Add position if digipeater is in our station list
            digi_station = self.stations.get(callsign)
            if digi_station and digi_station.last_position:
                pos = digi_station.last_position
                entry['position'] = {
                    'latitude': pos.latitude,
                    'longitude': pos.longitude,
                    'grid_square': pos.grid_square
                }

            result.append(entry)

        # Sort by packets_relayed descending
        result.sort(key=lambda x: x['packets_relayed'], reverse=True)

        return result

    def get_network_path_usage(
        self, hours: Optional[int] = None
    ) -> Dict[str, Any]:
        """Get network-wide path usage statistics from ReceptionEvents.

        Scans all stations' reception events to count how different path types
        are being used across the network (not just this digipeater).

        Args:
            hours: Only include receptions from last N hours (None = all time)

        Returns:
            Dictionary with path usage statistics:
            {
                "path_usage": {
                    "WIDE1-1": {"count": 150, "percentage": 45.5, "stations": 25},
                    "WIDE2-2": {"count": 100, "percentage": 30.3, "stations": 18},
                    ...
                },
                "total_packets": 330
            }
        """
        from datetime import timedelta

        # Calculate cutoff time
        cutoff_time = None
        if hours is not None:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

        # Track path usage
        path_counts = {}  # path_type -> count
        path_stations = {}  # path_type -> set of station callsigns
        total_packets = 0

        # Scan all stations' receptions
        for station in self.stations.values():
            for reception in station.receptions:
                # Skip if outside time window
                if cutoff_time and reception.timestamp < cutoff_time:
                    continue

                # Skip if not RF
                if not reception.direct_rf:
                    continue

                # Skip if no digipeater path
                if not reception.digipeater_path:
                    continue

                # Classify the path type
                path_type = self._classify_path_type(reception.digipeater_path)

                # Count it
                if path_type not in path_counts:
                    path_counts[path_type] = 0
                    path_stations[path_type] = set()

                path_counts[path_type] += 1
                path_stations[path_type].add(station.callsign)
                total_packets += 1

        # Build result with percentages
        path_usage = {}
        for path_type, count in path_counts.items():
            percentage = (count / total_packets * 100) if total_packets > 0 else 0
            path_usage[path_type] = {
                'count': count,
                'percentage': round(percentage, 1),
                'stations': len(path_stations[path_type])
            }

        return {
            'path_usage': path_usage,
            'total_packets': total_packets
        }

    def _classify_path_type(self, digipeater_path: List[str]) -> str:
        """Classify a digipeater path by extracting alias patterns (WIDE/RELAY/TRACE).

        Strips out specific digipeater callsigns and only reports the aliases
        that were requested, which is what matters for understanding network usage.

        Args:
            digipeater_path: List of digipeater hops (e.g., ["N0ABC*", "WIDE2-1"])

        Returns:
            Alias pattern for grouping (e.g., "WIDE1-1", "WIDE2-2", etc.)
        """
        if not digipeater_path:
            return "Direct"

        # Extract only aliases (WIDE, RELAY, TRACE, etc.)
        # Ignore specific digipeater callsigns
        aliases = []
        for hop in digipeater_path:
            hop_clean = hop.rstrip('*').upper()

            # Check if this is an alias (starts with known alias prefixes)
            # Common aliases: WIDE, RELAY, TRACE, TEMP, LOCAL
            if hop_clean.startswith(('WIDE', 'RELAY', 'TRACE', 'TEMP', 'LOCAL')):
                aliases.append(hop_clean)
            # Ignore specific callsigns (e.g., "N0ABC-1", "W1XYZ-15")

        # If we found aliases, report them
        if aliases:
            # For single alias, return as-is
            if len(aliases) == 1:
                return aliases[0]
            # For multiple aliases, show the path
            elif len(aliases) <= 3:
                return ','.join(aliases)
            else:
                # Very unusual - show first 2 plus count
                return f"{aliases[0]},{aliases[1]}+{len(aliases)-2}"

        # If no aliases found (only specific digipeater callsigns)
        return "Via Digipeater"

    def get_network_heatmap(
        self, days: int = 7
    ) -> Dict[str, Any]:
        """Get network-wide time-of-day activity heatmap from ReceptionEvents.

        Scans all stations' reception events to build a 7x24 grid showing
        activity patterns across the network by day of week and hour of day.

        Args:
            days: Number of days to analyze (default: 7)

        Returns:
            Dictionary with heatmap data:
            {
                "heatmap": [
                    [0, 0, 0, 1, 2, 3, ...],  # Sunday (24 hours)
                    [0, 1, 1, 2, 3, 4, ...],  # Monday
                    ...                        # ... through Saturday
                ],
                "peak_hour": 15,
                "peak_day": 4,  # Thursday
                "total_packets": 1250,
                "days_analyzed": 7
            }
        """
        from datetime import timedelta

        # Calculate cutoff time
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)

        # Initialize 7x24 grid (day of week × hour of day)
        heatmap = [[0 for _ in range(24)] for _ in range(7)]
        total_packets = 0

        # Scan all stations' receptions
        for station in self.stations.values():
            for reception in station.receptions:
                # Skip if outside time window
                if reception.timestamp < cutoff_time:
                    continue

                # Skip if not RF
                if not reception.direct_rf:
                    continue

                # Skip if no digipeater path (direct packets only, or digipeated)
                # Actually, let's count ALL RF packets, not just digipeated ones
                # This gives a better picture of network activity

                # Extract day of week (0=Monday, 6=Sunday in Python)
                # Convert to (0=Sunday, 6=Saturday) for consistency
                day_of_week = (reception.timestamp.weekday() + 1) % 7
                hour_of_day = reception.timestamp.hour

                # Increment the grid
                heatmap[day_of_week][hour_of_day] += 1
                total_packets += 1

        # Find peak hour and day
        peak_count = 0
        peak_hour = 0
        peak_day = 0

        for day in range(7):
            for hour in range(24):
                if heatmap[day][hour] > peak_count:
                    peak_count = heatmap[day][hour]
                    peak_day = day
                    peak_hour = hour

        return {
            'heatmap': heatmap,
            'peak_hour': peak_hour,
            'peak_day': peak_day,
            'total_packets': total_packets,
            'days_analyzed': days
        }

    def get_digipeater_coverage(self) -> Dict[str, Dict]:
        """Get digipeater coverage data for mapping.

        Returns a dictionary of digipeaters and the stations they heard DIRECTLY
        over RF (first hop only). Excludes stations heard via:
        - Internet/iGate (heard_direct = False)
        - Other digipeaters (second+ hop)

        This shows each digipeater's actual direct RF coverage footprint.

        Returns:
            Dictionary mapping digipeater callsigns to coverage data:
            {
                "DIGI-CALL": {
                    "callsign": "DIGI-CALL",
                    "position": {...} or None,
                    "stations_heard": [
                        {
                            "callsign": "STATION-CALL",
                            "position": {...},
                            "last_heard": "ISO timestamp",
                            "packets": 10
                        },
                        ...
                    ],
                    "station_count": 5,
                    "has_position": True/False
                },
                ...
            }
        """
        coverage = {}

        # Iterate through all stations to find which digipeaters heard them
        # Only include stations heard directly over RF (not via iGate/internet)
        for station in self.stations.values():
            if not station.digipeaters_heard_by:
                continue

            # Skip stations not heard directly over RF
            if not station.heard_direct:
                continue

            for digi_call in station.digipeaters_heard_by:
                digi_upper = digi_call.upper()

                # Initialize digipeater entry if not exists
                if digi_upper not in coverage:
                    digi_station = self.stations.get(digi_upper)
                    coverage[digi_upper] = {
                        "callsign": digi_upper,
                        "position": None,
                        "stations_heard": [],
                        "station_count": 0,
                        "has_position": False
                    }

                    # Add digipeater's own position if available
                    if digi_station and digi_station.last_position:
                        pos = digi_station.last_position
                        coverage[digi_upper]["position"] = {
                            "latitude": pos.latitude,
                            "longitude": pos.longitude,
                            "grid_square": pos.grid_square
                        }
                        coverage[digi_upper]["has_position"] = True

                # Add this station to the digipeater's heard list
                station_data = {
                    "callsign": station.callsign,
                    "last_heard": station.last_heard.isoformat(),
                    "packets": station.packets_heard
                }

                # Add station position if available
                if station.last_position:
                    station_data["position"] = {
                        "latitude": station.last_position.latitude,
                        "longitude": station.last_position.longitude,
                        "grid_square": station.last_position.grid_square
                    }

                coverage[digi_upper]["stations_heard"].append(station_data)
                coverage[digi_upper]["station_count"] = len(coverage[digi_upper]["stations_heard"])

        return coverage

    def format_station_table_row(self, station: APRSStation) -> Dict[str, str]:
        """Format station for table display. Delegates to APRSFormatters."""
        return APRSFormatters.format_station_table_row(station)

    def format_station_detail(self, station: APRSStation, pressure_threshold: float = 0.3) -> str:
        """Format detailed station information. Delegates to APRSFormatters."""
        return APRSFormatters.format_station_detail(
            station,
            pressure_threshold=pressure_threshold,
            get_zambretti_forecast=lambda cs, **kw: self.get_zambretti_forecast(cs, **kw),
        )

    def _format_temperature_chart(
        self, weather_history: List[APRSWeather], width: int = 60
    ) -> str:
        """Create text-based temperature chart. Delegates to APRSFormatters."""
        return APRSFormatters._format_temperature_chart(weather_history, width)

    def _format_wind_rose(
        self, weather_history: List[APRSWeather]
    ) -> str:
        """Create text-based wind rose. Delegates to APRSFormatters."""
        return APRSFormatters._format_wind_rose(weather_history)

    def clear_database(self):
        """Clear all APRS database entries (stations, messages, positions, weather).

        Returns:
            Tuple of (stations_cleared, messages_cleared)
        """
        station_count = len(self.stations)
        message_count = len(self.monitored_messages)

        self.stations.clear()
        self.messages.clear()
        self.monitored_messages.clear()
        self.weather_reports.clear()
        self.position_reports.clear()

        return (station_count, message_count)

    def prune_database(self, days: int):
        """Prune database entries older than specified days.

        Args:
            days: Number of days - entries last heard more than this many days ago will be removed

        Returns:
            Tuple of (stations_pruned, messages_pruned)
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)

        # Prune stations
        stations_to_remove = []
        for callsign, station in self.stations.items():
            if station.last_heard < cutoff_time:
                stations_to_remove.append(callsign)

        for callsign in stations_to_remove:
            del self.stations[callsign]
            # Also remove from position and weather reports
            if callsign in self.position_reports:
                del self.position_reports[callsign]
            if callsign in self.weather_reports:
                del self.weather_reports[callsign]

        # Prune messages
        messages_before = len(self.monitored_messages)
        self.monitored_messages = [
            msg
            for msg in self.monitored_messages
            if msg.timestamp >= cutoff_time
        ]
        self.messages = [
            msg for msg in self.messages if msg.timestamp >= cutoff_time
        ]
        messages_pruned = messages_before - len(self.monitored_messages)

        return (len(stations_to_remove), messages_pruned)

    def record_digipeater_activity(
        self, station_call: str, path_type: str, original_path: List[str],
        frame_number: Optional[int] = None
    ) -> None:
        """Record a digipeater activity event.

        Args:
            station_call: Callsign of station that was digipeated
            path_type: Path classification (e.g., "WIDE1-1", "WIDE2-1", "Direct", "Other")
            original_path: Original path from packet (list of callsigns)
            frame_number: Optional reference to frame buffer
        """
        now = datetime.now(timezone.utc)

        # Create activity event
        activity = DigipeaterActivity(
            timestamp=now,
            station_call=station_call,
            path_type=path_type,
            original_path=original_path,
            frame_number=frame_number,
        )

        # Append to activities list
        self.digipeater_stats.activities.append(activity)

        # Increment counter
        self.digipeater_stats.packets_digipeated += 1

        # Keep only last 500 activities
        if len(self.digipeater_stats.activities) > 500:
            self.digipeater_stats.activities = self.digipeater_stats.activities[-500:]

        # Recompute aggregates
        self._recompute_digipeater_aggregates()

    def _recompute_digipeater_aggregates(self) -> None:
        """Recompute digipeater aggregate statistics with 3-tier time retention.

        Three-tier retention policy:
        - Last hour: ALL samples (full detail for current activity)
        - 1 hour to 1 day: one sample every 15 minutes (recent trends)
        - Older than 1 day: one sample per hour (long-term history)

        Aggregates:
        - top_stations: Count by station_call
        - path_usage: Count by path_type
        """
        # Skip during migration
        if self._migration_mode:
            return

        now = datetime.now(timezone.utc)
        activities = self.digipeater_stats.activities

        # Sort by timestamp (newest first)
        activities.sort(key=lambda a: a.timestamp, reverse=True)

        # Only run retention policy when activities exceeds threshold
        if len(activities) > 250:
            # Build retention list with three-tier policy
            retained = []
            last_15min = None
            last_hour = None

            for act in activities:
                age = now - act.timestamp

                # Tier 1: Keep ALL activities from the last hour (full detail)
                if age <= timedelta(hours=1):
                    retained.append(act)
                # Tier 2: 1 hour to 1 day - keep one sample every 15 minutes
                elif age <= timedelta(days=1):
                    # Keep if no 15-min sample yet, or if 15+ min since last kept
                    if last_15min is None or (
                        last_15min - act.timestamp
                    ) >= timedelta(minutes=15):
                        retained.append(act)
                        last_15min = act.timestamp
                # Tier 3: Older than 1 day - keep one sample per hour
                else:
                    # Keep if no hourly sample yet, or if 1+ hour since last kept
                    if last_hour is None or (
                        last_hour - act.timestamp
                    ) >= timedelta(hours=1):
                        retained.append(act)
                        last_hour = act.timestamp

            # Update activities with retained samples
            self.digipeater_stats.activities = retained
            activities = retained

        # Recompute aggregates from all retained activities
        top_stations = {}
        path_usage = {}

        for act in activities:
            # Count by station
            top_stations[act.station_call] = top_stations.get(act.station_call, 0) + 1
            # Count by path type
            path_usage[act.path_type] = path_usage.get(act.path_type, 0) + 1

        # Update stats
        self.digipeater_stats.top_stations = top_stations
        self.digipeater_stats.path_usage = path_usage
