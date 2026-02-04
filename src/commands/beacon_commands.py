"""
BEACON command handlers for GPS position beaconing.

Handles automatic and manual APRS position beacon transmission.
"""

from .base import CommandHandler, command
from src.utils import print_pt, print_info, print_error
from datetime import datetime


class BeaconCommandHandler(CommandHandler):
    """Handles BEACON configuration and transmission commands."""

    def __init__(self, cmd_processor):
        """
        Initialize beacon command handler.

        Args:
            cmd_processor: Reference to main CommandProcessor instance
        """
        self.cmd_processor = cmd_processor
        self.tnc_config = cmd_processor.tnc_config
        super().__init__()

    @command("BEACON",
             help_text="GPS beacon configuration and control",
             usage="BEACON [ON|OFF|INTERVAL|PATH|SYMBOL|COMMENT|NOW]",
             category="aprs")
    async def beacon(self, args):
        """Configure and control GPS position beaconing."""
        if not args:
            # Show beacon status
            status = self.tnc_config.get("BEACON")
            interval = self.tnc_config.get("BEACON_INTERVAL")
            path = self.tnc_config.get("BEACON_PATH")
            symbol = self.tnc_config.get("BEACON_SYMBOL")
            comment = self.tnc_config.get("BEACON_COMMENT")
            print_pt(f"BEACON: {status}")
            print_pt(f"  Interval: {interval} minutes")
            print_pt(f"  Path: {path}")
            print_pt(f"  Symbol: {symbol}")
            print_pt(f"  Comment: {comment}")

            # Show last beacon time
            if self.cmd_processor.last_beacon_time:
                elapsed = (datetime.now() - self.cmd_processor.last_beacon_time).total_seconds()
                elapsed_min = int(elapsed // 60)
                elapsed_sec = int(elapsed % 60)
                time_str = self.cmd_processor.last_beacon_time.strftime("%H:%M:%S")
                print_pt(f"  Last beacon: {time_str} ({elapsed_min}m {elapsed_sec}s ago)")

                # Show time until next beacon
                if status == "ON":
                    beacon_interval_sec = int(interval) * 60
                    remaining = max(0, beacon_interval_sec - elapsed)
                    remaining_min = int(remaining // 60)
                    remaining_sec = int(remaining % 60)
                    if remaining > 0:
                        print_pt(f"  Next beacon: in {remaining_min}m {remaining_sec}s")
                    else:
                        print_pt(f"  Next beacon: due now")
            else:
                print_pt(f"  Last beacon: never")

            if self.cmd_processor.gps_locked and self.cmd_processor.gps_position:
                pos = self.cmd_processor.gps_position
                print_pt(f"  GPS: {pos['latitude']:.6f}, {pos['longitude']:.6f} (LOCKED)")
            else:
                print_pt(f"  GPS: NO LOCK")
            return

        subcmd = args[0].upper()

        # Dispatch to subcommand handlers
        handler_map = {
            "ON": self._beacon_on,
            "OFF": self._beacon_off,
            "INTERVAL": self._beacon_interval,
            "PATH": self._beacon_path,
            "SYMBOL": self._beacon_symbol,
            "COMMENT": self._beacon_comment,
            "NOW": self._beacon_now
        }

        if subcmd in handler_map:
            await handler_map[subcmd](args[1:])
        else:
            print_error("Usage: BEACON <ON|OFF|INTERVAL|PATH|SYMBOL|COMMENT|NOW>")

    async def _beacon_on(self, args):
        """Enable automatic beaconing."""
        self.tnc_config.set("BEACON", "ON")
        print_info("BEACON set to ON")

    async def _beacon_off(self, args):
        """Disable automatic beaconing."""
        self.tnc_config.set("BEACON", "OFF")
        print_info("BEACON set to OFF")

    async def _beacon_interval(self, args):
        """Set beacon interval in minutes."""
        if not args:
            print_error("Usage: BEACON INTERVAL <minutes>")
            return
        try:
            interval = int(args[0])
            if interval < 1:
                print_error("Interval must be at least 1 minute")
                return
            self.tnc_config.set("BEACON_INTERVAL", str(interval))
            print_info(f"Beacon interval set to {interval} minutes")
        except ValueError:
            print_error("Invalid interval value")

    async def _beacon_path(self, args):
        """Set beacon digipeater path."""
        if not args:
            print_error("Usage: BEACON PATH <path>")
            return
        path = " ".join(args)
        self.tnc_config.set("BEACON_PATH", path)
        print_info(f"Beacon path set to {path}")

    async def _beacon_symbol(self, args):
        """Set beacon APRS symbol."""
        if not args:
            print_error("Usage: BEACON SYMBOL <table><code>")
            print_error("Example: BEACON SYMBOL /[ (jogger)")
            return
        symbol = args[0]
        if len(symbol) != 2:
            print_error("Symbol must be exactly 2 characters (table + code)")
            return
        self.tnc_config.set("BEACON_SYMBOL", symbol)
        print_info(f"Beacon symbol set to {symbol}")

    async def _beacon_comment(self, args):
        """Set beacon comment text."""
        if not args:
            print_error("Usage: BEACON COMMENT <text>")
            return
        comment = " ".join(args)
        self.tnc_config.set("BEACON_COMMENT", comment)
        print_info(f"Beacon comment set to: {comment}")

    async def _beacon_now(self, args):
        """Send beacon immediately."""
        # Try GPS first, fall back to MYLOCATION
        if self.cmd_processor.gps_locked and self.cmd_processor.gps_position:
            print_info("Sending beacon now (GPS)...")
            await self.cmd_processor._send_position_beacon(self.cmd_processor.gps_position)
        elif self.tnc_config.get("MYLOCATION"):
            print_info("Sending beacon now (MYLOCATION)...")
            await self.cmd_processor._send_position_beacon(None)  # Use MYLOCATION
        else:
            print_error("No position available (GPS unavailable and MYLOCATION not set)")
