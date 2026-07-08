"""APRS database persistence.

Provides the :class:`APRSDatabaseMixin`, which supplies the database
save/load behaviour for :class:`~src.aprs.manager.APRSManager`.

Responsibilities:
- GZIP-compressed JSON persistence (with optional ujson acceleration)
- Legacy plain-JSON fallback and automatic migration to GZIP on next save
- Atomic writes to prevent corruption
- Async (thread-pool) wrappers for non-blocking startup/save
- Migration-state hook-in (``self.migrations``)

The mixin only manipulates state that lives on ``APRSManager`` instances
(``self.stations``, ``self.monitored_messages``, ``self.digipeater_stats``,
etc.), so it is composed into the manager via multiple inheritance rather
than delegation. This keeps the public API (``save_database``,
``load_database`` and the ``*_async`` variants) unchanged.
"""

import asyncio
import gzip
import json
import os
import time
from datetime import datetime, timezone

# Try to use ujson for faster serialization (3-5x speedup)
try:
    import ujson
    HAS_UJSON = True
except ImportError:
    HAS_UJSON = False

from src.utils import print_debug, print_error, print_info

from .models import (
    APRSMessage, APRSPosition, APRSWeather, APRSStatus,
    APRSTelemetry, APRSStation, ensure_utc_aware
)
from .digipeater_stats import DigipeaterStats
from .weather_forecast import _parse_pressure_from_raw


class APRSDatabaseMixin:
    """Database persistence behaviour for :class:`APRSManager`."""

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
