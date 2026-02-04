"""
Weather station command handlers.

Handles configuration of weather station integration for APRS beaconing.
"""

from .base import CommandHandler, command
from src.utils import print_pt, print_info, print_error
from src.weather_manager import WeatherStationManager


class WeatherCommandHandler(CommandHandler):
    """Handles weather station configuration commands."""

    def __init__(self, cmd_processor):
        """
        Initialize weather command handler.

        Args:
            cmd_processor: Reference to main CommandProcessor instance
        """
        self.cmd_processor = cmd_processor
        self.tnc_config = cmd_processor.tnc_config
        self.weather_manager = cmd_processor.weather_manager
        super().__init__()

    @command("WX_ENABLE",
             help_text="Enable/disable weather station integration",
             usage="WX_ENABLE [ON|OFF]",
             category="weather")
    async def wx_enable(self, args):
        """Enable or disable weather station integration."""
        if not args:
            status = self.tnc_config.get("WX_ENABLE")
            print_pt(f"WX_ENABLE: {status}")
            return

        value = args[0].upper()
        if value not in ["ON", "OFF"]:
            print_error("Usage: WX_ENABLE <ON|OFF>")
            return

        self.tnc_config.set("WX_ENABLE", value)
        enabled = (value == "ON")
        self.weather_manager.configure(enabled=enabled)

        if enabled and not self.weather_manager._connected:
            # Try to connect
            await self.weather_manager.connect()
        elif not enabled and self.weather_manager._connected:
            # Disconnect
            await self.weather_manager.disconnect()

        print_info(f"WX_ENABLE set to {value}")

    @command("WX_BACKEND",
             help_text="Set weather station backend type",
             usage="WX_BACKEND [backend]",
             category="weather")
    async def wx_backend(self, args):
        """Set weather station backend (ecowitt, weewx, etc)."""
        if not args:
            backend = self.tnc_config.get("WX_BACKEND")
            print_pt(f"WX_BACKEND: {backend}")
            print_pt("")
            print_pt("Available backends:")
            for backend_id, info in WeatherStationManager.list_backends().items():
                print_pt(f"  {backend_id:12} - {info['description']}")
            return

        backend = args[0].lower()
        if self.weather_manager.configure(backend=backend):
            self.tnc_config.set("WX_BACKEND", backend)
            print_info(f"WX_BACKEND set to {backend}")

    @command("WX_ADDRESS",
             help_text="Set weather station host/IP address",
             usage="WX_ADDRESS [address]",
             category="weather")
    async def wx_address(self, args):
        """Set weather station server address."""
        if not args:
            address = self.tnc_config.get("WX_ADDRESS")
            print_pt(f"WX_ADDRESS: {address or '(not set)'}")
            return

        address = args[0]
        if self.weather_manager.configure(address=address):
            self.tnc_config.set("WX_ADDRESS", address)
            print_info(f"WX_ADDRESS set to {address}")

    @command("WX_PORT",
             help_text="Set weather station port number",
             usage="WX_PORT [port]",
             category="weather")
    async def wx_port(self, args):
        """Set weather station server port."""
        if not args:
            port = self.tnc_config.get("WX_PORT")
            print_pt(f"WX_PORT: {port or '(auto)'}")
            return

        try:
            port = int(args[0])
            if self.weather_manager.configure(port=port):
                self.tnc_config.set("WX_PORT", str(port))
                print_info(f"WX_PORT set to {port}")
        except ValueError:
            print_error("WX_PORT must be a number (1-65535)")

    @command("WX_INTERVAL",
             help_text="Set weather update interval (seconds)",
             usage="WX_INTERVAL [seconds]",
             category="weather")
    async def wx_interval(self, args):
        """Set weather data update interval."""
        if not args:
            interval = self.tnc_config.get("WX_INTERVAL")
            print_pt(f"WX_INTERVAL: {interval} seconds")
            return

        try:
            interval = int(args[0])
            if self.weather_manager.configure(update_interval=interval):
                self.tnc_config.set("WX_INTERVAL", str(interval))
                print_info(f"WX_INTERVAL set to {interval} seconds")
        except ValueError:
            print_error("WX_INTERVAL must be a number (30-3600)")

    @command("WX_AVERAGE_WIND",
             help_text="Enable/disable wind speed averaging",
             usage="WX_AVERAGE_WIND [ON|OFF]",
             category="weather")
    async def wx_average_wind(self, args):
        """Enable or disable wind speed averaging for beacons."""
        if not args:
            status = self.tnc_config.get("WX_AVERAGE_WIND")
            print_pt(f"WX_AVERAGE_WIND: {status}")
            print_pt("")
            print_pt("Wind averaging for beacons:")
            print_pt("  ON  - Average wind speed over beacon interval (recommended)")
            print_pt("  OFF - Use instantaneous wind reading")
            return

        value = args[0].upper()
        if value not in ["ON", "OFF"]:
            print_error("Usage: WX_AVERAGE_WIND <ON|OFF>")
            return

        self.tnc_config.set("WX_AVERAGE_WIND", value)
        self.weather_manager.average_wind = (value == "ON")
        print_info(f"WX_AVERAGE_WIND set to {value}")

    @command("WXTREND",
             help_text="Set pressure trend threshold for forecasting",
             usage="WXTREND [threshold]",
             category="weather")
    async def wxtrend(self, args):
        """Set pressure tendency threshold for Zambretti forecasting."""
        if not args:
            threshold = self.tnc_config.get("WXTREND")
            print_pt(f"WXTREND: {threshold} mb/hr")
            print_pt("")
            print_pt("Pressure tendency threshold for Zambretti weather forecasting:")
            print_pt("  - Determines when pressure is 'rising', 'falling', or 'steady'")
            print_pt("  - Higher values = less sensitive (fewer weather change predictions)")
            print_pt("  - Lower values = more sensitive (more weather change predictions)")
            print_pt("")
            print_pt("Recommended values:")
            print_pt("  0.17 - WMO/NOAA standard (0.5 mb in 3 hours)")
            print_pt("  0.30 - Default (conservative, fewer false alarms)")
            print_pt("  0.50 - Very conservative")
            return

        try:
            value = float(args[0])
            if value < 0.05 or value > 1.0:
                print_error("WXTREND must be between 0.05 and 1.0 mb/hr")
                return

            self.tnc_config.set("WXTREND", str(value))
            print_info(f"WXTREND set to {value} mb/hr")
            print_pt("")
            print_pt("Note: This affects Zambretti forecasts for all weather stations")
        except ValueError:
            print_error("WXTREND must be a number (e.g., 0.3)")
