"""FSY Packet Console package.

Re-exports all public symbols for backward-compatible imports
(e.g. ``from src.console import CommandProcessor`` continues to work).
"""

from .frame_history import FrameHistory, FrameHistoryEntry
from .tnc_config import TNCConfig
from .completers import TNCCompleter, CommandCompleter
from .parsers import (
    parse_and_track_aprs_frame,
    decode_control_field,
    decode_aprs_packet_type,
    calculate_hop_count,
)
from .processor import CommandProcessor
from .monitors import (
    gps_monitor,
    tnc_monitor,
    heartbeat_monitor,
    autosave_monitor,
    message_retry_monitor,
    connection_watcher,
)
from .main import command_loop, main, run

__all__ = [
    "FrameHistory",
    "FrameHistoryEntry",
    "TNCConfig",
    "TNCCompleter",
    "CommandCompleter",
    "parse_and_track_aprs_frame",
    "decode_control_field",
    "decode_aprs_packet_type",
    "calculate_hop_count",
    "CommandProcessor",
    "gps_monitor",
    "tnc_monitor",
    "heartbeat_monitor",
    "autosave_monitor",
    "message_retry_monitor",
    "connection_watcher",
    "command_loop",
    "main",
    "run",
]
