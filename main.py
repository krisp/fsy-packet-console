#!/usr/bin/env python3
"""
FSY Packet Console - Entry Point

Professional packet radio and APRS platform with universal TNC support.
"""

import os
import sys
from datetime import datetime


class TeeLogger:
    """Write to both a file and the original stream with timestamps."""

    MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB

    def __init__(self, file_path, original_stream):
        self.file_path = os.path.expanduser(file_path)
        self.original = original_stream
        self.at_line_start = True

        # Rotate log if it's too large
        self._rotate_if_needed()

        # Open in append mode
        self.file = open(self.file_path, 'a', buffering=1)

    def _rotate_if_needed(self):
        """Rotate log file if it exceeds MAX_LOG_SIZE."""
        if os.path.exists(self.file_path):
            size = os.path.getsize(self.file_path)
            if size > self.MAX_LOG_SIZE:
                # Rename old log with timestamp
                timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                backup_path = f"{self.file_path}.{timestamp}"
                os.rename(self.file_path, backup_path)
                print(f"Rotated log: {backup_path}", file=sys.__stderr__)

    def write(self, data):
        # Write to terminal without modification
        self.original.write(data)

        # Write to file with timestamps
        if not data:
            return

        for char in data:
            # Add timestamp at the start of each line
            if self.at_line_start and char != '\n':
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.file.write(f"[{timestamp}] ")
                self.at_line_start = False

            # Write the character
            self.file.write(char)

            # Track if we just wrote a newline
            if char == '\n':
                self.at_line_start = True

    def flush(self):
        self.original.flush()
        self.file.flush()

    def close(self):
        self.file.close()

    def __getattr__(self, attr):
        """Forward any other attributes to the original stream."""
        return getattr(self.original, attr)


if __name__ == "__main__":
    import argparse
    from src.console import run

    parser = argparse.ArgumentParser(description="FSY Packet Console")
    parser.add_argument(
        "-t", "--tnc", action="store_true", help="Start in TNC mode"
    )
    parser.add_argument(
        "-c",
        "--connect",
        metavar="CALLSIGN",
        help="Auto-connect to remote station (requires -t)",
    )
    parser.add_argument(
        "-d",
        "--debug",
        nargs="?",
        type=int,
        const=2,
        default=0,
        metavar="LEVEL",
        help="Enable debug mode at startup (optional level 0-6, default: 2)",
    )
    parser.add_argument(
        "-s",
        "--serial",
        metavar="PORT",
        help="Use serial KISS TNC instead of BLE (e.g., /dev/ttyUSB0)",
    )
    parser.add_argument(
        "-b",
        "--baud",
        type=int,
        default=9600,
        help="Serial baud rate (default: 9600)",
    )
    parser.add_argument(
        "-k",
        "--kiss-tcp",
        metavar="HOST:PORT",
        help="Connect to KISS-over-TCP TNC (e.g., direwolf.local:8001, 192.168.1.100:8001)",
    )
    parser.add_argument(
        "-r",
        "--radio-mac",
        metavar="MAC",
        help="Bluetooth MAC address of radio (BLE mode only, e.g., 38:D2:00:01:62:C2)",
    )
    parser.add_argument(
        "-l",
        "--log",
        nargs="?",
        const="~/.fsy-console.log",
        metavar="FILE",
        help="Log all console output to file (default: ~/.fsy-console.log)",
    )

    args = parser.parse_args()

    # Validate that -c requires -t
    if args.connect and not args.tnc:
        parser.error("-c/--connect requires -t/--tnc")

    # Only one transport mode allowed
    if args.serial and args.kiss_tcp:
        parser.error("Cannot use both --serial and --kiss-tcp")

    # Parse KISS-TCP argument
    tcp_host = None
    tcp_port = 8001  # Default Direwolf KISS port

    if args.kiss_tcp:
        if ':' in args.kiss_tcp:
            parts = args.kiss_tcp.split(':', 1)
            tcp_host = parts[0]
            try:
                tcp_port = int(parts[1])
            except ValueError:
                parser.error(f"Invalid port in '{args.kiss_tcp}'")
        else:
            tcp_host = args.kiss_tcp
            # Use default port 8001

    # Install logging to file if requested
    log_file = None
    if args.log:
        from src.utils import set_console_log_file

        log_path = os.path.expanduser(args.log)
        print(f"Logging to: {log_path}", file=sys.stderr)

        # Open log file with rotation
        MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB
        if os.path.exists(log_path) and os.path.getsize(log_path) > MAX_LOG_SIZE:
            timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup_path = f"{log_path}.{timestamp}"
            os.rename(log_path, backup_path)
            print(f"Rotated log: {backup_path}", file=sys.stderr)

        log_file = open(log_path, 'a', buffering=1)

        # Set up logging for prompt_toolkit output (print_pt)
        set_console_log_file(log_file)

        print(f"Logging enabled to: {log_path}")

    try:
        run(
            auto_tnc=args.tnc,
            auto_connect=args.connect,
            auto_debug=args.debug,
            serial_port=args.serial,
            serial_baud=args.baud,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            radio_mac=args.radio_mac,
        )
    finally:
        # Close log file if it was opened
        if log_file:
            log_file.close()
