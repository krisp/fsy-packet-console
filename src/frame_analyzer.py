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


class _FrameRenderer:
    """Encapsulates ANSI/HTML rendering for format_frame_detailed."""

    _ANSI_COLORS = {
        'green': Colors.GREEN, 'cyan': Colors.CYAN, 'blue': Colors.BLUE,
        'yellow': Colors.YELLOW, 'red': Colors.RED, 'magenta': Colors.MAGENTA,
        'gray': Colors.GRAY,
    }

    def __init__(self, output_format: str):
        self.fmt = output_format
        self.lines: list = []
        self._HTML = None
        if output_format != 'ansi':
            from prompt_toolkit import HTML as _H
            self._HTML = _H

    def separator(self) -> None:
        """Add bold separator line."""
        sep = '=' * 80
        if self.fmt == 'ansi':
            self.lines.append(f"{Colors.BOLD}{sep}{Colors.RESET}")
        else:
            self.lines.append(self._HTML(f"<b>{sep}</b>"))

    def heading(self, text: str) -> None:
        """Add cyan bold section heading."""
        if self.fmt == 'ansi':
            self.lines.append(f"\n{Colors.CYAN}{Colors.BOLD}{text}{Colors.RESET}")
        else:
            self.lines.append(self._HTML(f"\n<cyan><b>{text}</b></cyan>"))

    def bold(self, text: str) -> None:
        """Add bold text line."""
        if self.fmt == 'ansi':
            self.lines.append(f"  {Colors.BOLD}{text}{Colors.RESET}")
        else:
            self.lines.append(self._HTML(f"  <b>{text}</b>"))

    def colored(self, text: str, color: str) -> None:
        """Add colored text line."""
        if self.fmt == 'ansi':
            c = self._ANSI_COLORS.get(color, '')
            self.lines.append(f"{c}{text}{Colors.RESET}")
        else:
            self.lines.append(self._HTML(f"<{color}>{sanitize_for_xml(text)}</{color}>"))

    def field(self, label: str, value, color: str = None, indent: int = 2) -> None:
        """Add a labeled field, optionally colored."""
        pad = ' ' * indent
        if self.fmt == 'ansi':
            if color:
                c = self._ANSI_COLORS.get(color, '')
                self.lines.append(f"{pad}{label}: {c}{value}{Colors.RESET}")
            else:
                self.lines.append(f"{pad}{label}: {value}")
        else:
            sv = sanitize_for_xml(str(value))
            if color:
                self.lines.append(self._HTML(f"{pad}{label}: <{color}>{sv}</{color}>"))
            else:
                self.lines.append(self._HTML(f"{pad}{label}: {sv}"))

    def bold_field(self, label: str, value) -> None:
        """Add a bold-label field."""
        if self.fmt == 'ansi':
            self.lines.append(f"  {Colors.BOLD}{label}:{Colors.RESET} {value}")
        else:
            self.lines.append(self._HTML(f"  <b>{label}:</b> {sanitize_for_xml(str(value))}"))

    def text(self, line: str) -> None:
        """Add plain text line."""
        if self.fmt == 'ansi':
            self.lines.append(line)
        else:
            self.lines.append(self._HTML(sanitize_for_xml(line)))

    def blank(self) -> None:
        """Add empty line."""
        self.lines.append("")

    def result(self) -> Union[str, list]:
        """Return lines in appropriate format."""
        if self.fmt == 'ansi':
            return '\n'.join(self.lines)
        return self.lines


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
    r = _FrameRenderer(output_format)
    byte_count = len(decoded['raw']['full_frame']) if 'raw' in decoded and 'full_frame' in decoded['raw'] else 0

    # Header
    dir_color = "green" if direction == "TX" else "cyan"
    r.blank()
    r.separator()
    if output_format == 'ansi':
        dc = Colors.GREEN if direction == "TX" else Colors.CYAN
        r.lines.append(f"{Colors.BOLD}Frame {frame_num} [{dc}{direction}{Colors.RESET}{Colors.BOLD}]: {timestamp} ({byte_count} bytes){Colors.RESET}")
    else:
        r.lines.append(r._HTML(f"<b>Frame {frame_num}: <{dir_color}>{direction}</{dir_color}> {timestamp} ({byte_count} bytes)</b>"))
    r.separator()

    # Error check
    if 'error' in decoded:
        r.colored(f"ERROR: {decoded['error']}", 'red')
        return r.result()

    # KISS Layer
    r.heading("KISS Layer")
    kiss = decoded['kiss']
    r.field("Command", f"0x{kiss['command']:02X} ({kiss['command_type']})")

    # AX.25 Layer
    r.heading("AX.25 Layer")
    _format_ax25_addresses(r, decoded, output_format)
    _format_ax25_control(r, decoded)

    # PID
    if decoded['ax25']['pid']:
        pid = decoded['ax25']['pid']
        r.field("PID", f"0x{pid['value']:02X} ({pid['description']})")

    # Information field
    if decoded['info']:
        r.heading(f"Information Field ({decoded['info']['length']} bytes)")
        r.field("Text", decoded['info']['text'], 'magenta')

    # APRS Layer
    if decoded['aprs']:
        _format_aprs_layer(r, decoded['aprs'], output_format)

    # Hex dumps
    _format_hex_dumps(r, decoded)

    return r.result()


