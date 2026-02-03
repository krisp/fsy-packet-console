#!/usr/bin/env python3
"""
KISS/AX.25/APRS Protocol Analyzer
Decodes packet radio frames with Wireshark-style output
"""

import sys
import re
import argparse
import gzip
import json
import base64
import os
from datetime import datetime


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


def decode_ax25_address(data, is_dest=False):
    """
    Decode AX.25 address field (7 bytes)

    Returns:
        dict with callsign, ssid, has_been_repeated, command_response, last_address
    """
    if len(data) < 7:
        return None

    # Decode callsign (6 bytes, shifted right by 1)
    callsign = ''.join(chr(b >> 1) for b in data[:6]).strip()

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


def decode_control_byte(control):
    """
    Decode AX.25 control byte

    Returns:
        dict with frame_type, ns, nr, pf, description
    """
    # Check frame type
    if (control & 0x01) == 0:
        # I-frame (Information)
        frame_type = "I"
        ns = (control >> 1) & 0x07
        nr = (control >> 5) & 0x07
        pf = bool(control & 0x10)
        desc = f"I-frame: N(S)={ns}, N(R)={nr}, P/F={pf}"
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
        nr = None
        ns = None

    return {
        'frame_type': frame_type,
        'ns': ns if frame_type == 'I' else None,
        'nr': nr if frame_type in ('I', 'S') else None,
        'pf': pf if frame_type in ('I', 'S', 'U') else None,
        'description': desc,
        'raw': control
    }


def decode_aprs_info(info_str):
    """
    Decode APRS information field

    Returns:
        dict with packet_type, details
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
        return {
            'type': 'APRS Position',
            'details': {
                'position': info_str[:40] + '...' if len(info_str) > 40 else info_str
            }
        }

    # MIC-E (destination address encoding)
    elif first_char == '`' or first_char == "'":
        return {
            'type': 'APRS MIC-E Position',
            'details': {
                'data': info_str[:40] + '...' if len(info_str) > 40 else info_str
            }
        }

    # Weather
    elif first_char == '_':
        return {
            'type': 'APRS Weather',
            'details': {
                'weather': info_str[:50] + '...' if len(info_str) > 50 else info_str
            }
        }

    # Telemetry
    elif info_str.startswith('T#'):
        return {
            'type': 'APRS Telemetry',
            'details': {
                'telemetry': info_str
            }
        }

    # Status
    elif first_char == '>':
        return {
            'type': 'APRS Status',
            'details': {
                'status': info_str[1:]
            }
        }

    # Object
    elif first_char == ';':
        return {
            'type': 'APRS Object',
            'details': {
                'object': info_str[:40] + '...' if len(info_str) > 40 else info_str
            }
        }

    # Item
    elif first_char == ')':
        return {
            'type': 'APRS Item',
            'details': {
                'item': info_str[:40] + '...' if len(info_str) > 40 else info_str
            }
        }

    # Third-party
    elif first_char == '}':
        return {
            'type': 'APRS Third-Party',
            'details': {
                'third_party': info_str[:50] + '...' if len(info_str) > 50 else info_str
            }
        }

    else:
        return {
            'type': 'Unknown APRS',
            'details': {
                'data': info_str[:50] + '...' if len(info_str) > 50 else info_str
            }
        }


def hex_dump(data, bytes_per_line=16):
    """
    Create a hex dump with ASCII representation

    Returns:
        list of formatted strings
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


def decode_kiss_frame(frame_hex):
    """
    Decode a complete KISS frame

    Returns:
        dict with all decoded fields
    """
    try:
        frame = bytes.fromhex(frame_hex)
    except ValueError as e:
        return {'error': f'Invalid hex: {e}'}

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

        # Try to decode as APRS
        result['aprs'] = decode_aprs_info(info_str)
    else:
        result['info'] = None

    return result


