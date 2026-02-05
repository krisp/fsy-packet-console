"""Packet duplicate detection with MD5 hash-based 30-second window."""

import hashlib
import time
from datetime import datetime
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

    def set_stations_reference(self, stations_dict):
        """Set reference to APRSManager's stations dictionary.

        Args:
            stations_dict: Dictionary of APRSStation objects
        """
        self._stations_dict = stations_dict

    def is_duplicate(self, callsign: str, info: str) -> bool:
        """Check if packet is a duplicate based on source and content.

        Packets from the same source with identical content within the
        duplicate window are considered duplicates.

        Args:
            callsign: Source callsign
            info: Packet information field content

        Returns:
            True if packet is a duplicate, False otherwise
        """
        # Create hash of source + content
        packet_key = f"{callsign.upper()}:{info}"
        packet_hash = hashlib.md5(packet_key.encode()).hexdigest()

        current_time = time.time()

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
                   stations_dict: Dict = None):
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
        """
        # Use provided dict or stored reference
        if stations_dict is None:
            stations_dict = self._stations_dict

        if stations_dict is None:
            return  # No way to record without stations dict

        if not digipeater_path:
            return  # No digipeaters to record

        # Strip asterisk from callsign (APRS path marker, not part of callsign)
        callsign_upper = callsign.upper().rstrip('*')
        now = datetime.now()

        # Create station if it doesn't exist
        if callsign_upper not in stations_dict:
            stations_dict[callsign_upper] = APRSStation(
                callsign=callsign_upper,
                first_heard=now,
                last_heard=now,
                packets_heard=0,
            )

        # Update last_heard timestamp (don't increment packet count for duplicates)
        stations_dict[callsign_upper].last_heard = now

        # Store complete digipeater path (legacy single path field)
        stations_dict[callsign_upper].digipeater_path = [d.upper() for d in digipeater_path]

        # Store in all-paths list (new)
        if digipeater_path:
            normalized_path = [d.upper() for d in digipeater_path]
            if normalized_path not in stations_dict[callsign_upper].digipeater_paths:
                stations_dict[callsign_upper].digipeater_paths.append(normalized_path)
        else:
            # Heard direct (no digipeaters) - add "DIRECT" to paths
            stations_dict[callsign_upper].heard_zero_hop = True
            if ["DIRECT"] not in stations_dict[callsign_upper].digipeater_paths:
                stations_dict[callsign_upper].digipeater_paths.append(["DIRECT"])

        # Mark all stations in the digipeater path as digipeaters
        # Only mark stations we've actually heard (don't create phantom entries)
        for digi_call in digipeater_path:
            digi_upper = digi_call.upper().rstrip('*')
            if digi_upper and digi_upper in stations_dict:
                if not stations_dict[digi_upper].is_digipeater:
                    stations_dict[digi_upper].is_digipeater = True

        # Track only FIRST digipeater for coverage mapping
        # (the one that heard the station directly over RF)
        first_digi = digipeater_path[0].upper().rstrip('*')
        if first_digi and first_digi not in stations_dict[callsign_upper].digipeaters_heard_by:
            stations_dict[callsign_upper].digipeaters_heard_by.append(first_digi)