def _format_ax25_addresses(r: _FrameRenderer, decoded: Dict, output_format: str) -> None:
    """Format AX.25 destination, source, and digipeater path."""
    # Destination
    dest = decoded['ax25']['destination']
    if output_format == 'ansi':
        r.field("Destination", dest['full'], 'green')
        r.field("Callsign", dest['callsign'], indent=4)
        r.field("SSID", dest['ssid'], indent=4)
        r.field("Command", dest['command'], indent=4)
    else:
        r.field("Destination", dest['full'], 'green')
        r.lines.append(r._HTML(
            f"    Callsign: {sanitize_for_xml(dest['callsign'])}, "
            f"SSID: {dest['ssid']}, Command: {dest['command']}"
        ))

    # Source
    src = decoded['ax25']['source']
    if output_format == 'ansi':
        r.field("Source", src['full'], 'green')
        r.field("Callsign", src['callsign'], indent=4)
        r.field("SSID", src['ssid'], indent=4)
        r.field("Command", src['command'], indent=4)
    else:
        r.field("Source", src['full'], 'green')
        r.lines.append(r._HTML(
            f"    Callsign: {sanitize_for_xml(src['callsign'])}, "
            f"SSID: {src['ssid']}, Command: {src['command']}"
        ))

    # Digipeater path
    digipeaters = decoded['ax25']['digipeaters']
    if digipeaters:
        hop_s = 's' if len(digipeaters) > 1 else ''
        r.field("Digipeater Path", f"({len(digipeaters)} hop{hop_s})")
        for i, digi in enumerate(digipeaters, 1):
            if output_format == 'ansi':
                mark = f"{Colors.YELLOW}*{Colors.RESET}" if digi['has_been_repeated'] else ""
                r.lines.append(f"    [{i}] {Colors.BLUE}{digi['full']}{mark}{Colors.RESET}")
                r.lines.append(f"        Has been repeated: {digi['has_been_repeated']}")
            else:
                rep = "<yellow>*</yellow>" if digi['has_been_repeated'] else ""
                r.lines.append(r._HTML(
                    f"    [{i}] <blue>{sanitize_for_xml(digi['full'])}{rep}</blue>"
                    f" (repeated: {digi['has_been_repeated']})"
                ))
    else:
        r.field("Digipeater Path", "(none)")


def _format_ax25_control(r: _FrameRenderer, decoded: Dict) -> None:
    """Format AX.25 control byte fields."""
    ctrl = decoded['ax25']['control']
    r.field("Control", f"0x{ctrl['raw']:02X} - {ctrl['description']}", 'yellow')
    if ctrl['ns'] is not None:
        r.field("N(S)", ctrl['ns'], indent=4)
    if ctrl['nr'] is not None:
        r.field("N(R)", ctrl['nr'], indent=4)
    if r.fmt == 'ansi' and ctrl['pf'] is not None:
        r.field("P/F", ctrl['pf'], indent=4)


