"""
Debug command handlers.

Handles debug level configuration, station filtering, frame buffer operations,
and frame history dumping/watching.
"""

import asyncio
import os

from prompt_toolkit import HTML
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.key_binding import KeyBindings

from .base import CommandHandler, command
from src import constants
from src.device_id import get_device_identifier
from src.frame_analyzer import (
    decode_ax25_address,
    decode_kiss_frame,
    format_frame_detailed
)
from src.utils import print_pt, print_info, print_error, print_header, print_debug


class DebugCommandHandler(CommandHandler):
    """Handles debug configuration and frame analysis commands."""

    def __init__(self, cmd_processor):
        """
        Initialize debug command handler.

        Args:
            cmd_processor: Reference to main CommandProcessor instance
        """
        self.cmd_processor = cmd_processor
        self.frame_history = cmd_processor.frame_history
        super().__init__()

    def _format_detailed_frame(self, frame, index=1):
        """
        Format a frame with detailed protocol analysis.

        Replicates console.py's format_detailed_frame wrapper logic.

        Args:
            frame: FrameHistoryEntry object
            index: Frame number in sequence

        Returns:
            List of HTML-formatted strings for display
        """
        # Decode the KISS frame
        decoded = decode_kiss_frame(frame.raw_bytes)

        # Format timestamp
        time_str = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]

        # Use format_frame_detailed from frame_analyzer with HTML output
        lines = format_frame_detailed(
            decoded=decoded,
            frame_num=index,
            timestamp=time_str,
            direction=frame.direction,
            output_format='html'
        )

        # Add console-specific device identification if APRS data is present
        if decoded.get('aprs') and 'error' not in decoded:
            aprs = decoded['aprs']
            dest = decoded.get('ax25', {}).get('destination', {})
            dest_call = dest.get('callsign') if dest else None

            device_info = None
            try:
                device_id = get_device_identifier()

                # Try to identify by tocall (destination address) for normal APRS
                if dest_call:
                    device_info = device_id.identify_by_tocall(dest_call)

                # For MIC-E, try to identify by comment suffix
                details = aprs.get('details', {})
                if not device_info and aprs.get('type') == 'APRS MIC-E Position' and 'comment' in details:
                    device_info = device_id.identify_by_mice(details.get('comment', ''))

                if device_info:
                    # Find where to insert (before hex dump)
                    hex_dump_idx = None
                    for i, line in enumerate(lines):
                        if 'KISS Frame Hex Dump' in line or 'Hex Dump' in line:
                            hex_dump_idx = i
                            break

                    # Build device info line
                    device_line = f"  <yellow>Device:</yellow> {device_info['vendor']}"
                    if device_info.get('model'):
                        device_line += f" {device_info['model']}"
                    if device_info.get('class'):
                        device_line += f" ({device_info['class']})"

                    # Insert before hex dump if found, otherwise at end
                    if hex_dump_idx is not None:
                        lines.insert(hex_dump_idx, device_line)
                    else:
                        lines.append(device_line)
            except Exception:
                pass

        return lines

    @command("DEBUG",
             help_text="Debug level, filtering, and frame analysis",
             usage="DEBUG [level|filter|save|dump] ...",
             category="debug")
    async def debug(self, args):
        """Set debug level (0-6), filter by station, or dump frame history.

        Usage:
            debug [level]               - Show or set debug level (0-6)
            debug <level> filter <call> - Set debug level only for specific station
            debug filter                - Show active station filters
            debug filter clear          - Clear all station filters
            debug save                  - Save frame buffer to disk now
            debug dump [n] [brief]      - Dump last n frames (default=10)
                                          brief = compact hex output
            debug dump [n] detail       - Dump with Wireshark-style protocol analysis
            debug dump n-m [brief|detail] - Dump frames n through m (range)
            debug dump detail watch     - Watch mode: live protocol analysis of incoming frames
        """
        if not args:
            # Show current level and available levels
            print_info(f"Current debug level: {constants.DEBUG_LEVEL}")
            if constants.DEBUG_STATION_FILTERS:
                print_info("")
                print_info("Active station filters:")
                for call, level in sorted(constants.DEBUG_STATION_FILTERS.items()):
                    print_info(f"  {call}: level {level}")
            print_info("")
            print_info("Debug levels:")
            print_info("  0 = Off (no debug output)")
            print_info("  1 = TNC monitor")
            print_info("  2 = Critical errors and events")
            print_info("  3 = Connection state changes")
            print_info("  4 = Frame transmission/reception")
            print_info("  5 = Protocol details, retransmissions")
            print_info("  6 = Everything (BLE, config, hex dumps)")
            print_info("")
            print_info("Usage: debug <level>  or  debug <level> filter <callsign>")
            print_info("       debug filter  or  debug filter clear")
            print_info("       debug save")
            print_info("       debug dump [n|-n|n-m] [brief|detail|watch]")
            print_info("Example: debug 5 filter k1mal-7  (debug level 5 for K1MAL-7 only)")
            print_info("         debug 2  (sets global level to 2, clears filters)")
            print_info("         debug save  (save frame buffer to disk)")
            print_info("         debug dump 5 brief  or  debug dump 3 detail")
            return

        # Check for filter subcommand
        if args[0].lower() == "filter":
            await self._debug_filter(args[1:])
            return

        # Check for save subcommand
        if args[0].lower() == "save":
            await self._debug_save()
            return

        # Check for dump subcommand
        if args[0].lower() == "dump":
            await self._debug_dump(args[1:])
            return

        # Handle debug level setting
        try:
            level = int(args[0])
            if level < 0 or level > 6:
                print_error("Debug level must be 0-6")
                return

            # Check if this is a per-station filter (debug <level> filter <callsign>)
            if len(args) >= 3 and args[1].lower() == "filter":
                callsign = args[2].upper().strip()
                constants.DEBUG_STATION_FILTERS[callsign] = level
                print_info(f"Station filter set: {callsign} -> debug level {level}")
                print_info(f"Global debug level remains: {constants.DEBUG_LEVEL}")
                print_info("(Frames involving this station will use the higher level)")
                return

            # Setting global level - clear station filters as requested
            if constants.DEBUG_STATION_FILTERS:
                constants.DEBUG_STATION_FILTERS.clear()
                print_info("Station filters cleared (global level set)")

            # Track previous level for file logging management
            prev_level = constants.DEBUG_LEVEL

            constants.DEBUG_LEVEL = level
            # Update legacy DEBUG flag for backward compatibility
            constants.DEBUG = level > 0

            if level == 0:
                print_info("Debug mode: OFF")
            else:
                print_info(f"Debug level set to: {level}")
                level_desc = {
                    1: "Received Frames",
                    2: "Critical errors and events",
                    3: "Connection state changes",
                    4: "Frame TX/RX details",
                    5: "Protocol details, retransmissions + FILE LOGGING",
                    6: "Everything (BLE, config, hex dumps) + FILE LOGGING",
                }
                print_info(f"  {level_desc.get(level, 'Unknown')}")
        except (ValueError, IndexError):
            print_error("Usage: debug <level>  or  debug dump [n]")
            print_error("Example: debug 3  or  debug dump 5")

    async def _debug_filter(self, args):
        """Handle debug filter subcommands."""
        if len(args) == 0:
            # Show current filters
            if constants.DEBUG_STATION_FILTERS:
                print_info("Active station filters:")
                for call, level in sorted(constants.DEBUG_STATION_FILTERS.items()):
                    print_info(f"  {call}: level {level}")
            else:
                print_info("No station filters active")
                print_info("Use: debug <level> filter <callsign>")
            return
        elif len(args) == 1 and args[0].lower() == "clear":
            # Clear all filters
            constants.DEBUG_STATION_FILTERS.clear()
            print_info("All station filters cleared")
            return
        else:
            print_error("Usage: debug filter  or  debug filter clear")
            return

    async def _debug_save(self):
        """Save frame buffer to disk."""
        print_info("Saving frame buffer to disk...")
        self.frame_history.save_to_disk()

        # Show stats (access BUFFER_FILE from the instance's class)
        buffer_file = self.frame_history.BUFFER_FILE
        if os.path.exists(buffer_file):
            size = os.path.getsize(buffer_file)
            size_kb = size / 1024
            print_info(f"✓ Saved {len(self.frame_history.frames)} frames to {buffer_file}")
            print_info(f"  File size: {size_kb:.1f} KB")
        else:
            print_info("✓ Frame buffer saved")

    async def _debug_dump(self, args):
        """Dump frame history with various formats."""
        # Handle debug dump [n] [brief|detail] [watch]
        count = None
        specific_frame = None
        frame_range = None  # (start, end) tuple for range
        brief_mode = False
        detail_mode = False
        watch_mode = False
        format_specified = None  # Track which format was specified

        # Parse arguments (count and/or brief/detail/watch)
        for arg in args:
            if arg.lower() == "brief":
                if format_specified == "detail":
                    print_error("Cannot specify both 'brief' and 'detail'")
                    return
                brief_mode = True
                detail_mode = False
                format_specified = "brief"
            elif arg.lower() == "detail":
                if format_specified == "brief":
                    print_error("Cannot specify both 'brief' and 'detail'")
                    return
                detail_mode = True
                brief_mode = False
                format_specified = "detail"
            elif arg.lower() == "watch":
                watch_mode = True
                # If no format specified yet, default to detail for watch mode
                if not format_specified:
                    detail_mode = True
                    format_specified = "detail"
            else:
                # Check if this is a range (e.g., "4-13")
                if '-' in arg and not arg.startswith('-'):
                    try:
                        parts = arg.split('-')
                        if len(parts) == 2:
                            start = int(parts[0])
                            end = int(parts[1])
                            if start > 0 and end > 0 and start <= end:
                                frame_range = (start, end)
                            else:
                                print_error("Range must be start-end where start <= end and both > 0")
                                return
                        else:
                            print_error("Invalid range format (use start-end)")
                            return
                    except ValueError:
                        print_error(f"Invalid range: {arg}")
                        return
                else:
                    try:
                        num = int(arg)
                        if num < 0:
                            # Negative = last n frames
                            count = abs(num)
                        elif num > 0:
                            # Positive = specific frame number
                            specific_frame = num
                        else:
                            print_error("Frame number must be non-zero")
                            return
                    except ValueError:
                        print_error(f"Unknown argument: {arg}")
                        return

        # Handle watch mode
        if watch_mode:
            await self._debug_watch_mode()
            return

        # Get frames
        if frame_range is not None:
            # Get frames in range
            start, end = frame_range
            frames = []
            for frame_num in range(start, end + 1):
                frame = self.frame_history.get_by_number(frame_num)
                if frame:
                    frames.append(frame)

            if not frames:
                print_error(f"No frames found in range #{start} to #{end}")
                if len(self.frame_history.frames) > 0:
                    oldest = self.frame_history.frames[0].frame_number
                    newest = self.frame_history.frames[-1].frame_number
                    print_info(f"Available frames: #{oldest} to #{newest}")
                return
            elif len(frames) < (end - start + 1):
                missing = (end - start + 1) - len(frames)
                print_info(f"Found {len(frames)} frames in range #{start}-#{end} ({missing} missing)")
        elif specific_frame is not None:
            # Get specific frame by number
            frame = self.frame_history.get_by_number(specific_frame)
            if not frame:
                print_error(f"Frame #{specific_frame} not found in history")
                if len(self.frame_history.frames) > 0:
                    oldest = self.frame_history.frames[0].frame_number
                    newest = self.frame_history.frames[-1].frame_number
                    print_info(f"Available frames: #{oldest} to #{newest}")
                return
            frames = [frame]
        else:
            # Get last n frames (default 10 if count is None)
            if count is None:
                count = 10
            frames = self.frame_history.get_recent(count)

        if not frames:
            print_info("No frames in history")
            return

        # Determine header text
        if frame_range:
            header_suffix = f"frames #{frame_range[0]}-#{frame_range[1]}"
        elif specific_frame:
            header_suffix = f"frame #{specific_frame}"
        else:
            header_suffix = f"last {len(frames)} frames"

        if detail_mode:
            # Detail mode - Wireshark-style protocol analysis
            print_header(
                f"Frame History - Detail ({header_suffix})"
            )

            # Track ACKs
            acks_found = []

            for frame in frames:
                # Format frame with detailed analysis
                detail_lines = self._format_detailed_frame(frame, frame.frame_number)
                for line in detail_lines:
                    print_pt(line)
                print_pt("")  # Blank line between frames

                # Check for ACKs
                try:
                    raw_bytes = frame.raw_bytes
                    if len(raw_bytes) > 3:
                        ax25_payload = raw_bytes[2:-1]
                        # Quick check for APRS message
                        if len(ax25_payload) > 20:
                            # Extract info field (after addresses, control, pid)
                            offset = 14
                            while offset < len(ax25_payload) and not ax25_payload[offset - 1] & 0x01:
                                offset += 7
                            if offset + 2 < len(ax25_payload):
                                info_bytes = ax25_payload[offset + 2:]
                                info_str = info_bytes.decode('ascii', errors='replace').rstrip('\r\n\x00')
                                if info_str.startswith(':') and len(info_str) >= 11:
                                    message_text = info_str[11:]
                                    if message_text.startswith('ack'):
                                        src = decode_ax25_address(ax25_payload[7:14])
                                        to_call = info_str[1:10].strip()
                                        msg_id = message_text[3:].split('{')[0].strip()
                                        if src:
                                            acks_found.append(f"{src['full']} -> {to_call} (ID: {msg_id})")
                except Exception:
                    pass

        elif brief_mode:
            # Brief mode - compact output
            print_header(
                f"Frame History - Brief ({header_suffix})"
            )
            for frame in frames:
                time_str = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]
                direction_color = (
                    "green" if frame.direction == "TX" else "cyan"
                )
                hex_str = frame.raw_bytes.hex()
                print_pt(
                    HTML(
                        f"[{frame.frame_number}] <{direction_color}>{frame.direction}</{direction_color}> {time_str} ({len(frame.raw_bytes)}b): {hex_str}"
                    )
                )
        else:
            # Verbose mode - hex editor style
            print_header(f"Frame History ({header_suffix})")

            for frame in frames:
                time_str = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]
                direction_color = (
                    "green" if frame.direction == "TX" else "cyan"
                )

                print_pt(
                    HTML(
                        f"<b>[{frame.frame_number}] <{direction_color}>{frame.direction}</{direction_color}> {time_str}</b> ({len(frame.raw_bytes)} bytes)"
                    )
                )

                # Print hex dump with ASCII
                for line in frame.format_hex_lines():
                    print_pt(line)
                print_pt("")  # Blank line between frames

    async def _debug_watch_mode(self):
        """Watch mode for debug dump - continuously display detailed protocol analysis for incoming frames."""
        print_header("Frame Watch Mode - Live Protocol Analysis")
        print_pt(HTML("<yellow>Monitoring for incoming frames...</yellow>"))
        print_pt(HTML("<gray>Press ESC or type '~~~' to exit</gray>"))
        print_pt("")

        # Track the last frame we've seen
        last_frame_count = len(self.frame_history.frames)
        frame_counter = 0

        # Set up key bindings for escape detection
        kb = KeyBindings()
        exit_requested = asyncio.Event()

        @kb.add('escape')
        def _(event):
            """Handle escape key."""
            exit_requested.set()
            event.app.exit()

        # Create a prompt session for input handling
        session = PromptSession(key_bindings=kb)

        async def monitor_frames():
            """Monitor for new frames and display them."""
            nonlocal last_frame_count, frame_counter

            while not exit_requested.is_set():
                try:
                    # Check if there are new frames
                    current_frame_count = len(self.frame_history.frames)

                    if current_frame_count > last_frame_count:
                        # Get only the new frames
                        new_frames = list(self.frame_history.frames)[last_frame_count:current_frame_count]

                        for frame in new_frames:
                            frame_counter += 1

                            # Format and display the frame with detailed analysis
                            # (format_detailed_frame includes its own header separator)
                            print_pt("")
                            detail_lines = self._format_detailed_frame(frame, frame_counter)
                            for line in detail_lines:
                                print_pt(line)

                        last_frame_count = current_frame_count

                    # Small delay to avoid busy waiting
                    await asyncio.sleep(0.1)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print_debug(f"Error in frame monitor: {e}", level=2)
                    break

        async def input_handler():
            """Handle user input for ~~~ exit sequence and ESC key."""
            try:
                # Single prompt call - waits for user input (ESC or ~~~)
                # This processes keyboard events (including ESC key binding)
                line = await session.prompt_async(
                    HTML("<gray>[Watching... type '~~~' or press ESC to exit]</gray> "),
                    key_bindings=kb
                )

                # Check if user typed ~~~ (line will be None if ESC was pressed)
                if line is not None and line.strip() == "~~~":
                    exit_requested.set()
                elif line is None:
                    # ESC was pressed (prompt returned None)
                    exit_requested.set()

            except (KeyboardInterrupt, EOFError):
                exit_requested.set()
            except asyncio.CancelledError:
                pass

        # Run both tasks concurrently
        monitor_task = asyncio.create_task(monitor_frames())
        input_task = asyncio.create_task(input_handler())

        try:
            # Wait for either task to complete (exit requested)
            done, pending = await asyncio.wait(
                [monitor_task, input_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except KeyboardInterrupt:
            pass
        finally:
            print_pt("")
            print_pt(HTML("<yellow>Watch mode exited</yellow>"))
            print_pt(HTML(f"<gray>Monitored {frame_counter} frame(s)</gray>"))
            print_pt("")
