"""APRS data models and dataclasses.

Defines all the core data structures used throughout the APRS tracking system:
- APRSMessage: Sent/received messages with retry tracking
- APRSPosition: Position reports with grid square
- APRSWeather: Weather data from stations
- APRSStatus: Status text reports
- APRSTelemetry: Analog/digital telemetry packets
- ReceptionEvent: Single packet reception event (ground truth for events-based architecture)
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
class ReceptionEvent:
    """Single reception of a packet (direct RF or relayed).

    This is the ground truth for packet receptions. Instead of maintaining
    fragile aggregate fields (hop_count, heard_direct, relay_paths, etc.),
    we record each reception as an event and compute aggregates from the
    complete history.

    Attributes:
        timestamp: When the packet was received
        hop_count: 0=direct RF, 1+=digipeated, 999=unknown/igated
        direct_rf: True if heard on RF, False if via iGate third-party
        relay_call: iGate callsign that relayed (None if direct)
        digipeater_path: RF path taken (empty if direct RF)
        packet_type: Type of packet ('position', 'weather', 'message', 'status', 'telemetry')
        frame_number: Optional reference back to frame buffer for audit trail
    """
    timestamp: datetime
    hop_count: int  # 0=direct, 1+=relayed, 999=unknown/igated
    direct_rf: bool  # True if heard on RF, False if third-party/iGate
    relay_call: Optional[str] = None  # iGate that relayed (None if direct)
    digipeater_path: List[str] = field(default_factory=list)  # RF path taken
    packet_type: str = "unknown"  # Position, weather, message, status, telemetry
    frame_number: Optional[int] = None  # Reference to frame buffer


@dataclass
class APRSStation:
    """Represents an APRS station with all known information."""

    callsign: str
    first_heard: datetime
    last_heard: datetime

    # Reception history - single source of truth
    receptions: List[ReceptionEvent] = field(
        default_factory=list
    )  # Complete packet reception history (keep last 200)

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

    # Device identification
    device: Optional[str] = None  # Device/radio type (e.g., "Yaesu FTM-400DR")

    # Digipeater participation tracking
    is_digipeater: bool = False  # True if this station has acted as a digipeater
    digipeaters_heard_by: List[str] = field(
        default_factory=list
    )  # First-hop digipeaters only (for coverage circles)

    # DEPRECATED AGGREGATE FIELDS (replaced by computed properties from receptions)
    # These fields are maintained for backward compatibility during transition.
    # New code should use the @property methods below, which compute values from receptions.
    #
    # Fields being replaced:
    # - relay_paths → @property (computed from receptions)
    # - heard_direct → @property (computed from receptions)
    # - hop_count → @property (computed from receptions)
    # - heard_zero_hop → @property (computed from receptions)
    # - last_heard_zero_hop → @property (computed from receptions)
    # - zero_hop_packet_count → @property (computed from receptions)
    # - digipeater_path → @property (computed from receptions)
    # - digipeater_paths → @property (computed from receptions)
    # - digipeaters_heard_by (currently not converted to property - still computed separately)

    # Computed Properties (Single Source of Truth: receptions)

    @property
    def hop_count(self) -> int:
        """Minimum hop count from direct RF receptions (exclude iGate).

        Returns:
            Minimum hop count (0=direct RF, 1+=digipeated, 999=unknown/no receptions)
        """
        direct = [r.hop_count for r in self.receptions
                  if r.direct_rf and r.hop_count < 999]
        return min(direct) if direct else 999

    @property
    def heard_direct(self) -> bool:
        """True if we've ever heard this station directly on RF.

        Returns:
            True if any reception was direct RF, False otherwise
        """
        return any(r.direct_rf for r in self.receptions)

    @property
    def heard_zero_hop(self) -> bool:
        """True if we've ever heard zero-hop (direct RF, no digipeaters).

        Returns:
            True if any direct RF reception had zero hops, False otherwise
        """
        return any(r.hop_count == 0 and r.direct_rf for r in self.receptions)

    @property
    def zero_hop_packet_count(self) -> int:
        """Count of zero-hop receptions (direct RF, no digipeaters).

        Returns:
            Number of receptions with hop_count=0 and direct_rf=True
        """
        return sum(1 for r in self.receptions
                   if r.hop_count == 0 and r.direct_rf)

    @property
    def relay_paths(self) -> List[str]:
        """Unique iGates that relayed this station.

        Returns:
            Sorted list of unique relay callsigns (empty if only direct RF)
        """
        relays = {r.relay_call for r in self.receptions if r.relay_call}
        return sorted(relays)

    @property
    def last_heard_zero_hop(self) -> Optional[datetime]:
        """Timestamp of last zero-hop reception (direct RF, no digipeaters).

        Returns:
            Timestamp of most recent zero-hop reception, or None if never heard zero-hop
        """
        zero_hops = [r.timestamp for r in self.receptions
                     if r.hop_count == 0 and r.direct_rf]
        return max(zero_hops) if zero_hops else None

    @property
    def digipeater_path(self) -> List[str]:
        """Complete digipeater path from last direct RF packet.

        Returns:
            Digipeater path from most recent direct RF reception, empty list if none
        """
        direct_rf_receptions = [r for r in self.receptions if r.direct_rf]
        return direct_rf_receptions[-1].digipeater_path if direct_rf_receptions else []

    @property
    def digipeater_paths(self) -> List[List[str]]:
        """All unique digipeater paths observed.

        Returns:
            List of all unique paths, sorted. Includes empty list for direct RF packets.
        """
        paths = set()
        for r in self.receptions:
            if r.digipeater_path:
                paths.add(tuple(r.digipeater_path))
            else:
                # Mark direct RF packets with empty tuple
                paths.add(())
        # Convert back to lists and filter out empty lists unless it was explicit
        result = [list(p) for p in sorted(paths)]
        return result
