"""Migration #005: Rebuild digipeater statistics from ReceptionEvent history.

Date: 2026-02-06
Version: v0.8.9+

Problem:
--------
The digipeater statistics dashboard was just implemented, but existing databases
have no digipeater statistics. However, ReceptionEvent history contains all the
information needed to rebuild digipeater activity - we just need to scan for
packets where MYCALL appears in the digipeater_path.

Solution:
---------
Scan all stations' ReceptionEvent lists and identify packets that were digipeated
by our station (MYCALL in digipeater_path). Reconstruct DigipeaterActivity records
with proper path type classification and populate digipeater_stats.

This migration:
1. Gets MYCALL and MYALIAS from TNC config
2. Scans all stations' receptions lists
3. Identifies packets we digipeated (our callsign in digipeater_path)
4. Extracts path type (WIDE1-1, WIDE2-2, Direct, Other)
5. Creates DigipeaterActivity records (newest first, limit 500)
6. Recomputes aggregates (top_stations, path_usage)

Impact:
-------
✅ Recovers historical digipeater activity from existing data
✅ No data loss - uses existing ReceptionEvent ground truth
✅ Respects 500-activity limit with newest-first ordering
✅ Safe to re-run (idempotent - rebuilds from same data)
✅ Backward compatible - initializes empty stats if no config

Notes:
------
- Requires ReceptionEvent architecture (migration m003+)
- Uses MYCALL and MYALIAS from TNC config to identify our digipeating
- Path type classification follows digipeater.py logic
- Newest events kept (time-sorted descending before limiting to 500)
"""

from typing import Dict, Any, List
from datetime import datetime, timezone

from src.aprs.digipeater_stats import DigipeaterActivity, DigipeaterStats


def _classify_path_type(digipeater_path: List[str], mycall: str, myalias: str) -> str:
    """Classify the path type from the digipeater path.

    Args:
        digipeater_path: List of callsigns in the digipeater path
        mycall: Our station callsign
        myalias: Our station alias (e.g., "WIDE1")

    Returns:
        Path type: "WIDE1-1", "WIDE2-2", "WIDE2-1", "Direct", or "Other"
    """
    if not digipeater_path:
        return "Direct"

    # Find our position in the path
    our_position = -1
    used_alias = None

    for i, hop in enumerate(digipeater_path):
        # Remove asterisk (H-bit marker)
        clean_hop = hop.rstrip('*')

        # Check if this is our callsign or alias
        if clean_hop.upper() == mycall.upper():
            our_position = i
            used_alias = mycall
            break
        elif myalias and clean_hop.upper().startswith(myalias.upper()):
            our_position = i
            used_alias = clean_hop
            break

    if our_position < 0:
        # We're not in the path - shouldn't happen, but handle gracefully
        return "Other"

    # If we used MYCALL directly, it's direct addressing
    if used_alias and used_alias.upper() == mycall.upper():
        return "Direct"

    # Extract the WIDEn-N pattern from what we used
    if used_alias:
        clean = used_alias.upper().rstrip('*')
        if clean.startswith('WIDE1-1') or clean == 'WIDE1':
            return "WIDE1-1"
        elif clean.startswith('WIDE2-2'):
            return "WIDE2-2"
        elif clean.startswith('WIDE2-1'):
            return "WIDE2-1"
        elif clean.startswith('WIDE'):
            # Other WIDE variants
            return clean.split('-')[0] if '-' in clean else clean

    return "Other"


