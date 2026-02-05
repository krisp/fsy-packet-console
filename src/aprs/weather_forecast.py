"""Weather forecasting algorithms and barometric pressure calculations.

Implements the Zambretti weather forecasting algorithm based on the 1915
Negretti and Zambra weather forecaster. Also provides barometric formula
for pressure adjustments.
"""

import re
from typing import Optional

# Zambretti forecast descriptions (indices 0-25)
ZAMBRETTI_FORECASTS = {
    0: 'Settled fine',
    1: 'Fine weather',
    2: 'Becoming fine',
    3: 'Fine, becoming less settled',
    4: 'Fine, possible showers',
    5: 'Fairly fine, improving',
    6: 'Fairly fine, possible showers early',
    7: 'Fairly fine, showery later',
    8: 'Showery early, improving',
    9: 'Changeable, mending',
    10: 'Fairly fine, showers likely',
    11: 'Rather unsettled clearing later',
    12: 'Unsettled, probably improving',
    13: 'Showery, bright intervals',
    14: 'Showery, becoming less settled',
    15: 'Changeable, some rain',
    16: 'Unsettled, short fine intervals',
    17: 'Unsettled, rain later',
    18: 'Unsettled, some rain',
    19: 'Mostly very unsettled',
    20: 'Occasional rain, worsening',
    21: 'Rain at times, very unsettled',
    22: 'Rain at frequent intervals',
    23: 'Rain, very unsettled',
    24: 'Stormy, may improve',
    25: 'Stormy, much rain'
}


def adjust_pressure_to_sea_level(
    station_pressure_mb: float,
    altitude_m: float,
    temperature_f: Optional[float] = None
) -> float:
    """Adjust station pressure to sea-level equivalent.

    Uses the barometric formula to calculate what the pressure would be
    at sea level based on the pressure measured at a given altitude.

    Args:
        station_pressure_mb: Pressure at station in millibars
        altitude_m: Station altitude in meters
        temperature_f: Temperature in Fahrenheit (uses standard temp if None)

    Returns:
        Sea-level pressure in millibars
    """
    if altitude_m == 0:
        return station_pressure_mb

    # Use standard temperature (15°C = 59°F) if not provided
    temp_k = 288.15  # 15°C in Kelvin
    if temperature_f is not None:
        temp_c = (temperature_f - 32) * 5/9
        temp_k = temp_c + 273.15

    # Barometric formula
    # SLP = SP * (1 - (0.0065 * h) / (T + 0.0065 * h + 273.15))^-5.257
    exponent = -5.257
    slp = station_pressure_mb * (1 - (0.0065 * altitude_m) / temp_k) ** exponent

    return slp


