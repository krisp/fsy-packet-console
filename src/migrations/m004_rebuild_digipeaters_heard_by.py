"""Migration #004: Rebuild digipeaters_heard_by from ReceptionEvents.

Rebuilds the digipeaters_heard_by field for all stations by analyzing their
complete reception history (ReceptionEvent list). This fixes databases that
were migrated with m003 before the digipeater tracking bug was fixed.

The digipeaters_heard_by field tracks which digipeaters heard each station
for the first hop (direct RF contact). This is used for digipeater coverage
visualization in the web UI.

This migration is safe to run multiple times (idempotent).
"""

from typing import Dict, Any


def migrate(aprs_manager, console) -> Dict[str, Any]:
    """Rebuild digipeaters_heard_by from ReceptionEvent history.

    Args:
        aprs_manager: APRSManager instance with station database
        console: Console instance (not used, but required by migration interface)

    Returns:
        Dict with migration statistics
    """
    rebuilt_count = 0
    total_digipeaters_found = 0
    stations_with_digipeaters = 0

    for callsign, station in aprs_manager.stations.items():
        # Clear existing digipeaters_heard_by (rebuild from scratch)
        old_count = len(station.digipeaters_heard_by)
        station.digipeaters_heard_by.clear()

        # Rebuild from receptions
        for reception in station.receptions:
            # Only consider direct RF receptions where the first digipeater has repeated
            # The first digi with asterisk (*) is the one that heard the station directly
            # Multi-hop paths are fine - we just care about the FIRST hop
            if (reception.direct_rf and
                len(reception.digipeater_path) >= 1 and
                reception.digipeater_path[0].endswith('*')):

                # Extract the first digipeater (the one that heard the station directly)
                first_digi = reception.digipeater_path[0].upper().rstrip('*')

                # Add to list if not already present
                if first_digi and first_digi not in station.digipeaters_heard_by:
                    station.digipeaters_heard_by.append(first_digi)

        # Track statistics
        new_count = len(station.digipeaters_heard_by)
        if new_count > 0:
            stations_with_digipeaters += 1
            total_digipeaters_found += new_count

        # Only count as "rebuilt" if we changed something
        if old_count != new_count:
            rebuilt_count += 1

    return {
        'stations_processed': len(aprs_manager.stations),
        'stations_rebuilt': rebuilt_count,
        'stations_with_digipeaters': stations_with_digipeaters,
        'total_digipeaters_found': total_digipeaters_found,
    }


def format_result(result: Dict[str, Any]) -> str:
    """Format migration results for user display.

    Args:
        result: Dictionary returned by migrate()

    Returns:
        Formatted string for console output
    """
    return (
        f"Processed {result['stations_processed']} station(s), "
        f"rebuilt {result['stations_rebuilt']} station(s)\n"
        f"    Found {result['total_digipeaters_found']} digipeater relationship(s) "
        f"across {result['stations_with_digipeaters']} station(s)"
    )
