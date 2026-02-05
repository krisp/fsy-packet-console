"""Migration #001: Recover zero_hop_packet_count from frame buffer.

Date: 2026-02-05
Version: v0.8.8

Problem:
--------
Old database entries have heard_zero_hop=True but zero_hop_packet_count=0
because packet counting was added later. Some of these are legitimate RF
contacts that should appear in the DX list.

Solution:
---------
Parse entire frame buffer to count actual zero-hop packets and update
the counts for stations that were genuinely heard direct on RF.

Impact:
-------
- Migrates existing stations with stale data
- Does not affect new stations (already tracked correctly)
- Safe to re-run (idempotent)
"""

from typing import Dict, Any
from datetime import datetime, timedelta, timezone

from src.protocol import decode_ax25_address


def _parse_frame_buffer_for_zero_hop(aprs_manager, frame_history, hours=None) -> Dict[str, int]:
    """Parse frame buffer to count zero-hop packets for candidate stations.

    Args:
        aprs_manager: APRSManager instance
        frame_history: FrameHistory object from CommandProcessor
        hours: Look back this many hours, or None to scan entire buffer

    Returns:
        Dict mapping callsign to number of zero-hop packets found
    """
    # Find stations needing migration
    candidates = [s for s in aprs_manager.stations.values()
                 if s.heard_zero_hop and s.zero_hop_packet_count == 0]

    if not candidates:
        return {}

    candidate_calls = {s.callsign for s in candidates}

    # Calculate cutoff time (None means no cutoff - scan all frames)
    cutoff_time = None
    if hours is not None:
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

    migrated = {}

    # Process frames from history
    for frame in frame_history.get_recent():
        # Only check RX frames
        if frame.direction != 'RX':
            continue

        # Check time window if specified
        if cutoff_time is not None and frame.timestamp < cutoff_time:
            continue

        try:
            # Parse KISS frame - strip KISS framing
            data = frame.raw_bytes
            if len(data) < 2:
                continue

            # Skip KISS header (0xC0 0x00)
            if data[0] == 0xC0:
                data = data[1:]
            if len(data) > 0 and data[0] == 0x00:
                data = data[1:]
            if len(data) > 0 and data[-1] == 0xC0:
                data = data[:-1]

            # Un-escape KISS escapes
            unescaped = bytearray()
            i = 0
            while i < len(data):
                if data[i] == 0xDB and i + 1 < len(data):
                    if data[i + 1] == 0xDC:
                        unescaped.append(0xC0)
                        i += 2
                    elif data[i + 1] == 0xDD:
                        unescaped.append(0xDB)
                        i += 2
                    else:
                        unescaped.append(data[i])
                        i += 1
                else:
                    unescaped.append(data[i])
                    i += 1

            data = bytes(unescaped)

            # Decode AX.25 addresses
            if len(data) < 14:  # Need at least dest(7) + src(7)
                continue

            offset = 0

            # Skip destination
            _, offset, is_last = decode_ax25_address(data, offset)

            # Get source callsign
            src, offset, is_last = decode_ax25_address(data, offset)
            if not src:
                continue

            # Extract base callsign (remove SSID marker if present)
            src_call = src.split('-')[0] if '-' in src else src
            src_call = src_call.rstrip('*')  # Remove H-bit marker

            # Count digipeaters to determine hop count
            digi_count = 0
            while not is_last and offset + 7 <= len(data):
                digi, offset, is_last = decode_ax25_address(data, offset)
                if digi:
                    digi_count += 1
                else:
                    break

            # Parse info field to check for third-party packets
            # Skip control byte (1 byte) and PID byte (1 byte)
            info_offset = offset + 2
            if info_offset < len(data):
                info_field = data[info_offset:]
                # Check if this is a third-party packet (starts with '}')
                if len(info_field) > 0 and info_field[0] == ord('}'):
                    # Third-party packets (igated from APRS-IS) should never count as zero-hop
                    continue

            # Check if this is a candidate station
            full_call = None
            for call in candidate_calls:
                if call.startswith(src_call):
                    full_call = call
                    break

            if not full_call:
                continue

            # If zero hops, count it
            if digi_count == 0:
                if full_call not in migrated:
                    migrated[full_call] = 0
                migrated[full_call] += 1

        except Exception:
            # Skip frames that can't be parsed
            continue

    # Update station zero_hop_packet_count
    for callsign, count in migrated.items():
        if callsign in aprs_manager.stations:
            aprs_manager.stations[callsign].zero_hop_packet_count = count

    return migrated


def migrate(aprs_manager, console) -> Dict[str, Any]:
    """Run migration: recover zero-hop packet counts from frame buffer.

    Args:
        aprs_manager: APRSManager instance
        console: CommandProcessor instance with frame_history

    Returns:
        Dict with migration statistics:
        {
            'candidates': int,      # Stations needing migration
            'migrated': int,        # Stations successfully migrated
            'total_packets': int,   # Total zero-hop packets found
            'stations': dict,       # Callsign -> packet count mapping
            'skipped': str          # Reason if skipped (optional)
        }
    """
    # Check if frame history is available
    if not hasattr(console, 'frame_history'):
        return {
            'migrated': 0,
            'skipped': 'Frame history not available'
        }

    # Find candidates (heard_zero_hop=True but zero_hop_packet_count=0)
    candidates = [s for s in aprs_manager.stations.values()
                 if s.heard_zero_hop and s.zero_hop_packet_count == 0]

    if not candidates:
        return {
            'migrated': 0,
            'skipped': 'No stations needing migration'
        }

    # Run migration - use hours=None to scan ENTIRE buffer (all available frames)
    migrated = _parse_frame_buffer_for_zero_hop(
        aprs_manager,
        console.frame_history,
        hours=None  # None = scan all available frames in buffer
    )

    total_packets = sum(migrated.values()) if migrated else 0

    return {
        'candidates': len(candidates),
        'migrated': len(migrated),
        'total_packets': total_packets,
        'stations': migrated if migrated else {}
    }