def _format_aprs_layer(r: _FrameRenderer, aprs: Dict, output_format: str) -> None:
    """Format APRS protocol layer details."""
    details = aprs.get('details', {})
    r.heading("APRS Layer")
    r.field("Packet Type", aprs['type'], 'green')

    # ACK/REJ indicators
    for flag, label in [('is_ack', 'ACK'), ('is_rej', 'REJ')]:
        if details.get(flag):
            if output_format == 'ansi':
                r.lines.append(f"  {Colors.RED}>>> {label} MESSAGE <<<{Colors.RESET}")
            else:
                r.lines.append(r._HTML(
                    f"  <red><b>&gt;&gt;&gt; {label} MESSAGE &lt;&lt;&lt;</b></red>"
                ))

    # MIC-E specific fields
    if aprs['type'] == 'APRS MIC-E Position' and 'dest_encoded' in details:
        r.bold("MIC-E Encoded:")
        r.field("Destination", f"{details['dest_encoded']} (encodes lat/msg)", 'yellow', indent=4)
        if 'message_type' in details:
            r.field("Message Type", details['message_type'], 'green', indent=4)

    # Position data
    if 'latitude' in details and 'longitude' in details:
        _format_position(r, details)

    # Weather data
    if details.get('has_weather') or aprs['type'] == 'APRS Weather':
        _format_weather(r, details)

    # Message data
    for key, label in [('to', 'Message To'), ('message', 'Message Text'), ('message_id', 'Message ID')]:
        if key in details and details[key]:
            r.bold_field(label, details[key])

    # Status
    if 'status' in details:
        r.bold_field("Status", details['status'])

    # Remaining APRS fields
    skip_keys = {
        'is_ack', 'is_rej', 'latitude', 'longitude', 'symbol', 'comment',
        'has_weather', 'temperature', 'wind_dir', 'wind_speed', 'wind_gust',
        'humidity', 'pressure', 'rain_1h', 'rain_24h', 'to', 'message',
        'message_id', 'status', 'speed', 'course', 'message_type',
        'dest_encoded', 'format', 'decode_error'
    }
    for key, value in details.items():
        if key not in skip_keys and value is not None:
            r.field(key.capitalize(), value)


def _format_position(r: _FrameRenderer, details: Dict) -> None:
    """Format APRS position fields."""
    r.bold("Position:")
    r.field("Latitude", f"{details['latitude']:.6f}°", indent=4)
    r.field("Longitude", f"{details['longitude']:.6f}°", indent=4)

    try:
        from src.aprs.geo_utils import latlon_to_maidenhead
        grid = latlon_to_maidenhead(details['latitude'], details['longitude'])
        r.field("Grid Square", grid, 'cyan', indent=4)
    except Exception:
        pass

    if 'symbol' in details:
        r.field("Symbol", details['symbol'], indent=4)
    if 'speed' in details:
        r.field("Speed", f"{details['speed']} mph", 'green', indent=4)
    if 'course' in details:
        r.field("Course", f"{details['course']}°", 'green', indent=4)

    if 'comment' in details and details['comment']:
        comment = details['comment']
        if details.get('has_weather'):
            comment = re.sub(r'[cstgrhpPb]\d{2,5}', '', comment).strip()
        if comment:
            r.field("Comment", comment, indent=4)


def _format_weather(r: _FrameRenderer, details: Dict) -> None:
    """Format APRS weather data fields."""
    wx_fields = [
        ('temperature', 'Temperature', '°F'),
        ('wind_speed', 'Wind', None),
        ('wind_gust', 'Gust', ' mph'),
        ('humidity', 'Humidity', '%'),
        ('pressure', 'Pressure', None),
        ('rain_1h', 'Rain (1h)', None),
        ('rain_24h', 'Rain (24h)', None),
    ]
    wx_lines = []
    for key, label, suffix in wx_fields:
        if key not in details:
            continue
        val = details[key]
        if key == 'wind_speed':
            wind_dir = details.get('wind_dir', 0)
            wx_lines.append(f"    Wind: {wind_dir}° @ {val} mph")
        elif key == 'pressure':
            wx_lines.append(f"    {label}: {val:.1f} mbar")
        elif key in ('rain_1h', 'rain_24h'):
            wx_lines.append(f"    {label}: {val:.2f} in")
        else:
            wx_lines.append(f"    {label}: {val}{suffix}")

    if wx_lines:
        r.bold("Weather Data:")
        for line in wx_lines:
            r.text(line)


def _format_hex_dumps(r: _FrameRenderer, decoded: Dict) -> None:
    """Format hex dump sections."""
    if 'raw' in decoded and 'ax25_payload' in decoded['raw']:
        r.heading("Hex Dump (AX.25 Payload)")
        for line in hex_dump(decoded['raw']['ax25_payload']):
            r.text(line)

    if 'raw' in decoded and 'full_frame' in decoded['raw']:
        hex_str = decoded['raw']['full_frame'].hex()
        r.colored(f"\nFull KISS Frame (hex): {hex_str}", 'gray')
