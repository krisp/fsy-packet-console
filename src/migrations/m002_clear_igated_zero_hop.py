"""Migration #002: Clear zero-hop flags for igated stations.

Date: 2026-02-05
Version: v0.8.8

Problem:
--------
Buggy code (before fix in commit eced065) incorrectly marked igated stations
as heard_zero_hop=True when the RF path from iGate to us was direct.

This created stations like TA9DMR-10 (Turkey) appearing as zero-hop from US,
which is geographically impossible.

These stations have:
- relay_paths containing iGate callsigns (proves they were igated)
- heard_zero_hop=True (INCORRECT - should be False)
- zero_hop_packet_count > 0 (INCORRECT - should be 0)

Solution:
---------
Clear all zero-hop tracking fields for any station that has relay_paths.
If a station was igated, it can never be zero-hop by definition.

Impact:
-------
- Fixes DX list showing distant igated stations
- Clears incorrect data from buggy code
- Safe to run multiple times (idempotent)
"""

from typing import Dict, Any


def migrate(aprs_manager, console) -> Dict[str, Any]:
    """Run migration: clear zero-hop flags for igated stations.

    Args:
        aprs_manager: APRSManager instance
        console: CommandProcessor instance (unused but required by interface)

    Returns:
        Dict with migration statistics:
        {
            'cleared': int,     # Number of stations cleared
            'stations': list,   # List of cleared callsigns
        }
    """
    cleared_stations = []

    for callsign, station in aprs_manager.stations.items():
        # If station has relay_paths, it was heard via iGate (third-party)
        # These should NEVER be marked as zero-hop
        if station.relay_paths and station.heard_zero_hop:
            # Clear all zero-hop tracking fields
            station.heard_zero_hop = False
            station.zero_hop_packet_count = 0
            station.last_heard_zero_hop = None
            cleared_stations.append(callsign)

    return {
        'cleared': len(cleared_stations),
        'stations': cleared_stations
    }
