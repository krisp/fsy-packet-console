"""APRS Web API - REST API handlers and serialization.

Provides JSON serialization for APRS data structures and HTTP request handlers
for the web interface.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from aiohttp import web

from .aprs_manager import APRSManager
from .aprs.models import (
    APRSMessage,
    APRSPosition,
    APRSStation,
    APRSStatus,
    APRSTelemetry,
    APRSWeather,
)
from .aprs.geo_utils import maidenhead_to_latlon, calculate_dew_point


def serialize_datetime(dt: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO 8601 string.

    Args:
        dt: Datetime object to serialize

    Returns:
        ISO 8601 formatted string or None
    """
    if dt is None:
        return None
    return dt.isoformat()


def serialize_position(pos: Optional[APRSPosition]) -> Optional[Dict[str, Any]]:
    """Convert APRSPosition to JSON dict.

    Args:
        pos: APRSPosition object

    Returns:
        Dictionary with position data or None
    """
    if pos is None:
        return None

    return {
        "timestamp": serialize_datetime(pos.timestamp),
        "station": pos.station,
        "latitude": pos.latitude,
        "longitude": pos.longitude,
        "altitude": pos.altitude,
        "symbol_table": pos.symbol_table,
        "symbol_code": pos.symbol_code,
        "comment": pos.comment,
        "grid_square": pos.grid_square
    }


def serialize_weather(wx: Optional[APRSWeather]) -> Optional[Dict[str, Any]]:
    """Convert APRSWeather to JSON dict.

    Args:
        wx: APRSWeather object

    Returns:
        Dictionary with weather data or None
    """
    if wx is None:
        return None

    # Calculate dew point if we have temp and humidity
    dew_point = None
    if wx.temperature is not None and wx.humidity is not None:
        dew_point = calculate_dew_point(wx.temperature, wx.humidity)

    return {
        "timestamp": serialize_datetime(wx.timestamp),
        "station": wx.station,
        "latitude": wx.latitude,
        "longitude": wx.longitude,
        "temperature": wx.temperature,
        "dew_point": dew_point,
        "humidity": wx.humidity,
        "pressure": wx.pressure,
        "pressure_tendency": wx.pressure_tendency,
        "pressure_change_3h": wx.pressure_change_3h,
        "wind_speed": wx.wind_speed,
        "wind_direction": wx.wind_direction,
        "wind_gust": wx.wind_gust,
        "rain_1h": wx.rain_1h,
        "rain_24h": wx.rain_24h,
        "rain_since_midnight": wx.rain_since_midnight,
        "raw_data": wx.raw_data
    }


def serialize_message(msg: APRSMessage) -> Dict[str, Any]:
    """Convert APRSMessage to JSON dict.

    Args:
        msg: APRSMessage object

    Returns:
        Dictionary with message data
    """
    return {
        "timestamp": serialize_datetime(msg.timestamp),
        "from_call": msg.from_call,
        "to_call": msg.to_call,
        "message": msg.message,
        "message_id": msg.message_id,
        "direction": msg.direction,
        "digipeated": msg.digipeated,
        "ack_received": msg.ack_received,
        "failed": msg.failed,
        "retry_count": msg.retry_count,
        "last_sent": serialize_datetime(msg.last_sent),
        "read": msg.read
    }