def _scan_receptions_for_digipeater_activity(
    aprs_manager,
    mycall: str,
    myalias: str
) -> List[DigipeaterActivity]:
    """Scan all stations' receptions to find packets we digipeated.

    Args:
        aprs_manager: APRSManager instance
        mycall: Our station callsign
        myalias: Our station alias (e.g., "WIDE1")

    Returns:
        List of DigipeaterActivity objects (unsorted)
    """
    activities = []

    # Scan all stations
    for station in aprs_manager.stations.values():
        # Check each reception event
        for reception in station.receptions:
            # Skip if not RF
            if not reception.direct_rf:
                continue

            # Skip if no digipeater path (direct reception)
            if not reception.digipeater_path:
                continue

            # Check if we're in the digipeater path
            our_call_upper = mycall.upper()
            our_alias_upper = myalias.upper() if myalias else ""

            in_path = False
            for hop in reception.digipeater_path:
                clean_hop = hop.rstrip('*').upper()
                if clean_hop == our_call_upper:
                    in_path = True
                    break
                if our_alias_upper and clean_hop.startswith(our_alias_upper):
                    in_path = True
                    break

            if not in_path:
                continue

            # We digipeated this packet - create activity record
            path_type = _classify_path_type(
                reception.digipeater_path,
                mycall,
                myalias
            )

            activity = DigipeaterActivity(
                timestamp=reception.timestamp,
                station_call=station.callsign,
                path_type=path_type,
                original_path=reception.digipeater_path.copy(),
                frame_number=reception.frame_number
            )
            activities.append(activity)

    return activities


def migrate(aprs_manager, console) -> Dict[str, Any]:
    """Run migration: rebuild digipeater stats from ReceptionEvent history.

    Args:
        aprs_manager: APRSManager instance
        console: CommandProcessor instance (for config access)

    Returns:
        Dict with migration statistics:
        {
            'total_activities': int,     # Total digipeater activities found
            'kept_activities': int,       # Activities kept (after 500 limit)
            'unique_stations': int,       # Unique stations digipeated
            'path_breakdown': dict,       # Path types and counts
            'time_range': dict,           # Earliest and latest timestamps
            'skipped': str                # Reason if skipped (optional)
        }
    """
    # Get TNC config for MYCALL and MYALIAS
    if not hasattr(console, 'radio') or not hasattr(console.radio, 'tnc_config'):
        return {
            'total_activities': 0,
            'skipped': 'TNC config not available'
        }

    tnc_config = console.radio.tnc_config
    mycall = tnc_config.get('mycall', '').strip()
    myalias = tnc_config.get('myalias', '').strip()

    if not mycall:
        return {
            'total_activities': 0,
            'skipped': 'MYCALL not configured'
        }

    # Scan all receptions for our digipeater activity
    activities = _scan_receptions_for_digipeater_activity(
        aprs_manager,
        mycall,
        myalias
    )

    if not activities:
        # Initialize empty stats
        aprs_manager.digipeater_stats = DigipeaterStats(
            session_start=datetime.now(timezone.utc)
        )
        return {
            'total_activities': 0,
            'kept_activities': 0,
            'unique_stations': 0,
            'path_breakdown': {},
            'skipped': 'No digipeater activity found in ReceptionEvents'
        }

    # Sort by timestamp descending (newest first)
    activities.sort(key=lambda a: a.timestamp, reverse=True)

    # Keep only the newest 500 activities
    kept_activities = activities[:500]

    # Find earliest timestamp from kept activities for session_start
    earliest = min(a.timestamp for a in kept_activities)
    latest = max(a.timestamp for a in kept_activities)

    # Create new DigipeaterStats with recovered activities
    aprs_manager.digipeater_stats = DigipeaterStats(
        session_start=earliest,
        packets_digipeated=len(kept_activities),
        activities=kept_activities
    )

    # Recompute aggregates (top_stations, path_usage)
    aprs_manager._recompute_digipeater_aggregates()

    # Gather statistics for report
    unique_stations = len(set(a.station_call for a in kept_activities))
    path_breakdown = {}
    for activity in kept_activities:
        path_breakdown[activity.path_type] = \
            path_breakdown.get(activity.path_type, 0) + 1

    return {
        'total_activities': len(activities),
        'kept_activities': len(kept_activities),
        'unique_stations': unique_stations,
        'path_breakdown': path_breakdown,
        'time_range': {
            'earliest': earliest.isoformat(),
            'latest': latest.isoformat()
        },
        'mycall': mycall,
        'myalias': myalias
    }
