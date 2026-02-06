"""Packet duplicate detection with MD5 hash-based 30-second window."""

import hashlib
import time
from datetime import datetime, timezone
from typing import Dict, List, Set

from .models import APRSStation
from src.utils import print_debug

# Duplicate packet suppression window (seconds)
DUPLICATE_WINDOW = 30


class DuplicateDetector:
    """Manages duplicate packet detection using hash-based caching.

    Suppresses multiple digipeater copies of the same packet while allowing
    new packets from the same station. Uses MD5 hashing of callsign+content
    with a 30-second sliding window.
    """

    def __init__(self, window_seconds: int = DUPLICATE_WINDOW):
        """Initialize the duplicate detector.

        Args:
            window_seconds: Time window for considering packets as duplicates
        """
        self.window_seconds = window_seconds
        self._duplicate_cache: Dict[str, float] = {}  # hash -> timestamp
        self._stations_dict = None  # Will be set by APRSManager
        self._manager = None  # Will be set by APRSManager

    def set_stations_reference(self, stations_dict):
        """Set reference to APRSManager's stations dictionary.

        Args:
            stations_dict: Dictionary of APRSStation objects
        """
        self._stations_dict = stations_dict

    def set_manager_reference(self, manager):
        """Set reference to the APRSManager instance.

        Args:
            manager: APRSManager instance
        """
        self._manager = manager

    def is_duplicate(self, callsign: str, info: str, timestamp: float = None) -> bool:
        """Check if packet is a duplicate based on source and content.

        Packets from the same source with identical content within the
        duplicate window are considered duplicates.

        Args:
            callsign: Source callsign
            info: Packet information field content
            timestamp: Optional timestamp for the packet (defaults to now, used by migrations)

        Returns:
            True if packet is a duplicate, False otherwise
        """
        # Create hash of source + content
        packet_key = f"{callsign.upper()}:{info}"
        packet_hash = hashlib.md5(packet_key.encode()).hexdigest()

        # Use provided timestamp or current time
        current_time = timestamp if timestamp is not None else time.time()

        # Clean old entries from cache (older than duplicate window)
        expired = [
            h
            for h, ts in self._duplicate_cache.items()
            if current_time - ts > self.window_seconds
        ]
        for h in expired:
            del self._duplicate_cache[h]

        # Check if this packet hash exists in cache
        if packet_hash in self._duplicate_cache:
            # Duplicate found
            print_debug(
                f"APRS duplicate suppressed: {callsign} (digipeated copy)",
                level=6,
            )
            return True

        # Not a duplicate - add to cache
        self._duplicate_cache[packet_hash] = current_time
        return False

    def record_path(self, callsign: str, digipeater_path: List[str],
                   stations_dict: Dict = None, timestamp: float = None, frame_number: int = None, relay_call: str = None):
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
            stations_dict: Optional reference to stations dictionary for legacy support
            timestamp: Optional timestamp for the packet (used by migrations)
            frame_number: Optional frame buffer reference number
            relay_call: Optional relay station callsign for third-party packets
        """
        # Use provided dict or stored reference
        # NOTE: For duplicates, we still want to track digipeater paths for coverage analysis.
        # Create a ReceptionEvent via _get_or_create_station to record the path.

        if stations_dict is None:
            stations_dict = self._stations_dict

        if stations_dict is None:
            return  # No way to record without stations dict

        if not digipeater_path:
            return  # No digipeaters to record

        # Import APRSManager to access _get_or_create_station
        # (We can't directly import at module level due to circular dependency)
        from src.aprs.manager import APRSManager

        # Find the APRSManager instance that owns this stations_dict
        # This is a bit hacky, but necessary for the architecture
        manager = getattr(self, '_manager', None)
        if manager and hasattr(manager, '_get_or_create_station'):
            # Convert timestamp float to datetime if provided (timezone-aware UTC)
            timestamp_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None

            # Use _get_or_create_station to track the path via ReceptionEvent
            # Mark as duplicate so packets_heard doesn't increment
            # Pass relay_call if provided (third-party packets can be duplicates too!)
            manager._get_or_create_station(
                callsign=callsign,
                relay_call=relay_call,  # Pass through relay info (None for RF, iGate for third-party)
                hop_count=len(digipeater_path),  # Estimate based on path length
                is_duplicate=True,  # Don't increment packet count
                digipeater_path=digipeater_path,
                packet_type="unknown",  # We don't know the type for duplicates
                frame_number=frame_number,  # Preserve frame number for migration tracking
                timestamp=timestamp_dt,  # Pass historical timestamp
            )