def serialize_station(station: APRSStation, include_history: bool = False) -> Dict[str, Any]:
    """Convert APRSStation to JSON dict.

    Args:
        station: APRSStation object
        include_history: Whether to include full position and weather history

    Returns:
        Dictionary with station data
    """
    # Check if station has path history (2+ positions with different coordinates)
    # This identifies mobile stations, not just stations that beacon the same position
    has_path = False
    if station.position_history and len(station.position_history) >= 2:
        valid_positions = [p for p in station.position_history
                         if not (p.latitude == 0.0 and p.longitude == 0.0)]

        if len(valid_positions) >= 2:
            # Check if any positions are different (station has actually moved)
            first_pos = valid_positions[0]
            for pos in valid_positions[1:]:
                # Consider it movement if lat/lon differs by more than 0.0001 degrees (~11 meters)
                lat_diff = abs(pos.latitude - first_pos.latitude)
                lon_diff = abs(pos.longitude - first_pos.longitude)
                if lat_diff > 0.0001 or lon_diff > 0.0001:
                    has_path = True
                    break

    data = {
        "callsign": station.callsign,
        "device": station.device,  # Device/radio type (e.g., "Yaesu FTM-400DR")
        "first_heard": serialize_datetime(station.first_heard),
        "last_heard": serialize_datetime(station.last_heard),
        "has_position": station.last_position is not None,
        "has_path": has_path,
        "last_position": serialize_position(station.last_position),
        "has_weather": station.last_weather is not None,
        "last_weather": serialize_weather(station.last_weather),
        "is_digipeater": station.is_digipeater,
        "messages_received": station.messages_received,
        "messages_sent": station.messages_sent,
        "packets_heard": station.packets_heard,
        "heard_direct": station.heard_direct,
        "hop_count": station.hop_count if station.hop_count != 999 else None,
        "heard_zero_hop": station.heard_zero_hop,
        "zero_hop_packet_count": station.zero_hop_packet_count,
        "last_heard_zero_hop": serialize_datetime(station.last_heard_zero_hop),
        "relay_paths": station.relay_paths[:5],  # Limit to recent 5
        "digipeater_path": station.digipeater_path,  # Complete digipeater path (legacy)
        "digipeater_paths": station.digipeater_paths,  # All unique paths observed
        "digipeaters_heard_by": station.digipeaters_heard_by  # First hop only
    }

    if include_history:
        if station.position_history:
            data["position_history"] = [serialize_position(pos) for pos in station.position_history]
        if station.weather_history:
            data["weather_history"] = [serialize_weather(wx) for wx in station.weather_history]

    return data


