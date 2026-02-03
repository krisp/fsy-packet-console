#!/usr/bin/env python3
"""
FSY Packet Console - Entry Point

Professional packet radio and APRS platform with universal TNC support.
"""

import os
import sys


class TeeLogger:
    """Write to both a file and the original stream."""

    def __init__(self, file_path, original_stream):
        self.file = open(os.path.expanduser(file_path), 'a', buffering=1)
        self.original = original_stream

    def write(self, data):
        self.original.write(data)
        self.file.write(data)

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
        const="~/.fsy-console",
        metavar="FILE",
        help="Log all console output to file (default: ~/.fsy-console)",
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
        log_file = TeeLogger(args.log, sys.stdout)
        sys.stdout = log_file
        sys.stderr = log_file

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
