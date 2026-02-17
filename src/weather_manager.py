"""Weather Station Manager.

Manages weather station connections and provides a unified interface for
fetching weather data from various hardware vendors (Ecowitt, Davis, etc.).

Supports both network-based (HTTP/TCP) and serial-based weather stations.
"""

import asyncio
import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.utils import print_debug, print_error, print_info
from src.weather_stations import EcowittWeatherStation, WeatherData, WeatherStation


class WeatherStationManager:
    """Manages weather station connection and data fetching.

    Provides a unified interface for different weather station backends,
    handling connection lifecycle, periodic updates, and error recovery.

    Example:
        manager = WeatherStationManager()
        manager.configure(backend='ecowitt', address='192.168.1.124')
        await manager.connect()
        data = await manager.get_current_weather()
    """

    # Supported weather station backends
    BACKENDS = {
        'ecowitt': {
            'name': 'Ecowitt',
            'class': EcowittWeatherStation,
            'connection_type': 'http',
            'default_port': 80,
            'description': 'Ecowitt GW1000/GW1100/GW2000 Gateway'
        },
    }

    def __init__(self):
        """Initialize weather station manager."""
        self.backend: Optional[str] = None
        self.address: Optional[str] = None
        self.port: Optional[int] = None
        self.enabled: bool = False
        self.update_interval: int = 300  # seconds (5 minutes default)
        self.average_wind: bool = True  # Enable wind averaging for beacons

        self._station: Optional[WeatherStation] = None
        self._last_data: Optional[WeatherData] = None
        self._last_update: Optional[datetime] = None
        self._update_task: Optional[asyncio.Task] = None
        self._connected: bool = False
        self._weather_history: List[WeatherData] = []  # Recent readings for averaging

    def configure(
        self,
        backend: Optional[str] = None,
        address: Optional[str] = None,
        port: Optional[int] = None,
        enabled: Optional[bool] = None,
        update_interval: Optional[int] = None
    ) -> bool:
        """Configure weather station settings.

        Args:
            backend: Backend type ('ecowitt', 'davis', etc.)
            address: IP address, hostname, or serial port path
            port: Port number (for network stations)
            enabled: Enable/disable weather station
            update_interval: Update interval in seconds

        Returns:
            True if configuration valid, False otherwise
        """
        # Validate backend
        if backend is not None:
            if backend not in self.BACKENDS:
                valid = ', '.join(self.BACKENDS.keys())
                print_error(f"Invalid backend '{backend}'. Valid: {valid}")
                return False
            self.backend = backend

        # Validate address
        if address is not None:
            if not address.strip():
                print_error("Address cannot be empty")
                return False
            self.address = address.strip()

        # Validate port
        if port is not None:
            if not (1 <= port <= 65535):
                print_error(f"Invalid port {port}. Must be 1-65535")
                return False
            self.port = port

        # Set enabled state
        if enabled is not None:
            self.enabled = enabled

        # Validate update interval
        if update_interval is not None:
            if not (30 <= update_interval <= 3600):
                print_error(f"Invalid interval {update_interval}. Must be 30-3600 seconds")
                return False
            self.update_interval = update_interval

        return True

    async def connect(self) -> bool:
        """Connect to configured weather station.

        Returns:
            True if connection successful, False otherwise
        """
        if not self.enabled:
            print_debug("Weather station not enabled", level=3)
            return False

        if not self.backend or not self.address:
            print_error("Weather station not configured (set WX_BACKEND and WX_ADDRESS)")
            return False

        # Disconnect existing connection
        if self._station:
            await self.disconnect()

        try:
            # Get backend info
            backend_info = self.BACKENDS[self.backend]

            # Determine port
            port = self.port
            if port is None:
                port = backend_info.get('default_port', 80)

            # Create station instance
            station_class = backend_info['class']
            connection_type = backend_info['connection_type']

            if connection_type == 'http':
                self._station = station_class(
                    host=self.address,
                    port=port
                )
            elif connection_type == 'serial':
                # Serial stations don't use port
                self._station = station_class(
                    port=self.address  # Serial path
                )
            else:
                print_error(f"Unknown connection type: {connection_type}")
                return False

            # Test connection
            if not await self._station.test_connection():
                print_error(f"Failed to connect to {backend_info['name']} at {self.address}")
                self._station = None
                return False

            self._connected = True
            print_info(f"✓ Connected to {backend_info['name']}")

            # Get station info
            info = self._station.get_station_info()
            print_debug(f"Station info: {info}", level=2)

            # Start periodic updates
            await self.start_updates()

            return True

        except Exception as e:
            print_error(f"Failed to connect to weather station: {e}")
            self._station = None
            self._connected = False
            return False

    async def disconnect(self):
        """Disconnect from weather station."""
        # Stop updates
        await self.stop_updates()

        # Clear station
        self._station = None
        self._connected = False
        print_info("Weather station disconnected")

    async def start_updates(self):
        """Start periodic weather data updates."""
        if self._update_task and not self._update_task.done():
            return  # Already running

        self._update_task = asyncio.create_task(self._update_loop())
        print_debug(f"Weather updates started (interval: {self.update_interval}s)", level=2)

    async def stop_updates(self):
        """Stop periodic weather data updates."""
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
            self._update_task = None
            print_debug("Weather updates stopped", level=2)

    async def _update_loop(self):
        """Background task for periodic weather updates."""
        while True:
            try:
                # Fetch weather data
                data = await self.get_current_weather()
                if data:
                    self._last_data = data
                    self._last_update = datetime.now(timezone.utc)

                    # Add to history for wind averaging
                    self._weather_history.append(data)

                    # Keep last 60 minutes of readings (for longest beacon interval)
                    # Assuming update_interval >= 30s, max 120 readings
                    max_history = 120
                    if len(self._weather_history) > max_history:
                        self._weather_history = self._weather_history[-max_history:]

                    print_debug(
                        f"Weather updated: {data.temperature_outdoor}°F, "
                        f"{data.pressure_relative:.1f}mb, "
                        f"wind {data.wind_speed}mph",
                        level=3
                    )

                # Wait for next update
                await asyncio.sleep(self.update_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print_error(f"Weather update failed: {e}")
                # Continue running despite error
                await asyncio.sleep(self.update_interval)

    async def get_current_weather(self) -> Optional[WeatherData]:
        """Fetch current weather data from station.

        Returns:
            WeatherData object, or None if unavailable
        """
        if not self._station or not self._connected:
            return None

        try:
            data = await self._station.fetch_weather()
            return data
        except Exception as e:
            print_error(f"Failed to fetch weather data: {e}")
            return None

    def get_cached_weather(self) -> Optional[WeatherData]:
        """Get last cached weather data without fetching.

        Returns:
            Most recently fetched WeatherData, or None if unavailable
        """
        return self._last_data

    def get_beacon_weather(self, beacon_interval_seconds: int = 600) -> Optional[WeatherData]:
        """Get weather data for beaconing with wind averaging.

        Averages wind speed over the beacon interval period and uses peak gust.
        Returns instantaneous values for temperature, humidity, and pressure.

        This follows meteorological best practices:
        - Wind speed: Mean average over beacon period (sustained wind)
        - Wind gust: Maximum gust during beacon period (peak conditions)
        - Wind direction: Vector average (proper meteorological method)
        - Other parameters: Instantaneous (slow-changing values)

        Args:
            beacon_interval_seconds: Beacon interval in seconds (default: 600 = 10 min)

        Returns:
            WeatherData with averaged wind, or None if unavailable
        """
        if not self._last_data:
            return None

        # If wind averaging disabled, return instantaneous data
        if not self.average_wind:
            return self._last_data

        # If no history available, return instantaneous data
        if not self._weather_history:
            return self._last_data

        # Calculate time window
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=beacon_interval_seconds)

        # Collect readings within beacon interval
        recent_readings = [
            r for r in self._weather_history
            if r.timestamp >= window_start
        ]

        # Fallback to instantaneous if insufficient data
        if not recent_readings:
            print_debug(
                f"No weather readings in last {beacon_interval_seconds}s, "
                "using instantaneous data",
                level=3
            )
            return self._last_data

        # Calculate averaged wind speed (sustained)
        wind_speeds = [r.wind_speed for r in recent_readings if r.wind_speed is not None]
        if wind_speeds:
            avg_wind_speed = sum(wind_speeds) / len(wind_speeds)
        else:
            avg_wind_speed = None

        # Calculate peak gust
        gusts = [r.wind_gust for r in recent_readings if r.wind_gust is not None]
        if gusts:
            peak_gust = max(gusts)
        else:
            peak_gust = None

        # Calculate vector-averaged wind direction
        directions = [r.wind_direction for r in recent_readings if r.wind_direction is not None]
        if directions:
            avg_direction = self._vector_average_direction(directions)
        else:
            avg_direction = None

        # Create weather data with averaged wind
        beacon_weather = WeatherData(
            timestamp=now,
            temperature_outdoor=self._last_data.temperature_outdoor,  # Instantaneous
            temperature_indoor=self._last_data.temperature_indoor,    # Instantaneous
            dew_point=self._last_data.dew_point,                      # Instantaneous
            humidity_outdoor=self._last_data.humidity_outdoor,        # Instantaneous
            humidity_indoor=self._last_data.humidity_indoor,          # Instantaneous
            pressure_relative=self._last_data.pressure_relative,      # Instantaneous
            pressure_absolute=self._last_data.pressure_absolute,      # Instantaneous
            wind_speed=avg_wind_speed,                                # AVERAGED ✓
            wind_gust=peak_gust,                                      # PEAK ✓
            wind_direction=avg_direction,                             # VECTOR AVERAGE ✓
            rain_rate=self._last_data.rain_rate,                      # Instantaneous
            rain_event=self._last_data.rain_event,                    # Cumulative
            rain_hourly=self._last_data.rain_hourly,                  # Cumulative
            rain_daily=self._last_data.rain_daily,                    # Cumulative
            rain_weekly=self._last_data.rain_weekly,                  # Cumulative
            rain_monthly=self._last_data.rain_monthly,                # Cumulative
            rain_yearly=self._last_data.rain_yearly,                  # Cumulative
            uv_index=self._last_data.uv_index,                        # Instantaneous
            solar_radiation=self._last_data.solar_radiation,          # Instantaneous
        )

        if wind_speeds or gusts or directions:
            print_debug(
                f"Beacon weather (averaged over {beacon_interval_seconds}s): "
                f"wind {avg_wind_speed:.1f}mph (avg of {len(wind_speeds)} readings), "
                f"gust {peak_gust:.1f}mph (peak), "
                f"dir {avg_direction}° (vector avg of {len(directions)} readings)",
                level=2
            )

        return beacon_weather

    def _vector_average_direction(self, directions: List[int]) -> int:
        """Calculate vector average of wind directions.

        Properly handles the 0°/360° discontinuity by converting directions
        to unit vectors, averaging them, and converting back to degrees.

        This is the meteorologically correct method for averaging wind directions.

        Args:
            directions: List of wind directions in degrees (0-360)

        Returns:
            Average direction in degrees (0-360)
        """
        if not directions:
            return 0

        # Convert to radians and calculate vector components
        sin_sum = sum(math.sin(math.radians(d)) for d in directions)
        cos_sum = sum(math.cos(math.radians(d)) for d in directions)

        # Calculate average direction
        avg_rad = math.atan2(sin_sum, cos_sum)
        avg_deg = math.degrees(avg_rad)

        # Normalize to 0-360
        return int(avg_deg % 360)

    def get_status(self) -> dict:
        """Get weather station status.

        Returns:
            Dictionary with status information
        """
        status = {
            'enabled': self.enabled,
            'configured': self.backend is not None and self.address is not None,
            'connected': self._connected,
            'backend': self.backend,
            'address': self.address,
            'port': self.port,
            'update_interval': self.update_interval,
            'last_update': self._last_update.isoformat() if self._last_update else None,
            'has_data': self._last_data is not None,
        }

        if self._station:
            status['station_info'] = self._station.get_station_info()

        return status

    @classmethod
    def list_backends(cls) -> dict:
        """List available weather station backends.

        Returns:
            Dictionary of backend_id -> backend_info
        """
        return cls.BACKENDS.copy()
