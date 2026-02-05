"""APRS data models and dataclasses.

Defines all the core data structures used throughout the APRS tracking system:
- APRSMessage: Sent/received messages with retry tracking
- APRSPosition: Position reports with grid square
- APRSWeather: Weather data from stations
- APRSStatus: Status text reports
- APRSTelemetry: Analog/digital telemetry packets
- APRSStation: Complete station profile with history
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class APRSMessage:
    """Represents an APRS message."""

    timestamp: datetime
    from_call: str
    to_call: str
    message: str
    message_id: Optional[str] = None
    direction: str = "received"  # 'sent' or 'received'
    digipeated: bool = False  # Only relevant for sent messages - true if heard digipeated
    ack_received: bool = False  # Only relevant for sent messages - true if ACK received
    failed: bool = (
        False  # Only relevant for sent messages - true if max retries exceeded
    )
    retry_count: int = 0  # Number of retries attempted
    last_sent: Optional[datetime] = (
        None  # Timestamp of last transmission attempt
    )
    read: bool = False  # Only relevant for received messages


@dataclass
class APRSPosition:
    """Represents an APRS position report."""

    timestamp: datetime
    station: str
    latitude: float  # Decimal degrees
    longitude: float  # Decimal degrees
    altitude: Optional[float] = None  # Feet
    symbol_table: str = "/"
    symbol_code: str = ">"
    comment: str = ""
    grid_square: str = ""  # Maidenhead grid square
    device: Optional[str] = None  # Device/radio type (e.g., "Yaesu FTM-400DR")


@dataclass
class APRSWeather:
    """Represents an APRS weather report."""

    timestamp: datetime
    station: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    temperature: Optional[float] = None  # Fahrenheit
    humidity: Optional[int] = None  # Percentage
    pressure: Optional[float] = None  # mbar
    wind_speed: Optional[float] = None  # MPH
    wind_direction: Optional[int] = None  # Degrees
    wind_gust: Optional[float] = None  # MPH
    rain_1h: Optional[float] = None  # inches
    rain_24h: Optional[float] = None  # inches
    rain_since_midnight: Optional[float] = None  # inches
    raw_data: str = ""
    pressure_tendency: Optional[str] = None  # 'rising', 'falling', 'steady'
    pressure_change_3h: Optional[float] = None  # Change in mbar over 3 hours


@dataclass
class APRSStatus:
    """Represents an APRS status report."""

    timestamp: datetime
    station: str
    status_text: str


@dataclass
class APRSTelemetry:
    """Represents an APRS telemetry packet."""

    timestamp: datetime
    station: str
    sequence: int  # 000-999
    analog: List[int]  # 5 analog values (0-255)
    digital: str  # 8 digital bits as string


@dataclass
class APRSStation:
    """Represents an APRS station with all known information."""

    callsign: str
    first_heard: datetime
    last_heard: datetime

    # Position data
    last_position: Optional[APRSPosition] = None
    position_history: List[APRSPosition] = field(
        default_factory=list
    )  # Historical position reports with intelligent retention

    # Weather data
    last_weather: Optional[APRSWeather] = None
    weather_history: List[APRSWeather] = field(
        default_factory=list
    )  # Historical weather reports with intelligent retention

    # Status data
    last_status: Optional[APRSStatus] = None

    # Telemetry data
    last_telemetry: Optional[APRSTelemetry] = None
    telemetry_sequence: List[APRSTelemetry] = field(
        default_factory=list
    )  # Recent telemetry packets

    # Message statistics
    messages_received: int = 0  # Messages from this station
    messages_sent: int = 0  # Messages to this station

    # Packet statistics
    packets_heard: int = 0  # Total packets from this station

    # Third-party tracking
    relay_paths: List[str] = field(
        default_factory=list
    )  # Relay stations (iGates) that forwarded packets
    heard_direct: bool = (
        False  # True if heard directly on RF (not via third-party)
    )
    hop_count: int = (
        999  # Minimum hop count observed (0 = direct RF, 999 = unknown)
    )
    heard_zero_hop: bool = (
        False  # True if we've EVER heard this station with 0 hops (direct, no digipeaters)
    )
    last_heard_zero_hop: Optional[datetime] = (
        None  # Timestamp of last zero-hop packet (direct RF only, no digipeaters)
    )
    zero_hop_packet_count: int = 0  # Count of packets heard with 0 hops (direct RF only)

    # Device identification
    device: Optional[str] = None  # Device/radio type (e.g., "Yaesu FTM-400DR")

    # Digipeater tracking (for coverage mapping and routing)
    digipeater_path: List[str] = field(
        default_factory=list
    )  # Complete digipeater path from last packet (all hops) - DEPRECATED, use digipeater_paths

    digipeater_paths: List[List[str]] = field(
        default_factory=list
    )  # All unique digipeater paths observed (including "DIRECT" for 0-hop)

    digipeaters_heard_by: List[str] = field(
        default_factory=list
    )  # First-hop digipeaters only (for coverage circles)

    is_digipeater: bool = False  # True if this station has acted as a digipeater