def format_frame_output(frame_num, header_text, byte_count, decoded):
    """
    Format decoded frame in Wireshark-style output

    Args:
        frame_num: Frame number (for compatibility)
        header_text: Custom header text (e.g., "Frame 42 [RX]: 12:34:56.789 (100 bytes)")
        byte_count: Byte count string
        decoded: Decoded frame data
    """
    lines = []

    # Header
    lines.append("")
    lines.append(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
    lines.append(f"{Colors.BOLD}{header_text}{Colors.RESET}")
    lines.append(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")

    # Check for errors
    if 'error' in decoded:
        lines.append(f"{Colors.RED}ERROR: {decoded['error']}{Colors.RESET}")
        return '\n'.join(lines)

    # KISS Layer
    lines.append(f"\n{Colors.CYAN}{Colors.BOLD}KISS Layer{Colors.RESET}")
    lines.append(f"  Command: 0x{decoded['kiss']['command']:02X} ({decoded['kiss']['command_type']})")

    # AX.25 Layer
    lines.append(f"\n{Colors.CYAN}{Colors.BOLD}AX.25 Layer{Colors.RESET}")

    # Destination
    dest = decoded['ax25']['destination']
    lines.append(f"  Destination: {Colors.GREEN}{dest['full']}{Colors.RESET}")
    lines.append(f"    Callsign: {dest['callsign']}")
    lines.append(f"    SSID: {dest['ssid']}")
    lines.append(f"    Command: {dest['command']}")

    # Source
    src = decoded['ax25']['source']
    lines.append(f"  Source: {Colors.GREEN}{src['full']}{Colors.RESET}")
    lines.append(f"    Callsign: {src['callsign']}")
    lines.append(f"    SSID: {src['ssid']}")
    lines.append(f"    Command: {src['command']}")

    # Digipeater path
    if decoded['ax25']['digipeaters']:
        lines.append(f"  Digipeater Path: ({len(decoded['ax25']['digipeaters'])} hop{'s' if len(decoded['ax25']['digipeaters']) > 1 else ''})")
        for i, digi in enumerate(decoded['ax25']['digipeaters'], 1):
            repeated_mark = f"{Colors.YELLOW}*{Colors.RESET}" if digi['has_been_repeated'] else ""
            lines.append(f"    [{i}] {Colors.BLUE}{digi['full']}{repeated_mark}{Colors.RESET}")
            lines.append(f"        Has been repeated: {digi['has_been_repeated']}")
    else:
        lines.append(f"  Digipeater Path: (none)")

    # Control
    ctrl = decoded['ax25']['control']
    lines.append(f"  Control: 0x{ctrl['raw']:02X} - {Colors.YELLOW}{ctrl['description']}{Colors.RESET}")
    if ctrl['ns'] is not None:
        lines.append(f"    N(S): {ctrl['ns']}")
    if ctrl['nr'] is not None:
        lines.append(f"    N(R): {ctrl['nr']}")
    if ctrl['pf'] is not None:
        lines.append(f"    P/F: {ctrl['pf']}")

    # PID
    if decoded['ax25']['pid']:
        pid = decoded['ax25']['pid']
        lines.append(f"  PID: 0x{pid['value']:02X} ({pid['description']})")

    # Information field
    if decoded['info']:
        lines.append(f"\n{Colors.CYAN}{Colors.BOLD}Information Field ({decoded['info']['length']} bytes){Colors.RESET}")
        lines.append(f"  Text: {Colors.MAGENTA}{decoded['info']['text']}{Colors.RESET}")

    # APRS Layer
    if decoded['aprs']:
        lines.append(f"\n{Colors.CYAN}{Colors.BOLD}APRS Layer{Colors.RESET}")
        lines.append(f"  Packet Type: {Colors.GREEN}{decoded['aprs']['type']}{Colors.RESET}")

        for key, value in decoded['aprs']['details'].items():
            if value is not None:
                if key == 'is_ack' and value:
                    lines.append(f"  {Colors.RED}>>> ACK MESSAGE <<<{Colors.RESET}")
                elif key == 'is_rej' and value:
                    lines.append(f"  {Colors.RED}>>> REJ MESSAGE <<<{Colors.RESET}")
                else:
                    lines.append(f"  {key.capitalize()}: {value}")

    # Hex dump
    lines.append(f"\n{Colors.CYAN}{Colors.BOLD}Hex Dump (AX.25 Payload){Colors.RESET}")
    hex_lines = hex_dump(decoded['raw']['ax25_payload'])
    lines.extend(hex_lines)

    # Full frame hex dump
    lines.append(f"\n{Colors.GRAY}Full KISS Frame (hex):{Colors.RESET}")
    lines.append(f"{Colors.GRAY}  {decoded['raw']['full_frame'].hex()}{Colors.RESET}")

    return '\n'.join(lines)


def parse_input_line(line):
    """
    Parse input line in multiple formats:
    Format 1: [1] RX 11:49:44.979 (42b): c00092884040...
    Format 2: [DEBUG 12:31:24.651] TNC RX (54 bytes): c00092884040...

    Returns:
        tuple of (frame_num, timestamp, byte_count, frame_hex) or None
    """
    line = line.strip()

    # Pattern 1: [N] RX HH:MM:SS.mmm (XXb): hexhexhex...
    pattern1 = r'\[(\d+)\]\s+RX\s+([\d:\.]+)\s+\((\d+)b\):\s+([0-9a-fA-F]+)'
    match = re.match(pattern1, line)
    if match:
        frame_num = match.group(1)
        timestamp = match.group(2)
        byte_count = match.group(3)
        frame_hex = match.group(4)
        return (frame_num, timestamp, byte_count, frame_hex)

    # Pattern 2: [DEBUG HH:MM:SS.mmm] TNC RX (XX bytes): hexhexhex...
    pattern2 = r'\[DEBUG\s+([\d:\.]+)\]\s+TNC\s+RX\s+\((\d+)\s+bytes\):\s+([0-9a-fA-F]+)'
    match = re.match(pattern2, line)
    if match:
        timestamp = match.group(1)
        byte_count = match.group(2)
        frame_hex = match.group(3)
        # No frame number in debug format, use "D" prefix for debug
        return ("D", timestamp, byte_count, frame_hex)

    # Pattern 3: Just hex data (no metadata)
    if re.match(r'^[0-9a-fA-F]+$', line):
        frame_hex = line
        byte_count = str(len(frame_hex) // 2)
        return ("?", "00:00:00.000", byte_count, frame_hex)

    return None


def load_frame_buffer(buffer_file=None):
    """
    Load frames from the frame buffer database.

    Args:
        buffer_file: Path to buffer file (default: ~/.console_frame_buffer.json.gz)

    Returns:
        List of tuples (frame_num, timestamp, byte_count, frame_hex, direction)
    """
    if buffer_file is None:
        buffer_file = os.path.expanduser("~/.console_frame_buffer.json.gz")

    if not os.path.exists(buffer_file):
        print(f"{Colors.RED}Error: Frame buffer file not found: {buffer_file}{Colors.RESET}")
        return []

    try:
        with gzip.open(buffer_file, 'rt', encoding='utf-8') as f:
            data = json.load(f)

        frames = []
        for frame_data in data.get('frames', []):
            frame_num = frame_data['frame_number']
            timestamp = datetime.fromisoformat(frame_data['timestamp']).strftime('%H:%M:%S.%f')[:-3]
            raw_bytes = base64.b64decode(frame_data['raw_bytes'])
            frame_hex = raw_bytes.hex()
            byte_count = str(len(raw_bytes))
            direction = frame_data.get('direction', 'RX')

            frames.append((frame_num, timestamp, byte_count, frame_hex, direction))

        return frames

    except Exception as e:
        print(f"{Colors.RED}Error loading frame buffer: {e}{Colors.RESET}")
        return []


def analyze_frame(frame_num, timestamp, byte_count, frame_hex, direction='RX'):
    """Analyze a single frame and return decoded data and output."""
    # Decode the frame
    decoded = decode_kiss_frame(frame_hex)

    # Format output with direction indicator
    header_text = f"Frame {frame_num} [{direction}]: {timestamp} ({byte_count} bytes)"
    output = format_frame_output(frame_num, header_text, byte_count, decoded)

    # Track ACKs
    is_ack = (decoded.get('aprs') and
              decoded['aprs'].get('details', {}).get('is_ack'))

    ack_info = None
    if is_ack:
        src = decoded['ax25']['source']['full']
        to = decoded['aprs']['details']['to']
        msg_id = decoded['aprs']['details']['message_id']
        ack_info = f"{src} -> {to} (ID: {msg_id})"

    return decoded, output, ack_info


def main():
    """Main function - reads input and analyzes frames"""
    parser = argparse.ArgumentParser(
        description='KISS/AX.25/APRS Protocol Analyzer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Analyze frames from stdin
  %(prog)s

  # Load and analyze all frames from buffer
  %(prog)s --buffer

  # Analyze specific frame numbers
  %(prog)s --buffer --frames 42 43 44

  # Analyze frame range
  %(prog)s --buffer --range 100-150

  # Use custom buffer file
  %(prog)s --buffer-file /path/to/buffer.json.gz --frames 5
        '''
    )

    parser.add_argument(
        '--buffer', '-b',
        action='store_true',
        help='Load frames from frame buffer database (~/.console_frame_buffer.json.gz)'
    )

    parser.add_argument(
        '--buffer-file',
        metavar='FILE',
        help='Custom frame buffer file path'
    )

    parser.add_argument(
        '--frames', '-f',
        nargs='+',
        type=int,
        metavar='N',
        help='Specific frame numbers to analyze'
    )

    parser.add_argument(
        '--range', '-r',
        metavar='START-END',
        help='Range of frame numbers (e.g., 100-150)'
    )

    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='List all available frames without analyzing'
    )

    args = parser.parse_args()

    # Determine source of frames
    if args.buffer or args.buffer_file or args.frames or args.range or args.list:
        # Load from buffer database
        print(f"{Colors.BOLD}KISS/AX.25/APRS Protocol Analyzer{Colors.RESET}")
        print(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")

        buffer_file = args.buffer_file if args.buffer_file else None
        all_frames = load_frame_buffer(buffer_file)

        if not all_frames:
            return

        print(f"Loaded {len(all_frames)} frames from buffer")

        if all_frames:
            print(f"Frame range: {all_frames[0][0]} to {all_frames[-1][0]}")

        print(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
        print()

        # List mode
        if args.list:
            print(f"{Colors.BOLD}Available Frames:{Colors.RESET}\n")
            for frame_num, timestamp, byte_count, _, direction in all_frames:
                print(f"  [{direction}] Frame {frame_num:5d}: {timestamp} ({byte_count:4s} bytes)")
            print()
            return

        # Determine which frames to analyze
        frames_to_analyze = []

        if args.frames:
            # Specific frame numbers
            frame_nums = set(args.frames)
            frames_to_analyze = [f for f in all_frames if f[0] in frame_nums]

            # Check for missing frames
            found_nums = {f[0] for f in frames_to_analyze}
            missing = frame_nums - found_nums
            if missing:
                print(f"{Colors.YELLOW}Warning: Frame(s) not found: {sorted(missing)}{Colors.RESET}\n")

        elif args.range:
            # Frame range
            try:
                start, end = map(int, args.range.split('-'))
                frames_to_analyze = [f for f in all_frames if start <= f[0] <= end]

                if not frames_to_analyze:
                    print(f"{Colors.YELLOW}No frames in range {start}-{end}{Colors.RESET}")
                    return

            except ValueError:
                print(f"{Colors.RED}Error: Invalid range format. Use START-END (e.g., 100-150){Colors.RESET}")
                return

        else:
            # All frames
            frames_to_analyze = all_frames

        # Analyze selected frames
        frames_analyzed = 0
        acks_found = []

        for frame_num, timestamp, byte_count, frame_hex, direction in frames_to_analyze:
            decoded, output, ack_info = analyze_frame(frame_num, timestamp, byte_count, frame_hex, direction)
            print(output)
            frames_analyzed += 1

            if ack_info:
                acks_found.append(ack_info)

    else:
        # Original stdin mode
        print(f"{Colors.BOLD}KISS/AX.25/APRS Protocol Analyzer{Colors.RESET}")
        print(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
        print()
        print("Paste your frame capture below (one frame per line).")
        print()
        print("Supported formats:")
        print("  [1] RX 11:49:44.979 (42b): c00092884040...")
        print("  [DEBUG 12:31:24.651] TNC RX (54 bytes): c00092884040...")
        print("  c00092884040...  (raw hex)")
        print()
        print("Press Ctrl+D (Linux/Mac) or Ctrl+Z then Enter (Windows) when done.")
        print(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
        print()

        frames_analyzed = 0
        acks_found = []

        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue

                parsed = parse_input_line(line)
                if not parsed:
                    print(f"{Colors.YELLOW}Warning: Could not parse line: {line[:60]}...{Colors.RESET}")
                    continue

                frame_num, timestamp, byte_count, frame_hex = parsed
                decoded, output, ack_info = analyze_frame(frame_num, timestamp, byte_count, frame_hex)
                print(output)
                frames_analyzed += 1

                if ack_info:
                    acks_found.append(ack_info)

        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}Interrupted by user{Colors.RESET}")

    # Summary
    print()
    print(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}Analysis Summary{Colors.RESET}")
    print(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
    print(f"Frames analyzed: {frames_analyzed}")

    if acks_found:
        print(f"\n{Colors.GREEN}ACK Messages Found:{Colors.RESET}")
        for ack in acks_found:
            print(f"  {Colors.GREEN}✓{Colors.RESET} {ack}")
    else:
        print(f"\n{Colors.YELLOW}No ACK messages found{Colors.RESET}")


if __name__ == '__main__':
    main()
