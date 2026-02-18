"""Command processor for the FSY Packet Console."""

import asyncio
import functools
import random
import string
import sys
import traceback
from datetime import datetime, timezone

from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from src import constants
from src.aprs_manager import APRSManager
from src.aprs.geo_utils import maidenhead_to_latlon
from src.ax25_adapter import AX25Adapter, build_ui_kiss_frame
from src.commands.tnc_commands import TNCCommandHandler
from src.commands.beacon_commands import BeaconCommandHandler
from src.commands.weather_commands import WeatherCommandHandler
from src.commands.aprs_console_commands import APRSConsoleCommandHandler
from src.commands.debug_commands import DebugCommandHandler
from src.commands.radio_commands import RadioCommandHandler
from src.utils import (
    print_debug,
    print_error,
    print_header,
    print_info,
    print_pt,
    print_warning,
)

from .frame_history import FrameHistory
from .tnc_config import TNCConfig
from .completers import TNCCompleter


class CommandProcessor:
    def __init__(self, radio, serial_mode=False, tnc_config=None):
        self.radio = radio
        self.serial_mode = serial_mode  # True if using serial TNC (no radio control)
        self.console_mode = "aprs" if serial_mode else "radio"  # Start in APRS mode for serial

        def _radio(cmd_name):
            return functools.partial(self._dispatch_radio_command, _cmd_name=cmd_name)

        self.commands = {
            "help": self.cmd_help,
            "status": _radio("status"),
            "health": _radio("health"),
            "notifications": _radio("notifications"),
            "vfo": _radio("vfo"),
            "setvfo": _radio("setvfo"),
            "active": _radio("active"),
            "dual": _radio("dual"),
            "scan": _radio("scan"),
            "squelch": _radio("squelch"),
            "volume": _radio("volume"),
            "bss": _radio("bss"),
            "setbss": _radio("setbss"),
            "poweron": _radio("poweron"),
            "poweroff": _radio("poweroff"),
            "channel": _radio("channel"),
            "list": _radio("list"),
            "power": _radio("power"),
            "freq": _radio("freq"),
            "dump": _radio("dump"),
            "debug": self._dispatch_debug_command,
            "tncsend": self._dispatch_tnc_command,
            "aprs": self._dispatch_aprs_command,
            "pws": self._dispatch_pws_command,
            "scan_ble": _radio("scan_ble"),
            "gps": _radio("gps"),
            "tnc": self.cmd_tnc,
            "quit": self.cmd_quit,
            "exit": self.cmd_quit,
        }
        # TNC configuration and state
        # Use provided config or create new one (for shared instance across command processor and web server)
        self.tnc_config = tnc_config if tnc_config is not None else TNCConfig()
        self.tnc_connected_to = None
        self.tnc_mode = False
        self.tnc_conversation_mode = (
            False  # Track if in conversation mode (vs command mode)
        )
        self.tnc_debug_frames = False
        self._original_debug_state = (
            None  # Save original DEBUG state for restoration
        )
        self._tnc_text_buffer = (
            ""  # Buffer for accumulating text across frames
        )

        # APRS message and weather tracking
        # Use existing APRS manager if already created (e.g., by web server)
        # Otherwise create a new one
        if hasattr(self.radio, 'aprs_manager') and self.radio.aprs_manager:
            self.aprs_manager = self.radio.aprs_manager
            # Update retry config from TNC config if it changed
            retry_count = int(self.tnc_config.get("RETRY") or "3")
            retry_fast = int(self.tnc_config.get("RETRY_FAST") or "20")
            retry_slow = int(self.tnc_config.get("RETRY_SLOW") or "600")
            self.aprs_manager.max_retries = retry_count
            self.aprs_manager.retry_fast = retry_fast
            self.aprs_manager.retry_slow = retry_slow
        else:
            mycall = self.tnc_config.get("MYCALL") or "NOCALL"
            retry_count = int(self.tnc_config.get("RETRY") or "3")
            retry_fast = int(self.tnc_config.get("RETRY_FAST") or "20")
            retry_slow = int(self.tnc_config.get("RETRY_SLOW") or "600")
            self.aprs_manager = APRSManager(mycall, max_retries=retry_count,
                                           retry_fast=retry_fast, retry_slow=retry_slow)
            # Attach to radio so tnc_monitor() can access it
            self.radio.aprs_manager = self.aprs_manager

        # Frame history for debugging
        debug_buffer_setting = self.tnc_config.get("DEBUG_BUFFER") or "10"
        if debug_buffer_setting.upper() == "OFF":
            self.frame_history = FrameHistory(buffer_mode=False)
            load_info = self.frame_history.load_from_disk()
            if load_info['loaded']:
                print_info(f"Frame buffer: Simple mode (last 10 frames), loaded {load_info['frame_count']} frames")
                print_info(f"  Starting at frame #{load_info['start_frame']}, next frame will be #{self.frame_history.frame_counter + 1}")
            else:
                print_info("Frame buffer: Simple mode (last 10 frames), starting fresh at frame #1")
        else:
            debug_buffer_mb = int(debug_buffer_setting)
            self.frame_history = FrameHistory(max_size_mb=debug_buffer_mb, buffer_mode=True)
            load_info = self.frame_history.load_from_disk()
            if load_info['loaded']:
                size_kb = load_info['file_size_kb']
                print_info(f"Frame buffer: {debug_buffer_mb} MB buffer, loaded {load_info['frame_count']} frames ({size_kb:.1f} KB)")
                print_info(f"  Starting at frame #{load_info['start_frame']}, next frame will be #{self.frame_history.frame_counter + 1}")
                if load_info['corrupted_frames'] > 0:
                    print_warning(f"  Skipped {load_info['corrupted_frames']} corrupted frames during load")
            else:
                print_info(f"Frame buffer: {debug_buffer_mb} MB buffer, starting fresh at frame #1")

        # Run database migrations (after APRS manager and frame history are initialized)
        from src.migrations import run_startup_migrations
        run_startup_migrations(self.aprs_manager, self)

        # GPS state
        self.gps_position = None  # Current GPS position from radio
        self.gps_locked = False  # GPS lock status

        # Initialize weather station manager
        from src.weather_manager import WeatherStationManager
        self.weather_manager = WeatherStationManager()

        # Configure from saved settings
        backend = self.tnc_config.get("WX_BACKEND")
        address = self.tnc_config.get("WX_ADDRESS")
        port_str = self.tnc_config.get("WX_PORT")
        interval_str = self.tnc_config.get("WX_INTERVAL") or "300"
        enabled = self.tnc_config.get("WX_ENABLE") == "ON"

        port = int(port_str) if port_str else None
        interval = int(interval_str) if interval_str else 300

        self.weather_manager.configure(
            backend=backend if backend else None,
            address=address if address else None,
            port=port,
            enabled=enabled,
            update_interval=interval
        )

        # Configure wind averaging
        average_wind = self.tnc_config.get("WX_AVERAGE_WIND") == "ON"
        self.weather_manager.average_wind = average_wind

        # Load last beacon time from config
        last_beacon_str = self.tnc_config.get("LAST_BEACON")
        if last_beacon_str:
            try:
                self.last_beacon_time = datetime.fromisoformat(last_beacon_str).astimezone(timezone.utc)
                print_debug(f"Loaded last beacon time: {self.last_beacon_time}", level=6)
            except (ValueError, TypeError):
                self.last_beacon_time = None
        else:
            self.last_beacon_time = None

        self.gps_poll_task = None  # Background GPS polling task
        self.gps_consecutive_failures = 0  # Track consecutive GPS failures for auto-recovery
        self.gps_needs_restart = False  # Flag to trigger GPS task restart
        # Attach to radio so frame hooks can access it
        self.radio.cmd_processor = self

        # AX.25 adapter - use shared adapter if available, otherwise create new one
        try:
            if hasattr(self.radio, "shared_ax25") and self.radio.shared_ax25:
                # Use the shared adapter (created in main() for AGWPE compatibility)
                self.ax25 = self.radio.shared_ax25
                print_debug(
                    "CommandProcessor: Using shared AX25Adapter instance",
                    level=6,
                )
            else:
                # Create new adapter (fallback for standalone use)
                self.ax25 = AX25Adapter(
                    self.radio,
                    get_mycall=lambda: self.tnc_config.get("MYCALL"),
                    get_txdelay=lambda: self.tnc_config.get("TXDELAY"),
                )
                print_debug(
                    "CommandProcessor: Created new AX25Adapter instance",
                    level=6,
                )

            # Register callback to display received data
            self.ax25.register_callback(self._tnc_receive_callback)
            try:
                self.radio.register_kiss_callback(self.ax25.handle_incoming)
            except Exception:
                pass
        except Exception as e:
            print_error(f"Failed to initialize AX25Adapter: {e}")
            sys.exit(1)

        # Initialize command handlers
        self.tnc_handler = TNCCommandHandler(self)
        self.beacon_handler = BeaconCommandHandler(self)
        self.weather_handler = WeatherCommandHandler(self)
        self.aprs_console_handler = APRSConsoleCommandHandler(self)
        self.debug_handler = DebugCommandHandler(self)
        self.radio_handler = RadioCommandHandler(self)

    async def _broadcast_mylocation_to_web(self, grid_square: str) -> bool:
        """
        Broadcast MYLOCATION grid square to web UI as GPS position.

        Converts Maidenhead grid square to lat/lon and sends GPS update
        to connected web clients via WebSocket.

        Args:
            grid_square: Maidenhead grid square (e.g., 'FN42pr')

        Returns:
            True if broadcast succeeded, False otherwise
        """
        try:
            lat, lon = maidenhead_to_latlon(grid_square)
            if self.aprs_manager._web_broadcast:
                await self.aprs_manager._web_broadcast('gps_update', {
                    'latitude': lat,
                    'longitude': lon,
                    'altitude': None,
                    'locked': True,
                    'source': 'MYLOCATION'
                })
            return True
        except Exception as e:
            print_debug(f"Failed to broadcast MYLOCATION: {e}", level=6)
            return False

    async def _dispatch_aprs_command(self, args):
        """Dispatch APRS command to handler."""
        await self.aprs_console_handler.aprs(args)

    async def _dispatch_debug_command(self, args):
        """Dispatch DEBUG command to handler."""
        await self.debug_handler.debug(args)

    async def _dispatch_pws_command(self, args):
        """Dispatch PWS (Personal Weather Station) command to handler."""
        await self.weather_handler.pws(args)

    async def _dispatch_radio_command(self, args, _cmd_name=None):
        """Dispatch radio control command to handler."""
        if _cmd_name:
            await self.radio_handler.dispatch(_cmd_name.upper(), args)

    async def _dispatch_tnc_command(self, args):
        """Dispatch TNCSEND command to TNC handler."""
        await self.tnc_handler.tncsend(args)

    async def process(self, line):
        """Process a command line with mode-aware dispatching."""
        parts = line.strip().split()
        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        # Handle mode switching commands
        if cmd == "aprs":
            if not args:
                # Switch to APRS mode
                self.console_mode = "aprs"
                print_info("Switched to APRS mode (APRS commands no longer need 'aprs' prefix)")
                return
            # Fall through to handle "aprs" command with subcommands

        elif cmd == "radio":
            if not args:
                # Switch to radio mode
                if self.serial_mode:
                    print_error("Radio mode not available in serial mode (no radio control)")
                    return
                self.console_mode = "radio"
                print_info("Switched to radio mode (radio commands no longer need 'radio' prefix)")
                return
            # Fall through to handle "radio" prefix in APRS mode

        # Mode-aware command routing
        if self.console_mode == "aprs":
            # In APRS mode:
            # - APRS subcommands work without "aprs" prefix
            # - Radio commands need "radio" prefix (if not in serial mode)

            # Check if it's an APRS subcommand without prefix
            aprs_subcommands = ["message", "msg", "station", "wx", "weather"]
            if cmd in aprs_subcommands:
                # Rewrite as "aprs <subcommand> ..."
                cmd = "aprs"
                args = [parts[0]] + args  # Prepend original command as first arg

            # Handle "radio" prefix for radio commands
            elif cmd == "radio" and args:
                if self.serial_mode:
                    print_error("Radio commands not available in serial mode")
                    return
                # Remove "radio" prefix and dispatch
                cmd = args[0].lower()
                args = args[1:]

        elif self.console_mode == "radio":
            # In radio mode:
            # - Radio commands work without prefix
            # - "aprs" prefix required for APRS commands (handled normally)
            pass

        # Dispatch command
        if cmd in self.commands:
            try:
                await self.commands[cmd](args)
            except Exception as e:
                print_error(f"Command failed: {e}")
                traceback.print_exc()
        else:
            if self.console_mode == "aprs":
                print_error(
                    f"Unknown command: {cmd}. Type 'help' for available commands."
                )
            else:
                print_error(
                    f"Unknown command: {cmd}. Type 'help' for available commands."
                )

    async def show_startup_status(self):
        """Display startup status screen with VFO info."""
        # In serial mode, skip radio status and show APRS mode info
        if self.serial_mode:
            print_header("Console Ready")
            print_pt("")
            print_info(f"Mode: APRS (serial KISS TNC)")
            print_info(f"MYCALL: {self.tnc_config.get('MYCALL')}")
            mylocation = self.tnc_config.get('MYLOCATION')
            if mylocation:
                print_info(f"MYLOCATION: {mylocation}")
                # Broadcast MYLOCATION position to web UI on startup
                await self._broadcast_mylocation_to_web(mylocation)

            print_pt("")
            print_pt(HTML("<gray>Type 'help' for available commands</gray>"))
            print_pt(HTML(f"<gray>Console mode: <b>{self.console_mode}</b> (use APRS commands without prefix)</gray>"))
            print_pt("")
            return

        try:
            # Get radio settings and status
            settings = await self.radio.get_settings()
            status = await self.radio.get_status()
            volume = await self.radio.get_volume()

            if not settings:
                print_error("Unable to read radio settings")
                return

            # Get channel details for both VFOs
            ch_a = await self.radio.read_channel(settings["channel_a"])
            ch_b = await self.radio.read_channel(settings["channel_b"])

            print_header("Radio Status")
            print_pt("")

            # Determine active VFO: prefer `double_channel` (radio's dual-watch
            # mode) when present, otherwise fall back to `vfo_x`.
            mode = settings.get("double_channel", None)
            if mode is not None and mode in (1, 2):
                active_vfo = "A" if mode != 2 else "B"
            else:
                vfo_x = settings.get("vfo_x", None)
                active_vfo = "A" if (vfo_x is None or vfo_x == 0) else "B"
            active_a = "●" if active_vfo == "A" else "○"
            active_b = "●" if active_vfo == "B" else "○"

            if ch_a:
                print_pt(
                    HTML(
                        f"<b>VFO A {active_a}</b>  CH{settings['channel_a']:3d}  {ch_a['tx_freq_mhz']:.4f} MHz  {ch_a['power']:4s}  {ch_a['name']}"
                    )
                )
            else:
                print_pt(
                    HTML(
                        f"<b>VFO A {active_a}</b>  CH{settings['channel_a']:3d}"
                    )
                )

            if ch_b:
                print_pt(
                    HTML(
                        f"<b>VFO B {active_b}</b>  CH{settings['channel_b']:3d}  {ch_b['tx_freq_mhz']:.4f} MHz  {ch_b['power']:4s}  {ch_b['name']}"
                    )
                )
            else:
                print_pt(
                    HTML(
                        f"<b>VFO B {active_b}</b>  CH{settings['channel_b']:3d}"
                    )
                )

            print_pt("")

            # Radio state
            if status:
                power_state = "ON" if status["is_power_on"] else "OFF"
                power_color = "green" if status["is_power_on"] else "red"
                print_pt(
                    HTML(
                        f"Power: <{power_color}>{power_state}</{power_color}>"
                    )
                )

            # Dual watch
            dual_mode = settings.get("double_channel", 0)
            if dual_mode == 1:
                print_pt("Dual Watch: A+B")
            elif dual_mode == 2:
                print_pt("Dual Watch: B+A")
            else:
                print_pt("Dual Watch: Off")

            # Volume and squelch
            squelch = settings.get("squelch_level", 0)
            if volume is not None:
                print_pt(f"Volume: {volume}/15    Squelch: {squelch}/15")
            else:
                print_pt(f"Squelch: {squelch}/15")

            print_pt("")
            print_pt(HTML("<gray>Type 'help' for commands</gray>"))
            print_pt(HTML(f"<gray>Console mode: <b>{self.console_mode}</b> (type 'aprs' to switch to APRS mode)</gray>"))
            print_pt("")

        except Exception as e:
            print_error(f"Failed to read status: {e}")

    async def cmd_help(self, args):
        """Show mode-aware help."""
        print_header(f"Available Commands (Mode: {self.console_mode.upper()})")

        if self.console_mode == "aprs":
            # APRS mode help
            print_pt(HTML("<b>Mode Switching:</b>"))
            if not self.serial_mode:
                print_pt(HTML("  <b>radio</b>             - Switch to radio mode"))
            print_pt("")

            print_pt(HTML("<b>APRS Commands (no prefix needed):</b>"))
            print_pt(HTML("  <b>message read</b>       - Read APRS messages"))
            print_pt(HTML("  <b>message send &lt;call&gt; &lt;text&gt;</b> - Send APRS message"))
            print_pt(HTML("  <b>message clear</b>      - Clear all messages"))
            print_pt(HTML("  <b>station list [N]</b>   - List last N heard stations"))
            print_pt(HTML("  <b>station info &lt;call&gt;</b> - Show station details"))
            print_pt(HTML("  <b>wx list [sort]</b>     - List weather stations"))
            print_pt("")

            if not self.serial_mode:
                print_pt(HTML("<b>Radio Commands:</b>"))
                print_pt(HTML("  <gray>Radio commands require 'radio' prefix in APRS mode</gray>"))
                print_pt(HTML("  <gray>(Type 'radio' to switch modes for direct access)</gray>"))
                print_pt("")

            print_pt(HTML("<b>TNC Configuration:</b>"))
            print_pt(HTML("  <b>tnc</b>               - Enter TNC-2 terminal mode"))
            print_pt(HTML("  <b>tnc display</b>       - Show TNC parameters"))
            print_pt(HTML("  <b>tnc mycall &lt;call&gt;</b> - Set your callsign"))
            print_pt(HTML("  <b>tnc monitor &lt;on|off&gt;</b> - Enable/disable monitoring"))
            print_pt("")

        elif self.console_mode == "radio":
            # Radio mode help
            print_pt(HTML("<b>Mode Switching:</b>"))
            print_pt(HTML("  <b>aprs</b>              - Switch to APRS mode"))
            print_pt("")

            print_pt(HTML("<b>Radio Control:</b>"))
            print_pt(
                HTML("  <b>status</b>            - Show current radio status")
            )
            print_pt(HTML("  <b>health</b>            - Show connection health"))
            print_pt(
                HTML("  <b>notifications</b>     - Check BLE notification status")
            )
            print_pt(
                HTML("  <b>vfo</b>               - Show VFO A/B configuration")
            )
            print_pt(
                HTML(
                    "  <b>setvfo &lt;a|b&gt; &lt;ch&gt;</b>  - Set VFO to channel 1-256"
                )
            )
            print_pt(HTML("  <b>active &lt;a|b&gt;</b>      - Switch active VFO"))
            print_pt(
                HTML(
                    "  <b>dual &lt;off|ab|ba&gt;</b> - Set dual watch (off/A+B/B+A)"
                )
            )
            print_pt(HTML("  <b>scan &lt;on|off&gt;</b>    - Enable/disable scan"))
            print_pt(HTML("  <b>squelch &lt;0-15&gt;</b>   - Set squelch level"))
            print_pt(
                HTML("  <b>volume &lt;0-15&gt;</b>    - Get/set volume level")
            )
            print_pt("")
            print_pt(HTML("<b>Channel Management:</b>"))
            print_pt(
                HTML("  <b>channel &lt;id&gt;</b>     - Show channel details")
            )
            print_pt(HTML("  <b>list [start] [end]</b> - List channels"))
            print_pt(
                HTML(
                    "  <b>power &lt;id&gt; &lt;lvl&gt;</b>  - Set power (low/med/high)"
                )
            )
            print_pt(
                HTML(
                    "  <b>freq &lt;id&gt; &lt;tx&gt; &lt;rx&gt;</b> - Set frequencies"
                )
            )
            print_pt("")
            print_pt(HTML("<b>BSS Settings:</b>"))
            print_pt(
                HTML("  <b>bss</b>                    - Show BSS/APRS settings")
            )
            print_pt(
                HTML(
                    "  <b>setbss &lt;param&gt; &lt;val&gt;</b>  - Set BSS parameter"
                )
            )
            print_pt("")

            print_pt(HTML("<b>APRS Commands:</b>"))
            print_pt(HTML("  <gray>APRS commands require 'aprs' prefix in radio mode</gray>"))
            print_pt(HTML("  <gray>(Type 'aprs' to switch modes for direct access)</gray>"))
            print_pt("")

            print_pt(HTML("<b>TNC Configuration:</b>"))
            print_pt(HTML("  <b>tnc</b>               - Enter TNC terminal mode"))
            print_pt(HTML("  <b>tnc display</b>       - Show TNC parameters"))
            print_pt(HTML("  <b>tnc mycall &lt;call&gt;</b> - Set your callsign"))
            print_pt(HTML("  <b>tnc tncsend &lt;hex&gt;</b> - Send raw hex to TNC"))
            print_pt("")

        # Common commands (both modes)
        print_pt(HTML("<b>Utility:</b>"))
        print_pt(HTML("  <b>dump</b>              - Dump raw settings"))
        print_pt(HTML("  <b>debug</b>             - Toggle debug output"))
        print_pt(HTML("  <b>pws [show|fetch]</b>  - Personal Weather Station"))
        print_pt(HTML("  <b>help</b>              - Show this help"))
        print_pt(HTML("  <b>quit</b> / <b>exit</b>      - Exit application"))
        print_pt("")

        # Show server ports from TNC config
        tnc_port = self.tnc_config.get("TNC_PORT") or "8001"
        agwpe_port = self.tnc_config.get("AGWPE_PORT") or "8000"
        webui_port = self.tnc_config.get("WEBUI_PORT") or "8002"
        print_pt(HTML(f"<b>TNC TCP Bridge:</b> Port {tnc_port} (bidirectional)"))
        print_pt(HTML(f"<b>AGWPE Bridge:</b> Port {agwpe_port}"))
        print_pt(HTML(f"<b>Web UI:</b> Port {webui_port}"))
        print_pt("")


    async def cmd_quit(self, args):
        """Quit the application."""
        print_info("Exiting...")

        # Stop accepting new frames/packets FIRST
        self.radio.running = False

        # Wait for background tasks to finish (tnc_monitor, autosave, gps, heartbeat, etc.)
        # They check radio.running and will exit their loops
        if hasattr(self.radio, 'background_tasks'):
            try:
                # Wait up to 2 seconds for all tasks to finish
                await asyncio.wait_for(
                    asyncio.gather(*self.radio.background_tasks, return_exceptions=True),
                    timeout=2.0
                )
                print_debug("Background tasks stopped", level=2)
            except asyncio.TimeoutError:
                print_debug("Background tasks did not stop in time, proceeding anyway", level=2)
            except Exception as e:
                print_debug(f"Error waiting for background tasks: {e}", level=2)

        # Now close the TNC connection (after tasks have stopped using it)
        if hasattr(self.radio, 'transport') and self.radio.transport:
            try:
                await self.radio.transport.close()
                print_debug("TNC connection closed", level=2)
            except Exception as e:
                print_debug(f"Error closing TNC: {e}", level=2)

        # Now save - TNC closed, no more frames being added
        save_tasks = []

        # Save APRS station database
        save_tasks.append(self.aprs_manager.save_database_async())

        # Save frame buffer (now safe - TNC closed)
        if hasattr(self, 'frame_history'):
            save_tasks.append(self.frame_history.save_to_disk_async())

        # Wait for both saves to complete (runs concurrently)
        print_info("Saving database and frame buffer...")
        results = await asyncio.gather(*save_tasks, return_exceptions=True)

        # Report results
        if len(results) >= 1 and isinstance(results[0], int) and results[0] > 0:
            print_info(f"Saved {results[0]} station(s) to APRS database")

    async def cmd_tnc(self, args, auto_connect=None):
        """TNC commands and terminal mode.

        Usage:
            tnc                     - Enter TNC terminal mode
            tnc <command> [args]    - Execute TNC command from any mode

        Examples:
            tnc display             - Show TNC parameters
            tnc mycall N0CALL       - Set your callsign
            tnc monitor on          - Enable packet monitoring
        """
        # If args provided, dispatch to TNC handler for command execution
        if args:
            await self.tnc_handler.dispatch(args[0].upper(), args[1:])
            return

        # No args - enter TNC terminal mode
        print_header("TNC Terminal Mode")
        print_pt(
            HTML(
                f"<gray>Current MYCALL: <b>{self.tnc_config.get('MYCALL')}</b></gray>"
            )
        )
        print_pt(
            HTML(
                "<gray>Use '~~~' to toggle between conversation and command mode</gray>"
            )
        )
        print_pt("")

        self.tnc_mode = True

        # Flag to trigger auto-connect after setup
        do_auto_connect = auto_connect
        # Disable tnc_monitor display - AX25Adapter callback handles everything
        self.radio.tnc_mode_active = True
        # Add keybinding for Ctrl+] to toggle between conversation and command mode

        kb = KeyBindings()

        @kb.add("c-]")
        def _escape_tnc(event):
            # Toggle conversation mode and exit prompt to refresh
            print_debug("Ctrl+] keybinding triggered", level=6)
            self.tnc_conversation_mode = not self.tnc_conversation_mode
            # Exit with special marker to trigger mode change message
            event.app.exit(result="<<<TOGGLE_MODE>>>")

        @kb.add("?")
        def _show_tnc_help(event):
            """Show context-sensitive help when '?' is pressed (IOS-style)."""
            buffer = event.current_buffer
            text_before_cursor = buffer.text[: buffer.cursor_position]

            # IOS-style context help: "command ?" shows options for next token
            # If text ends with space, show completions. Otherwise insert "?" literally
            if text_before_cursor.strip() and not text_before_cursor.endswith(' '):
                # Not asking for help - insert ? as regular character
                buffer.insert_text('?')
                return

            # Get completions at current position
            document = Document(
                text=buffer.text, cursor_position=buffer.cursor_position
            )
            completions = list(
                tnc_completer.get_completions(document, CompleteEvent())
            )

            # Display available options
            if completions:
                print_pt("\n<Available options>")
                for comp in completions:
                    if comp.display_meta:
                        # Convert FormattedText to plain string
                        meta_text = (
                            to_plain_text(comp.display_meta)
                            if hasattr(comp.display_meta, "__iter__")
                            else str(comp.display_meta)
                        )
                        print_pt(f"  {comp.text:<15} {meta_text}")
                    else:
                        print_pt(f"  {comp.text}")
                print_pt("")  # Blank line after help
            else:
                # No completions - show general TNC help
                print_pt(
                    "\n<TNC Commands: CONNECT, DISCONNECT, CONVERSE, STATUS, K/UNPROTO, MONITOR, DIGIPEATER, DISPLAY, RESET>"
                )
                print_pt("")

            # Redisplay the prompt with current text intact
            # This is done automatically by not calling validate_and_handle()

        # Create TNC completer for command mode
        tnc_completer = TNCCompleter()

        session = PromptSession(
            completer=tnc_completer,
            complete_while_typing=False,
            key_bindings=kb,
        )
        # initialize pyax25 AX25 instance for TNC mode
        try:
            if getattr(self, "ax25", None) is not None:
                try:
                    self.ax25.init_ax25()
                except Exception as e:
                    print_error(
                        f"Failed to initialize pyax25 AX25 instance: {e}"
                    )
                    return
        except Exception:
            pass
        # Apply DEBUGFRAMES setting from TNC config and register debug callback
        try:
            # Save original DEBUG_LEVEL state before entering TNC mode
            if self._original_debug_state is None:
                self._original_debug_state = constants.DEBUG_LEVEL

            df = (self.tnc_config.get("DEBUGFRAMES") or "").upper()
            self.tnc_debug_frames = df in ("ON", "1", "YES", "TRUE")

            # Set DEBUG_LEVEL based on DEBUGFRAMES and console debug mode
            # If DEBUGFRAMES is ON, enable at least level 1 (frame debugging)
            # If console debug is already higher, keep the higher level
            if self.tnc_debug_frames:
                # Enable frame debugging (level 1) at minimum
                if constants.DEBUG_LEVEL < 1:
                    constants.DEBUG_LEVEL = 1
            else:
                # Restore original level
                constants.DEBUG_LEVEL = self._original_debug_state

            if getattr(self, "ax25", None) is not None:
                if self.tnc_debug_frames:
                    try:
                        self.ax25.register_frame_debug(
                            self._tnc_frame_debug_cb
                        )
                    except Exception:
                        pass
                else:
                    try:
                        self.ax25.register_frame_debug(None)
                    except Exception:
                        pass
        except Exception:
            pass

        with patch_stdout():
            while self.tnc_mode and self.radio.running:
                try:
                    # Handle auto-connect on first iteration
                    if do_auto_connect:
                        await asyncio.sleep(
                            0.5
                        )  # Give TNC mode time to initialize
                        print_info(f"Auto-connecting to {do_auto_connect}...")
                        await self._process_tnc_command(
                            f"connect {do_auto_connect}"
                        )
                        do_auto_connect = None  # Only do this once
                        continue  # Skip to next iteration to show connected prompt

                    # Sync tnc_connected_to with actual link state
                    # If adapter reports link is down, clear our connected state
                    if self.tnc_connected_to and not getattr(
                        self.radio, "tnc_link_established", True
                    ):
                        print_pt("")  # New line to clear prompt
                        print_info(
                            f"*** DISCONNECTED from {self.tnc_connected_to}"
                        )
                        self.tnc_connected_to = None
                        self.tnc_conversation_mode = (
                            False  # Exit conversation mode on disconnect
                        )
                        continue  # Restart loop immediately to show updated prompt

                    # Show different prompt based on connection state and conversation mode
                    if self.tnc_connected_to:
                        if self.tnc_conversation_mode:
                            # No prompt in conversation mode - let BBS/node prompts be visible
                            prompt_text = ""
                        else:
                            prompt_text = f"<b><cyan>TNC({self.tnc_connected_to}:CMD)&gt;</cyan></b> "
                    else:
                        if self.tnc_conversation_mode:
                            prompt_text = (
                                "<b><yellow>TNC(CONV)&gt;</yellow></b> "
                            )
                        else:
                            prompt_text = "<b><cyan>TNC&gt;</cyan></b> "

                    # Create prompt task and disconnect watcher task
                    prompt_task = asyncio.create_task(
                        session.prompt_async(
                            HTML(prompt_text), key_bindings=kb
                        )
                    )

                    async def watch_disconnect():
                        """Monitor connection state and return when disconnected."""
                        initial_connection = self.tnc_connected_to
                        if not initial_connection:
                            # Not connected - wait forever (will be cancelled by prompt)
                            await asyncio.Event().wait()
                            return False
                        # Connected - monitor for disconnect
                        while self.tnc_connected_to == initial_connection:
                            await asyncio.sleep(0.1)  # Check every 100ms
                            if not getattr(
                                self.radio, "tnc_link_established", True
                            ):
                                return True  # Disconnected
                        return False  # Connection changed or cleared

                    watcher_task = asyncio.create_task(watch_disconnect())

                    # Wait for either prompt completion or disconnect detection
                    done, pending = await asyncio.wait(
                        {prompt_task, watcher_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Cancel the pending task
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                    # Check which task completed
                    if watcher_task in done and watcher_task.result():
                        # Disconnected while waiting for input
                        print_pt("")  # New line
                        print_info(
                            f"*** DISCONNECTED from {self.tnc_connected_to}"
                        )
                        self.tnc_connected_to = None
                        self.tnc_conversation_mode = (
                            False  # Exit conversation mode on disconnect
                        )
                        continue  # Show updated prompt

                    # Prompt completed normally, get the result
                    line = prompt_task.result()

                    # Debug: show what we received (only in debug mode)
                    if line and constants.DEBUG_LEVEL >= 1:
                        print_debug(f"Received input: {repr(line)}", level=6)

                    if not line:
                        continue

                    # Check for mode toggle marker from Ctrl+] keybinding
                    if line == "<<<TOGGLE_MODE>>>":
                        # Mode was already toggled in keybinding, just show feedback
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Check for text-based escape sequence: ~~~
                    if line.strip() == "~~~":
                        print_debug("Escape sequence '~~~' detected", level=6)
                        self.tnc_conversation_mode = (
                            not self.tnc_conversation_mode
                        )
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Check for escape sequence (fallback if keybinding doesn't work)
                    if line == "\x1d":  # ^]
                        print_debug(
                            "Ctrl+] received as input character (keybinding fallback)",
                            level=6,
                        )
                        self.tnc_conversation_mode = (
                            not self.tnc_conversation_mode
                        )
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Process based on conversation mode
                    if self.tnc_conversation_mode:
                        # In conversation mode - send text (either connected or UI frames)
                        await self._tnc_send_text(line)
                    else:
                        # In command mode - process as TNC command
                        await self._process_tnc_command(line)

                except EOFError:
                    print_pt("")
                    break
                except KeyboardInterrupt:
                    print_pt("")
                    print_info("Interrupt received - cleaning up...")
                    if self.tnc_connected_to:
                        try:
                            await self._tnc_disconnect()
                        except Exception:
                            pass
                    # Exit TNC mode on keyboard interrupt
                    break
                except Exception as e:
                    print_error(f"TNC error: {e}")

        self.tnc_mode = False
        # Re-enable tnc_monitor display for regular console mode
        self.radio.tnc_mode_active = False
        # Restore original DEBUG_LEVEL when exiting TNC mode
        if self._original_debug_state is not None:
            constants.DEBUG_LEVEL = self._original_debug_state
            self._original_debug_state = None
        # close pyax25 AX25 instance when leaving TNC mode
        try:
            if getattr(self, "ax25", None) is not None:
                try:
                    await self.ax25.close_ax25()
                except Exception as e:
                    print_error(f"Error closing AX25 adapter: {e}")
        except Exception:
            pass
        print_info("Exited TNC mode")
        print_pt("")

    def _tnc_frame_debug_cb(self, direction, kiss_frame: bytes):
        try:
            if not self.tnc_debug_frames:
                return
            if direction == "tx":
                print_debug(
                    f"TNC TX KISS ({len(kiss_frame)} bytes): {kiss_frame.hex()}",
                    level=4,
                )
                try:
                    ascii_repr = "".join(
                        chr(b) if 32 <= b <= 126 else "." for b in kiss_frame
                    )
                    print_debug(f"TNC TX ASCII: {ascii_repr}", level=4)
                except Exception:
                    pass
            elif direction == "rx":
                print_debug(
                    f"TNC RX KISS ({len(kiss_frame)} bytes): {kiss_frame.hex()}",
                    level=4,
                )
                try:
                    ascii_repr = "".join(
                        chr(b) if 32 <= b <= 126 else "." for b in kiss_frame
                    )
                    print_debug(f"TNC RX ASCII: {ascii_repr}", level=4)
                except Exception:
                    pass
        except Exception:
            pass

    async def _process_tnc_command(self, line):
        """Process TNC command using handler dispatch."""
        parts = line.strip().split()
        if not parts:
            return

        cmd = parts[0].upper()
        args = parts[1:]

        # Try TNC protocol commands
        if await self.tnc_handler.dispatch(cmd, args):
            return

        # Try beacon commands
        if await self.beacon_handler.dispatch(cmd, args):
            return

        # Try weather commands
        if await self.weather_handler.dispatch(cmd, args):
            return

        # Handle generic TNC-2 parameters
        if cmd in self.tnc_config.settings:
            if not args:
                # Display current value
                print_pt(f"{cmd}: {self.tnc_config.get(cmd)}")
            else:
                # Set new value
                value = " ".join(args)
                self.tnc_config.set(cmd, value)
                print_info(f"{cmd} set to {value}")
                # Apply DEBUGFRAMES immediately if changed
                try:
                    if cmd == "DEBUGFRAMES":
                        v = value.upper()
                        self.tnc_debug_frames = v in (
                            "ON",
                            "1",
                            "YES",
                            "TRUE",
                        )

                        # Enable/disable DEBUG_LEVEL based on DEBUGFRAMES
                        if self.tnc_debug_frames:
                            constants.DEBUG_LEVEL = 1  # Frame debugging
                        else:
                            constants.DEBUG_LEVEL = 0  # No debugging

                        if getattr(self, "ax25", None) is not None:
                            if self.tnc_debug_frames:
                                self.ax25.register_frame_debug(
                                    self._tnc_frame_debug_cb
                                )
                            else:
                                self.ax25.register_frame_debug(None)
                except Exception:
                    pass
        else:
            print_error(f"Unknown TNC command: {cmd}")
            print_pt("Type DISPLAY for available parameters")

    async def _tnc_connect(self, callsign, path=None):
        """Connect to a station."""
        if path is None:
            path = []

        if self.tnc_connected_to:
            print_error(f"Already connected to {self.tnc_connected_to}")
            print_pt("DISCONNECT first")
            return

        mycall = self.tnc_config.get("MYCALL")
        if mycall == "NOCALL":
            print_error("Set MYCALL first")
            return

        print_info(f"Connecting to {callsign}...")
        if path:
            print_pt(f"Via: {', '.join(path)}")

        try:
            # If AX25 adapter exists, use it exclusively — no fallback.
            if getattr(self, "ax25", None) is not None:
                # Use 3 second timeout per attempt, 5 retries = 15 seconds total
                ok = await self.ax25.connect(
                    callsign, path or [], timeout=3.0, max_retries=5
                )
                if ok:
                    self.tnc_connected_to = callsign
                    self.tnc_conversation_mode = (
                        True  # Automatically enter conversation mode
                    )
                    self._tnc_text_buffer = (
                        ""  # Clear text buffer for new connection
                    )
                    print_info(f"*** LINK ESTABLISHED with {callsign}")
                    print_pt(
                        HTML(
                            "<gray>Type text to send, type '~~~' for command mode</gray>"
                        )
                    )
                else:
                    print_error("Connect failed: no response after 5 attempts")
                return

            # No AX25 adapter — continue with UI fallback behavior


            frame = build_ui_kiss_frame(
                source=mycall,
                dest=callsign,
                path=path or [],
                info=b"*** CONNECTED\r",
            )

            await self.radio.send_tnc_data(frame)

            self.tnc_connected_to = callsign
            self.tnc_conversation_mode = (
                True  # Automatically enter conversation mode
            )
            print_info(f"*** CONNECTED to {callsign}")
            print_pt(
                HTML(
                    "<gray>Type text to send, type '~~~' for command mode</gray>"
                )
            )

        except Exception as e:
            print_error(f"Connect failed: {e}")

    async def _tnc_disconnect(self):
        """Disconnect from current station."""
        if not self.tnc_connected_to:
            print_warning("Not connected")
            return

        callsign = self.tnc_connected_to
        mycall = self.tnc_config.get("MYCALL")

        try:
            # If AX25 adapter exists, use it exclusively
            if getattr(self, "ax25", None) is not None:
                await self.ax25.disconnect()
                print_info(f"*** LINK TEARDOWN requested for {callsign}")
            else:
                # Send disconnect message as UI fallback
    

                frame = build_ui_kiss_frame(
                    source=mycall,
                    dest=callsign,
                    path=[],
                    info=b"*** DISCONNECTED\r",
                )
                await self.radio.send_tnc_data(frame)

                print_info(f"*** DISCONNECTED from {callsign}")
            # Clear link-layer state if set
            try:
                self.radio.tnc_link_established = False
                self.radio.tnc_connected_callsign = None
                self.radio.tnc_pending_connect = None
                self.radio.tnc_connect_event.clear()
            except Exception:
                pass
            self.tnc_connected_to = None
            self.tnc_conversation_mode = (
                False  # Exit conversation mode on disconnect
            )
            self._tnc_text_buffer = ""  # Clear text buffer on disconnect

        except Exception as e:
            print_error(f"Disconnect failed: {e}")
            self.tnc_connected_to = None
            self.tnc_conversation_mode = (
                False  # Exit conversation mode on disconnect
            )
            self._tnc_text_buffer = ""  # Clear text buffer on disconnect

    async def _tnc_send_text(self, text):
        """Send text to connected station or as UI frame if disconnected."""
        mycall = self.tnc_config.get("MYCALL")

        # Handle disconnected conversation mode - send UI frames
        if not self.tnc_connected_to:
            # Parse UNPROTO setting: "DEST VIA PATH1,PATH2"
            unproto = self.tnc_config.get("UNPROTO") or "CQ"
            parts = unproto.split()
            dest = parts[0] if parts else "CQ"
            path = []
            if len(parts) > 2 and parts[1].upper() == "VIA":
                path = [p.strip() for p in " ".join(parts[2:]).split(",")]

            try:
    

                payload = (text + "\r").encode("ascii", errors="replace")
                kiss_frame = build_ui_kiss_frame(mycall, dest, path, payload)
                await self.radio.send_tnc_data(kiss_frame)
                print_pt(HTML(f"<green>&gt;</green> {text}"))
            except Exception as e:
                print_error(f"Send failed: {e}")
            return

        try:
            # Build UI frame with text (append CR to follow TNC-2 line convention)
            payload = (text + "\r").encode("ascii", errors="replace")

            # If AX25Adapter exists, use it exclusively (no fallback)
            if getattr(self, "ax25", None) is not None:
                ok = await self.ax25.send_info(
                    mycall, self.tnc_connected_to, [], payload
                )
                if not ok:
                    print_error(
                        "Send failed: link not established or pyax25 send error"
                    )
                    return
            else:
                # Fallback to existing radio behavior when adapter is not present
                if getattr(self.radio, "tnc_link_established", False):
                    # Use RadioController helper to send linked I-frame (handles queuing)
                    await self.radio.send_linked_info(
                        mycall, self.tnc_connected_to, [], payload
                    )
                else:
        

                    kiss_frame = build_ui_kiss_frame(
                        mycall, self.tnc_connected_to, [], payload
                    )
                    await self.radio.send_tnc_data(kiss_frame)

            # Echo locally
            print_pt(HTML(f"<green>&gt;</green> {text}"))

        except Exception as e:
            print_error(f"Send failed: {e}")

    def _tnc_receive_callback(self, parsed_frame):
        """Callback for received AX.25 frames from the adapter.

        Args:
            parsed_frame: Dict with keys: src, dst, path, pid, info
        """
        try:
            # Only display if in TNC mode
            if not self.tnc_mode:
                return

            src = parsed_frame.get("src", "")
            dst = parsed_frame.get("dst", "")
            info = parsed_frame.get("info", b"")

            # If connected and this is from our connected station, display the data
            if self.tnc_connected_to and src == self.tnc_connected_to and info:
                try:
                    # Decode text (keep \r as-is for now to properly handle line continuation)
                    text = info.decode("ascii", errors="replace")

                    # Add to buffer
                    self._tnc_text_buffer += text

                    # Split by \r to find complete lines
                    lines = self._tnc_text_buffer.split("\r")

                    # Last element is incomplete (no \r at end), keep it in buffer
                    self._tnc_text_buffer = lines[-1]

                    # Display all complete lines (all but the last)
                    for line in lines[:-1]:
                        if line:  # Only display non-empty lines
                            print_pt(line)
                except Exception:
                    pass
            # If MONITOR is ON, display all frames
            elif self.tnc_config.get("MONITOR") == "ON":
                try:
                    if info:
                        # Convert \r to \n for proper display
                        text = (
                            info.decode("ascii", errors="replace")
                            .replace("\r", "\n")
                            .rstrip("\n")
                        )
                        if text:
                            header = (
                                f"{src}>{dst}"
                                if src and dst
                                else (src or dst or "")
                            )
                            path = parsed_frame.get("path", [])
                            if path:
                                header += "," + ",".join(path)
                            print_pt(HTML(f"<gray>[{header}] {text}</gray>"))
                except Exception:
                    pass
        except Exception as e:
            if constants.DEBUG:
                print_debug(f"_tnc_receive_callback error: {e}")

    async def _tnc_status(self):
        """Show TNC status."""
        mycall = self.tnc_config.get("MYCALL")
        myalias = self.tnc_config.get("MYALIAS")

        print_pt(HTML(f"<b>MYCALL:</b> {mycall}"))
        if myalias:
            print_pt(HTML(f"<b>MYALIAS:</b> {myalias}"))

        print_pt(HTML(f"<b>UNPROTO:</b> {self.tnc_config.get('UNPROTO')}"))
        print_pt(HTML(f"<b>MONITOR:</b> {self.tnc_config.get('MONITOR')}"))

        if self.tnc_connected_to:
            print_pt(HTML(f"<b>Connected to:</b> {self.tnc_connected_to}"))
        else:
            print_pt(HTML("<gray>Not connected</gray>"))

        # Show internal state for debugging
        if getattr(self, "ax25", None) is not None:
            tx_worker_status = (
                "running"
                if (self.ax25._tx_task and not self.ax25._tx_task.done())
                else "STOPPED"
            )
            tx_queue_len = len(self.ax25._tx_queue)
            print_pt(HTML(f"<b>TX Worker:</b> {tx_worker_status}"))
            print_pt(HTML(f"<b>TX Queue:</b> {tx_queue_len} frame(s)"))
            print_pt(
                HTML(f"<b>N(S)/N(R):</b> {self.ax25._ns}/{self.ax25._nr}")
            )

    async def gps_poll_and_beacon_task(self):
        """Background task to poll GPS and send beacons when enabled."""

        while self.radio.running and not self.gps_needs_restart:
            try:
                # Poll GPS every 5 seconds
                await asyncio.sleep(5)

                # Check GPS lock
                gps_locked = await self.radio.check_gps_lock()

                # Try getting position anyway (for debugging - lock check may be inaccurate)
                position = await self.radio.get_gps_position()

                if position:
                    # We got valid position data - update lock status
                    self.gps_position = position
                    self.gps_locked = True  # Override lock check if we got valid data
                    self.gps_consecutive_failures = 0  # Reset failure counter on success
                    print_debug(f"GPS: {position['latitude']:.6f}, {position['longitude']:.6f} (lock_check={gps_locked})", level=6)

                    # Broadcast position update to web clients
                    if self.aprs_manager._web_broadcast:
                        await self.aprs_manager._web_broadcast('gps_update', {
                            'latitude': position['latitude'],
                            'longitude': position['longitude'],
                            'altitude': position.get('altitude'),
                            'locked': True
                        })

                    # Check if beacon is enabled and due
                    if self.tnc_config.get("BEACON") == "ON":
                        beacon_interval = int(self.tnc_config.get("BEACON_INTERVAL") or "10")

                        # Check if it's time to beacon
                        now = datetime.now(timezone.utc)
                        should_beacon = False

                        if self.last_beacon_time is None:
                            should_beacon = True  # First beacon
                        else:
                            elapsed = (now - self.last_beacon_time).total_seconds()
                            if elapsed >= (beacon_interval * 60):
                                should_beacon = True

                        if should_beacon:
                            await self._send_position_beacon(position)
                else:
                    # No GPS position data
                    self.gps_position = None
                    self.gps_locked = False
                    self.gps_consecutive_failures += 1
                    print_debug(f"GPS: No position data (lock_check={gps_locked}, failures={self.gps_consecutive_failures})", level=6)

                    # Auto-recovery: Restart GPS task after 3 consecutive failures
                    # This recovers from GPS getting stuck returning error responses
                    if self.gps_consecutive_failures == 3:
                        print_warning("GPS auto-recovery: Restarting GPS task after 3 consecutive failures")
                        self.gps_needs_restart = True
                        # Break out of GPS polling loop - gps_monitor will restart us
                        break

                    # Check if beacon is enabled with manual location (MYLOCATION)
                    if self.tnc_config.get("BEACON") == "ON" and self.tnc_config.get("MYLOCATION"):
                        beacon_interval = int(self.tnc_config.get("BEACON_INTERVAL") or "10")

                        # Check if it's time to beacon
                        now = datetime.now(timezone.utc)
                        should_beacon = False

                        if self.last_beacon_time is None:
                            should_beacon = True  # First beacon
                        else:
                            elapsed = (now - self.last_beacon_time).total_seconds()
                            if elapsed >= (beacon_interval * 60):
                                should_beacon = True

                        if should_beacon:
                            await self._send_position_beacon(None)  # Use MYLOCATION

            except Exception as e:
                print_error(f"GPS poll task error: {e}")
                await asyncio.sleep(10)  # Back off on error

    async def _send_position_beacon(self, position=None):
        """Send APRS position beacon.

        Args:
            position: GPS position dict with latitude, longitude, altitude, etc.
                     If None, will use MYLOCATION grid square if configured.
        """

        try:
            # Get beacon settings
            mycall = self.tnc_config.get("MYCALL")
            symbol = self.tnc_config.get("BEACON_SYMBOL") or "/["
            comment = self.tnc_config.get("BEACON_COMMENT") or ""
            path_str = self.tnc_config.get("BEACON_PATH") or "WIDE1-1"

            # Parse path
            path = [p.strip() for p in path_str.split(",")]

            # Determine position source
            lat = None
            lon = None
            alt = None
            source = None

            if position:
                # Use GPS position
                lat = position['latitude']
                lon = position['longitude']
                alt = position.get('altitude')
                source = "GPS"
            else:
                # Try manual location (Maidenhead grid square)
                mylocation = self.tnc_config.get("MYLOCATION")
                if mylocation:
                    try:
                        lat, lon = maidenhead_to_latlon(mylocation)
                        source = f"Grid {mylocation.upper()}"
                    except ValueError as e:
                        print_error(f"Invalid MYLOCATION '{mylocation}': {e}")
                        return

            if lat is None or lon is None:
                print_error("No position available (GPS unavailable and MYLOCATION not set)")
                return

            # Convert to APRS lat/lon format (DDMM.HH N/S, DDDMM.HH E/W)
            lat_deg = int(abs(lat))
            lat_min = (abs(lat) - lat_deg) * 60
            lat_dir = 'N' if lat >= 0 else 'S'
            lat_str = f"{lat_deg:02d}{lat_min:05.2f}{lat_dir}"

            lon_deg = int(abs(lon))
            lon_min = (abs(lon) - lon_deg) * 60
            lon_dir = 'E' if lon >= 0 else 'W'
            lon_str = f"{lon_deg:03d}{lon_min:05.2f}{lon_dir}"

            # Symbol table and code
            symbol_table = symbol[0] if len(symbol) >= 1 else '/'
            symbol_code = symbol[1] if len(symbol) >= 2 else '['

            # Check if weather data is available
            wx_string = ""
            wx_source = None
            if hasattr(self, 'weather_manager') and self.weather_manager.enabled:
                # Get beacon interval for wind averaging
                beacon_interval_min = int(self.tnc_config.get("BEACON_INTERVAL") or "10")
                beacon_interval_sec = beacon_interval_min * 60

                # Get weather data with wind averaging over beacon interval
                wx_data = self.weather_manager.get_beacon_weather(beacon_interval_sec)
                if wx_data:
                    # Format weather data in APRS Complete Weather Report format
                    # Format: _DIR/SPDgGUSTtTEMPrRAINpRAIN24PhHUMbPRESSURE
                    # where _ is the weather symbol (underscore)
                    wx_parts = []

                    # Wind direction (3 digits, degrees)
                    if wx_data.wind_direction is not None:
                        wx_parts.append(f"{int(wx_data.wind_direction):03d}")
                    else:
                        wx_parts.append("...")

                    wx_parts.append("/")

                    # Wind speed (3 digits, mph)
                    if wx_data.wind_speed is not None:
                        wx_parts.append(f"{int(wx_data.wind_speed):03d}")
                    else:
                        wx_parts.append("...")

                    # Wind gust (g = gust, 3 digits, mph)
                    if wx_data.wind_gust is not None:
                        wx_parts.append(f"g{int(wx_data.wind_gust):03d}")

                    # Temperature (t = temp, 3 digits, °F, can be negative)
                    if wx_data.temperature_outdoor is not None:
                        temp_int = int(wx_data.temperature_outdoor)
                        if temp_int < 0:
                            # Negative temps: -01 to -99
                            wx_parts.append(f"t{temp_int:03d}")
                        else:
                            wx_parts.append(f"t{temp_int:03d}")

                    # Rain last hour (r = rain 1h, 3 digits, hundredths of inch)
                    if wx_data.rain_hourly is not None:
                        rain_hundredths = int(wx_data.rain_hourly * 100)
                        wx_parts.append(f"r{rain_hundredths:03d}")

                    # Rain last 24h (p = rain 24h, 3 digits, hundredths of inch)
                    if wx_data.rain_daily is not None:
                        rain_hundredths = int(wx_data.rain_daily * 100)
                        wx_parts.append(f"p{rain_hundredths:03d}")

                    # Rain since midnight (P = rain midnight, 3 digits, hundredths of inch)
                    if wx_data.rain_event is not None:
                        rain_hundredths = int(wx_data.rain_event * 100)
                        wx_parts.append(f"P{rain_hundredths:03d}")

                    # Humidity (h = humidity, 2 digits, %, 00 = 100%)
                    if wx_data.humidity_outdoor is not None:
                        humidity = wx_data.humidity_outdoor
                        if humidity == 100:
                            wx_parts.append("h00")
                        else:
                            wx_parts.append(f"h{humidity:02d}")

                    # Barometric pressure (b = pressure, 5 digits, tenths of mbar)
                    if wx_data.pressure_relative is not None:
                        pressure_tenths = int(wx_data.pressure_relative * 10)
                        wx_parts.append(f"b{pressure_tenths:05d}")

                    wx_string = "".join(wx_parts)
                    wx_source = "wx"
                    symbol_code = "_"  # Use weather symbol

            # Build position report (! = position without timestamp)
            # Format: !DDMM.HHN/DDDMM.HHW_WEATHER/A=ALTCOMMENT
            info = f"!{lat_str}/{lon_str}{symbol_code}"

            # Add weather data if available
            if wx_string:
                info += wx_string

            # Add altitude if available (format: /A=XXXXXX feet)
            if alt is not None:
                alt_feet = int(alt * 3.28084)  # Convert meters to feet
                info += f"/A={alt_feet:06d}"

            if comment:
                info += comment

            # Send via APRS
            await self.radio.send_aprs(mycall, info, to_call="APFSYC", path=path)

            # Update timestamp (both in-memory and persisted to config)
            now = datetime.now(timezone.utc)
            self.last_beacon_time = now
            self.tnc_config.set("LAST_BEACON", now.isoformat())

            # Show beacon info
            if wx_source:
                print_info(f"📡 Beacon sent ({source} + weather): {lat:.6f}, {lon:.6f}")
            else:
                print_info(f"📡 Beacon sent ({source}): {lat:.6f}, {lon:.6f}")

        except Exception as e:
            print_error(f"Failed to send position beacon: {e}")

    async def _send_aprs_message(self, to_call: str, message_text: str):
        """Send an APRS message with automatic tracking and retry.

        Args:
            to_call: Destination callsign
            message_text: Message content (max 67 characters)

        This method:
        1. Generates a unique message ID
        2. Formats the APRS message packet
        3. Transmits via radio
        4. Adds to APRS manager for tracking/retry
        """
        try:
            # Generate message ID (1-5 alphanumeric characters)
            message_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))

            # Format APRS message: :CALLSIGN :message{ID
            # Pad callsign to 9 characters
            to_padded = to_call.upper().ljust(9)
            info = f":{to_padded}:{message_text}{{{message_id}"

            # Get my callsign
            mycall = self.tnc_config.get("MYCALL") or "NOCALL"

            # Send via radio
            await self.radio.send_aprs(mycall, info, to_call="APFSYC", path=None)

            # Track in APRS manager for retry/ack handling
            self.aprs_manager.add_sent_message(to_call.upper(), message_text, message_id)

            print_info(f"📤 Message sent to {to_call}: {message_text}")

        except Exception as e:
            print_error(f"Failed to send APRS message: {e}")

    async def _send_aprs_ack(self, to_call: str, message_id: str):
        """Send an APRS acknowledgment for a received message.

        Args:
            to_call: Callsign to send ACK to
            message_id: Message ID being acknowledged
        """
        try:
            # Format APRS ACK: :CALLSIGN :ack{ID
            to_padded = to_call.upper().ljust(9)
            info = f":{to_padded}:ack{message_id}"

            # Get my callsign
            mycall = self.tnc_config.get("MYCALL") or "NOCALL"

            # Send via radio
            await self.radio.send_aprs(mycall, info, to_call="APFSYC", path=None)

            print_debug(f"📥 ACK sent to {to_call} for message {message_id}", level=5)

        except Exception as e:
            print_error(f"Failed to send APRS ACK: {e}")

