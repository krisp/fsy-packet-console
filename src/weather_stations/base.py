"""Abstract base class for weather station integrations.

Defines the standard interface that all weather station implementations must follow,
ensuring consistent data format and behavior across different hardware vendors.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class WeatherData:
    """Standardized weather data structure.

    All measurements use standard units for consistency:
    - Temperature: Fahrenheit
    - Pressure: millibars (mb)
    - Wind speed: miles per hour (mph)
    - Wind direction: degrees (0-360)
    - Humidity: percentage (0-100)
    - Rain: inches
    """

    timestamp: datetime

    # Temperature
    temperature_outdoor: Optional[float] = None  # °F
    temperature_indoor: Optional[float] = None  # °F
    dew_point: Optional[float] = None  # °F

    # Humidity
    humidity_outdoor: Optional[int] = None  # %
    humidity_indoor: Optional[int] = None  # %

    # Pressure
    pressure_absolute: Optional[float] = None  # mb
    pressure_relative: Optional[float] = None  # mb (sea-level adjusted)

    # Wind
    wind_speed: Optional[float] = None  # mph
    wind_gust: Optional[float] = None  # mph
    wind_direction: Optional[int] = None  # degrees (0-360)

    # Precipitation
    rain_rate: Optional[float] = None  # in/hr
    rain_event: Optional[float] = None  # in
    rain_hourly: Optional[float] = None  # in
    rain_daily: Optional[float] = None  # in
    rain_weekly: Optional[float] = None  # in
    rain_monthly: Optional[float] = None  # in
    rain_yearly: Optional[float] = None  # in

    # Solar/UV (optional - not all stations have these)
    solar_radiation: Optional[float] = None  # W/m²
    uv_index: Optional[float] = None

    # Station metadata
    station_model: Optional[str] = None
    firmware_version: Optional[str] = None

    def to_aprs_format(self) -> dict:
        """Convert to APRS weather format.

        Returns:
            Dictionary with APRS weather fields suitable for
            APRSManager.send_weather_report()
        """
        return {
            'temperature': self.temperature_outdoor,
            'humidity': self.humidity_outdoor,
            'pressure': self.pressure_relative,
            'wind_speed': self.wind_speed,
            'wind_gust': self.wind_gust,
            'wind_direction': self.wind_direction,
            'rain_1h': self.rain_hourly,
            'rain_24h': self.rain_daily,
            'rain_since_midnight': self.rain_event,
        }


class WeatherStation(ABC):
    """Abstract base class for weather station integrations.

    Subclasses must implement methods to fetch and parse weather data
    from specific hardware vendors (Ecowitt, Davis, Ambient, etc.).

    Example usage:
        station = EcowittWeatherStation(host='192.168.1.124')
        data = await station.fetch_weather()
        if data:
            print(f"Temperature: {data.temperature_outdoor}°F")
    """

    def __init__(self, host: str, port: Optional[int] = None):
        """Initialize weather station connection.

        Args:
            host: IP address or hostname of weather station
            port: Optional port number (default varies by vendor)
        """
        self.host = host
        self.port = port
        self._last_fetch: Optional[datetime] = None
        self._last_data: Optional[WeatherData] = None

    @abstractmethod
    async def fetch_weather(self) -> Optional[WeatherData]:
        """Fetch current weather data from station.

        Returns:
            WeatherData object with current conditions, or None if fetch failed

        Raises:
            ConnectionError: If unable to connect to weather station
            ValueError: If response data is invalid or unparseable
        """
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """Test connectivity to weather station.

        Returns:
            True if station is reachable and responding, False otherwise
        """
        pass

    @abstractmethod
    def get_station_info(self) -> dict:
        """Get static information about the weather station.

        Returns:
            Dictionary with station metadata (model, firmware, etc.)
        """
        pass

    @property
    def last_fetch_time(self) -> Optional[datetime]:
        """Timestamp of last successful data fetch."""
        return self._last_fetch

    @property
    def last_data(self) -> Optional[WeatherData]:
        """Most recently fetched weather data (cached)."""
        return self._last_data
