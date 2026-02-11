"""
Radio control command handlers.

Handles all radio hardware control commands including VFO, audio, channels,
power management, and diagnostic utilities.
"""

import asyncio
import time
from datetime import datetime, timezone
from prompt_toolkit import HTML

from .base import CommandHandler, command
from src.utils import (
    print_pt,
    print_info,
    print_error,
    print_header,
    print_warning,
    print_debug,
    print_table_row,
)
from src.constants import (
    TNC_RX_UUID,
    RADIO_INDICATE_UUID,
    RADIO_WRITE_UUID,
    TNC_TX_UUID,
    TNC_TCP_PORT,
)


class RadioCommandHandler(CommandHandler):
    """Handles radio control and diagnostic commands."""

    def __init__(self, cmd_processor):
        """
        Initialize radio command handler.

        Args:
            cmd_processor: Reference to main CommandProcessor instance
        """
        self.cmd_processor = cmd_processor
        self.radio = cmd_processor.radio
        self.serial_mode = cmd_processor.serial_mode
        super().__init__()

    @command("STATUS",
             help_text="Show current radio status",
             usage="STATUS",
             category="radio")
    async def status(self, args):
        """Show radio status."""
        print_info("Reading status...")
        status = await self.radio.get_status()
        settings = await self.radio.get_settings()

        if status:
            print_header("Radio Status")
            on_color = "green" if status["is_power_on"] else "red"
            print_pt(
                HTML(
                    f"Power:         <{on_color}>{'ON' if status['is_power_on'] else 'OFF'}</{on_color}>"
                )
            )

            tx_color = "red" if status["is_in_tx"] else "gray"
            print_pt(
                HTML(
                    f"TX:            <{tx_color}>{'TRANSMITTING' if status['is_in_tx'] else 'Idle'}</{tx_color}>"
                )
            )

            rx_color = "green" if status["is_in_rx"] else "gray"
            print_pt(
                HTML(
                    f"RX:            <{rx_color}>{'RECEIVING' if status['is_in_rx'] else 'Idle'}</{rx_color}>"
                )
            )

            scan_color = "yellow" if status["is_scan"] else "gray"
            print_pt(
                HTML(
                    f"Scan:          <{scan_color}>{'Active' if status['is_scan'] else 'Off'}</{scan_color}>"
                )
            )

            # Add 1 to channel ID for display (radio uses 0-based internally, displays 1-based)
            print_pt(f"Channel:       {status['curr_ch_id'] + 1}")
            print_pt(f"RSSI:          {status['rssi']}/15")

            if settings:
                # Prefer double_channel for active VFO selection when available
                mode = settings.get("double_channel", None)
                if mode is not None and mode in (1, 2):
                    active_vfo = "A" if mode != 2 else "B"
                else:
                    vfo_x = settings.get("vfo_x", 0)
                    active_vfo = "A" if vfo_x == 0 else "B"
                print_pt(f"Active VFO:    {active_vfo}")

                dual_mode = settings.get("double_channel", 0)
                dual_str = (
                    "Off"
                    if dual_mode == 0
                    else "A+B" if dual_mode == 1 else "B+A"
                )
                print_pt(f"Dual Watch:    {dual_str}")

            # TNC status
            idle_time = int(self.radio.get_tnc_idle_time())
            print_pt(
                f"TNC Packets:   {self.radio.tnc_packet_count} ({idle_time}s idle)"
            )

            # TNC Bridge status
            if self.radio.tnc_bridge:
                if self.radio.tnc_bridge.client_address:
                    print_pt(
                        HTML(
                            f"TNC Bridge:    <green>Connected ({self.radio.tnc_bridge.client_address})</green>"
                        )
                    )
                else:
                    print_pt(
                        HTML(
                            f"TNC Bridge:    <yellow>Listening on port {TNC_TCP_PORT}</yellow>"
                        )
                    )

            print_pt("")
        else:
            print_error("Failed to read status")

    @command("HEALTH",
             help_text="Show connection health diagnostics",
             usage="HEALTH",
             category="radio")
    async def health(self, args):
        """Show connection health."""
        print_header("Connection Health")

        # Check connection status (BLE mode only)
        if self.radio.client:
            connected = self.radio.client.is_connected
            conn_color = "green" if connected else "red"
            print_pt(
                HTML(
                    f"BLE Connected:     <{conn_color}>{'Yes' if connected else 'No'}</{conn_color}>"
                )
            )
        else:
            print_pt(HTML("Mode:              <green>Serial KISS TNC</green>"))

        idle_time = int(self.radio.get_tnc_idle_time())
        idle_color = (
            "green"
            if idle_time < 60
            else "yellow" if idle_time < 300 else "red"
        )
        print_pt(
            HTML(
                f"TNC Idle Time:     <{idle_color}>{idle_time}s</{idle_color}>"
            )
        )

        print_pt(f"TNC Packets RX:    {self.radio.tnc_packet_count}")
        print_pt(f"Heartbeat Fails:   {self.radio.heartbeat_failures}")

        hb_time = int(
            (datetime.now(timezone.utc) - self.radio.last_heartbeat).total_seconds()
        )
        print_pt(f"Last Heartbeat:    {hb_time}s ago")

        print_pt("")
        print_info("Running health check...")
        healthy = await self.radio.check_connection_health()

        if healthy:
            print_info("✓ Connection is healthy")
        else:
            print_error("✗ Connection appears unhealthy")

        print_pt("")

    @command("NOTIFICATIONS",
             help_text="Check BLE notification status",
             usage="NOTIFICATIONS",
             category="radio")
    async def notifications(self, args):
        """Check notification status."""
        print_header("Notification Status")

        if not self.radio.client:
            print_error("Notifications command not available in serial mode")
            return

        try:
            # Check if notifications are still enabled
            services = self.radio.client.services

            for service in services:
                for char in service.characteristics:
                    if char.uuid == TNC_RX_UUID:
                        print_info(f"TNC RX UUID found: {char.uuid}")
                        print_info(f"  Properties: {char.properties}")

                    if char.uuid == RADIO_INDICATE_UUID:
                        print_info(f"Radio indicate UUID found: {char.uuid}")
                        print_info(f"  Properties: {char.properties}")

            print_pt("")
            print_warning(
                "If TNC packets have stopped, restart the application"
            )

        except Exception as e:
            print_error(f"Failed to check notifications: {e}")

        print_pt("")

    @command("DUMP",
             help_text="Dump raw radio settings for analysis",
             usage="DUMP",
             category="radio")
    async def dump(self, args):
        """Dump raw settings bytes for analysis."""
        print_info("Reading raw settings...")
        settings = await self.radio.get_settings()

        if settings and "raw_data" in settings:
            data = settings["raw_data"]
            print_header("Raw Settings Data")

            # Print hex dump
            print_pt("Hex dump:")
            for i in range(0, len(data), 16):
                chunk = data[i : i + 16]
                hex_str = " ".join(f"{b:02x}" for b in chunk)
                ascii_str = "".join(
                    chr(b) if 32 <= b <= 126 else "." for b in chunk
                )
                print_pt(f"  {i:04x}: {hex_str:<48} {ascii_str}")

            print_pt(HTML(f"\n<yellow>Decoded VFO settings:</yellow>"))
            print_pt(f"  VFO A: Channel {settings['channel_a']}")
            print_pt(f"  VFO B: Channel {settings['channel_b']}")
            print_pt(
                f"  Active VFO: {'A' if settings.get('vfo_x', 0) == 0 else 'B'}"
            )
            print_pt("")
        else:
            print_error("Failed to read settings")

    @command("VFO",
             help_text="Show VFO A/B configuration",
             usage="VFO",
             category="radio")
    async def vfo(self, args):
        """Show VFO A/B configuration."""
        print_info("Reading VFO settings...")
        settings = await self.radio.get_settings()

        if settings:
            print_header("VFO Configuration")

            # Determine active VFO: prefer `double_channel` when present,
            # otherwise fall back to `vfo_x`.
            mode = settings.get("double_channel", None)
            if mode is not None and mode in (1, 2):
                active_vfo = "A" if mode != 2 else "B"
            else:
                vfo_x = settings.get("vfo_x", None)
                active_vfo = "A" if (vfo_x is None or vfo_x == 0) else "B"
            active_marker_a = "●" if active_vfo == "A" else "○"
            active_marker_b = "●" if active_vfo == "B" else "○"

            print_pt(
                HTML(
                    f"VFO A (Main):  {active_marker_a} Channel {settings['channel_a']}"
                )
            )
            print_pt(
                HTML(
                    f"VFO B (Sub):   {active_marker_b} Channel {settings['channel_b']}"
                )
            )

            mode = settings.get("double_channel", 0)
            mode_str = "Off" if mode == 0 else "A+B" if mode == 1 else "B+A"
            print_pt(f"Dual Watch:    {mode_str}")
            print_pt(
                f"Scan:          {'On' if settings.get('scan', False) else 'Off'}"
            )
            print_pt(f"Squelch:       {settings.get('squelch_level', 0)}")
            print_pt("")

            print_info(f"Reading VFO A details (CH{settings['channel_a']})...")
            ch_a = await self.radio.read_channel(settings["channel_a"])
            if ch_a:
                self._print_channel_details(ch_a)

            print_info(f"Reading VFO B details (CH{settings['channel_b']})...")
            ch_b = await self.radio.read_channel(settings["channel_b"])
            if ch_b:
                self._print_channel_details(ch_b)
        else:
            print_error("Failed to read VFO settings")

    @command("SETVFO",
             help_text="Set VFO to specific channel",
             usage="SETVFO <a|b> <channel>",
             category="radio")
    async def setvfo(self, args):
        """Set VFO to a specific channel."""
        if len(args) < 2:
            print_error("Usage: setvfo <a|b> <channel>")
            print_error("Channel must be 1-256")
            return

        vfo = args[0].lower()
        if vfo not in ["a", "b"]:
            print_error("VFO must be 'a' or 'b'")
            return

        try:
            channel_id = int(args[1])
        except ValueError:
            print_error("Channel ID must be a number")
            return

        if channel_id < 1 or channel_id > 256:
            print_error("Channel ID must be 1-256")
            return

        print_info(f"Setting VFO {vfo.upper()} to channel {channel_id}...")
        success = await self.radio.set_vfo(vfo, channel_id)

        if success:
            print_info(f"✓ VFO {vfo.upper()} set to channel {channel_id}")

            channel = await self.radio.read_channel(channel_id)
            if channel:
                print_pt(
                    f"  {channel['name']}: {channel['tx_freq_mhz']:.4f} MHz"
                )

            await asyncio.sleep(0.3)
            settings = await self.radio.get_settings()
            if settings:
                actual_a = settings["channel_a"]
                actual_b = settings["channel_b"]
                print_info(f"Verified: VFO A={actual_a}, VFO B={actual_b}")
        else:
            print_error(f"Failed to set VFO {vfo.upper()}")

    @command("ACTIVE",
             help_text="Switch active VFO",
             usage="ACTIVE <a|b>",
             category="radio")
    async def active(self, args):
        """Switch active VFO."""
        if not args:
            print_error("Usage: active <a|b>")
            return

        vfo = args[0].lower()
        if vfo not in ["a", "b"]:
            print_error("VFO must be 'a' or 'b'")
            return

        print_info(f"Switching to VFO {vfo.upper()}...")
        success = await self.radio.set_active_vfo(vfo)

        if success:
            print_info(f"✓ Active VFO set to {vfo.upper()}")
        else:
            print_error("Failed to switch VFO")

    @command("DUAL",
             help_text="Set dual watch mode",
             usage="DUAL <off|ab|ba>",
             category="radio")
    async def dual(self, args):
        """Set dual watch mode."""
        if not args:
            print_error("Usage: dual <off|ab|ba>")
            return

        mode_str = args[0].lower()
        if mode_str == "off":
            mode = 0
        elif mode_str == "ab":
            mode = 1
        elif mode_str == "ba":
            mode = 2
        else:
            print_error("Mode must be: off, ab, or ba")
            return

        print_info(f"Setting dual watch to {mode_str.upper()}...")
        success = await self.radio.set_dual_watch(mode)

        if success:
            print_info(f"✓ Dual watch set to {mode_str.upper()}")
        else:
            print_error("Failed to set dual watch")

    @command("SCAN",
             help_text="Enable/disable scanning",
             usage="SCAN <on|off>",
             category="radio")
    async def scan(self, args):
        """Enable/disable scanning."""
        if not args:
            print_error("Usage: scan <on|off>")
            return

        state = args[0].lower()
        if state == "on":
            enabled = True
        elif state == "off":
            enabled = False
        else:
            print_error("State must be: on or off")
            return

        print_info(f"Setting scan to {state.upper()}...")
        success = await self.radio.set_scan(enabled)

        if success:
            print_info(f"✓ Scan {state.upper()}")
        else:
            print_error("Failed to set scan")

    @command("SQUELCH",
             help_text="Set squelch level (0-15)",
             usage="SQUELCH <0-15>",
             category="radio")
    async def squelch(self, args):
        """Set squelch level."""
        if not args:
            print_error("Usage: squelch <0-15>")
            return

        try:
            level = int(args[0])
        except ValueError:
            print_error("Level must be a number 0-15")
            return

        if level < 0 or level > 15:
            print_error("Level must be 0-15")
            return

        print_info(f"Setting squelch to {level}...")
        success = await self.radio.set_squelch(level)

        if success:
            print_info(f"✓ Squelch set to {level}")
        else:
            print_error("Failed to set squelch")

    @command("VOLUME",
             help_text="Get or set volume level (0-15)",
             usage="VOLUME [0-15]",
             category="radio")
    async def volume(self, args):
        """Get or set volume."""
        if not args:
            # Get volume
            print_info("Reading volume...")
            level = await self.radio.get_volume()
            if level is not None:
                print_info(f"Volume: {level}/15")
            else:
                print_error("Failed to read volume")
            return

        # Set volume
        try:
            level = int(args[0])
        except ValueError:
            print_error("Level must be a number 0-15")
            return

        if level < 0 or level > 15:
            print_error("Level must be 0-15")
            return

        print_info(f"Setting volume to {level}...")
        success = await self.radio.set_volume(level)

        if success:
            print_info(f"✓ Volume set to {level}")
        else:
            print_error("Failed to set volume")

    @command("BSS",
             help_text="Show BSS/APRS radio settings",
             usage="BSS",
             category="radio")
    async def bss(self, args):
        """Show BSS/APRS settings."""
        print_info("Reading BSS settings...")
        bss = await self.radio.get_bss_settings()

        print_debug(f"cmd_bss: received bss = {bss}", level=6)

        if bss:
            print_header("BSS/APRS Settings")
            print_pt(f"APRS Callsign:     {bss['aprs_callsign']}")
            print_pt(f"APRS SSID:         {bss['aprs_ssid']}")
            print_pt(f"APRS Symbol:       {bss['aprs_symbol']}")
            print_pt(f"Beacon Message:    {bss['beacon_message']}")
            print_pt(
                f"Share Location:    {'Yes' if bss['should_share_location'] else 'No'}"
            )
            print_pt(f"Share Interval:    {bss['location_share_interval']}s")
            print_pt(f"PTT Release ID:    {bss['ptt_release_id_info']}")
            print_pt(f"Max Fwd Times:     {bss['max_fwd_times']}")
            print_pt(f"Time To Live:      {bss['time_to_live']}")
            print_pt("")
        else:
            print_error("Failed to read BSS settings")

    @command("SETBSS",
             help_text="Set BSS parameter",
             usage="SETBSS <param> <value>",
             category="radio")
    async def setbss(self, args):
        """Set BSS parameter."""
        if len(args) < 2:
            print_error("Usage: setbss <param> <value>")
            print_error("Parameters: callsign, ssid, symbol, beacon, interval")
            return

        param = args[0].lower()
        value = " ".join(args[1:])

        bss = await self.radio.get_bss_settings()
        if not bss:
            print_error("Failed to read BSS settings")
            return

        try:
            if param == "callsign":
                bss["aprs_callsign"] = value.upper()[:6]
            elif param == "ssid":
                bss["aprs_ssid"] = int(value) & 0x0F
            elif param == "symbol":
                bss["aprs_symbol"] = value[:2]
            elif param == "beacon":
                bss["beacon_message"] = value[:18]
            elif param == "interval":
                bss["location_share_interval"] = int(value)
            else:
                print_error(f"Unknown parameter: {param}")
                return

            print_info(f"Setting {param} to {value}...")
            success = await self.radio.write_bss_settings(bss)

            if success:
                print_info(f"✓ BSS {param} updated")
            else:
                print_error("Failed to update BSS settings")

        except ValueError as e:
            print_error(f"Invalid value: {e}")

    @command("CHANNEL",
             help_text="Show channel details",
             usage="CHANNEL <id>",
             category="radio")
    async def channel(self, args):
        """Show channel details."""
        if not args:
            print_error("Usage: channel <id>")
            return

        try:
            channel_id = int(args[0])
        except ValueError:
            print_error("Channel ID must be a number")
            return

        print_info(f"Reading channel {channel_id}...")
        channel = await self.radio.read_channel(channel_id)

        if channel:
            self._print_channel_details(channel)
        else:
            print_error(f"Failed to read channel {channel_id}")

    @command("LIST",
             help_text="List channels in table format",
             usage="LIST [start] [end]",
             category="radio")
    async def list_channels(self, args):
        """List channels in table format."""
        if len(args) == 0:
            start = 1
            end = 30
        elif len(args) == 1:
            start = int(args[0])
            end = start + 9
        elif len(args) >= 2:
            start = int(args[0])
            end = int(args[1])

        start = max(1, min(start, 256))
        end = max(1, min(end, 256))

        if start > end:
            print_error("Start channel must be <= end channel")
            return

        print_header(f"Channel List ({start}-{end})")

        widths = [4, 12, 12, 12, 8, 8, 5]
        print_table_row(
            [
                "CH",
                "Name",
                "TX (MHz)",
                "RX (MHz)",
                "TX Tone",
                "RX Tone",
                "Power",
            ],
            widths,
            header=True,
        )

        for ch_id in range(start, end + 1):
            channel = await self.radio.read_channel(ch_id)

            if channel:
                print_table_row(
                    [
                        ch_id,
                        channel["name"][:12],
                        f"{channel['tx_freq_mhz']:.4f}",
                        f"{channel['rx_freq_mhz']:.4f}",
                        channel["tx_tone"][:8],
                        channel["rx_tone"][:8],
                        channel["power"],
                    ],
                    widths,
                )
            else:
                print_table_row(
                    [ch_id, "---", "---", "---", "---", "---", "---"], widths
                )

            await asyncio.sleep(0.05)

        print_pt("")

    @command("POWER",
             help_text="Set channel power level",
             usage="POWER <channel> <level>",
             category="radio")
    async def power(self, args):
        """Set power level."""
        if len(args) < 2:
            print_error("Usage: power <channel> <level>")
            return

        try:
            channel_id = int(args[0])
        except ValueError:
            print_error("Channel ID must be a number")
            return

        power_level = args[1].lower()
        if power_level not in ["low", "med", "medium", "high", "max"]:
            print_error("Power level must be: low, med, or high")
            return

        print_info(f"Setting channel {channel_id} to {power_level} power...")
        success = await self.radio.set_channel_power(channel_id, power_level)

        if success:
            print_info(f"✓ Channel {channel_id} power set to {power_level}")
        else:
            print_error(f"Failed to set power for channel {channel_id}")

    @command("FREQ",
             help_text="Set channel frequency",
             usage="FREQ <channel> <tx_mhz> <rx_mhz>",
             category="radio")
    async def freq(self, args):
        """Set frequency."""
        if len(args) < 3:
            print_error(
                "Usage: freq <channel> <tx_mhz> <rx_mhz>"
            )
            return

        try:
            channel_id = int(args[0])
            tx_freq = float(args[1])
            rx_freq = float(args[2])
        except ValueError:
            print_error("Invalid frequency values")
            return

        print_info(f"Reading channel {channel_id}...")
        channel = await self.radio.read_channel(channel_id)

        if not channel:
            print_error(f"Failed to read channel {channel_id}")
            return

        channel["tx_freq_mhz"] = tx_freq
        channel["rx_freq_mhz"] = rx_freq

        print_info(f"Writing new frequencies to channel {channel_id}...")
        success = await self.radio.write_channel(channel)

        if success:
            print_info(
                f"✓ Channel {channel_id} updated: TX={tx_freq} MHz, RX={rx_freq} MHz"
            )
        else:
            print_error(f"Failed to update channel {channel_id}")

    @command("SCAN_BLE",
             help_text="Scan BLE characteristics for audio channels",
             usage="SCAN_BLE",
             category="radio")
    async def scan_ble(self, args):
        """Scan all BLE characteristics to find audio channels."""
        print_header("BLE Characteristics Scan")

        if not self.radio.client:
            print_error("BLE scan not available in serial mode")
            return

        try:
            services = self.radio.client.services

            for service in services:
                print_pt(HTML(f"\n<b>Service:</b> {service.uuid}"))
                print_pt(f"  Handle: {service.handle}")

                for char in service.characteristics:
                    props = ", ".join(char.properties)
                    print_pt(
                        HTML(
                            f"\n  <yellow>Characteristic:</yellow> {char.uuid}"
                        )
                    )
                    print_pt(f"    Handle: {char.handle}")
                    print_pt(f"    Properties: {props}")

                    # Check if this is a known UUID
                    if char.uuid == RADIO_WRITE_UUID:
                        print_pt(
                            HTML("    <green>→ RADIO_WRITE (commands)</green>")
                        )
                    elif char.uuid == RADIO_INDICATE_UUID:
                        print_pt(
                            HTML(
                                "    <green>→ RADIO_INDICATE (responses)</green>"
                            )
                        )
                    elif char.uuid == TNC_TX_UUID:
                        print_pt(
                            HTML("    <green>→ TNC_TX (TNC transmit)</green>")
                        )
                    elif char.uuid == TNC_RX_UUID:
                        print_pt(
                            HTML("    <green>→ TNC_RX (TNC receive)</green>")
                        )
                    elif (
                        "notify" in char.properties
                        or "indicate" in char.properties
                    ):
                        print_pt(
                            HTML(
                                "    <cyan>→ Potential audio/data stream</cyan>"
                            )
                        )

                    # Show descriptors
                    if char.descriptors:
                        for desc in char.descriptors:
                            print_pt(f"      Descriptor: {desc.uuid}")

            print_pt("")
            print_info(
                "Look for characteristics with 'notify' property for audio streams"
            )
            print_pt("")

        except Exception as e:
            print_error(f"Failed to scan characteristics: {e}")

    @command("POWERON",
             help_text="Power on the radio",
             usage="POWERON",
             category="radio")
    async def poweron(self, args):
        """Power on the radio."""
        print_info("Powering on radio...")
        success = await self.radio.set_hardware_power(True)
        if success:
            print_info("✓ Radio powered on")
        else:
            print_error("Failed to power on radio")

    @command("POWEROFF",
             help_text="Power off the radio",
             usage="POWEROFF",
             category="radio")
    async def poweroff(self, args):
        """Power off the radio."""
        print_info("Powering off radio...")
        success = await self.radio.set_hardware_power(False)
        if success:
            print_info("✓ Radio powered off")
        else:
            print_error("Failed to power off radio")

    @command("GPS",
             help_text="Show GPS status or restart GPS polling",
             usage="GPS [restart]",
             category="radio")
    async def gps(self, args):
        """Show GPS status or restart GPS polling task."""
        if args and args[0].lower() == "restart":
            # Restart GPS polling task
            print_info("Restarting GPS polling...")
            success = await self._restart_gps_task()
            if success:
                print_info("✓ GPS polling restarted")
            else:
                print_error("Failed to restart GPS polling")
            return

        # Show GPS status
        print_header("GPS Status")

        # Check if command processor is available
        if not hasattr(self.radio, 'cmd_processor') or not self.radio.cmd_processor:
            print_error("Command processor not available")
            return

        cmd_proc = self.radio.cmd_processor

        # Display lock status
        lock_color = "green" if cmd_proc.gps_locked else "red"
        lock_status = "LOCKED" if cmd_proc.gps_locked else "NO LOCK"
        print_pt(HTML(f"Lock Status:   <{lock_color}>{lock_status}</{lock_color}>"))

        # Display position if available
        if cmd_proc.gps_position:
            pos = cmd_proc.gps_position
            print_pt(f"Latitude:      {pos['latitude']:.6f}°")
            print_pt(f"Longitude:     {pos['longitude']:.6f}°")

            if pos.get('altitude') is not None:
                print_pt(f"Altitude:      {pos['altitude']} m")

            if pos.get('speed') is not None:
                print_pt(f"Speed:         {pos['speed']} km/h")

            if pos.get('heading') is not None:
                print_pt(f"Heading:       {pos['heading']}°")

            if pos.get('accuracy') is not None:
                print_pt(f"Accuracy:      {pos['accuracy']} m")

            # Calculate time since last update
            if pos.get('timestamp'):
                age = int(time.time() - pos['timestamp'])
                print_pt(f"Last Update:   {age}s ago")
        else:
            print_pt("Position:      Not available")

        print_pt("")

        # Show beacon status if available
        if hasattr(cmd_proc, 'tnc_config'):
            beacon_enabled = cmd_proc.tnc_config.get("BEACON") == "ON"
            beacon_color = "green" if beacon_enabled else "gray"
            beacon_status = "ENABLED" if beacon_enabled else "DISABLED"
            print_pt(HTML(f"Beacon:        <{beacon_color}>{beacon_status}</{beacon_color}>"))

            if beacon_enabled:
                interval = cmd_proc.tnc_config.get("BEACON_INTERVAL") or "10"
                print_pt(f"Interval:      {interval} minutes")

                if cmd_proc.last_beacon_time:
                    elapsed = int((datetime.now(timezone.utc) - cmd_proc.last_beacon_time).total_seconds())
                    print_pt(f"Last Beacon:   {elapsed}s ago")

        print_pt("")
        print_info("Use 'GPS RESTART' to restart GPS polling task")
        print_pt("")

    async def _restart_gps_task(self):
        """Restart the GPS polling task.

        This function resets the GPS communication state by:
        1. Flushing stale responses from the BLE rx_queue
        2. Cancelling the existing GPS polling task
        3. Creating a fresh GPS polling task

        Returns:
            bool: True if restart was successful, False otherwise
        """
        try:
            # Step 1: Flush stale GPS responses from rx_queue
            # This clears any error responses that might be stuck in the queue
            print_debug("Flushing rx_queue to clear stale GPS responses...", level=2)
            flushed_count = 0
            while not self.radio.rx_queue.empty():
                try:
                    _ = self.radio.rx_queue.get_nowait()
                    flushed_count += 1
                except asyncio.QueueEmpty:
                    break

            if flushed_count > 0:
                print_debug(f"Flushed {flushed_count} stale response(s) from queue", level=2)

            # Step 2: Find and cancel the existing GPS task
            if hasattr(self.radio, 'background_tasks'):
                for task in self.radio.background_tasks:
                    # Check if this is the GPS task by examining its coroutine name
                    # Note: Accessing _coro.cr_code.co_name is implementation-dependent
                    # and may need adjustment in future Python versions
                    if hasattr(task, '_coro') and hasattr(task._coro, 'cr_code'):
                        if 'gps_monitor' in task._coro.cr_code.co_name:
                            print_debug("Cancelling existing GPS task...", level=2)
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                            # Remove from background tasks list
                            self.radio.background_tasks.remove(task)
                            break

            # Step 3: Import gps_monitor here to avoid circular import
            # (console.py imports RadioCommandHandler from this module)
            from src.console import gps_monitor

            # Step 4: Create new GPS task with clean state
            new_task = asyncio.create_task(gps_monitor(self.radio))

            # Add to background tasks if the list exists
            if hasattr(self.radio, 'background_tasks'):
                self.radio.background_tasks.append(new_task)

            print_debug("New GPS task created with flushed queue", level=2)
            return True

        except Exception as e:
            print_error(f"Error restarting GPS task: {e}")
            return False

    def _print_channel_details(self, channel):
        """Print formatted channel details."""
        print_header(f"Channel {channel['id']}")
        print_pt(f"Name:      {channel['name']}")
        print_pt(f"TX Freq:   {channel['tx_freq_mhz']:.4f} MHz")
        print_pt(f"RX Freq:   {channel['rx_freq_mhz']:.4f} MHz")
        print_pt(f"TX Tone:   {channel['tx_tone']}")
        print_pt(f"RX Tone:   {channel['rx_tone']}")
        print_pt(f"Power:     {channel['power']}")
        print_pt(f"Bandwidth: {channel['bandwidth']}")
        print_pt("")
