"""Geographic and meteorological utility functions.

Provides utilities for:
- Maidenhead grid square conversions (latlon ↔ grid)
- Dew point calculations
"""

import math
from typing import Optional, Tuple


def latlon_to_maidenhead(lat: float, lon: float) -> str:
    """Convert latitude/longitude to 6-digit Maidenhead grid square.

    Args:
        lat: Latitude in decimal degrees (-90 to +90)
        lon: Longitude in decimal degrees (-180 to +180)

    Returns:
        6-character Maidenhead grid square (e.g., "FN31pr")
    """
    # Adjust longitude to 0-360 range
    lon_adj = lon + 180
    lat_adj = lat + 90

    # Field (first 2 chars): 20° lon x 10° lat
    field_lon = int(lon_adj / 20)
    field_lat = int(lat_adj / 10)

    # Square (next 2 digits): 2° lon x 1° lat within field
    square_lon = int((lon_adj % 20) / 2)
    square_lat = int((lat_adj % 10) / 1)

    # Subsquare (last 2 chars): 5' lon x 2.5' lat within square
    # 2° = 120', so 120/24 = 5' per subsquare
    # 1° = 60', so 60/24 = 2.5' per subsquare
    subsq_lon = int(((lon_adj % 2) * 60) / 5)
    subsq_lat = int(((lat_adj % 1) * 60) / 2.5)

    # Build grid square string
    grid = (
        chr(ord("A") + field_lon)
        + chr(ord("A") + field_lat)
        + str(square_lon)
        + str(square_lat)
        + chr(ord("a") + subsq_lon)
        + chr(ord("a") + subsq_lat)
    )

    return grid


