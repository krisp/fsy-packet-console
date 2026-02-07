"""APRS tracking and parsing package.

This package provides modular, maintainable components for APRS message tracking,
weather report parsing, and position reporting.

The structure is:
- models.py: Data classes for APRS packets
- geo_utils.py: Geographic calculations (Maidenhead, dew point)
- weather_forecast.py: Weather forecasting (Zambretti algorithm)
- formatters.py: Display formatting for APRS data
- duplicate_detector.py: Packet deduplication logic
- manager.py: Main APRSManager for station tracking
"""

# Re-export all public classes and functions for backwards compatibility
from .models import (
    APRSMessage, APRSPosition, APRSWeather, APRSStatus,
    APRSTelemetry, APRSStation
)
from .geo_utils import latlon_to_maidenhead, maidenhead_to_latlon, calculate_dew_point
from .weather_forecast import (
    adjust_pressure_to_sea_level, calculate_zambretti_code, ZAMBRETTI_FORECASTS
)
from .formatters import APRSFormatters
from .duplicate_detector import DuplicateDetector
from .digipeater_stats import DigipeaterActivity, DigipeaterStats

# Import manager when available (will be added after refactoring)
try:
    from .manager import APRSManager
except ImportError:
    APRSManager = None

__all__ = [
    # Data models
    'APRSMessage', 'APRSPosition', 'APRSWeather', 'APRSStatus',
    'APRSTelemetry', 'APRSStation',

    # Digipeater stats
    'DigipeaterActivity', 'DigipeaterStats',

    # Geographic utilities
    'latlon_to_maidenhead', 'maidenhead_to_latlon', 'calculate_dew_point',

    # Weather utilities
    'adjust_pressure_to_sea_level', 'calculate_zambretti_code', 'ZAMBRETTI_FORECASTS',

    # Utility classes
    'APRSFormatters', 'DuplicateDetector',

    # Main manager (if available)
    'APRSManager',
]
