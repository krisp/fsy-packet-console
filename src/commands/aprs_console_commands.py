"""
APRS console command handlers.

Handles APRS message reading, weather station listing, station tracking,
and database operations for the main console interface.
"""

import os

from .base import CommandHandler, command
from src.utils import print_pt, print_info, print_error, print_header, print_warning
from prompt_toolkit import HTML
from src.aprs.formatters import APRSFormatters
from src.aprs.geo_utils import latlon_to_maidenhead, maidenhead_to_latlon, calculate_distance_miles


class APRSConsoleCommandHandler(CommandHandler):
    """Handles APRS console commands for message and station management."""

    def __init__(self, cmd_processor):
        """
        Initialize APRS console command handler.

        Args:
            cmd_processor: Reference to main CommandProcessor instance
        """
        self.cmd_processor = cmd_processor
        self.aprs_manager = cmd_processor.aprs_manager
        self.tnc_config = cmd_processor.tnc_config
        super().__init__()

    @command("APRS",
             help_text="APRS message, station, and database commands",
             usage="APRS <message|wx|position|station|database> ...",
             category="aprs")
    async def aprs(self, args):
        """APRS commands - message handling, stations, weather, and database."""
        if not args:
            print_info("APRS Commands:")
            print_info("  aprs message read [all]                - Read APRS messages (all = include read)")
            print_info("  aprs message send <call> <text>    - Send APRS message")
            print_info("  aprs message clear                 - Clear all messages")
            print_info("  aprs message monitor list [N]      - List monitored messages (last N)")
            print_info("  aprs wx list [last|name|temp|humidity|pressure] - List weather stations")
            print_info("  aprs position list                 - List station positions")
            print_info("  aprs station list [name|packets|last|hops] - List all heard stations (default: last)")
            print_info("  aprs station dx                    - DX list (zero-hop stations by distance)")
            print_info("  aprs station show <callsign>       - Show detailed station info")
            print_info("  aprs station receptions <callsign> [N] - Show reception events (last N, default 20)")
            print_info("  aprs database save                 - Manually save database to disk")
            print_info("  aprs database clear                - Clear entire APRS database")
            print_info("  aprs database prune <days>         - Remove entries older than N days")
            return

        subcmd = args[0].lower()

        # Dispatch to subcommand handlers
        if subcmd in ("message", "msg"):
            await self._message_commands(args[1:])
        elif subcmd in ("wx", "weather"):
            await self._wx_commands(args[1:])
        elif subcmd in ("position", "pos"):
            await self._position_commands(args[1:])
        elif subcmd == "station":
            await self._station_commands(args[1:])
        elif subcmd in ("database", "db"):
            await self._database_commands(args[1:])
        else:
            print_error(f"Unknown APRS command: {subcmd}")
            print_info("Use 'aprs' with no args for help")

    async def _message_commands(self, args):
        """Handle APRS message subcommands."""
        if not args:
            print_error("Usage: aprs message <read|send|clear|monitor> ...")
            return

        action = args[0].lower()

        if action == "read":
            await self._message_read(args[1:])
        elif action == "send":
            await self._message_send(args[1:])
        elif action == "monitor":
            await self._message_monitor(args[1:])
        elif action == "clear":
            await self._message_clear()
        else:
            print_error(f"Unknown message action: {action}")
            print_error("Use: read, send, clear, monitor")

    async def _message_read(self, args):
        """Read APRS messages."""
        show_all = len(args) > 0 and args[0].lower() == "all"
        messages = self.aprs_manager.get_messages(unread_only=not show_all)

        if not messages:
            if show_all:
                print_info("No APRS messages")
            else:
                print_info("No unread APRS messages")
            return

        print_header(f"APRS Messages ({len(messages)})")
        for idx, msg in enumerate(messages, 1):
            time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")

            if msg.direction == "sent":
                # Sent message - show delivery status and recipient
                if msg.ack_received:
                    # Acknowledged - green checkmark
                    print_pt(HTML(f"[{idx}] <green>✓</green> {time_str} To: {msg.to_call}"))
                elif msg.failed:
                    # Failed after max retries - red X
                    status_info = " (digipeated)" if msg.digipeated else " (not sent)"
                    retry_info = f" tried {msg.retry_count}x"
                    print_pt(HTML(f"[{idx}] <red>✗</red> {time_str} To: {msg.to_call}{status_info}{retry_info}"))
                elif msg.digipeated:
                    # Digipeated but not ACKed - cyan arrow (on RF, waiting for recipient)
                    retry_info = f" (retry {msg.retry_count})" if msg.retry_count > 0 else ""
                    print_pt(HTML(f"[{idx}] <cyan>→</cyan> {time_str} To: {msg.to_call}{retry_info}"))
                else:
                    # Not digipeated yet - yellow dots (trying to get on RF)
                    retry_info = f" (retry {msg.retry_count})" if msg.retry_count > 0 else ""
                    print_pt(HTML(f"[{idx}] <yellow>⋯</yellow> {time_str} To: {msg.to_call}{retry_info}"))
                print_pt(f"  {msg.message}")
            else:
                # Received message - show read status and sender
                status = "NEW" if not msg.read else "READ"
                print_pt(f"[{idx}] [{status}] {time_str} From: {msg.from_call}")
                # Show message ID if present (in braces like APRS protocol)
                if msg.message_id:
                    print_pt(f"  {msg.message} {{{msg.message_id}}}")
                else:
                    print_pt(f"  {msg.message}")

        # Mark all as read
        marked = self.aprs_manager.mark_all_read()
        if marked > 0:
            print_info(f"Marked {marked} message(s) as read")

    async def _message_send(self, args):
        """Send APRS message."""
        if len(args) < 2:
            print_error("Usage: aprs message send <callsign> <message text>")
            print_error("Example: aprs message send K1MAL hello world")
            return

        to_call = args[0].upper()
        message_text = " ".join(args[1:])

        # Send APRS message
        await self.cmd_processor._send_aprs_message(to_call, message_text)

    async def _message_monitor(self, args):
        """List monitored messages."""
        if not args or args[0].lower() != "list":
            print_error("Usage: aprs message monitor list [count]")
            print_error("Example: aprs message monitor list 20")
            return

        # Get limit if specified
        limit = None
        if len(args) > 1:
            try:
                limit = int(args[1])
            except ValueError:
                print_error("Count must be a number")
                return

        messages = self.aprs_manager.get_monitored_messages(limit=limit)

        if not messages:
            print_info("No monitored messages")
            return

        if limit:
            print_header(f"Monitored APRS Messages (last {len(messages)})")
        else:
            print_header(f"Monitored APRS Messages ({len(messages)} total)")

        for idx, msg in enumerate(messages, 1):
            # Show from/to for monitored messages
            time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            msg_id_str = f" {{{msg.message_id}}}" if msg.message_id else ""
            print_pt(f"[{idx}] {time_str} {msg.from_call}>{msg.to_call}: {msg.message}{msg_id_str}")

    async def _message_clear(self):
        """Clear all messages."""
        count = self.aprs_manager.clear_messages()
        if count > 0:
            print_info(f"Cleared {count} message(s)")
        else:
            print_info("No messages to clear")

    async def _wx_commands(self, args):
        """Handle weather station listing."""
        if not args or args[0].lower() != "list":
            print_error("Usage: aprs wx list [last|name|temp|humidity|pressure]")
            return

        # Parse optional sort parameter
        sort_by = "last"  # Default: most recent first
        if len(args) > 1:
            sort_arg = args[1].lower()
            if sort_arg in ("name", "temp", "humidity", "pressure", "last"):
                sort_by = sort_arg
            else:
                print_error(f"Invalid sort order: {sort_arg}")
                print_error("Valid options: last, name, temp, humidity, pressure")
                return

        weather_stations = self.aprs_manager.get_weather_stations(sort_by=sort_by)

        if not weather_stations:
            print_info("No weather stations heard yet")
            return

        # Show sort order in header
        sort_labels = {
            "last": "by Last Update",
            "name": "by Name",
            "temp": "by Temperature",
            "humidity": "by Humidity",
            "pressure": "by Pressure",
        }
        sort_label = sort_labels.get(sort_by, "")
        print_header(f"Weather Stations ({len(weather_stations)}) - Sorted {sort_label}")

        # Table header
        print_pt(HTML("<b>Callsign     Grid      Temp    Humidity  Pressure  Last Update</b>"))
        print_pt(HTML("<gray>────────────────────────────────────────────────────────────────────</gray>"))

        # Table rows
        for station in weather_stations:
            fmt = APRSFormatters.format_weather(station)
            # Calculate grid from lat/lon if available
            if station.latitude and station.longitude:
                grid = latlon_to_maidenhead(station.latitude, station.longitude)
            else:
                grid = "---"
            last_update = station.timestamp.strftime("%H:%M:%S")
            print_pt(
                f"{fmt['station']:<12} {grid:<9} {fmt['temp']:<7} {fmt['humidity']:<9} {fmt['pressure']:<9} {last_update:<12}"
            )

    async def _position_commands(self, args):
        """Handle position listing."""
        if not args or args[0].lower() != "list":
            print_error("Usage: aprs position list")
            return

        positions = self.aprs_manager.get_position_reports()

        if not positions:
            print_info("No position reports received yet")
            return

        print_header(f"APRS Positions ({len(positions)})")

        # Table header
        print_pt(HTML("<b>Callsign     Latitude   Longitude  Grid      Symbol  Last Update</b>"))
        print_pt(HTML("<gray>───────────────────────────────────────────────────────────────────────</gray>"))

        # Table rows
        for position in positions:
            fmt = APRSFormatters.format_position(position)
            last_update = position.timestamp.strftime("%H:%M:%S")
            print_pt(
                f"{fmt['station']:<12} {fmt['latitude']:<10} {fmt['longitude']:<10} {fmt['grid']:<9} {fmt['symbol']:<7} {last_update:<12}"
            )

    async def _station_commands(self, args):
        """Handle station listing and detail commands."""
        if not args:
            print_error("Usage: aprs station <list|dx|show|receptions> ...")
            return

        action = args[0].lower()

        if action == "list":
            await self._station_list(args[1:])
        elif action == "dx":
            await self._station_dx(args[1:])
        elif action == "show":
            await self._station_show(args[1:])
        elif action in ("receptions", "events", "rx"):
            await self._station_receptions(args[1:])
        else:
            print_error(f"Unknown station action: {action}")
            print_error("Use: list, dx, show, receptions")

    async def _station_list(self, args):
        """List all heard stations."""
        # Parse optional sort parameter
        sort_by = "last"  # Default: most recent first
        if args:
            sort_arg = args[0].lower()
            if sort_arg in ("name", "packets", "last", "hops"):
                sort_by = sort_arg
            else:
                print_error(f"Invalid sort order: {sort_arg}")
                print_error("Valid options: name, packets, last, hops")
                return

        stations = self.aprs_manager.get_all_stations(sort_by=sort_by)

        if not stations:
            print_info("No stations heard yet")
            return

        # Show sort order in header
        sort_labels = {
            "name": "by Name",
            "packets": "by Packets",
            "last": "by Last Heard",
            "hops": "by Hops",
        }
        sort_label = sort_labels.get(sort_by, "")
        print_header(f"APRS Stations Heard ({len(stations)}) - Sorted {sort_label}")

        # Table header
        print_pt(HTML("<b>Callsign     Grid      Temp    Last Heard  Packets  Hops</b>"))
        print_pt(HTML("<gray>────────────────────────────────────────────────────────────────</gray>"))

        # Table rows
        for station in stations:
            fmt = APRSFormatters.format_station_table_row(station)
            hops_str = "RF" if fmt["hops"] == 0 else str(fmt["hops"]) if fmt["hops"] < 999 else "?"
            print_pt(
                f"{fmt['callsign']:<12} {fmt['grid']:<9} {fmt['temp']:<7} {fmt['last_heard']:<11} {fmt['packets']:<8} {hops_str:<4}"
            )

    async def _station_dx(self, args):
        """Show DX list - zero-hop stations sorted by distance."""
        # Get zero-hop stations
        stations = self.aprs_manager.get_zero_hop_stations()

        if not stations:
            print_info("No zero-hop stations heard yet")
            return

        # Get our position from GPS or MYLOCATION
        our_lat = None
        our_lon = None

        # Try GPS first (gps_position is a dict with 'latitude'/'longitude' keys)
        if self.cmd_processor.gps_position:
            our_lat = self.cmd_processor.gps_position.get('latitude')
            our_lon = self.cmd_processor.gps_position.get('longitude')

        # Fall back to MYLOCATION if GPS not available or incomplete
        if our_lat is None or our_lon is None:
            # Fall back to MYLOCATION
            mylocation = self.tnc_config.get("MYLOCATION")
            if mylocation:
                try:
                    our_lat, our_lon = maidenhead_to_latlon(mylocation)
                except ValueError:
                    print_error(f"Invalid MYLOCATION: {mylocation}")
                    return
            else:
                print_error("No GPS lock and MYLOCATION not configured")
                print_info("Set your position with: config set MYLOCATION <grid>")
                print_info("Example: config set MYLOCATION FN31pr")
                return

        # Calculate distances and filter stations with positions
        dx_list = []
        for station in stations:
            if station.last_position:
                distance = calculate_distance_miles(
                    our_lat, our_lon,
                    station.last_position.latitude,
                    station.last_position.longitude
                )
                dx_list.append({
                    'station': station,
                    'distance': distance,
                    'grid': station.last_position.grid_square
                })

        # Sort by distance descending (furthest first)
        dx_list.sort(key=lambda x: x['distance'], reverse=True)

        # Show header
        print_header(f"DX List - Zero-Hop Stations ({len(dx_list)})")

        if not dx_list:
            print_info("Zero-hop stations heard but none have position data")
            return

        # Table header
        print_pt(HTML("<b>Callsign     Grid      Distance  Last Heard  Zero-Hop Packets</b>"))
        print_pt(HTML("<gray>──────────────────────────────────────────────────────────────</gray>"))

        # Table rows
        for item in dx_list:
            station = item['station']
            distance = item['distance']
            last_heard = station.last_heard.strftime("%H:%M:%S")

            # Format distance with proper units
            distance_str = f"{distance:.1f} mi"

            print_pt(
                f"{station.callsign:<12} {item['grid']:<9} {distance_str:<9} "
                f"{last_heard:<11} {station.zero_hop_packet_count:<16}"
            )

    async def _station_show(self, args):
        """Show detailed station information."""
        if not args:
            print_error("Usage: aprs station show <callsign>")
            print_error("Example: aprs station show N1TKS")
            return

        callsign = args[0].upper()
        station = self.aprs_manager.get_station(callsign)

        if not station:
            print_error(f"Station {callsign} not found")
            print_info("Use 'aprs station list' to see all stations")
            return

        print_header(f"Station Details: {callsign}")
        # Get WXTREND threshold from config
        try:
            threshold = float(self.tnc_config.get("WXTREND"))
        except (ValueError, TypeError):
            threshold = 0.3  # Default fallback
        detail = APRSFormatters.format_station_detail(station, pressure_threshold=threshold)
        print_pt(detail)

    async def _station_receptions(self, args):
        """Show reception event history for a station."""
        if not args:
            print_error("Usage: aprs station receptions <callsign> [N]")
            print_error("Example: aprs station receptions N1TKS 50")
            print_error("Shows last N reception events (default: 20)")
            return

        callsign = args[0].upper()
        station = self.aprs_manager.get_station(callsign)

        if not station:
            print_error(f"Station {callsign} not found")
            print_info("Use 'aprs station list' to see all stations")
            return

        # Parse optional limit
        limit = 20  # Default
        if len(args) > 1:
            try:
                limit = int(args[1])
                if limit < 1:
                    print_error("Limit must be at least 1")
                    return
                if limit > 200:
                    print_warning(f"Limiting to 200 (max stored)")
                    limit = 200
            except ValueError:
                print_error(f"Invalid number: {args[1]}")
                return

        if not station.receptions:
            print_info(f"No reception events recorded for {callsign}")
            return

        # Sort receptions newest-first before displaying (handles new packets added after migration)
        sorted_receptions = sorted(station.receptions, key=lambda r: r.timestamp, reverse=True)
        receptions = sorted_receptions[:limit]
        total_count = len(station.receptions)

        print_header(f"Reception Events: {callsign} (Showing {len(receptions)} of {total_count})")

        # Table header
        print_pt(HTML("<b>Time                 Type      Hops  Direct  Relay      Path</b>"))
        print_pt(HTML("<gray>────────────────────────────────────────────────────────────────────────────────</gray>"))

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        for i, rx in enumerate(receptions, 1):
            # Format timestamp with relative time
            time_diff = now - rx.timestamp
            if time_diff.total_seconds() < 60:
                time_str = "just now"
            elif time_diff.total_seconds() < 3600:
                mins = int(time_diff.total_seconds() / 60)
                time_str = f"{mins}m ago"
            elif time_diff.total_seconds() < 86400:
                hours = int(time_diff.total_seconds() / 3600)
                time_str = f"{hours}h ago"
            else:
                days = int(time_diff.total_seconds() / 86400)
                time_str = f"{days}d ago"

            # Format timestamp in LOCAL time (convert from UTC storage)
            local_ts = rx.timestamp.astimezone()  # Converts to system local timezone
            ts = local_ts.strftime("%m/%d %H:%M:%S")
            timestamp_display = f"{ts} ({time_str:>8})"

            # Packet type (truncated to 9 chars)
            pkt_type = (rx.packet_type or "unknown")[:9]

            # Hop count
            if rx.hop_count == 999:
                hops_str = "iGate"
            elif rx.hop_count == 0:
                hops_str = "0"
            else:
                hops_str = str(rx.hop_count)

            # Direct RF indicator
            direct_str = "✓" if rx.direct_rf else "✗"

            # Relay (iGate callsign)
            relay_str = (rx.relay_call or "-")[:10]

            # Digipeater path (truncated to fit)
            if rx.digipeater_path:
                path_str = ",".join(rx.digipeater_path)
                if len(path_str) > 35:
                    path_str = path_str[:32] + "..."
            else:
                path_str = "-"

            # Color coding based on type
            if rx.hop_count == 0:
                # Zero-hop (direct RF, no digipeaters) - green
                color = "green"
            elif rx.direct_rf:
                # RF with digipeaters - yellow
                color = "yellow"
            else:
                # iGate - blue
                color = "cyan"

            line = f"<{color}>{timestamp_display:20} {pkt_type:9} {hops_str:5} {direct_str:7} {relay_str:10} {path_str}</{color}>"
            print_pt(HTML(line))

        # Summary footer
        print_pt(HTML("<gray>────────────────────────────────────────────────────────────────────────────────</gray>"))

        # Calculate statistics
        zero_hop_count = sum(1 for r in station.receptions if r.hop_count == 0)
        rf_count = sum(1 for r in station.receptions if r.direct_rf)
        igate_count = sum(1 for r in station.receptions if not r.direct_rf)

        stats = f"Total: {total_count} receptions | Zero-hop: {zero_hop_count} | RF: {rf_count} | iGate: {igate_count}"
        print_info(stats)

    async def _database_commands(self, args):
        """Handle database operations."""
        if not args:
            print_error("Usage: aprs database <save|clear|prune> ...")
            print_error("  aprs database save          - Manually save database to disk")
            print_error("  aprs database clear         - Clear entire APRS database")
            print_error("  aprs database prune <days>  - Remove entries older than N days")
            return

        action = args[0].lower()

        if action == "save":
            await self._database_save()
        elif action == "clear":
            await self._database_clear()
        elif action == "prune":
            await self._database_prune(args[1:])
        else:
            print_error(f"Unknown database action: {action}")
            print_error("Use: save, clear, prune")

    async def _database_save(self):
        """Manually save the database."""
        print_info("Saving APRS database...")
        count = self.aprs_manager.save_database()
        if count > 0:
            # Get file size
            db_file = self.aprs_manager.db_file
            try:
                size = os.path.getsize(db_file)
                size_kb = size / 1024
                print_info(f"✓ Saved {count} station(s) to {db_file}")
                print_info(f"  File size: {size_kb:.1f} KB")
            except Exception:
                print_info(f"✓ Saved {count} station(s)")
        else:
            print_error("Failed to save database (check error messages above)")

    async def _database_clear(self):
        """Clear entire database."""
        # Confirm before clearing
        print_warning("This will delete ALL APRS stations and messages from the database!")
        print_warning("Database file will be deleted on next save.")
        print_info("Type 'yes' to confirm:")

        # Note: This is a simplified version - in real implementation,
        # we'd need to get user input, but that requires prompt_toolkit session
        # For now, just print the warning
        print_error("Database clear requires interactive confirmation")
        print_info("Use the web UI or manually delete ~/.aprs_database.json")

    async def _database_prune(self, args):
        """Prune old entries from database."""
        if not args:
            print_error("Usage: aprs database prune <days>")
            print_error("Example: aprs database prune 30")
            return

        try:
            days = int(args[0])
            if days < 1:
                print_error("Days must be at least 1")
                return
        except ValueError:
            print_error("Days must be a number")
            return

        print_info(f"Pruning entries older than {days} days...")
        count = self.aprs_manager.prune_database(days)

        if count > 0:
            print_info(f"✓ Removed {count} old station(s)")
            # Auto-save after pruning
            saved = self.aprs_manager.save_database()
            if saved > 0:
                print_info(f"✓ Database saved ({saved} stations remaining)")
        else:
            print_info("No old entries to prune")
