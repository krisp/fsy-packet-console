"""Ecowitt weather station integration.

Supports Ecowitt GW1000, GW1100, GW1200, GW2000 gateways via local HTTP API.
Uses the undocumented /get_livedata_info endpoint for real-time sensor data.
"""

import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from src.weather_stations.base import WeatherData, WeatherStation


# Ecowitt sensor ID mappings (from reverse engineering)
ECOWITT_SENSOR_IDS = {
    '0x01': 'indoor_temp',
    '0x02': 'outdoor_temp',
    '0x03': 'dew_point',
    '0x04': 'wind_chill',
    '0x05': 'heat_index',
    '0x06': 'indoor_humidity',
    '0x07': 'outdoor_humidity',
    '0x08': 'abs_pressure',
    '0x09': 'rel_pressure',
    '0x0A': 'wind_direction',
    '0x0B': 'wind_speed',
    '0x0C': 'wind_gust',
    '0x0D': 'rain_event',
    '0x0E': 'rain_rate',
    '0x0F': 'rain_gain',
    '0x10': 'rain_day',
    '0x11': 'rain_week',
    '0x12': 'rain_month',
    '0x13': 'rain_year',
    '0x15': 'light',
    '0x16': 'uv',
    '0x17': 'uvi',
    '0x18': 'time',
    '0x19': 'daily_max_wind',
    '0x1A': 'temp1',
    '0x1B': 'temp2',
    '0x1C': 'temp3',
    '0x1D': 'temp4',
    '0x1E': 'temp5',
    '0x1F': 'temp6',
    '0x20': 'temp7',
    '0x21': 'temp8',
    '0x6D': 'wind_dir_avg',
    '0x7C': 'rain_total',
}