def maidenhead_to_latlon(grid: str) -> Tuple[float, float]:
    """Convert Maidenhead grid square to latitude/longitude (center of grid).

    Supports 2-10 character grid squares:
    - 2 chars: Field (20° x 10°) e.g., "FN"
    - 4 chars: Square (2° x 1°) e.g., "FN31"
    - 6 chars: Subsquare (5' x 2.5') e.g., "FN31pr"
    - 8 chars: Extended subsquare (12.5" x 6.25") e.g., "FN31pr34"
    - 10 chars: Super extended (0.52" x 0.26") e.g., "FN31pr34ab"

    Args:
        grid: Maidenhead grid square (2-10 characters)

    Returns:
        Tuple of (latitude, longitude) in decimal degrees, representing
        the center of the grid square.

    Raises:
        ValueError: If grid square format is invalid
    """
    grid = grid.upper()
    grid_len = len(grid)

    if grid_len < 2 or grid_len > 10 or grid_len % 2 != 0:
        raise ValueError(
            f"Grid square must be 2, 4, 6, 8, or 10 characters, got {grid_len}"
        )

    # Field (characters 0-1): A-R for lon, A-R for lat
    if not (grid[0].isalpha() and grid[1].isalpha()):
        raise ValueError(f"First 2 characters must be letters: {grid[:2]}")

    field_lon = ord(grid[0]) - ord('A')
    field_lat = ord(grid[1]) - ord('A')

    if field_lon < 0 or field_lon > 17 or field_lat < 0 or field_lat > 17:
        raise ValueError(f"Field must be A-R: {grid[:2]}")

    lon = field_lon * 20 - 180
    lat = field_lat * 10 - 90

    # Square (characters 2-3): 0-9 for lon, 0-9 for lat
    if grid_len >= 4:
        if not (grid[2].isdigit() and grid[3].isdigit()):
            raise ValueError(f"Characters 3-4 must be digits: {grid[2:4]}")

        square_lon = int(grid[2])
        square_lat = int(grid[3])
        lon += square_lon * 2
        lat += square_lat * 1

    # Subsquare (characters 4-5): a-x for lon, a-x for lat
    if grid_len >= 6:
        grid_lower = grid[4:6].lower()
        if not (grid_lower[0].isalpha() and grid_lower[1].isalpha()):
            raise ValueError(f"Characters 5-6 must be letters: {grid[4:6]}")

        subsq_lon = ord(grid_lower[0]) - ord('a')
        subsq_lat = ord(grid_lower[1]) - ord('a')

        if subsq_lon < 0 or subsq_lon > 23 or subsq_lat < 0 or subsq_lat > 23:
            raise ValueError(f"Subsquare must be a-x: {grid[4:6]}")

        lon += subsq_lon * (2.0 / 24)  # 5 arc-minutes
        lat += subsq_lat * (1.0 / 24)  # 2.5 arc-minutes

    # Extended subsquare (characters 6-7): 0-9 for lon, 0-9 for lat
    if grid_len >= 8:
        if not (grid[6].isdigit() and grid[7].isdigit()):
            raise ValueError(f"Characters 7-8 must be digits: {grid[6:8]}")

        ext_lon = int(grid[6])
        ext_lat = int(grid[7])
        lon += ext_lon * (2.0 / 240)  # 30 arc-seconds
        lat += ext_lat * (1.0 / 240)  # 15 arc-seconds

    # Super extended subsquare (characters 8-9): a-x for lon, a-x for lat
    if grid_len >= 10:
        grid_lower = grid[8:10].lower()
        if not (grid_lower[0].isalpha() and grid_lower[1].isalpha()):
            raise ValueError(f"Characters 9-10 must be letters: {grid[8:10]}")

        super_lon = ord(grid_lower[0]) - ord('a')
        super_lat = ord(grid_lower[1]) - ord('a')

        if super_lon < 0 or super_lon > 23 or super_lat < 0 or super_lat > 23:
            raise ValueError(f"Super extended must be a-x: {grid[8:10]}")

        lon += super_lon * (2.0 / 5760)  # 1.25 arc-seconds
        lat += super_lat * (1.0 / 5760)  # 0.625 arc-seconds

    # Return center of grid square by adding half the precision
    if grid_len == 2:
        lon += 10  # Half of 20°
        lat += 5   # Half of 10°
    elif grid_len == 4:
        lon += 1   # Half of 2°
        lat += 0.5 # Half of 1°
    elif grid_len == 6:
        lon += (2.0 / 48)  # Half of 5'
        lat += (1.0 / 48)  # Half of 2.5'
    elif grid_len == 8:
        lon += (2.0 / 480)  # Half of 30"
        lat += (1.0 / 480)  # Half of 15"
    elif grid_len == 10:
        lon += (2.0 / 11520) # Half of 1.25"
        lat += (1.0 / 11520) # Half of 0.625"

    return (lat, lon)


def calculate_dew_point(temp_f: float, humidity: int) -> Optional[float]:
    """Calculate dew point from temperature and humidity using Magnus formula.

    Args:
        temp_f: Temperature in Fahrenheit
        humidity: Relative humidity percentage (0-100)

    Returns:
        Dew point in Fahrenheit, or None if invalid inputs
    """
    if temp_f is None or humidity is None or humidity <= 0 or humidity > 100:
        return None

    # Convert F to C for calculation
    temp_c = (temp_f - 32) * 5.0 / 9.0

    # Magnus formula constants
    a = 17.27
    b = 237.3

    # Calculate gamma
    alpha = ((a * temp_c) / (b + temp_c)) + math.log(humidity / 100.0)

    # Calculate dew point in Celsius
    dew_point_c = (b * alpha) / (a - alpha)

    # Convert back to Fahrenheit
    dew_point_f = (dew_point_c * 9.0 / 5.0) + 32

    return dew_point_f


def calculate_distance_miles(lat1: float, lon1: float, lat2: float,
                             lon2: float) -> float:
    """Calculate distance between two points using Haversine formula.

    Args:
        lat1: First point latitude in decimal degrees
        lon1: First point longitude in decimal degrees
        lat2: Second point latitude in decimal degrees
        lon2: Second point longitude in decimal degrees

    Returns:
        Distance in miles
    """
    # Earth's radius in miles
    R_MILES = 3959

    # Convert to radians
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)

    # Haversine formula
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = (math.sin(dlat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))

    return R_MILES * c