def calculate_zambretti_code(
    sea_level_pressure_mb: float,
    pressure_trend: str,
    wind_direction: Optional[int] = None,
    month: int = None,
    hemisphere: str = 'N'
) -> int:
    """Calculate Zambretti forecast code from pressure data.

    Implements the beteljuice.com Zambretti algorithm (June 2008).
    Based on the 1915 Negretti and Zambra weather forecaster.

    Args:
        sea_level_pressure_mb: Sea-level adjusted pressure in millibars
        pressure_trend: 'rising', 'falling', or 'steady'
        wind_direction: Wind direction in degrees (0-360), None if calm
        month: Month number (1-12) for seasonal adjustment
        hemisphere: 'N' or 'S'

    Returns:
        Zambretti forecast index (0-25)
    """
    # Zambretti parameters
    z_baro_top = 1050.0  # Upper limit (UK weather range)
    z_baro_bottom = 950.0  # Lower limit
    z_range = z_baro_top - z_baro_bottom
    z_constant = z_range / 22.0

    # Start with sea-level adjusted pressure
    z_hpa = sea_level_pressure_mb

    # Determine season
    is_summer = False
    if month:
        if hemisphere == 'N':
            is_summer = 4 <= month <= 9  # Apr-Sep in Northern Hemisphere
        else:
            is_summer = month <= 3 or month >= 10  # Oct-Mar in Southern Hemisphere

    # Apply wind direction adjustment
    if wind_direction is not None:
        # Convert degrees to 16-point compass
        # N=0, NNE=22.5, NE=45, ENE=67.5, E=90, etc.
        wind_points = [
            ('N', 0), ('NNE', 22.5), ('NE', 45), ('ENE', 67.5),
            ('E', 90), ('ESE', 112.5), ('SE', 135), ('SSE', 157.5),
            ('S', 180), ('SSW', 202.5), ('SW', 225), ('WSW', 247.5),
            ('W', 270), ('WNW', 292.5), ('NW', 315), ('NNW', 337.5)
        ]

        # Find closest cardinal direction
        wind_cardinal = None
        min_diff = 360
        for name, angle in wind_points:
            diff = abs(wind_direction - angle)
            if diff > 180:
                diff = 360 - diff
            if diff < min_diff:
                min_diff = diff
                wind_cardinal = name

        # Apply wind adjustments (Northern Hemisphere)
        if hemisphere == 'N':
            wind_adjustments = {
                'N': 6, 'NNE': 5, 'NE': 5, 'ENE': 2,
                'E': -0.5, 'ESE': -2, 'SE': -5, 'SSE': -8.5,
                'S': -12, 'SSW': -10, 'SW': -6, 'WSW': -4.5,
                'W': -3, 'WNW': -0.5, 'NW': 1.5, 'NNW': 3
            }
            if wind_cardinal in wind_adjustments:
                z_hpa += (wind_adjustments[wind_cardinal] / 100.0) * z_range
        else:  # Southern Hemisphere
            wind_adjustments = {
                'S': 6, 'SSW': 5, 'SW': 5, 'WSW': 2,
                'W': -0.5, 'WNW': -2, 'NW': -5, 'NNW': -8.5,
                'N': -12, 'NNE': -10, 'NE': -6, 'ENE': -4.5,
                'E': -3, 'ESE': -0.5, 'SE': 1.5, 'SSE': 3
            }
            if wind_cardinal in wind_adjustments:
                z_hpa += (wind_adjustments[wind_cardinal] / 100.0) * z_range

    # Apply seasonal trend adjustment
    if hemisphere == 'N' and is_summer:
        if pressure_trend == 'rising':
            z_hpa += (7 / 100.0) * z_range
        elif pressure_trend == 'falling':
            z_hpa -= (7 / 100.0) * z_range
    elif hemisphere == 'S' and not is_summer:  # Winter in Southern
        if pressure_trend == 'rising':
            z_hpa += (7 / 100.0) * z_range
        elif pressure_trend == 'falling':
            z_hpa -= (7 / 100.0) * z_range

    # Clamp to valid range
    if z_hpa >= z_baro_top:
        z_hpa = z_baro_top - 1

    # Calculate option index (0-21)
    z_option = int((z_hpa - z_baro_bottom) / z_constant)
    z_option = max(0, min(21, z_option))

    # Zambretti lookup tables (map option to forecast index)
    rise_options = [25,25,25,24,24,19,16,12,11,9,8,6,5,2,1,1,0,0,0,0,0,0]
    steady_options = [25,25,25,25,25,25,23,23,22,18,15,13,10,4,1,1,0,0,0,0,0,0]
    fall_options = [25,25,25,25,25,25,25,25,23,23,21,20,17,14,7,3,1,1,1,0,0,0]

    # Select forecast based on trend
    if pressure_trend == 'rising':
        return rise_options[z_option]
    elif pressure_trend == 'falling':
        return fall_options[z_option]
    else:  # steady
        return steady_options[z_option]


def _parse_pressure_from_raw(raw_data: str) -> Optional[float]:
    """Extract and parse pressure from raw APRS data.

    Auto-detects format:
    - Tenths of mb: b10130 = 1013.0 mb
    - Hundredths of inHg: b02979 = 29.79 inHg → converted to mb

    Args:
        raw_data: Raw APRS packet string

    Returns:
        Pressure in millibars, or None if not found/invalid
    """
    if not raw_data:
        return None

    match = re.search(r"b(\d{5})", raw_data)
    if not match:
        return None

    raw_value = int(match.group(1))

    # Try as tenths of millibars first
    pressure_mb = raw_value / 10.0

    # Sanity check: valid atmospheric pressure is 900-1100 mb
    if 900 <= pressure_mb <= 1100:
        return pressure_mb
    else:
        # Try as hundredths of inHg (US format)
        pressure_inhg = raw_value / 100.0

        # Sanity check: valid inHg range is 25-32 inHg
        if 25 <= pressure_inhg <= 32:
            return pressure_inhg * 33.8639

    return None
