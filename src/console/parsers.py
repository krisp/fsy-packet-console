"""APRS frame parsing utilities."""

from typing import List

from src import constants
from src.frame_analyzer import (
    decode_control_byte,
    decode_aprs_info,
)
from src.protocol import (
    kiss_unwrap,
    parse_ax25_addresses_and_control,
)
from src.utils import print_debug


def decode_control_field(control):
    """
    Wrapper for decode_control_byte from frame_analyzer.
    Adapts the return value to match console.py's expected structure.
    """
    result = decode_control_byte(control)
    # Adapt frame_analyzer structure to console structure
    # frame_analyzer uses 'frame_type' and 'description', console expects 'type' and 'desc'
    result['type'] = result['frame_type']
    result['desc'] = result['description']
    return result


def decode_aprs_packet_type(info_str, dest_addr=None):
    """
    Wrapper for decode_aprs_info from frame_analyzer.
    Adapts the return value from nested structure to flat structure expected by console.

    frame_analyzer returns: {'type': 'X', 'details': {fields...}}
    console expects: {'type': 'X', fields...}
    """
    result = decode_aprs_info(info_str, dest_addr)

    # Flatten the structure by merging details into top level
    if 'details' in result:
        details = result.pop('details')
        result.update(details)

    return result


def calculate_hop_count(addresses: List[str]) -> int:
    """Calculate hop count from AX.25 address path.

    Counts the number of digipeaters that have been used (marked with asterisk).
    In AX.25, digipeaters beyond the destination and source (addresses[2:])
    represent the path. Each used digipeater adds 1 hop.

    Args:
        addresses: List of callsigns from AX.25 header (includes destination, source, digipeaters)

    Returns:
        Number of hops (0 = direct RF, higher = more digipeaters used)
    """
    # Direct RF if no digipe path or only dst/src
    if len(addresses) <= 2:
        return 0

    # Count USED digipeaters in path (those marked with asterisk)
    # addresses[2:] contains the digipeater path
    # Only count digipeaters with * (the "used" bit set in AX.25)
    used_count = sum(1 for digi in addresses[2:] if digi.endswith('*'))
    return used_count


