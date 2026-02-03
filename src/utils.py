"""Utility functions for the radio console."""

import html
import os
from datetime import datetime
from pathlib import Path

from prompt_toolkit import print_formatted_text as _print_pt_original
from prompt_toolkit.formatted_text import HTML, to_plain_text

from . import constants


# Debug log file handle (for DEBUG_LEVEL >= 5)
_debug_log_file = None
_debug_log_path = None

# Console log file handle (for -l option)
_console_log_file = None


def set_console_log_file(file_handle):
    """Set the console log file handle for print_pt output."""
    global _console_log_file
    _console_log_file = file_handle


def print_pt(*args, **kwargs):
    """Wrapper for print_formatted_text that also logs to file if enabled."""
    # Always print to terminal
    _print_pt_original(*args, **kwargs)

    # Also write to console log if enabled
    if _console_log_file:
        try:
            # Convert formatted text to plain text
            if args:
                text = to_plain_text(args[0])
                # Add timestamp at start of line
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                _console_log_file.write(f"[{timestamp}] {text}\n")
                _console_log_file.flush()
        except Exception:
            # Silently ignore logging errors
            pass


def timestamp():
    """Get formatted timestamp."""
    return datetime.now().strftime("%H:%M:%S")


def open_debug_log():
    """Open debug log file for high-level debugging (DEBUG_LEVEL >= 5).

    Creates a new log file in ~/.cache/console-debug-logs/ with timestamp.
    Format: debug-20260201T090932.log

    Returns:
        Path to the opened log file, or None if failed
    """
    global _debug_log_file, _debug_log_path

    # Close existing log if open
    close_debug_log()

    try:
        # Create log directory
        log_dir = Path.home() / ".cache" / "console-debug-logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Generate timestamped filename
        timestamp_str = datetime.now().strftime("%Y%m%dT%H%M%S")
        log_filename = f"debug-{timestamp_str}.log"
        log_path = log_dir / log_filename

        # Open file in append mode
        _debug_log_file = open(log_path, 'a', buffering=1)  # Line buffered
        _debug_log_path = str(log_path)

        # Write header
        _debug_log_file.write(f"=== Debug Log Started: {datetime.now().isoformat()} ===\n")
        _debug_log_file.write(f"=== Debug Level: {constants.DEBUG_LEVEL} ===\n\n")

        return _debug_log_path

    except Exception as e:
        print_error(f"Failed to open debug log: {e}")
        _debug_log_file = None
        _debug_log_path = None
        return None


def close_debug_log():
    """Close the debug log file if open."""
    global _debug_log_file, _debug_log_path

    if _debug_log_file:
        try:
            _debug_log_file.write(f"\n=== Debug Log Closed: {datetime.now().isoformat()} ===\n")
            _debug_log_file.close()
        except Exception:
            pass  # Silently ignore close errors
        finally:
            _debug_log_file = None
            _debug_log_path = None


def print_header(text):
    """Print a colored header."""
    print_pt(HTML(f"\n<b><cyan>{'='*70}</cyan></b>"))
    print_pt(HTML(f"<b><cyan>{text}</cyan></b>"))
    print_pt(HTML(f"<b><cyan>{'='*70}</cyan></b>"))


def _sanitize_for_html(text):
    """Remove control characters and escape HTML entities."""
    # Convert to string and filter control characters
    text_str = str(text)
    filtered = "".join(
        (
            c
            if (c >= " " and c != "\x7f") or c in "\n\r\t"
            else f"\\x{ord(c):02x}"
        )
        for c in text_str
    )
    # Then escape HTML entities
    return html.escape(filtered, quote=False)


def print_info(text, frame_num=None, buffer_mode=False):
    """Print info message with optional frame number.

    Args:
        text: Message text
        frame_num: Optional frame number to show after [INFO]
        buffer_mode: Whether to display frame number
    """
    safe_text = _sanitize_for_html(text)
    if frame_num is not None and buffer_mode:
        print_pt(HTML(f"<green>[INFO]</green> <gray>[{frame_num}]</gray> {safe_text}"))
    else:
        print_pt(HTML(f"<green>[INFO]</green> {safe_text}"))


def print_error(text):
    """Print error message."""
    safe_text = _sanitize_for_html(text)
    print_pt(HTML(f"<red>[ERROR]</red> {safe_text}"))


def print_status(text):
    """Print status message."""
    safe_text = _sanitize_for_html(text)
    print_pt(HTML(f"<blue>[STATUS]</blue> {safe_text}"))


