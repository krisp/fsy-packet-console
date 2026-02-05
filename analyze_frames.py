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

# Import shared frame analysis functions
from src.frame_analyzer import (
    decode_kiss_frame,
    format_frame_detailed,
    Colors
)


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


def strip_ansi_codes(text):
    """Strip ANSI escape codes from text."""
    import re
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


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


def analyze_frame(frame_num, timestamp, byte_count, frame_hex, direction='RX', use_colors=True):
    """Analyze a single frame and return decoded data and output."""
    # Decode the frame
    decoded = decode_kiss_frame(frame_hex)

    # Format output using format_frame_detailed with or without colors
    output_format = 'ansi' if use_colors else 'ansi'  # Both use ansi, but without color codes if not TTY
    output = format_frame_detailed(
        decoded=decoded,
        frame_num=frame_num,
        timestamp=timestamp,
        direction=direction,
        output_format=output_format
    )

    # Strip ANSI codes if not outputting to a TTY
    if not use_colors:
        output = strip_ansi_codes(output)

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

  # Pipe to grep (colors auto-disabled)
  %(prog)s -b -l | grep NTSGTE
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

    parser.add_argument(
        '--color',
        choices=['auto', 'always', 'never'],
        default='auto',
        help='When to use colors (default: auto - disable if piping)'
    )

    args = parser.parse_args()

    # Determine whether to use colors
    if args.color == 'always':
        use_colors = True
    elif args.color == 'never':
        use_colors = False
    else:  # auto
        use_colors = sys.stdout.isatty()

    # Determine source of frames
    if args.buffer or args.buffer_file or args.frames or args.range or args.list:
        # Load from buffer database
        # Helper to strip colors if needed
        def format_output(text):
            return strip_ansi_codes(text) if not use_colors else text

        header = f"{Colors.BOLD}KISS/AX.25/APRS Protocol Analyzer{Colors.RESET}"
        print(format_output(header))
        separator = f"{Colors.BOLD}{'=' * 80}{Colors.RESET}"
        print(format_output(separator))

        buffer_file = args.buffer_file if args.buffer_file else None
        all_frames = load_frame_buffer(buffer_file)

        if not all_frames:
            return

        print(f"Loaded {len(all_frames)} frames from buffer")

        if all_frames:
            print(f"Frame range: {all_frames[0][0]} to {all_frames[-1][0]}")

        print(format_output(separator))
        print()

        # List mode
        if args.list:
            list_header = f"{Colors.BOLD}Available Frames:{Colors.RESET}\n"
            print(format_output(list_header))
            for frame_num, timestamp, byte_count, frame_hex, direction in all_frames:
                # Decode frame to get callsigns and payload preview
                decoded = decode_kiss_frame(frame_hex)

                # Extract callsigns
                if 'error' in decoded:
                    from_call = "ERROR"
                    to_call = "ERROR"
                    payload_preview = decoded['error']
                else:
                    from_call = decoded['ax25']['source']['full']
                    to_call = decoded['ax25']['destination']['full']

                    # Get payload preview (first 40 chars)
                    if decoded.get('info'):
                        payload_text = decoded['info']['text']
                        # Sanitize to printable ASCII + common Unicode when piping
                        # Remove control characters and invalid UTF-8
                        if not use_colors:
                            # Strip non-printable characters
                            payload_text = ''.join(c if (c.isprintable() or c.isspace()) else '?' for c in payload_text)
                        payload_preview = payload_text[:40] + '...' if len(payload_text) > 40 else payload_text
                    else:
                        payload_preview = "(no info field)"

                # Format output as single line
                # Use ASCII arrow when not in TTY to avoid UTF-8 issues with grep
                arrow = "→" if use_colors else "->"
                line = f"  [{direction}] Frame {frame_num:5d}: {timestamp} ({byte_count:4s}b) {Colors.GREEN}{from_call:12}{Colors.RESET} {arrow} {Colors.GREEN}{to_call:12}{Colors.RESET} {Colors.MAGENTA}{payload_preview}{Colors.RESET}"
                print(format_output(line))
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
                warning = f"{Colors.YELLOW}Warning: Frame(s) not found: {sorted(missing)}{Colors.RESET}\n"
                print(format_output(warning))

        elif args.range:
            # Frame range
            try:
                start, end = map(int, args.range.split('-'))
                frames_to_analyze = [f for f in all_frames if start <= f[0] <= end]

                if not frames_to_analyze:
                    no_frames = f"{Colors.YELLOW}No frames in range {start}-{end}{Colors.RESET}"
                    print(format_output(no_frames))
                    return

            except ValueError:
                error = f"{Colors.RED}Error: Invalid range format. Use START-END (e.g., 100-150){Colors.RESET}"
                print(format_output(error))
                return

        else:
            # All frames
            frames_to_analyze = all_frames

        # Analyze selected frames
        frames_analyzed = 0
        acks_found = []

        for frame_num, timestamp, byte_count, frame_hex, direction in frames_to_analyze:
            decoded, output, ack_info = analyze_frame(frame_num, timestamp, byte_count, frame_hex, direction, use_colors)
            print(output)
            frames_analyzed += 1

            if ack_info:
                acks_found.append(ack_info)

    else:
        # Original stdin mode
        # Helper to strip colors if needed (same as buffer mode)
        def format_output(text):
            return strip_ansi_codes(text) if not use_colors else text

        print(format_output(f"{Colors.BOLD}KISS/AX.25/APRS Protocol Analyzer{Colors.RESET}"))
        print(format_output(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}"))
        print()
        print("Paste your frame capture below (one frame per line).")
        print()
        print("Supported formats:")
        print("  [1] RX 11:49:44.979 (42b): c00092884040...")
        print("  [DEBUG 12:31:24.651] TNC RX (54 bytes): c00092884040...")
        print("  c00092884040...  (raw hex)")
        print()
        print("Press Ctrl+D (Linux/Mac) or Ctrl+Z then Enter (Windows) when done.")
        print(format_output(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}"))
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
                    warning = f"{Colors.YELLOW}Warning: Could not parse line: {line[:60]}...{Colors.RESET}"
                    print(format_output(warning))
                    continue

                frame_num, timestamp, byte_count, frame_hex = parsed
                decoded, output, ack_info = analyze_frame(frame_num, timestamp, byte_count, frame_hex, use_colors=use_colors)
                print(output)
                frames_analyzed += 1

                if ack_info:
                    acks_found.append(ack_info)

        except KeyboardInterrupt:
            print(f"\n{Colors.YELLOW}Interrupted by user{Colors.RESET}")

    # Summary
    print()
    # Use strip_ansi_codes directly for summary since format_output may not be in scope
    if use_colors:
        print(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
        print(f"{Colors.BOLD}Analysis Summary{Colors.RESET}")
        print(f"{Colors.BOLD}{'=' * 80}{Colors.RESET}")
    else:
        print("=" * 80)
        print("Analysis Summary")
        print("=" * 80)
    print(f"Frames analyzed: {frames_analyzed}")

if __name__ == '__main__':
    main()
