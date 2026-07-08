"""APRS Message and Weather Tracking Manager.

Tracks APRS messages sent to our station and weather reports from other
stations. Packet parsing is provided by APRSParserMixin (parser.py) and
persistence by APRSDatabaseMixin (database.py); APRSManager composes both.
"""

import asyncio
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.utils import print_debug

# Import models and utilities from the modular package
from .models import (
    APRSMessage, APRSPosition, APRSWeather, APRSStation
)
from .database import APRSDatabaseMixin
from .parser import APRSParserMixin
from .duplicate_detector import DuplicateDetector
from .geo_utils import latlon_to_maidenhead, maidenhead_to_latlon, calculate_dew_point
from .formatters import APRSFormatters
from .weather_forecast import calculate_zambretti_code, ZAMBRETTI_FORECASTS
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

class APRSManager(APRSDatabaseMixin, APRSParserMixin):
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

    def prune_database(self, days: int) -> Tuple[int, int]:
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

        # Prune messages. The same message object may appear in both
        # monitored_messages and messages (personal/sent messages are
        # tracked in both), so count unique objects to avoid double
        # counting while still counting messages present in only one list.
        pruned_ids = {
            id(msg)
            for msg in self.monitored_messages
            if msg.timestamp < cutoff_time
        }
        pruned_ids.update(
            id(msg) for msg in self.messages if msg.timestamp < cutoff_time
        )
        self.monitored_messages = [
            msg
            for msg in self.monitored_messages
            if msg.timestamp >= cutoff_time
        ]
        self.messages = [
            msg for msg in self.messages if msg.timestamp >= cutoff_time
        ]

        return (len(stations_to_remove), len(pruned_ids))

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