class APIHandlers:
    """HTTP request handlers for the APRS Web API."""

    def __init__(self, aprs_manager, get_mycall, start_time, get_mylocation=None, get_wxtrend=None):
        """Initialize API handlers.

        Args:
            aprs_manager: APRSManager instance
            get_mycall: Callable that returns current MYCALL
            start_time: Server start datetime
            get_mylocation: Callable that returns current MYLOCATION (optional)
            get_wxtrend: Callable that returns current WXTREND threshold (optional)
        """
        self.aprs = aprs_manager
        self.get_mycall = get_mycall
        self.get_mylocation = get_mylocation or (lambda: "")
        self.get_wxtrend = get_wxtrend or (lambda: "0.3")
        self.start_time = start_time

    async def handle_get_stations(self, request: web.Request) -> web.Response:
        """GET /api/stations - Get all stations with optional sorting.

        Query params:
            sort_by: 'last' (default), 'name', 'packets', 'hops'
        """
        sort_by = request.query.get('sort_by', 'last')

        # Get all stations
        stations = list(self.aprs.stations.values())

        # Sort stations
        if sort_by == 'name':
            stations.sort(key=lambda s: s.callsign)
        elif sort_by == 'packets':
            stations.sort(key=lambda s: s.packets_heard, reverse=True)
        elif sort_by == 'hops':
            stations.sort(key=lambda s: (s.hop_count if s.hop_count != 999 else 9999))
        else:  # 'last' is default
            stations.sort(key=lambda s: s.last_heard, reverse=True)

        return web.json_response({
            "stations": [serialize_station(s) for s in stations],
            "count": len(stations)
        })

    async def handle_get_station(self, request: web.Request) -> web.Response:
        """GET /api/stations/{callsign} - Get detailed station info."""
        callsign = request.match_info['callsign'].upper()

        # Strip asterisk from callsign (APRS path marker, not part of callsign)
        # This handles old URLs/bookmarks that might have asterisks
        callsign_clean = callsign.rstrip('*')

        station = self.aprs.stations.get(callsign_clean)
        if not station:
            # Debug: show what we're looking for and what's available
            print(f"DEBUG: Looking for station '{callsign_clean}'")
            print(f"DEBUG: Available stations with similar names:")
            for key in self.aprs.stations.keys():
                if callsign_clean.replace('/', '') in key or key.replace('/', '') in callsign_clean:
                    print(f"  - '{key}'")
            raise web.HTTPNotFound(text=f"Station {callsign_clean} not found")

        return web.json_response(serialize_station(station, include_history=True))

    async def handle_get_station_paths(self, request: web.Request) -> web.Response:
        """POST /api/stations/paths - Get position history for multiple stations.

        Request body (JSON):
            {
                "callsigns": ["K1MAL-7", "W1ABC-9", ...],
                "cutoff_time": 1738454400  // Optional: Unix timestamp (seconds)
            }

        Returns:
            {
                "K1MAL-7": [{position1}, {position2}, ...],
                "W1ABC-9": [{position1}, {position2}, ...],
                ...
            }
        """
        try:
            data = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="Invalid JSON in request body")

        callsigns = data.get('callsigns', [])
        cutoff_time = data.get('cutoff_time', None)  # Unix timestamp in seconds

        if not isinstance(callsigns, list):
            raise web.HTTPBadRequest(text="'callsigns' must be an array")

        # Limit to 100 stations to prevent abuse
        callsigns = callsigns[:100]

        # Convert cutoff_time to datetime if provided
        cutoff_datetime = None
        if cutoff_time is not None:
            try:
                cutoff_datetime = datetime.fromtimestamp(cutoff_time)
            except (ValueError, OSError):
                raise web.HTTPBadRequest(text="Invalid cutoff_time timestamp")

        result = {}

        for callsign in callsigns:
            callsign_clean = callsign.upper().strip().rstrip('*')
            station = self.aprs.stations.get(callsign_clean)

            if station and station.position_history:
                positions = station.position_history

                # Filter by time if cutoff specified
                if cutoff_datetime:
                    positions = [p for p in positions if p.timestamp >= cutoff_datetime]

                # Only include if there are positions to return
                if positions:
                    result[callsign_clean] = [serialize_position(p) for p in positions]

        return web.json_response(result)

    async def handle_get_weather(self, request: web.Request) -> web.Response:
        """GET /api/weather - Get all weather stations.

        Query params:
            sort_by: 'last' (default), 'name', 'temp'
        """
        sort_by = request.query.get('sort_by', 'last')

        # Get all stations with weather data
        weather_stations = [s for s in self.aprs.stations.values() if s.last_weather is not None]

        # Sort stations
        if sort_by == 'name':
            weather_stations.sort(key=lambda s: s.callsign)
        elif sort_by == 'temp':
            weather_stations.sort(
                key=lambda s: s.last_weather.temperature if s.last_weather.temperature else -999,
                reverse=True
            )
        else:  # 'last' is default
            weather_stations.sort(key=lambda s: s.last_heard, reverse=True)

        return web.json_response({
            "weather_stations": [serialize_station(s) for s in weather_stations],
            "count": len(weather_stations)
        })

    async def handle_get_zambretti_forecast(self, request: web.Request) -> web.Response:
        """GET /api/zambretti/<callsign> - Get Zambretti weather forecast for station.

        Args:
            callsign: Station callsign from URL path

        Returns:
            JSON response with forecast data or error
        """
        callsign = request.match_info.get('callsign', '').upper()

        if not callsign:
            return web.json_response(
                {"error": "Callsign required"},
                status=400
            )

        # Get WXTREND threshold from config
        try:
            threshold = float(self.get_wxtrend())
        except (ValueError, TypeError):
            threshold = 0.3  # Default fallback

        forecast = self.aprs.get_zambretti_forecast(callsign, pressure_threshold=threshold)

        if forecast is None:
            return web.json_response(
                {"error": "No forecast available - station has no pressure data"},
                status=404
            )

        return web.json_response(forecast)

    async def handle_get_messages(self, request: web.Request) -> web.Response:
        """GET /api/messages - Get messages addressed to us.

        Query params:
            unread_only: 'true' to get only unread messages
        """
        unread_only = request.query.get('unread_only', 'false').lower() == 'true'

        messages = self.aprs.messages
        if unread_only:
            messages = [m for m in messages if not m.read]

        # Sort by timestamp, newest first
        messages = sorted(messages, key=lambda m: m.timestamp, reverse=True)

        return web.json_response({
            "messages": [serialize_message(m) for m in messages],
            "count": len(messages),
            "unread_count": sum(1 for m in self.aprs.messages if not m.read)
        })

    async def handle_get_monitored_messages(self, request: web.Request) -> web.Response:
        """GET /api/monitored_messages - Get all monitored APRS messages.

        Returns all messages heard on the network, not just those addressed to us.

        Query params:
            limit: Maximum number of messages to return (default: 100)
            callsign: Filter messages to/from specific callsign (optional)
        """
        # Handle limit parameter with validation for null/empty values
        limit_str = request.query.get('limit', '100')
        if limit_str in ('null', 'undefined', ''):
            limit = 100
        else:
            try:
                limit = int(limit_str)
            except ValueError:
                limit = 100
        callsign_filter = request.query.get('callsign', '').upper().strip()

        # Get monitored messages from APRS manager
        messages = self.aprs.monitored_messages

        # Filter by callsign if provided
        if callsign_filter:
            messages = [
                m for m in messages
                if m.from_call == callsign_filter or m.to_call == callsign_filter
            ]

        # Sort by timestamp, newest first
        messages = sorted(messages, key=lambda m: m.timestamp, reverse=True)

        # Apply limit only if no callsign filter (get all for specific station)
        if not callsign_filter:
            messages = messages[:limit]

        return web.json_response({
            "messages": [serialize_message(m) for m in messages],
            "count": len(messages),
            "total_count": len(self.aprs.monitored_messages)
        })

    async def handle_get_status(self, request: web.Request) -> web.Response:
        """GET /api/status - Get system status."""
        uptime = datetime.now() - self.start_time

        return web.json_response({
            "mycall": self.get_mycall(),
            "uptime_seconds": int(uptime.total_seconds()),
            "start_time": serialize_datetime(self.start_time),
            "station_count": len(self.aprs.stations),
            "message_count": len(self.aprs.messages),
            "unread_messages": sum(1 for m in self.aprs.messages if not m.read),
            "monitored_message_count": len(self.aprs.monitored_messages),
            "weather_station_count": sum(1 for s in self.aprs.stations.values() if s.last_weather),
            "direct_stations": sum(1 for s in self.aprs.stations.values() if s.heard_direct)
        })

    async def handle_get_digipeater_coverage(self, request: web.Request) -> web.Response:
        """GET /api/digipeaters - Get digipeater coverage data.

        Returns coverage information for all digipeaters including which
        stations they've heard and their positions for mapping.
        """
        coverage = self.aprs.get_digipeater_coverage()

        return web.json_response({
            "digipeaters": coverage,
            "count": len(coverage)
        })

    async def handle_get_digipeater(self, request: web.Request) -> web.Response:
        """GET /api/digipeaters/{callsign} - Get coverage data for one digipeater."""
        callsign = request.match_info['callsign'].upper()

        coverage = self.aprs.get_digipeater_coverage()
        if callsign not in coverage:
            raise web.HTTPNotFound(text=f"Digipeater {callsign} not found or has no coverage data")

        return web.json_response(coverage[callsign])

    async def handle_get_gps(self, request: web.Request) -> web.Response:
        """GET /api/gps - Get current GPS position.

        Returns the local station's GPS position if available, or MYLOCATION fallback.
        """
        # Get GPS position from command processor if available
        gps_position = None
        gps_locked = False

        # Access GPS data through APRS manager's command processor reference
        if hasattr(self.aprs, '_cmd_processor') and self.aprs._cmd_processor:
            cmd_processor = self.aprs._cmd_processor
            gps_position = getattr(cmd_processor, 'gps_position', None)
            gps_locked = getattr(cmd_processor, 'gps_locked', False)

        if gps_position and gps_locked:
            return web.json_response({
                "locked": True,
                "latitude": gps_position['latitude'],
                "longitude": gps_position['longitude'],
                "altitude": gps_position.get('altitude'),
                "speed": gps_position.get('speed'),
                "heading": gps_position.get('heading'),
                "timestamp": gps_position.get('timestamp'),
                "accuracy": gps_position.get('accuracy'),
                "source": "GPS"
            })
        else:
            # Try MYLOCATION as fallback
            mylocation = self.get_mylocation()
            if mylocation:
                try:
                    lat, lon = maidenhead_to_latlon(mylocation)
                    return web.json_response({
                        "locked": True,
                        "latitude": lat,
                        "longitude": lon,
                        "altitude": None,
                        "source": "MYLOCATION"
                    })
                except ValueError:
                    pass

            return web.json_response({
                "locked": False,
                "latitude": None,
                "longitude": None
            })

    async def handle_send_message(self, request: web.Request) -> web.Response:
        """POST /api/messages - Send APRS message.

        TODO: Implement authentication before enabling this endpoint.
        """
        raise web.HTTPNotImplemented(text="Message sending not yet implemented - authentication required")
