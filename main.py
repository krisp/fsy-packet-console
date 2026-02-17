#!/usr/bin/env python3
"""
FSY Packet Console - Entry Point

Professional packet radio and APRS platform with universal TNC support.
"""

import argparse
import os
import sys
from datetime import datetime
from src.console import run
from src import constants
from src.utils import set_console_log_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FSY Packet Console")
    parser.add_argument(
        "--version",
        action="version",
        version=f"FSY Packet Console v{constants.VERSION}",
    )
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
        "--init-kiss",
        action="store_true",
        help="Initialize TNC into KISS mode on startup (for TNCs that power up in command mode)",
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

        log_path = os.path.expanduser(args.log)

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
            init_kiss=args.init_kiss,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            radio_mac=args.radio_mac,
        )
    finally:
        # Close log file if it was opened
        if log_file:
            log_file.close()