def print_debug(text, level=2, stations=None):
    """Print debug message with optional per-station filtering.

    Args:
        text: The message to print
        level: Debug level (default=2 for general debugging)
               1 = Frame debugging (DEBUGFRAMES)
               2 = General debugging (BLE, heartbeat, etc.)
               Higher levels can be added in the future
        stations: Optional list of callsigns involved in this debug message
                  (source, dest, digipeaters, message participants, etc.)
                  Used for per-station debug filtering

    The message will be printed if:
    - DEBUG_LEVEL >= level, OR
    - Any station in 'stations' list has a filter level >= level in DEBUG_STATION_FILTERS

    At DEBUG_LEVEL >= 5, also writes to debug log file.
    """
    # Check global debug level
    should_print = constants.DEBUG_LEVEL >= level

    # Check per-station filters if stations provided
    if not should_print and stations and constants.DEBUG_STATION_FILTERS:
        for station in stations:
            # Normalize callsign (uppercase, handle SSIDs)
            station_upper = station.upper().strip()
            # Check both with and without SSID
            station_base = station_upper.split('-')[0]

            # Check exact match or base callsign match
            for filter_call, filter_level in constants.DEBUG_STATION_FILTERS.items():
                filter_upper = filter_call.upper().strip()
                filter_base = filter_upper.split('-')[0]

                # Match if exact match or base callsign matches
                if (station_upper == filter_upper or
                    station_base == filter_base or
                    station_upper.startswith(filter_upper) or
                    filter_upper.startswith(station_upper)):
                    if filter_level >= level:
                        should_print = True
                        break

            if should_print:
                break

    if should_print:
        safe_text = _sanitize_for_html(text)
        # Add high-precision timestamp
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]  # milliseconds
        print_pt(HTML(f"<gray>[DEBUG {ts}]</gray> {safe_text}"))

        # Also write to debug log file if DEBUG_LEVEL >= 5 or station filter >= 5
        if (constants.DEBUG_LEVEL >= 5 or
            (stations and any(constants.DEBUG_STATION_FILTERS.get(s.upper().split('-')[0], 0) >= 5
                             for s in stations if s))):
            if _debug_log_file:
                try:
                    # Write plain text (no HTML) with timestamp and level
                    station_tag = f" [{','.join(stations)}]" if stations else ""
                    _debug_log_file.write(f"[DEBUG {ts}] [L{level}]{station_tag} {text}\n")
                except Exception:
                    # Silently ignore write errors - don't disrupt operation
                    pass


def print_tnc(text, frame_num=None):
    """Print TNC data with HTML escaping and control character filtering.

    Args:
        text: TNC frame text to display
        frame_num: Optional frame number to show after [TNC timestamp]

    Note: TNC monitor output is shown only at debug level 1 or higher.
    Also logs all TNC frames to ~/tnc.log when debug level >= 1.
    """
    # Only show and log TNC monitor output at debug level 1+
    if constants.DEBUG_LEVEL < 1:
        return

    # Convert to string and filter out control characters (keep newlines/tabs)
    text_str = str(text)
    # Replace control characters with their hex representation or remove them
    filtered = "".join(
        (
            c
            if (c >= " " and c != "\x7f") or c in "\n\r\t"
            else f"\\x{ord(c):02x}"
        )
        for c in text_str
    )
    # Strip trailing whitespace to prevent extra line breaks
    filtered = filtered.rstrip()
    # Escape for HTML display
    safe_text = html.escape(filtered, quote=False)
    # Construct the message safely
    time_str = timestamp()

    # Display with optional frame number
    if frame_num is not None:
        print_pt(HTML(f"<yellow>[TNC {time_str}]</yellow> <gray>[{frame_num}]</gray> {safe_text}"))
    else:
        print_pt(HTML(f"<yellow>[TNC {time_str}]</yellow> {safe_text}"))

    # Log to tnc.log file (append mode, never cleaned)
    try:
        log_file = os.path.expanduser("~/tnc.log")
        with open(log_file, "a") as f:
            # Write with timestamp and newline
            f.write(f"[TNC {time_str}] {filtered}\n")
    except Exception:
        # Silently ignore logging errors - don't disrupt TNC operation
        pass


def print_warning(text):
    """Print warning message."""
    safe_text = _sanitize_for_html(text)
    print_pt(HTML(f"<orange>[WARNING]</orange> {safe_text}"))


def print_table_row(cols, widths, header=False):
    """Print a formatted table row."""
    row = "  "
    for i, (col, width) in enumerate(zip(cols, widths)):
        row += str(col).ljust(width) + "  "

    if header:
        print_pt(HTML(f"<b>{row}</b>"))
        print_pt("  " + "-" * (sum(widths) + len(widths) * 2))
    else:
        print_pt(row)
