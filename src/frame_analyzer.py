"""
Shared AX.25/KISS/APRS Frame Analysis Module

This module provides comprehensive frame decoding and formatting capabilities
shared between the standalone analyze_frames.py script and the console.py application.

Features:
- AX.25 address decoding with SSID support
- KISS frame unwrapping
- Control byte decoding (I/S/U frames)
- APRS packet type identification and detailed parsing
- MIC-E position decoding
- Weather data parsing
- Hex dump formatting
- Wireshark-style frame output (ANSI or HTML)
"""

import html
import re
from typing import Dict, List, Optional, Union


class Colors:
    """ANSI color codes for terminal output"""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    GRAY = '\033[90m'


def sanitize_for_xml(text: str) -> str:
    """
    Sanitize text for XML/HTML display by escaping HTML entities
    and filtering invalid XML characters.

    Valid XML 1.0 characters are:
    - Tab (0x09), LF (0x0A), CR (0x0D)
    - Printable characters (0x20 and above)

    Args:
        text: Input string to sanitize

    Returns:
        Sanitized string safe for XML/HTML parsing
    """
    # First escape HTML entities (&, <, >, etc.)
    escaped = html.escape(text)

    # Then filter out characters that are invalid in XML
    valid_chars = []
    for char in escaped:
        code = ord(char)
        if code == 0x09 or code == 0x0A or code == 0x0D or (code >= 0x20 and code <= 0xD7FF):
            valid_chars.append(char)
        else:
            # Replace invalid chars with '.'
            valid_chars.append('.')

    return ''.join(valid_chars)


def decode_ax25_address(data: bytes, is_dest: bool = False) -> Optional[Dict]:
    """
    Decode AX.25 address field (7 bytes).

    Args:
        data: 7-byte address field
        is_dest: True if this is a destination address

    Returns:
        Dict with callsign, ssid, has_been_repeated, command, last_address, raw
        or None if invalid
    """
    if len(data) < 7:
        return None

    # Decode callsign (6 bytes, shifted right by 1)
    # Filter to printable ASCII only (space to ~)
    callsign = ''.join(
        chr(b >> 1) if 32 <= (b >> 1) <= 126 else '?'
        for b in data[:6]
    ).strip()

    # Decode SSID byte
    ssid_byte = data[6]
    ssid = (ssid_byte >> 1) & 0x0F
    has_been_repeated = bool(ssid_byte & 0x80)  # H-bit
    reserved_bits = (ssid_byte >> 5) & 0x03
    last_address = bool(ssid_byte & 0x01)  # Extension bit

    # Command/Response bits (in reserved field)
    # For destination: C-bit is bit 7, for source: C-bit is bit 6
    if is_dest:
        command_bit = bool(reserved_bits & 0x02)
    else:
        command_bit = bool(reserved_bits & 0x01)

    return {
        'callsign': callsign,
        'ssid': ssid,
        'full': f"{callsign}-{ssid}" if ssid else callsign,
        'has_been_repeated': has_been_repeated,
        'command': command_bit,
        'last_address': last_address,
        'raw': data[:7]
    }


def decode_control_byte(control: int) -> Dict:
    """
    Decode AX.25 control byte.

    Args:
        control: Control byte value

    Returns:
        Dict with frame_type, ns, nr, pf, description, raw
    """
    # Check frame type
    if (control & 0x01) == 0:
        # I-frame (Information)
        frame_type = "I"
        ns = (control >> 1) & 0x07
        nr = (control >> 5) & 0x07
        pf = bool(control & 0x10)
        desc = f"I-frame: N(S)={ns}, N(R)={nr}, P/F={pf}"
        return {
            'frame_type': frame_type,
            'ns': ns,
            'nr': nr,
            'pf': pf,
            'description': desc,
            'raw': control
        }
    elif (control & 0x03) == 0x01:
        # S-frame (Supervisory)
        nr = (control >> 5) & 0x07
        pf = bool(control & 0x10)
        s_type = (control >> 2) & 0x03

        s_types = {
            0: "RR (Receive Ready)",
            1: "RNR (Receive Not Ready)",
            2: "REJ (Reject)",
            3: "SREJ (Selective Reject)"
        }
        frame_type = "S"
        desc = f"S-frame: {s_types.get(s_type, 'Unknown')}, N(R)={nr}, P/F={pf}"
        return {
            'frame_type': frame_type,
            'subtype': s_type,
            'ns': None,
            'nr': nr,
            'pf': pf,
            'description': desc,
            'raw': control
        }
    else:
        # U-frame (Unnumbered)
        frame_type = "U"
        pf = bool(control & 0x10)

        u_types = {
            0x2F: "SABM (Set Async Balanced Mode)",
            0x63: "UA (Unnumbered Ack)",
            0x43: "DISC (Disconnect)",
            0x0F: "DM (Disconnected Mode)",
            0x87: "FRMR (Frame Reject)",
            0x03: "UI (Unnumbered Information)"
        }
        desc = f"U-frame: {u_types.get(control, f'Unknown (0x{control:02X})')}, P/F={pf}"
        return {
            'frame_type': frame_type,
            'ns': None,
            'nr': None,
            'pf': pf,
            'description': desc,
            'raw': control
        }