def parse_and_track_aprs_frame(complete_frame, radio, timestamp=None, frame_number=None):
    """Parse APRS frame and update database tracking.

    This function runs in ALL modes (TNC and non-TNC) to ensure
    database tracking happens regardless of display mode.

    Args:
        complete_frame: Complete KISS frame bytes
        radio: RadioController instance
        timestamp: Optional timestamp for the frame (used by migrations to preserve historical timestamps)
        frame_number: Optional frame buffer reference number

    Returns:
        dict with:
            is_aprs: bool - Whether this is an APRS frame
            is_duplicate: bool - Whether this is a duplicate packet
            src_call: str - Source callsign
            dst_call: str - Destination callsign
            info_str: str - Decoded info field
            relay: str - Relay call if third-party packet
            hop_count: int - Number of digipeater hops
            digipeater_path: list - List of digipeater callsigns
            aprs_types: dict - Parsed APRS data:
                mic_e: MICEPosition or None
                object: ObjectPosition or None
                item: ItemPosition or None
                status: StatusReport or None
                telemetry: Telemetry or None
                message: Message or None
                weather: Weather or None
                position: Position or None
    """
    result = {
        'is_aprs': False,
        'is_duplicate': False,
        'src_call': None,
        'dst_call': None,
        'info_str': '',
        'relay': None,
        'hop_count': 999,
        'digipeater_path': [],
        'aprs_types': {
            'mic_e': None,
            'object': None,
            'item': None,
            'status': None,
            'telemetry': None,
            'message': None,
            'weather': None,
            'position': None,
        }
    }

    try:
        # Parse AX.25 frame
        payload = kiss_unwrap(complete_frame)
        addresses, control_byte, offset = parse_ax25_addresses_and_control(payload)

        # Extract PID and info field
        # NOTE: offset points to PID byte (after control byte)
        if offset < len(payload):
            pid = payload[offset]
            info_bytes = payload[offset + 1:]  # Info starts after PID
        else:
            return result  # No PID/info

        # Extract basic frame info
        if len(addresses) >= 2:
            result['src_call'] = addresses[1]
            result['dst_call'] = addresses[0]
            raw_path = addresses[2:] if len(addresses) > 2 else []

            # Filter out Q constructs (APRS-IS server metadata that should never appear in RF)
            # Badly configured iGates sometimes encode these into AX.25 paths when gating from APRS-IS
            # Q constructs are exactly 3 chars: q + two uppercase (qAC, qAO, qAR, qAS, qAX, qAZ, qAU)
            # Reference: http://www.aprs-is.net/q.aspx
            Q_CONSTRUCTS = {'QAC', 'QAO', 'QAR', 'QAS', 'QAX', 'QAZ', 'QAU'}

            # Filter Q constructs first
            filtered_path = [
                digi for digi in raw_path
                if digi.upper().rstrip('*') not in Q_CONSTRUCTS
            ]

            # Filter iGate trace callsigns (appear AFTER unused WIDE/RELAY aliases)
            # Proper path order: used digis (*), then unused aliases (WIDE2-1)
            # Trace callsigns appear AFTER unused aliases (bad iGate behavior)
            # Keep all unused WIDE/RELAY aliases, but truncate at first non-alias after them
            if constants.DEBUG and filtered_path:
                print_debug(f"TRACE: raw_path={raw_path}", level=6)
                print_debug(f"TRACE: filtered_path (after Q-filter)={filtered_path}", level=6)
            final_path = []
            seen_unused_alias = False
            for i, digi in enumerate(filtered_path):
                digi_upper = digi.upper().rstrip('*')
                is_unused_alias = (
                    not digi.endswith('*') and
                    (digi_upper.startswith('WIDE') or digi_upper.startswith('RELAY'))
                )
                if constants.DEBUG and filtered_path:
                    print_debug(f"TRACE: loop i={i} digi={digi} is_unused_alias={is_unused_alias} seen={seen_unused_alias}", level=6)
                if is_unused_alias:
                    # Keep unused aliases (WIDE1-1, WIDE2-1, etc.)
                    final_path.append(digi)
                    seen_unused_alias = True
                    if constants.DEBUG:
                        print_debug(f"TRACE:   -> appended {digi}, final_path={final_path}", level=6)
                elif seen_unused_alias:
                    # Non-alias callsign after unused alias = iGate trace, truncate here
                    if constants.DEBUG:
                        print_debug(f"TRACE:   -> breaking at {digi} (trace after unused alias)", level=6)
                    break
                else:
                    # Used digi (has *) or callsign before any unused alias
                    final_path.append(digi)
                    if constants.DEBUG:
                        print_debug(f"TRACE:   -> appended {digi} (used digi), final_path={final_path}", level=6)

            result['digipeater_path'] = final_path

            # Log when Q constructs or traces are filtered (indicates misbehaving iGate)
            filtered_q = [d for d in raw_path if d.upper().rstrip('*') in Q_CONSTRUCTS]
            filtered_trace = filtered_path[len(final_path):] if len(final_path) < len(filtered_path) else []

            if filtered_q and constants.DEBUG:
                print_debug(
                    f"Filtered Q construct(s) from path: {filtered_q} (bad iGate behavior)",
                    level=2
                )
            if filtered_trace and constants.DEBUG:
                print_debug(
                    f"Filtered iGate trace callsign(s) from path: {filtered_trace} (bad iGate behavior)",
                    level=2
                )

            result['hop_count'] = calculate_hop_count(addresses)
        else:
            return result  # Invalid frame

        # Check if this looks like APRS
        if not info_bytes:
            return result

        first_byte = info_bytes[0]
        first_char = chr(first_byte) if first_byte < 128 else None
        first_two = info_bytes[:2].decode("ascii", errors="ignore") if len(info_bytes) >= 2 else ""

        # APRS marker detection
        aprs_marker = bool(
            first_char and (
                first_char in ("!", "/", "@", ":", "=", "}", "'", "`", ";", ")", ">")
                or first_two.startswith("T#")
                or first_two.startswith("p")
                or first_byte in (0x1C, 0x1D, 0x1E, 0x1F)
            )
        )

        # Only treat as APRS if marker present (reduces false positives)
        if pid == 0xF0 and aprs_marker:
            result['is_aprs'] = True
        elif pid != 0xF0 and aprs_marker:
            result['is_aprs'] = True  # Heuristic match
        else:
            return result  # Not APRS

        # Decode info field
        info_str = info_bytes.decode("ascii", errors="replace")
        result['info_str'] = info_str

        # Check for third-party packet
        third_party = radio.aprs_manager.parse_third_party(result['src_call'], info_str)
        if third_party:
            source_call, relay_call, inner_info = third_party
            parse_call = source_call
            parse_info = inner_info
            result['relay'] = relay_call
            # Third-party packets (igated from APRS-IS) should NEVER count as zero-hop
            # Override hop_count to 999 (unknown/igated) regardless of RF path from iGate
            result['hop_count'] = 999
        else:
            parse_call = result['src_call']
            parse_info = info_str

        # Check for duplicate packet (suppresses digipeater copies)
        # Convert datetime timestamp to unix timestamp for duplicate detection
        timestamp_float = timestamp.timestamp() if timestamp else None
        result['is_duplicate'] = radio.aprs_manager.duplicate_detector.is_duplicate(parse_call, parse_info, timestamp_float)

        # Record digipeater paths even for duplicates (improves coverage accuracy)
        # Pass relay information to correctly mark third-party duplicates
        if result['is_duplicate'] and result['digipeater_path']:
            radio.aprs_manager.duplicate_detector.record_path(parse_call, result['digipeater_path'], timestamp=timestamp_float, frame_number=frame_number, relay_call=result.get('relay'))

        # Parse all APRS types (updates database in aprs_manager)
        # This happens even for duplicates to ensure tracking
        if not result['is_duplicate']:
            # MIC-E
            result['aprs_types']['mic_e'] = radio.aprs_manager.parse_aprs_mice(
                parse_call, result['dst_call'], parse_info, result['relay'],
                result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
            )

            # Object
            if not result['aprs_types']['mic_e']:
                result['aprs_types']['object'] = radio.aprs_manager.parse_aprs_object(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Item
            if not result['aprs_types']['object']:
                result['aprs_types']['item'] = radio.aprs_manager.parse_aprs_item(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Status
            if not result['aprs_types']['item']:
                result['aprs_types']['status'] = radio.aprs_manager.parse_aprs_status(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Telemetry
            if not result['aprs_types']['status']:
                result['aprs_types']['telemetry'] = radio.aprs_manager.parse_aprs_telemetry(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Message
            if not result['aprs_types']['telemetry']:
                result['aprs_types']['message'] = radio.aprs_manager.parse_aprs_message(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )

            # Weather and Position (can coexist)
            if not result['aprs_types']['message']:
                result['aprs_types']['weather'] = radio.aprs_manager.parse_aprs_weather(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'], timestamp=timestamp, frame_number=frame_number
                )
                result['aprs_types']['position'] = radio.aprs_manager.parse_aprs_position(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'],
                    result['dst_call'], timestamp=timestamp, frame_number=frame_number
                )

    except Exception as e:
        if constants.DEBUG:
            print_debug(f"APRS parsing exception: {e}", level=2)

    return result
