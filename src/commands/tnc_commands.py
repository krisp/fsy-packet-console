"""
TNC-2 protocol command handlers.

Handles core TNC-2 commands for AX.25 connection management,
configuration, and protocol parameters.
"""

from .base import CommandHandler, command
from src.utils import print_pt, print_info, print_error, print_debug
from prompt_toolkit import HTML
import asyncio


class TNCCommandHandler(CommandHandler):
    """Handles TNC-2 protocol commands."""

    def __init__(self, cmd_processor):
        """
        Initialize TNC command handler.

        Args:
            cmd_processor: Reference to main CommandProcessor instance
        """
        self.cmd_processor = cmd_processor
        self.tnc_config = cmd_processor.tnc_config
        self.ax25 = cmd_processor.ax25
        self.radio = cmd_processor.radio
        super().__init__()

    @command("CONNECT", "C",
             help_text="Connect to remote station",
             usage="CONNECT <callsign> [via <path>]",
             category="connection")
    async def connect(self, args):
        """Establish AX.25 connection to remote station."""
        if not args:
            print_error("Usage: CONNECT <callsign> [via <path>]")
            return

        # Parse callsign and optional via path
        callsign = args[0].upper()
        path = []
        if len(args) > 1 and args[1].upper() == "VIA":
            path = [p.upper() for p in args[2:]]

        await self.cmd_processor._tnc_connect(callsign, path)

    @command("DISCONNECT", "D",
             help_text="Disconnect from remote station",
             category="connection")
    async def disconnect(self, args):
        """Disconnect current AX.25 connection."""
        await self.cmd_processor._tnc_disconnect()

    @command("CONV", "CONVERSE",
             help_text="Enter conversation mode",
             category="connection")
    async def converse(self, args):
        """Enter conversation mode for text transmission."""
        # Enter conversation mode
        self.cmd_processor.tnc_conversation_mode = True
        if self.cmd_processor.tnc_connected_to:
            print_info(f"Entering conversation mode with {self.cmd_processor.tnc_connected_to}")
            print_pt(HTML("<gray>Type text to send, type '~~~' to return to command mode</gray>"))
        else:
            print_info("Entering conversation mode - will send UI frames to UNPROTO address")
            unproto = self.tnc_config.get("UNPROTO")
            print_pt(HTML(f"<gray>UNPROTO: {unproto}</gray>"))
            print_pt(HTML("<gray>Type text to send, type '~~~' to return to command mode</gray>"))

    @command("MYCALL",
             help_text="Display or set station callsign",
             usage="MYCALL [callsign]",
             category="config")
    async def mycall(self, args):
        """Display or set station callsign."""
        if not args:
            print_pt(f"MYCALL: {self.tnc_config.get('MYCALL')}")
            return
        callsign = args[0].upper()
        self.tnc_config.set("MYCALL", callsign)
        print_info(f"MYCALL set to {callsign}")

    @command("MYALIAS",
             help_text="Display or set station alias",
             usage="MYALIAS [alias]",
             category="config")
    async def myalias(self, args):
        """Display or set station alias for digipeating.

        MYALIAS allows your digipeater to respond to a generic alias in addition
        to your callsign. Common aliases: WIDE1, GATE, RELAY.

        When DIGIPEATER is ON, packets with MYALIAS in the path will be digipeated.
        Supports both exact match (WIDE1) and with SSID (WIDE1-1).

        Examples:
            MYALIAS WIDE1     - Respond to WIDE1 or WIDE1-1 in path
            MYALIAS GATE      - Act as a gateway digipeater
            MYALIAS           - Display current alias
        """
        if not args:
            alias = self.tnc_config.get('MYALIAS')
            print_pt(f"MYALIAS: {alias if alias else '(none)'}")
            if alias:
                print_pt("")
                print_pt("When DIGIPEATER is ON, will respond to:")
                print_pt(f"  - {self.tnc_config.get('MYCALL')} (your callsign)")
                print_pt(f"  - {alias} (your alias)")
                print_pt(f"  - {alias}-N (alias with any SSID)")
            return

        alias = args[0].upper()
        self.tnc_config.set("MYALIAS", alias)
        print_info(f"MYALIAS set to {alias}")
        print_pt("")
        print_pt("Note: Restart required for digipeater to use new alias")

    @command("MYLOCATION",
             help_text="Display or set Maidenhead grid square",
             usage="MYLOCATION [grid]",
             category="config")
    async def mylocation(self, args):
        """Display or set Maidenhead grid square location."""
        if not args:
            location = self.tnc_config.get('MYLOCATION')
            if location:
                print_pt(f"MYLOCATION: {location}")
            else:
                print_pt("MYLOCATION: (not set)")
            return
        grid = args[0].upper()
        # TNCConfig.set() handles validation and displays result
        if self.tnc_config.set("MYLOCATION", grid):
            # Broadcast position to web UI
            await self.cmd_processor._broadcast_mylocation_to_web(grid)
        else:
            print_error("Failed to set MYLOCATION")

    @command("UNPROTO",
             help_text="Set destination for unconnected frames",
             usage="UNPROTO <destination> [via <path>]",
             category="config")
    async def unproto(self, args):
        """Set destination address for unconnected frames."""
        if not args:
            print_pt(f"UNPROTO: {self.tnc_config.get('UNPROTO')}")
            return

        # Parse destination and optional via path
        dest = args[0].upper()
        path_str = ""
        if len(args) > 1 and args[1].upper() == "VIA":
            path = [p.upper() for p in args[2:]]
            path_str = " via " + ",".join(path)

        # Store full UNPROTO string with path
        full_unproto = f"{dest}{path_str}"
        self.tnc_config.set("UNPROTO", full_unproto)
        print_info(f"UNPROTO set to {full_unproto}")

    @command("MONITOR",
             help_text="Toggle frame monitoring",
             usage="MONITOR [ON|OFF]",
             category="config")
    async def monitor(self, args):
        """Enable or disable frame monitoring."""
        if not args:
            current = self.tnc_config.get("MONITOR")
            print_pt(f"MONITOR: {current}")
            return

        value = args[0].upper()
        self.tnc_config.set("MONITOR", value)
        print_info(f"MONITOR set to {value}")

    @command("AUTO_ACK",
             help_text="Automatically ACK APRS messages",
             usage="AUTO_ACK [ON|OFF]",
             category="aprs")
    async def auto_ack(self, args):
        """Enable or disable automatic APRS message acknowledgment."""
        if not args:
            current = self.tnc_config.get("AUTO_ACK")
            print_pt(f"AUTO_ACK: {current}")
            return

        value = args[0].upper()
        self.tnc_config.set("AUTO_ACK", value)
        print_info(f"AUTO_ACK set to {value}")

    @command("RETRY",
             help_text="Set message retry count",
             usage="RETRY <count>",
             category="aprs")
    async def retry(self, args):
        """Set number of message retry attempts."""
        if not args:
            count = self.tnc_config.get("RETRY") or "3"
            fast = self.tnc_config.get("RETRY_FAST") or "30"
            slow = self.tnc_config.get("RETRY_SLOW") or "300"
            print_pt(f"RETRY: {count}")
            print_pt(f"RETRY_FAST: {fast} seconds")
            print_pt(f"RETRY_SLOW: {slow} seconds")
            print_pt("")
            print_pt("Message retry strategy:")
            print_pt("  First retry: RETRY_FAST seconds")
            print_pt(f"  Retries 2-{count}: RETRY_SLOW seconds each")
            return

        try:
            count = int(args[0])
            if count < 0 or count > 10:
                print_error("Retry count must be 0-10")
                return
            self.tnc_config.set("RETRY", str(count))
            print_info(f"Message retry count set to {count}")
        except ValueError:
            print_error("RETRY must be a number")

    @command("RETRY_FAST",
             help_text="Set first retry timeout (seconds)",
             usage="RETRY_FAST <seconds>",
             category="aprs")
    async def retry_fast(self, args):
        """Set timeout for first retry attempt."""
        if not args:
            timeout = self.tnc_config.get("RETRY_FAST") or "30"
            print_pt(f"RETRY_FAST: {timeout} seconds")
            print_pt("")
            print_pt("First retry timeout after sending message.")
            return

        try:
            timeout = int(args[0])
            if timeout < 5 or timeout > 300:
                print_error("RETRY_FAST must be 5-300 seconds")
                return
            self.tnc_config.set("RETRY_FAST", str(timeout))
            print_info(f"Fast retry timeout set to {timeout} seconds")
        except ValueError:
            print_error("RETRY_FAST must be a number")

    @command("RETRY_SLOW",
             help_text="Set subsequent retry timeout (seconds)",
             usage="RETRY_SLOW <seconds>",
             category="aprs")
    async def retry_slow(self, args):
        """Set timeout for subsequent retry attempts."""
        if not args:
            timeout = self.tnc_config.get("RETRY_SLOW") or "300"
            print_pt(f"RETRY_SLOW: {timeout} seconds")
            print_pt("")
            print_pt("Subsequent retry timeout after first retry.")
            return

        try:
            timeout = int(args[0])
            if timeout < 30 or timeout > 3600:
                print_error("RETRY_SLOW must be 30-3600 seconds")
                return
            self.tnc_config.set("RETRY_SLOW", str(timeout))
            print_info(f"Slow retry timeout set to {timeout} seconds")
        except ValueError:
            print_error("RETRY_SLOW must be a number")

    @command("DEBUG_BUFFER",
             help_text="Frame buffer debugging control",
             usage="DEBUG_BUFFER [ON|OFF|SIZE <mb>|DUMP|CLEAR|LIST]",
             category="debug")
    async def debug_buffer(self, args):
        """Control frame buffer debugging."""
        if not args:
            enabled = self.tnc_config.get("DEBUG_BUFFER") or "OFF"
            size = self.tnc_config.get("DEBUG_BUFFER_SIZE") or "10"
            print_pt(f"DEBUG_BUFFER: {enabled}")
            print_pt(f"DEBUG_BUFFER_SIZE: {size} MB")
            if hasattr(self.cmd_processor, 'frame_history'):
                count = len(self.cmd_processor.frame_history.frames)
                print_pt(f"Current buffer: {count} frames")
            return

        subcmd = args[0].upper()

        if subcmd in ("ON", "OFF"):
            self.tnc_config.set("DEBUG_BUFFER", subcmd)
            print_info(f"DEBUG_BUFFER set to {subcmd}")

        elif subcmd == "SIZE":
            if len(args) < 2:
                print_error("Usage: DEBUG_BUFFER SIZE <mb>")
                return
            try:
                size_mb = float(args[1])
                if size_mb < 1 or size_mb > 100:
                    print_error("Size must be 1-100 MB")
                    return
                self.tnc_config.set("DEBUG_BUFFER_SIZE", str(size_mb))
                print_info(f"Buffer size set to {size_mb} MB")
            except ValueError:
                print_error("Size must be a number")

        elif subcmd == "DUMP":
            if hasattr(self.cmd_processor, 'frame_history'):
                await self.cmd_processor.frame_history.save()
                print_info("Frame buffer saved to ~/.console_frame_buffer.json.gz")
            else:
                print_error("Frame buffer not initialized")

        elif subcmd == "CLEAR":
            if hasattr(self.cmd_processor, 'frame_history'):
                self.cmd_processor.frame_history.clear()
                print_info("Frame buffer cleared")
            else:
                print_error("Frame buffer not initialized")

        elif subcmd == "LIST":
            if hasattr(self.cmd_processor, 'frame_history'):
                recent = self.cmd_processor.frame_history.get_recent(20)
                if not recent:
                    print_info("Frame buffer is empty")
                    return
                print_pt(HTML("<cyan><b>Recent Frames:</b></cyan>"))
                for frame in recent:
                    timestamp = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]
                    direction = frame.direction
                    size = len(frame.raw_bytes)
                    print_pt(f"  [{frame.frame_number}] {direction} {timestamp} ({size}b)")
            else:
                print_error("Frame buffer not initialized")

        else:
            print_error("Usage: DEBUG_BUFFER [ON|OFF|SIZE <mb>|DUMP|CLEAR|LIST]")

    @command("DIGIPEATER", "DIGI",
             help_text="Toggle digipeater mode",
             usage="DIGIPEATER [ON|OFF]",
             category="config")
    async def digipeater(self, args):
        """Enable or disable digipeater mode."""
        if not args:
            current = self.tnc_config.get("DIGIPEATER") or "OFF"
            print_pt(f"DIGIPEATER: {current}")
            print_pt("")
            print_pt("When ON, the TNC will digipeat packets with MYALIAS or MYCALL in path.")
            return

        value = args[0].upper()
        if value not in ("ON", "OFF"):
            print_error("Usage: DIGIPEATER [ON|OFF]")
            return

        self.tnc_config.set("DIGIPEATER", value)
        print_info(f"DIGIPEATER set to {value}")

        # Apply to AX25 adapter
        if hasattr(self.cmd_processor, 'ax25') and self.ax25:
            self.ax25.digipeater_enabled = (value == "ON")

    @command("DISPLAY",
             help_text="Display all TNC parameters",
             category="config")
    async def display(self, args):
        """Display all TNC configuration parameters."""
        print_pt(HTML("<cyan><b>TNC Parameters:</b></cyan>"))
        for key in sorted(self.tnc_config.settings.keys()):
            value = self.tnc_config.get(key)
            print_pt(f"  {key:20s} {value}")

    @command("STATUS",
             help_text="Display TNC status",
             category="general")
    async def status(self, args):
        """Display TNC connection status."""
        await self.cmd_processor._tnc_status()

    @command("RESET",
             help_text="Reset TNC connection state",
             category="general")
    async def reset(self, args):
        """Reset TNC connection state without power cycling."""
        print_info("Resetting TNC connection state...")

        # Disconnect if connected
        if self.cmd_processor.tnc_connected_to:
            await self.cmd_processor._tnc_disconnect()

        # Reset adapter state
        if self.ax25:
            async with self.ax25._tx_lock:
                self.ax25._tx_queue.clear()
            self.ax25._ns = 0
            self.ax25._nr = 0
            self.ax25._link_established = False
            self.ax25._pending_connect = None

        print_info("TNC connection state reset")

    @command("HARDRESET",
             help_text="Reset radio hardware (BLE only)",
             category="general")
    async def hardreset(self, args):
        """Reset radio hardware (BLE mode only)."""
        if self.cmd_processor.serial_mode:
            print_error("HARDRESET not available in serial mode")
            return

        print_info("Resetting radio hardware...")

        # Disconnect if connected
        if self.cmd_processor.tnc_connected_to:
            await self.cmd_processor._tnc_disconnect()

        # Reset adapter state
        if self.ax25:
            async with self.ax25._tx_lock:
                self.ax25._tx_queue.clear()
            self.ax25._ns = 0
            self.ax25._nr = 0
            self.ax25._link_established = False
            self.ax25._pending_connect = None

        # Send hardware reset command
        try:
            await self.radio.reset_radio()
            await asyncio.sleep(2.0)
            print_info("Radio hardware reset complete")
        except Exception as e:
            print_error(f"Hardware reset failed: {e}")

    @command("POWERCYCLE",
             help_text="Power cycle radio TNC",
             category="general")
    async def powercycle(self, args):
        """Power cycle the radio's TNC to clear stuck states."""
        print_info("Power cycling radio TNC...")
        try:
            # Disconnect if connected
            if self.cmd_processor.tnc_connected_to:
                await self.cmd_processor._tnc_disconnect()

            # Stop TX worker
            if self.ax25 and self.ax25._tx_task:
                print_debug("Stopping TX worker...", level=1)
                self.ax25._tx_task.cancel()
                try:
                    await self.ax25._tx_task
                except asyncio.CancelledError:
                    pass
                self.ax25._tx_task = None

            # Power OFF the radio
            print_info("  Turning radio OFF...")
            await self.radio.set_hardware_power(False)
            await asyncio.sleep(2.0)

            # Power ON the radio
            print_info("  Turning radio ON...")
            await self.radio.set_hardware_power(True)
            await asyncio.sleep(2.0)

            # Reset adapter state
            if self.ax25:
                async with self.ax25._tx_lock:
                    self.ax25._tx_queue.clear()
                self.ax25._ns = 0
                self.ax25._nr = 0
                self.ax25._link_established = False
                self.ax25._pending_connect = None

                # Restart TX worker
                print_debug("Restarting TX worker...", level=4)
                self.ax25._tx_task = asyncio.create_task(self.ax25._tx_worker())

            print_info("Power cycle complete - TNC should be fully reset")
        except Exception as e:
            print_error(f"Power cycle failed: {e}")

    @command("TNCSEND",
             help_text="Send raw hex data to TNC",
             usage="TNCSEND <hexdata>",
             category="tnc")
    async def tncsend(self, args):
        """Send raw hex data to TNC."""
        if not args:
            print_error("Usage: tncsend <hexdata>")
            print_error("Example: tncsend c000c0")
            return

        hex_str = "".join(args).replace(" ", "")

        try:
            data = bytes.fromhex(hex_str)
        except ValueError:
            print_error("Invalid hex data")
            return

        print_info(f"Sending {len(data)} bytes to TNC...")
        await self.cmd_processor.radio.send_tnc_data(data)
        print_info(f"✓ Sent: {data.hex()}")

    @command("EXIT", "QUIT",
             help_text="Exit TNC mode",
             category="general")
    async def exit(self, args):
        """Exit TNC mode and return to console."""
        # Disconnect gracefully before exiting
        if self.cmd_processor.tnc_connected_to:
            print_info("Disconnecting before exit...")
            await self.cmd_processor._tnc_disconnect()
        self.cmd_processor.tnc_mode = False