class EcowittWeatherStation(WeatherStation):
    """Ecowitt weather station implementation.

    Fetches data from Ecowitt gateways (GW1000, GW1100, GW1200, GW2000)
    via the local HTTP API endpoint /get_livedata_info.

    Example:
        station = EcowittWeatherStation(host='192.168.1.124')
        data = await station.fetch_weather()
        print(f"Temp: {data.temperature_outdoor}°F")
    """

    def __init__(self, host: str, port: int = 80, timeout: int = 10):
        """Initialize Ecowitt weather station connection.

        Args:
            host: IP address or hostname of Ecowitt gateway
            port: HTTP port (default: 80)
            timeout: Request timeout in seconds (default: 10)
        """
        super().__init__(host, port)
        self.timeout = timeout
        self.endpoint = f"http://{host}:{port}/get_livedata_info"

    async def fetch_weather(self) -> Optional[WeatherData]:
        """Fetch current weather data from Ecowitt gateway.

        Returns:
            WeatherData object with current conditions, or None if fetch failed

        Raises:
            ConnectionError: If unable to connect to gateway
            ValueError: If response data is invalid
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.endpoint,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    if response.status != 200:
                        raise ConnectionError(
                            f"Gateway returned HTTP {response.status}"
                        )

                    data = await response.json()

            # Parse the response into WeatherData
            weather = self._parse_response(data)

            # Cache the result
            self._last_fetch = datetime.now(timezone.utc)
            self._last_data = weather

            return weather

        except aiohttp.ClientError as e:
            raise ConnectionError(f"Failed to connect to Ecowitt gateway: {e}")
        except Exception as e:
            raise ValueError(f"Failed to parse Ecowitt response: {e}")

    async def test_connection(self) -> bool:
        """Test connectivity to Ecowitt gateway.

        Returns:
            True if gateway is reachable and responding, False otherwise
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.endpoint,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    return response.status == 200
        except Exception:
            return False

    def get_station_info(self) -> dict:
        """Get static information about the Ecowitt gateway.

        Returns:
            Dictionary with station metadata
        """
        return {
            'vendor': 'Ecowitt',
            'model': 'GW1100/GW2000',
            'host': self.host,
            'port': self.port,
            'api_type': 'HTTP',
            'endpoint': self.endpoint,
        }

    def _parse_response(self, data: dict) -> WeatherData:
        """Parse Ecowitt JSON response into WeatherData.

        Args:
            data: Raw JSON response from /get_livedata_info

        Returns:
            WeatherData object with parsed sensor values
        """
        weather = WeatherData(timestamp=datetime.now(timezone.utc))

        # Parse common_list (main sensors)
        for item in data.get('common_list', []):
            sensor_id = item.get('id')
            value_str = item.get('val', '')

            # Map hex ID to field name
            field_name = ECOWITT_SENSOR_IDS.get(sensor_id)
            if not field_name:
                continue

            # Parse value (strip units and convert to float)
            value = self._parse_value(value_str)
            if value is None:
                continue

            # Map to WeatherData fields
            self._map_field(weather, field_name, value, item.get('unit', ''))

        # Parse rain data
        for item in data.get('rain', []):
            sensor_id = item.get('id')
            value_str = item.get('val', '')
            field_name = ECOWITT_SENSOR_IDS.get(sensor_id)

            if field_name:
                value = self._parse_value(value_str)
                if value is not None:
                    self._map_field(weather, field_name, value, item.get('unit', ''))

        # Parse WH25 indoor sensor
        wh25 = data.get('wh25', [])
        if wh25:
            indoor = wh25[0]

            # Indoor temperature
            if 'intemp' in indoor:
                weather.temperature_indoor = self._parse_value(indoor['intemp'])

            # Indoor humidity
            if 'inhumi' in indoor:
                weather.humidity_indoor = int(
                    self._parse_value(indoor['inhumi']) or 0
                )

            # Absolute pressure (convert inHg to mb)
            if 'abs' in indoor:
                abs_inhg = self._parse_value(indoor['abs'])
                if abs_inhg:
                    weather.pressure_absolute = abs_inhg * 33.8639  # inHg to mb

            # Relative pressure (sea-level adjusted, convert inHg to mb)
            if 'rel' in indoor:
                rel_inhg = self._parse_value(indoor['rel'])
                if rel_inhg:
                    weather.pressure_relative = rel_inhg * 33.8639  # inHg to mb

        return weather

    def _parse_value(self, value_str: str) -> Optional[float]:
        """Extract numeric value from string with units.

        Examples:
            "14.0 F" -> 14.0
            "63%" -> 63.0
            "10.07 mph" -> 10.07
            "0.105 kPa" -> 0.105

        Args:
            value_str: String value with optional unit suffix

        Returns:
            Numeric value as float, or None if unparseable
        """
        if not value_str:
            return None

        # Match number (including negative, decimal)
        match = re.search(r'-?\d+\.?\d*', str(value_str))
        if match:
            try:
                return float(match.group())
            except ValueError:
                return None
        return None

    def _map_field(
        self,
        weather: WeatherData,
        field_name: str,
        value: float,
        unit: str
    ) -> None:
        """Map parsed sensor value to WeatherData field.

        Args:
            weather: WeatherData object to update
            field_name: Sensor field name from ECOWITT_SENSOR_IDS
            value: Parsed numeric value
            unit: Unit string from response
        """
        # Temperature fields (already in °F)
        if field_name == 'outdoor_temp':
            weather.temperature_outdoor = value
        elif field_name == 'indoor_temp':
            weather.temperature_indoor = value
        elif field_name == 'dew_point':
            weather.dew_point = value

        # Humidity fields
        elif field_name == 'outdoor_humidity':
            weather.humidity_outdoor = int(value)
        elif field_name == 'indoor_humidity':
            weather.humidity_indoor = int(value)

        # Pressure fields (convert if needed)
        elif field_name == 'abs_pressure':
            # Check unit - might be kPa, inHg, or mb
            if 'kPa' in unit:
                weather.pressure_absolute = value * 10.0  # kPa to mb
            elif 'inHg' in unit:
                weather.pressure_absolute = value * 33.8639  # inHg to mb
            else:
                weather.pressure_absolute = value  # assume mb

        elif field_name == 'rel_pressure':
            if 'kPa' in unit:
                weather.pressure_relative = value * 10.0
            elif 'inHg' in unit:
                weather.pressure_relative = value * 33.8639
            else:
                weather.pressure_relative = value

        # Wind fields (already in mph)
        elif field_name == 'wind_speed':
            weather.wind_speed = value
        elif field_name == 'wind_gust':
            weather.wind_gust = value
        elif field_name == 'wind_direction':
            weather.wind_direction = int(value)

        # Rain fields (already in inches)
        elif field_name == 'rain_rate':
            weather.rain_rate = value
        elif field_name == 'rain_event':
            weather.rain_event = value
        elif field_name == 'rain_day':
            weather.rain_daily = value
        elif field_name == 'rain_week':
            weather.rain_weekly = value
        elif field_name == 'rain_month':
            weather.rain_monthly = value
        elif field_name == 'rain_year':
            weather.rain_yearly = value

        # Solar/UV
        elif field_name == 'light':
            weather.solar_radiation = value
        elif field_name == 'uvi':
            weather.uv_index = value
