"""APRS Manager - Backwards Compatibility Shim.

DEPRECATED: This module is maintained for backwards compatibility only.
All functionality has been refactored into the src/aprs/ package.

New code should import from src.aprs instead:
    from src.aprs import APRSManager
    from src.aprs import APRSMessage, APRSPosition, APRSWeather
    from src.aprs import latlon_to_maidenhead, calculate_dew_point
    etc.

This shim re-exports everything from the new package and the original
manager for full backwards compatibility.
"""

# Re-export models, utilities, and classes from new modular packages
from src.aprs.models import (
    APRSMessage, APRSPosition, APRSWeather, APRSStatus,
    APRSTelemetry, APRSStation
)
from src.aprs.geo_utils import (
    latlon_to_maidenhead, maidenhead_to_latlon, calculate_dew_point
)
from src.aprs.weather_forecast import (
    adjust_pressure_to_sea_level, calculate_zambretti_code,
    ZAMBRETTI_FORECASTS, _parse_pressure_from_raw
)
from src.aprs.formatters import APRSFormatters
from src.aprs.duplicate_detector import DuplicateDetector, DUPLICATE_WINDOW

# Import the original manager implementation
from src.aprs.manager import APRSManager

# Module-level constants (for backwards compatibility)
MESSAGE_RETRY_TIMEOUT = 30  # DEPRECATED: Use fast/slow timeouts instead
MESSAGE_RETRY_FAST = 20  # seconds between fast retry attempts (not digipeated)
MESSAGE_RETRY_SLOW = 600  # seconds between slow retry attempts (digipeated but not ACKed) - 10 minutes
MESSAGE_MAX_RETRIES = 3  # maximum number of transmission attempts (original + 2 retries)

__all__ = [
    # Data models
    'APRSMessage', 'APRSPosition', 'APRSWeather', 'APRSStatus',
    'APRSTelemetry', 'APRSStation',

    # Geographic utilities
    'latlon_to_maidenhead', 'maidenhead_to_latlon', 'calculate_dew_point',

    # Weather utilities
    'adjust_pressure_to_sea_level', 'calculate_zambretti_code',
    'ZAMBRETTI_FORECASTS', '_parse_pressure_from_raw',

    # Utility classes
    'APRSFormatters', 'DuplicateDetector', 'DUPLICATE_WINDOW',

    # Main manager
    'APRSManager',

    # Module constants
    'MESSAGE_RETRY_TIMEOUT', 'MESSAGE_RETRY_FAST', 'MESSAGE_RETRY_SLOW',
    'MESSAGE_MAX_RETRIES'
]