def decode_aprs_info(info_str: str, dest_addr: Optional[str] = None) -> Dict:
    """
    Decode APRS information field and identify packet type.

    This function combines the comprehensive APRS parsing from console.py
    including MIC-E and weather data decoding.

    Args:
        info_str: APRS information field (text)
        dest_addr: Destination address (needed for MIC-E decoding)

    Returns:
        Dict with type and detailed fields
    """
    if not info_str:
        return {'type': 'Empty', 'details': {}}

    first_char = info_str[0] if len(info_str) > 0 else ''

    # APRS Message
    if first_char == ':':
        if len(info_str) >= 11:
            to_call = info_str[1:10].strip()
            message_text = info_str[11:]

            # Check for message ID
            msg_id = None
            if '{' in message_text:
                text, msg_id = message_text.split('{', 1)
                message_text = text

            # Check for ACK/REJ
            is_ack = message_text.startswith('ack')
            is_rej = message_text.startswith('rej')

            return {
                'type': 'APRS Message',
                'details': {
                    'to': to_call,
                    'message': message_text,
                    'message_id': msg_id,
                    'is_ack': is_ack,
                    'is_rej': is_rej
                }
            }

    # Position Reports
    elif first_char in ('!', '=', '@', '/'):
        result = {'type': 'APRS Position', 'details': {}}
        try:
            # Determine offset based on timestamp presence
            offset = 8 if first_char in ('@', '/') else 1

            if len(info_str) >= offset + 19:
                # Parse lat/lon
                lat_str = info_str[offset:offset+8]
                lon_str = info_str[offset+9:offset+18]
                symbol_table = info_str[offset+8] if offset+8 < len(info_str) else '/'
                symbol_code = info_str[offset+18] if offset+18 < len(info_str) else '>'
                comment = info_str[offset+19:].strip() if len(info_str) > offset+19 else ''

                # Parse latitude (DDMMmmN/S)
                lat_deg = int(lat_str[0:2])
                lat_min = float(lat_str[2:7])
                lat_dir = lat_str[7]
                latitude = lat_deg + (lat_min / 60.0)
                if lat_dir in ('S', 's'):
                    latitude = -latitude

                # Parse longitude (DDDMMmmW/E)
                lon_deg = int(lon_str[0:3])
                lon_min = float(lon_str[3:8])
                lon_dir = lon_str[8]
                longitude = lon_deg + (lon_min / 60.0)
                if lon_dir in ('W', 'w'):
                    longitude = -longitude

                result['details']['latitude'] = latitude
                result['details']['longitude'] = longitude
                result['details']['symbol'] = f"{symbol_table}{symbol_code}"
                result['details']['comment'] = comment

                # Check for weather data in comment
                if re.search(r"[cstgrhpPb]\d{2,3}", comment):
                    result['details']['has_weather'] = True

                    # Parse weather fields
                    match = re.search(r"c(\d{3})s(\d{3})", comment)
                    if match:
                        result['details']['wind_dir'] = int(match.group(1))
                        result['details']['wind_speed'] = int(match.group(2))

                    match = re.search(r"g(\d{3})", comment)
                    if match:
                        result['details']['wind_gust'] = int(match.group(1))

                    match = re.search(r"t(-?\d{3})", comment)
                    if match:
                        temp = int(match.group(1))
                        if temp > 200:
                            temp = temp - 256
                        result['details']['temperature'] = temp

                    match = re.search(r"h(\d{2})", comment)
                    if match:
                        humidity = int(match.group(1))
                        result['details']['humidity'] = 100 if humidity == 0 else humidity

                    match = re.search(r"b(\d{5})", comment)
                    if match:
                        result['details']['pressure'] = int(match.group(1)) / 10.0

                    match = re.search(r"r(\d{3})", comment)
                    if match:
                        result['details']['rain_1h'] = int(match.group(1)) / 100.0

                    match = re.search(r"p(\d{3})", comment)
                    if match:
                        result['details']['rain_24h'] = int(match.group(1)) / 100.0

        except Exception:
            pass
        return result

    # MIC-E (destination address encoding)
    elif first_char == '`' or first_char == "'":
        result = {'type': 'APRS MIC-E Position', 'details': {'format': 'compressed'}}

        # Decode MIC-E if we have destination address
        if dest_addr and len(dest_addr) >= 6 and len(info_str) >= 9:
            try:
                # Remove SSID from dest_addr if present
                dest_call = dest_addr.split("-")[0] if "-" in dest_addr else dest_addr

                # Decode latitude from destination address
                lat_digits = []
                msg_bits = []

                for ch in dest_call[:6]:
                    if "0" <= ch <= "9":
                        lat_digits.append(ch)
                        msg_bits.append(0)
                    elif "A" <= ch <= "J":
                        lat_digits.append(str(ord(ch) - ord("A")))
                        msg_bits.append(1)
                    elif "P" <= ch <= "Y":
                        lat_digits.append(str(ord(ch) - ord("P")))
                        msg_bits.append(1)
                    elif ch in ("K", "L", "Z"):
                        lat_digits.append("0")  # Space = zero
                        msg_bits.append(0 if ch == "L" else 1)

                if len(lat_digits) == 6 and len(msg_bits) >= 6:
                    # Decode latitude
                    lat_str = "".join(lat_digits)
                    lat_deg = int(lat_str[0:2])
                    lat_min = float(lat_str[2:4] + "." + lat_str[4:6])
                    latitude = lat_deg + (lat_min / 60.0)

                    # N/S from message bit 3
                    if msg_bits[3] == 0:
                        latitude = -latitude  # South

                    # Decode longitude from info bytes 1-3
                    lon_deg = ord(info_str[1]) - 28
                    lon_min = ord(info_str[2]) - 28
                    lon_min_frac = ord(info_str[3]) - 28

                    # Longitude offset from message bit 4
                    if msg_bits[4] == 1:
                        lon_deg += 100

                    longitude = lon_deg + ((lon_min + lon_min_frac / 100.0) / 60.0)

                    # E/W from message bit 5
                    if msg_bits[5] == 1:
                        longitude = -longitude  # West

                    # Extract speed and course
                    speed_course = ord(info_str[4]) - 28
                    speed = ((ord(info_str[5]) - 28) * 10) + ((speed_course // 10) % 10)
                    course = ((speed_course % 10) * 100) + (ord(info_str[6]) - 28)

                    # Symbol
                    symbol_code = info_str[7] if len(info_str) > 7 else ">"
                    symbol_table = info_str[8] if len(info_str) > 8 else "/"

                    # Comment (after position data)
                    comment = info_str[9:] if len(info_str) > 9 else ""

                    # Strip type indicator
                    if comment and ord(comment[0]) in (0x20, 0x3E, 0x5D, 0x60, 0x27):
                        comment = comment[1:]

                    # Keep only printable chars
                    comment = "".join(c for c in comment if 0x20 <= ord(c) <= 0x7E)

                    # Decode MIC-E message type from message bits
                    msg_type_bits = (msg_bits[0] << 2) | (msg_bits[1] << 1) | msg_bits[2]
                    msg_types = {
                        0: "Emergency",
                        1: "Priority",
                        2: "Special",
                        3: "Committed",
                        4: "Returning",
                        5: "In Service",
                        6: "En Route",
                        7: "Off Duty"
                    }
                    message_type = msg_types.get(msg_type_bits, f"Unknown ({msg_type_bits})")

                    # Store all decoded fields
                    result['details']['latitude'] = latitude
                    result['details']['longitude'] = longitude
                    result['details']['speed'] = speed
                    result['details']['course'] = course
                    result['details']['symbol'] = f"{symbol_table}{symbol_code}"
                    result['details']['comment'] = comment.strip()
                    result['details']['message_type'] = message_type
                    result['details']['dest_encoded'] = dest_call

            except Exception as e:
                # If decode fails, just show raw data
                result['details']['data'] = info_str[:40] + ('...' if len(info_str) > 40 else '')
                result['details']['decode_error'] = str(e)

        return result

    # Weather
    elif first_char == '_':
        result = {'type': 'APRS Weather', 'details': {}}
        try:
            # Parse weather fields from standalone weather report
            match = re.search(r"_(\d{3})/(\d{3})", info_str)
            if match:
                result['details']['wind_dir'] = int(match.group(1))
                result['details']['wind_speed'] = int(match.group(2))

            match = re.search(r"g(\d{3})", info_str)
            if match:
                result['details']['wind_gust'] = int(match.group(1))

            match = re.search(r"t(-?\d{3})", info_str)
            if match:
                temp = int(match.group(1))
                if temp > 200:
                    temp = temp - 256
                result['details']['temperature'] = temp

            match = re.search(r"h(\d{2})", info_str)
            if match:
                humidity = int(match.group(1))
                result['details']['humidity'] = 100 if humidity == 0 else humidity

            match = re.search(r"b(\d{5})", info_str)
            if match:
                result['details']['pressure'] = int(match.group(1)) / 10.0

            match = re.search(r"r(\d{3})", info_str)
            if match:
                result['details']['rain_1h'] = int(match.group(1)) / 100.0

            match = re.search(r"p(\d{3})", info_str)
            if match:
                result['details']['rain_24h'] = int(match.group(1)) / 100.0

            result['details']['weather'] = info_str[:50] + ('...' if len(info_str) > 50 else '')
        except Exception:
            pass
        return result

    # Telemetry
    elif info_str.startswith('T#'):
        return {
            'type': 'APRS Telemetry',
            'details': {'telemetry': info_str}
        }

    # Status
    elif first_char == '>':
        return {
            'type': 'APRS Status',
            'details': {'status': info_str[1:]}
        }

    # Object
    elif first_char == ';':
        return {
            'type': 'APRS Object',
            'details': {'object': info_str[:40] + ('...' if len(info_str) > 40 else '')}
        }

    # Item
    elif first_char == ')':
        return {
            'type': 'APRS Item',
            'details': {'item': info_str[:40] + ('...' if len(info_str) > 40 else '')}
        }

    # Third-party
    elif first_char == '}':
        return {
            'type': 'APRS Third-Party',
            'details': {'third_party': info_str[:50] + ('...' if len(info_str) > 50 else '')}
        }

    else:
        return {
            'type': 'Unknown APRS',
            'details': {'data': info_str[:50] + ('...' if len(info_str) > 50 else '')}
        }


def decode_kiss_frame(frame_data: Union[str, bytes]) -> Dict:
    """
    Decode a complete KISS frame.

    Args:
        frame_data: Either hex string or bytes

    Returns:
        Dict with all decoded fields or error
    """
    # Convert to bytes if needed
    if isinstance(frame_data, str):
        try:
            frame = bytes.fromhex(frame_data)
        except ValueError as e:
            return {'error': f'Invalid hex: {e}'}
    else:
        frame = frame_data

    if len(frame) < 3:
        return {'error': 'Frame too short'}

    # KISS framing
    if frame[0] != 0xC0 or frame[-1] != 0xC0:
        return {'error': 'Invalid KISS framing (missing 0xC0 delimiters)'}

    kiss_command = frame[1]
    ax25_payload = frame[2:-1]  # Remove KISS framing

    if len(ax25_payload) < 16:  # Minimum: dest(7) + src(7) + ctrl(1) + pid(1)
        return {'error': 'AX.25 payload too short'}

    result = {
        'kiss': {
            'command': kiss_command,
            'command_type': 'Data Frame' if kiss_command == 0 else f'Command {kiss_command}'
        },
        'ax25': {},
        'aprs': None,
        'raw': {
            'full_frame': frame,
            'ax25_payload': ax25_payload
        }
    }

    # Decode destination address
    dest = decode_ax25_address(ax25_payload[0:7], is_dest=True)
    if not dest:
        return {'error': 'Failed to decode destination'}
    result['ax25']['destination'] = dest

    # Decode source address
    src = decode_ax25_address(ax25_payload[7:14], is_dest=False)
    if not src:
        return {'error': 'Failed to decode source'}
    result['ax25']['source'] = src

    # Decode digipeater path
    offset = 14
    digipeaters = []
    while not ax25_payload[offset - 1] & 0x01:  # Check extension bit of previous address
        if offset + 7 > len(ax25_payload):
            return {'error': 'Truncated digipeater field'}

        digi = decode_ax25_address(ax25_payload[offset:offset+7])
        if not digi:
            return {'error': f'Failed to decode digipeater at offset {offset}'}
        digipeaters.append(digi)
        offset += 7

    result['ax25']['digipeaters'] = digipeaters

    # Control byte
    if offset >= len(ax25_payload):
        return {'error': 'Missing control byte'}

    control = decode_control_byte(ax25_payload[offset])
    result['ax25']['control'] = control
    offset += 1

    # PID (only for I-frames and UI frames)
    if control['frame_type'] in ('I', 'U') and offset < len(ax25_payload):
        pid = ax25_payload[offset]
        result['ax25']['pid'] = {
            'value': pid,
            'description': 'No layer 3' if pid == 0xF0 else f'Protocol 0x{pid:02X}'
        }
        offset += 1
    else:
        result['ax25']['pid'] = None

    # Information field
    if offset < len(ax25_payload):
        info_bytes = ax25_payload[offset:]
        info_str = info_bytes.decode('ascii', errors='replace').rstrip('\r\n\x00')

        result['info'] = {
            'bytes': info_bytes,
            'text': info_str,
            'length': len(info_bytes)
        }

        # Try to decode as APRS (use destination callsign for MIC-E)
        dest_call = dest['callsign'] if dest else None
        result['aprs'] = decode_aprs_info(info_str, dest_addr=dest_call)
    else:
        result['info'] = None

    return result


def hex_dump(data: bytes, bytes_per_line: int = 16) -> List[str]:
    """
    Create a hex dump with ASCII representation.

    Args:
        data: Bytes to dump
        bytes_per_line: Number of bytes per line

    Returns:
        List of formatted strings
    """
    lines = []
    for i in range(0, len(data), bytes_per_line):
        chunk = data[i:i+bytes_per_line]

        # Offset
        offset = f"{i:04x}"

        # Hex bytes
        hex_part = ' '.join(f"{b:02x}" for b in chunk)
        hex_part = hex_part.ljust(bytes_per_line * 3 - 1)

        # ASCII representation
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)

        lines.append(f"  {offset}  {hex_part}  {ascii_part}")

    return lines


def format_frame_detailed(
    decoded: Dict,
    frame_num: int,
    timestamp: str,
    direction: str = 'RX',
    output_format: str = 'ansi'
) -> Union[str, List]:
    """
    Format decoded frame in Wireshark-style output.

    Args:
        decoded: Decoded frame data from decode_kiss_frame()
        frame_num: Frame number (for header)
        timestamp: Timestamp string (e.g., "12:34:56.789")
        direction: Direction indicator ("RX" or "TX")
        output_format: 'ansi' for ANSI color codes, 'html' for prompt_toolkit HTML

    Returns:
        If output_format='ansi': Single string with ANSI codes
        If output_format='html': List of HTML() objects (for prompt_toolkit)
    """
    lines = []

    # Calculate byte count
    byte_count = len(decoded['raw']['full_frame']) if 'raw' in decoded and 'full_frame' in decoded['raw'] else 0

    # Format header
    if output_format == 'ansi':
        direction_color = Colors.GREEN if direction == "TX" else Colors.CYAN
        lines.append("")
        lines.append(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
        lines.append(f"{Colors.BOLD}Frame {frame_num} [{direction_color}{direction}{Colors.RESET}{Colors.BOLD}]: {timestamp} ({byte_count} bytes){Colors.RESET}")
        lines.append(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
    else:  # html
        from prompt_toolkit import HTML
        direction_color = "green" if direction == "TX" else "cyan"
        lines.append(HTML(f"<b>{'=' * 80}</b>"))
        lines.append(HTML(f"<b>Frame {frame_num}: <{direction_color}>{direction}</{direction_color}> {timestamp} ({byte_count} bytes)</b>"))
        lines.append(HTML(f"<b>{'=' * 80}</b>"))

    # Check for errors
    if 'error' in decoded:
        if output_format == 'ansi':
            lines.append(f"{Colors.RED}ERROR: {decoded['error']}{Colors.RESET}")
            return '\n'.join(lines)
        else:
            from prompt_toolkit import HTML
            lines.append(HTML(f"<red>ERROR: {decoded['error']}</red>"))
            return lines

    # KISS Layer
    if output_format == 'ansi':
        lines.append(f"\n{Colors.CYAN}{Colors.BOLD}KISS Layer{Colors.RESET}")
        lines.append(f"  Command: 0x{decoded['kiss']['command']:02X} ({decoded['kiss']['command_type']})")
    else:
        from prompt_toolkit import HTML
        lines.append(HTML(f"\n<cyan><b>KISS Layer</b></cyan>"))
        lines.append(HTML(f"  Command: 0x{decoded['kiss']['command']:02X} ({decoded['kiss']['command_type']})"))

    # AX.25 Layer
    if output_format == 'ansi':
        lines.append(f"\n{Colors.CYAN}{Colors.BOLD}AX.25 Layer{Colors.RESET}")
    else:
        from prompt_toolkit import HTML
        lines.append(HTML(f"\n<cyan><b>AX.25 Layer</b></cyan>"))

    # Destination
    dest = decoded['ax25']['destination']
    if output_format == 'ansi':
        lines.append(f"  Destination: {Colors.GREEN}{dest['full']}{Colors.RESET}")
        lines.append(f"    Callsign: {dest['callsign']}")
        lines.append(f"    SSID: {dest['ssid']}")
        lines.append(f"    Command: {dest['command']}")
    else:
        from prompt_toolkit import HTML
        lines.append(HTML(f"  Destination: <green><b>{sanitize_for_xml(dest['full'])}</b></green>"))
        lines.append(HTML(f"    Callsign: {sanitize_for_xml(dest['callsign'])}, SSID: {dest['ssid']}, Command: {dest['command']}"))

    # Source
    src = decoded['ax25']['source']
    if output_format == 'ansi':
        lines.append(f"  Source: {Colors.GREEN}{src['full']}{Colors.RESET}")
        lines.append(f"    Callsign: {src['callsign']}")
        lines.append(f"    SSID: {src['ssid']}")
        lines.append(f"    Command: {src['command']}")
    else:
        from prompt_toolkit import HTML
        lines.append(HTML(f"  Source: <green><b>{sanitize_for_xml(src['full'])}</b></green>"))
        lines.append(HTML(f"    Callsign: {sanitize_for_xml(src['callsign'])}, SSID: {src['ssid']}, Command: {src['command']}"))

    # Digipeater path
    digipeaters = decoded['ax25']['digipeaters']
    if digipeaters:
        if output_format == 'ansi':
            lines.append(f"  Digipeater Path: ({len(digipeaters)} hop{'s' if len(digipeaters) > 1 else ''})")
            for i, digi in enumerate(digipeaters, 1):
                repeated_mark = f"{Colors.YELLOW}*{Colors.RESET}" if digi['has_been_repeated'] else ""
                lines.append(f"    [{i}] {Colors.BLUE}{digi['full']}{repeated_mark}{Colors.RESET}")
                lines.append(f"        Has been repeated: {digi['has_been_repeated']}")
        else:
            from prompt_toolkit import HTML
            lines.append(HTML(f"  Digipeater Path: ({len(digipeaters)} hop{'s' if len(digipeaters) > 1 else ''})"))
            for i, digi in enumerate(digipeaters, 1):
                repeated = "<yellow>*</yellow>" if digi['has_been_repeated'] else ""
                lines.append(HTML(f"    [{i}] <blue>{sanitize_for_xml(digi['full'])}{repeated}</blue> (repeated: {digi['has_been_repeated']})"))
    else:
        if output_format == 'ansi':
            lines.append(f"  Digipeater Path: (none)")
        else:
            from prompt_toolkit import HTML
            lines.append(HTML(f"  Digipeater Path: (none)"))

    # Control
    ctrl = decoded['ax25']['control']
    if output_format == 'ansi':
        lines.append(f"  Control: 0x{ctrl['raw']:02X} - {Colors.YELLOW}{ctrl['description']}{Colors.RESET}")
        if ctrl['ns'] is not None:
            lines.append(f"    N(S): {ctrl['ns']}")
        if ctrl['nr'] is not None:
            lines.append(f"    N(R): {ctrl['nr']}")
        if ctrl['pf'] is not None:
            lines.append(f"    P/F: {ctrl['pf']}")
    else:
        from prompt_toolkit import HTML
        lines.append(HTML(f"  Control: 0x{ctrl['raw']:02X} - <yellow>{ctrl['description']}</yellow>"))
        if ctrl['ns'] is not None:
            lines.append(HTML(f"    N(S): {ctrl['ns']}"))
        if ctrl['nr'] is not None:
            lines.append(HTML(f"    N(R): {ctrl['nr']}"))

    # PID
    if decoded['ax25']['pid']:
        pid = decoded['ax25']['pid']
        if output_format == 'ansi':
            lines.append(f"  PID: 0x{pid['value']:02X} ({pid['description']})")
        else:
            from prompt_toolkit import HTML
            lines.append(HTML(f"  PID: 0x{pid['value']:02X} ({pid['description']})"))

    # Information field
    if decoded['info']:
        if output_format == 'ansi':
            lines.append(f"\n{Colors.CYAN}{Colors.BOLD}Information Field ({decoded['info']['length']} bytes){Colors.RESET}")
            lines.append(f"  Text: {Colors.MAGENTA}{decoded['info']['text']}{Colors.RESET}")
        else:
            from prompt_toolkit import HTML
            lines.append(HTML(f"\n<cyan><b>Information Field ({decoded['info']['length']} bytes)</b></cyan>"))
            lines.append(HTML(f"  <magenta>{sanitize_for_xml(decoded['info']['text'])}</magenta>"))

    # APRS Layer
    if decoded['aprs']:
        aprs = decoded['aprs']
        details = aprs.get('details', {})

        if output_format == 'ansi':
            lines.append(f"\n{Colors.CYAN}{Colors.BOLD}APRS Layer{Colors.RESET}")
            lines.append(f"  Packet Type: {Colors.GREEN}{aprs['type']}{Colors.RESET}")
        else:
            from prompt_toolkit import HTML
            lines.append(HTML(f"\n<cyan><b>APRS Layer</b></cyan>"))
            lines.append(HTML(f"  Packet Type: <green><b>{sanitize_for_xml(aprs['type'])}</b></green>"))

        # ACK/REJ indicators
        if details.get('is_ack'):
            if output_format == 'ansi':
                lines.append(f"  {Colors.RED}>>> ACK MESSAGE <<<{Colors.RESET}")
            else:
                from prompt_toolkit import HTML
                lines.append(HTML(f"  <red><b>&gt;&gt;&gt; ACK MESSAGE &lt;&lt;&lt;</b></red>"))
        if details.get('is_rej'):
            if output_format == 'ansi':
                lines.append(f"  {Colors.RED}>>> REJ MESSAGE <<<{Colors.RESET}")
            else:
                from prompt_toolkit import HTML
                lines.append(HTML(f"  <red><b>&gt;&gt;&gt; REJ MESSAGE &lt;&lt;&lt;</b></red>"))

        # MIC-E specific fields
        if aprs['type'] == 'APRS MIC-E Position' and 'dest_encoded' in details:
            if output_format == 'ansi':
                lines.append(f"  {Colors.BOLD}MIC-E Encoded:{Colors.RESET}")
                lines.append(f"    Destination: {Colors.YELLOW}{details['dest_encoded']}{Colors.RESET} (encodes lat/msg)")
            else:
                from prompt_toolkit import HTML
                lines.append(HTML(f"  <b>MIC-E Encoded:</b>"))
                lines.append(HTML(f"    Destination: <yellow>{sanitize_for_xml(details['dest_encoded'])}</yellow> (encodes lat/msg)"))

            if 'message_type' in details:
                if output_format == 'ansi':
                    lines.append(f"    Message Type: {Colors.GREEN}{details['message_type']}{Colors.RESET}")
                else:
                    from prompt_toolkit import HTML
                    lines.append(HTML(f"    Message Type: <green>{sanitize_for_xml(details['message_type'])}</green>"))

        # Position data
        if 'latitude' in details and 'longitude' in details:
            if output_format == 'ansi':
                lines.append(f"  {Colors.BOLD}Position:{Colors.RESET}")
                lines.append(f"    Latitude:  {details['latitude']:.6f}°")
                lines.append(f"    Longitude: {details['longitude']:.6f}°")
            else:
                from prompt_toolkit import HTML
                lines.append(HTML(f"  <b>Position:</b>"))
                lines.append(HTML(f"    Latitude:  {details['latitude']:.6f}°"))
                lines.append(HTML(f"    Longitude: {details['longitude']:.6f}°"))

            # Grid square calculation (if available)
            try:
                from src.aprs.geo_utils import latlon_to_maidenhead
                grid = latlon_to_maidenhead(details['latitude'], details['longitude'])
                if output_format == 'ansi':
                    lines.append(f"    Grid Square: {Colors.CYAN}{grid}{Colors.RESET}")
                else:
                    from prompt_toolkit import HTML
                    lines.append(HTML(f"    Grid Square: <cyan>{grid}</cyan>"))
            except Exception:
                pass

            if 'symbol' in details:
                if output_format == 'ansi':
                    lines.append(f"    Symbol: {details['symbol']}")
                else:
                    from prompt_toolkit import HTML
                    lines.append(HTML(f"    Symbol: {sanitize_for_xml(details['symbol'])}"))

            # MIC-E speed and course
            if 'speed' in details:
                if output_format == 'ansi':
                    lines.append(f"    Speed: {Colors.GREEN}{details['speed']} mph{Colors.RESET}")
                else:
                    from prompt_toolkit import HTML
                    lines.append(HTML(f"    Speed: <green>{details['speed']} mph</green>"))
            if 'course' in details:
                if output_format == 'ansi':
                    lines.append(f"    Course: {Colors.GREEN}{details['course']}°{Colors.RESET}")
                else:
                    from prompt_toolkit import HTML
                    lines.append(HTML(f"    Course: <green>{details['course']}°</green>"))

            # Comment (without weather data if present)
            if 'comment' in details and details['comment']:
                comment = details['comment']
                if details.get('has_weather'):
                    # Strip weather data for cleaner display
                    comment = re.sub(r'[cstgrhpPb]\d{2,5}', '', comment).strip()
                if comment:
                    if output_format == 'ansi':
                        lines.append(f"    Comment: {comment}")
                    else:
                        from prompt_toolkit import HTML
                        lines.append(HTML(f"    Comment: {sanitize_for_xml(comment)}"))

        # Weather data
        if details.get('has_weather') or aprs['type'] == 'APRS Weather':
            wx_lines = []
            if 'temperature' in details:
                wx_lines.append(f"    Temperature: {details['temperature']}°F")
            if 'wind_speed' in details:
                wind_dir = details.get('wind_dir', 0)
                wx_lines.append(f"    Wind: {wind_dir}° @ {details['wind_speed']} mph")
            if 'wind_gust' in details:
                wx_lines.append(f"    Gust: {details['wind_gust']} mph")
            if 'humidity' in details:
                wx_lines.append(f"    Humidity: {details['humidity']}%")
            if 'pressure' in details:
                wx_lines.append(f"    Pressure: {details['pressure']:.1f} mbar")
            if 'rain_1h' in details:
                wx_lines.append(f"    Rain (1h): {details['rain_1h']:.2f} in")
            if 'rain_24h' in details:
                wx_lines.append(f"    Rain (24h): {details['rain_24h']:.2f} in")

            if wx_lines:
                if output_format == 'ansi':
                    lines.append(f"  {Colors.BOLD}Weather Data:{Colors.RESET}")
                    lines.extend(wx_lines)
                else:
                    from prompt_toolkit import HTML
                    lines.append(HTML(f"  <b>Weather Data:</b>"))
                    for wx_line in wx_lines:
                        lines.append(HTML(wx_line))

        # Message data
        if 'to' in details:
            if output_format == 'ansi':
                lines.append(f"  {Colors.BOLD}Message To:{Colors.RESET} {details['to']}")
            else:
                from prompt_toolkit import HTML
                lines.append(HTML(f"  <b>Message To:</b> {sanitize_for_xml(details['to'])}"))
        if 'message' in details:
            if output_format == 'ansi':
                lines.append(f"  {Colors.BOLD}Message Text:{Colors.RESET} {details['message']}")
            else:
                from prompt_toolkit import HTML
                lines.append(HTML(f"  <b>Message Text:</b> {sanitize_for_xml(details['message'])}"))
        if 'message_id' in details and details['message_id']:
            if output_format == 'ansi':
                lines.append(f"  {Colors.BOLD}Message ID:{Colors.RESET} {details['message_id']}")
            else:
                from prompt_toolkit import HTML
                lines.append(HTML(f"  <b>Message ID:</b> {sanitize_for_xml(details['message_id'])}"))

        # Status
        if 'status' in details:
            if output_format == 'ansi':
                lines.append(f"  {Colors.BOLD}Status:{Colors.RESET} {details['status']}")
            else:
                from prompt_toolkit import HTML
                lines.append(HTML(f"  <b>Status:</b> {sanitize_for_xml(details['status'])}"))

        # Other APRS fields (object, item, telemetry, etc.)
        skip_keys = {
            'is_ack', 'is_rej', 'latitude', 'longitude', 'symbol', 'comment',
            'has_weather', 'temperature', 'wind_dir', 'wind_speed', 'wind_gust',
            'humidity', 'pressure', 'rain_1h', 'rain_24h', 'to', 'message',
            'message_id', 'status', 'speed', 'course', 'message_type',
            'dest_encoded', 'format', 'decode_error'
        }
        for key, value in details.items():
            if key not in skip_keys and value is not None:
                if output_format == 'ansi':
                    lines.append(f"  {key.capitalize()}: {value}")
                else:
                    from prompt_toolkit import HTML
                    lines.append(HTML(f"  {key.capitalize()}: {sanitize_for_xml(str(value))}"))

    # Hex dump
    if 'raw' in decoded and 'ax25_payload' in decoded['raw']:
        if output_format == 'ansi':
            lines.append(f"\n{Colors.CYAN}{Colors.BOLD}Hex Dump (AX.25 Payload){Colors.RESET}")
        else:
            from prompt_toolkit import HTML
            lines.append(HTML(f"\n<cyan><b>Hex Dump (AX.25 Payload)</b></cyan>"))

        hex_lines = hex_dump(decoded['raw']['ax25_payload'])
        if output_format == 'ansi':
            lines.extend(hex_lines)
        else:
            from prompt_toolkit import HTML
            for hex_line in hex_lines:
                lines.append(HTML(sanitize_for_xml(hex_line)))

    # Full frame hex dump
    if 'raw' in decoded and 'full_frame' in decoded['raw']:
        if output_format == 'ansi':
            lines.append(f"\n{Colors.GRAY}Full KISS Frame (hex):{Colors.RESET}")
            lines.append(f"{Colors.GRAY}  {decoded['raw']['full_frame'].hex()}{Colors.RESET}")
        else:
            from prompt_toolkit import HTML
            lines.append(HTML(f"\n<gray>Full KISS Frame (hex): {decoded['raw']['full_frame'].hex()}</gray>"))

    # Return format
    if output_format == 'ansi':
        return '\n'.join(lines)
    else:
        return lines
