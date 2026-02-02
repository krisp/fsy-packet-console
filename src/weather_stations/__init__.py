"""Weather Station Integration Module.

Provides a pluggable architecture for integrating various weather station brands
(Ecowitt, Davis, Ambient, etc.) into the APRS console for local weather beaconing.
"""

from src.weather_stations.base import WeatherStation, WeatherData
from src.weather_stations.ecowitt import EcowittWeatherStation

__all__ = ['WeatherStation', 'WeatherData', 'EcowittWeatherStation']
