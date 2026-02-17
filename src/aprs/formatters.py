"""APRS data formatting for display and presentation.

Provides formatters for displaying APRS packets, messages, positions, weather,
and stations in human-readable formats for console and web UIs.
"""

import re
from datetime import datetime
from typing import Dict, List, Optional

from .models import APRSMessage, APRSPosition, APRSWeather, APRSStation


class APRSFormatters:
    """Collection of static and class methods for formatting APRS data structures."""

    # Compass direction mapping for wind direction (16-point)
    WIND_DIRECTIONS_16 = [
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
    ]

    # Compass direction mapping for wind direction (8-point)
    WIND_DIRECTIONS_8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

    @staticmethod
    def format_message(msg: APRSMessage, index: int = None) -> str:
        """Format message for display.

        Args:
            msg: Message to format
            index: Optional message index number

        Returns:
            Formatted message string
        """
        time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"[{index}] " if index is not None else ""

        # Format based on direction
        if msg.direction == "sent":
            # Sent message - show ACK status and recipient
            ack_mark = "✓" if msg.ack_received else "⋯"
            return f"{prefix}[{ack_mark}] {time_str} To: {msg.to_call}\n  {msg.message}"
        else:
            # Received message - show read status and sender
            status = "NEW" if not msg.read else "READ"
            msg_id_str = f" {{{msg.message_id}}}" if msg.message_id else ""
            return f"{prefix}[{status}] {time_str} From: {msg.from_call}\n  {msg.message}{msg_id_str}"

    @staticmethod
    def format_weather(wx: APRSWeather) -> Dict[str, str]:
        """Format weather report for display.

        Args:
            wx: Weather report

        Returns:
            Dictionary of formatted weather fields
        """
        wind_str = APRSFormatters._format_wind(wx)
        return {
            "station": wx.station,
            "time": wx.timestamp.strftime("%H:%M:%S"),
            "temp": (
                f"{wx.temperature}°F" if wx.temperature is not None else "---"
            ),
            "humidity": (
                f"{wx.humidity}%" if wx.humidity is not None else "---"
            ),
            "wind": wind_str,
            "pressure": (
                f"{wx.pressure} mb" if wx.pressure is not None else "---"
            ),
            "rain_1h": f'{wx.rain_1h}"' if wx.rain_1h is not None else "---",
        }

    @staticmethod
    def _format_wind(wx: APRSWeather) -> str:
        """Format wind information.

        Args:
            wx: Weather report

        Returns:
            Formatted wind string
        """
        if wx.wind_speed is None:
            return "---"

        result = f"{wx.wind_speed} mph"

        if wx.wind_direction is not None:
            index = round(wx.wind_direction / 22.5) % 16
            result = f"{APRSFormatters.WIND_DIRECTIONS_16[index]} {result}"

        if wx.wind_gust is not None and wx.wind_gust > 0:
            result += f" (gust {wx.wind_gust})"

        return result

    @staticmethod
    def format_position(pos: APRSPosition) -> Dict[str, str]:
        """Format position report for display.

        Args:
            pos: Position report

        Returns:
            Dictionary of formatted position fields
        """
        return {
            "station": pos.station,
            "time": pos.timestamp.strftime("%H:%M:%S"),
            "latitude": f"{pos.latitude:.4f}",
            "longitude": f"{pos.longitude:.4f}",
            "grid": pos.grid_square,
            "symbol": f"{pos.symbol_table}{pos.symbol_code}",
            "comment": (
                pos.comment[:30] if len(pos.comment) > 30 else pos.comment
            ),  # Truncate long comments
        }

    @staticmethod
    def clean_position_comment(comment: str) -> str:
        """Clean position comment by removing redundant data fields.

        Strips weather data, altitude, course/speed, and other APRS data
        that's already parsed into dedicated fields.

        Args:
            comment: Raw comment from position report

        Returns:
            Cleaned comment (empty string if nothing meaningful remains)
        """
        if not comment:
            return ""

        # Strip common APRS data patterns:
        # - Weather: cdddsddd, tddd, hddd, rddd, pddd, Pddd, bddddd, gddd
        comment = re.sub(r"[ctrhpPbg]\d{2,5}", "", comment)
        # - Wind: _ddd/ddd
        comment = re.sub(r"_\d{3}/\d{3}", "", comment)
        # - Altitude: /A=xxxxxx
        comment = re.sub(r"/A=\d{6}", "", comment)
        # - Course/speed: ccc/sss
        comment = re.sub(r"\d{3}/\d{3}", "", comment)
        # - PHG (Power-Height-Gain): PHGxxxx
        comment = re.sub(r"PHG\d{4}", "", comment)
        # - RNG (Range): RNGxxxx
        comment = re.sub(r"RNG\d{4}", "", comment)
        # - DFS (Direction Finding): DFSxxxx
        comment = re.sub(r"DFS\d{4}", "", comment)

        # Strip leading/trailing whitespace
        comment = comment.strip()

        return comment

    @staticmethod
    def format_station_table_row(station: APRSStation) -> Dict[str, str]:
        """Format station for table display.

        Args:
            station: Station to format

        Returns:
            Dictionary of formatted fields
        """
        # Get grid square from position
        grid = (
            station.last_position.grid_square
            if station.last_position
            else "---"
        )

        # Get temperature from weather
        temp = (
            f"{station.last_weather.temperature}°F"
            if (
                station.last_weather
                and station.last_weather.temperature is not None
            )
            else "---"
        )

        # Format last heard time
        last_heard = station.last_heard.strftime("%H:%M:%S")

        return {
            "callsign": station.callsign,
            "grid": grid,
            "temp": temp,
            "last_heard": last_heard,
            "packets": str(station.packets_heard),
            "hops": station.hop_count,
        }

    @staticmethod
    def format_combined_notification(
        pos: APRSPosition, wx: APRSWeather, relay_call: str = None
    ) -> str:
        """Format combined position+weather notification for display.

        Args:
            pos: Position report
            wx: Weather report (from same packet)
            relay_call: Optional relay station (for third-party packets)

        Returns:
            Formatted string for single-line display
        """
        # Start with station (with relay path if third-party) and grid
        if relay_call:
            station_part = f"{pos.station} [📡 via {relay_call}]"
        else:
            station_part = pos.station
        parts = [f"{station_part}: {pos.grid_square}"]

        # Add weather summary (only non-None fields)
        weather_parts = []
        if wx.temperature is not None:
            weather_parts.append(f"{wx.temperature}°F")
        if wx.wind_speed is not None:
            wind_str = f"{wx.wind_speed}mph"
            if wx.wind_direction is not None:
                index = round(wx.wind_direction / 22.5) % 16
                wind_str = f"{APRSFormatters.WIND_DIRECTIONS_16[index]} {wind_str}"
            weather_parts.append(wind_str)
        if wx.humidity is not None:
            weather_parts.append(f"{wx.humidity}%H")
        if wx.pressure is not None:
            weather_parts.append(f"{wx.pressure}mb")

        if weather_parts:
            parts.append(", ".join(weather_parts))

        # Add cleaned comment if present and meaningful
        cleaned_comment = APRSFormatters.clean_position_comment(pos.comment)
        if cleaned_comment:
            parts.append(cleaned_comment)

        return " | ".join(parts)

    @staticmethod
    def format_station_detail(
        station: APRSStation,
        pressure_threshold: float = 0.3,
        get_zambretti_forecast=None
    ) -> str:
        """Format detailed station information.

        Args:
            station: Station to format
            pressure_threshold: Pressure tendency threshold for Zambretti forecast
            get_zambretti_forecast: Optional callback to get forecast (for dependency injection)

        Returns:
            Formatted multi-line string with all station details
        """
        lines = []
        lines.append(f"Station: {station.callsign}")
        if station.device:
            lines.append(f"Device: {station.device}")
        lines.append(
            f"First Heard: {station.first_heard.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        lines.append(
            f"Last Heard: {station.last_heard.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        lines.append(f"Packets Heard: {station.packets_heard}")
        lines.append("")

        # Position info
        if station.last_position:
            pos = station.last_position
            lines.append("Position:")
            lines.append(f"  Grid Square: {pos.grid_square}")
            lines.append(f"  Latitude: {pos.latitude:.4f}°")
            lines.append(f"  Longitude: {pos.longitude:.4f}°")
            if pos.altitude:
                lines.append(f"  Altitude: {pos.altitude} ft")
            lines.append(f"  Symbol: {pos.symbol_table}{pos.symbol_code}")
            if pos.comment:
                cleaned = APRSFormatters.clean_position_comment(pos.comment)
                if cleaned:
                    lines.append(f"  Comment: {cleaned}")
            lines.append(
                f"  Updated: {pos.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            lines.append("")
        else:
            lines.append("Position: Not available")
            lines.append("")

        # Weather info
        if station.last_weather:
            wx = station.last_weather
            lines.append("Weather:")
            if wx.temperature is not None:
                lines.append(f"  Temperature: {wx.temperature}°F")
            if wx.humidity is not None:
                lines.append(f"  Humidity: {wx.humidity}%")
            if wx.pressure is not None:
                lines.append(f"  Pressure: {wx.pressure} mb")
            if wx.wind_speed is not None:
                wind_str = f"{wx.wind_speed} mph"
                if wx.wind_direction is not None:
                    index = round(wx.wind_direction / 22.5) % 16
                    wind_str = f"{APRSFormatters.WIND_DIRECTIONS_16[index]} {wind_str}"
                lines.append(f"  Wind: {wind_str}")
            if wx.wind_gust is not None and wx.wind_gust > 0:
                lines.append(f"  Wind Gust: {wx.wind_gust} mph")
            if wx.rain_1h is not None:
                lines.append(f'  Rain (1h): {wx.rain_1h}"')
            if wx.rain_24h is not None:
                lines.append(f'  Rain (24h): {wx.rain_24h}"')
            if wx.rain_since_midnight is not None:
                lines.append(f'  Rain (midnight): {wx.rain_since_midnight}"')
            lines.append(
                f"  Updated: {wx.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            # Show weather history sample count
            if station.weather_history:
                history_count = len(station.weather_history)
                lines.append(
                    f"  History: {history_count} sample{'s' if history_count != 1 else ''} stored"
                )

            # Add temperature history chart if available
            if station.weather_history and any(
                wx.temperature is not None
                for wx in station.weather_history
            ):
                lines.append("")
                lines.append(APRSFormatters._format_temperature_chart(station.weather_history))

            # Add wind rose if wind data available
            if station.weather_history and any(
                wx.wind_direction is not None and wx.wind_speed is not None
                for wx in station.weather_history
            ):
                lines.append("")
                lines.append(APRSFormatters._format_wind_rose(station.weather_history))

            # Add Zambretti weather forecast if pressure available and callback provided
            if get_zambretti_forecast:
                forecast = get_zambretti_forecast(station.callsign, pressure_threshold=pressure_threshold)
                if forecast:
                    lines.append("")
                    lines.append("Forecast (Zambretti):")
                    lines.append(f"  {forecast['forecast']} (Code {forecast['code']})")

                    # Format trend with arrow
                    trend_arrow = '↑' if forecast['trend'] == 'rising' else '↓' if forecast['trend'] == 'falling' else '→'
                    lines.append(f"  Pressure trend: {trend_arrow} {forecast['trend']}")
                    lines.append(f"  Confidence: {forecast['confidence']}")

            lines.append("")
        else:
            lines.append("Weather: Not available")
            lines.append("")

        # Status info
        if station.last_status:
            status = station.last_status
            lines.append("Status:")
            lines.append(f"  {status.status_text}")
            lines.append(
                f"  Updated: {status.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            lines.append("")
        else:
            lines.append("Status: Not available")
            lines.append("")

        # Telemetry info
        if station.last_telemetry:
            telem = station.last_telemetry
            lines.append("Telemetry:")
            lines.append(f"  Sequence: {telem.sequence}")
            lines.append(
                f"  Analog Channels: {', '.join(str(v) for v in telem.analog)}"
            )
            lines.append(f"  Digital Bits: {telem.digital}")
            lines.append(
                f"  Updated: {telem.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            if len(station.telemetry_sequence) > 1:
                lines.append(
                    f"  History: {len(station.telemetry_sequence)} packets stored"
                )
            lines.append("")
        else:
            lines.append("Telemetry: Not available")
            lines.append("")

        # Message statistics
        lines.append("Messages:")
        lines.append(f"  Sent by station: {station.messages_sent}")
        lines.append(f"  Received (to us): {station.messages_received}")
        lines.append("")

        # Reception path information
        lines.append("Reception:")
        lines.append(
            f"  Heard direct on RF: {'Yes' if station.heard_direct else 'No'}"
        )
        hop_str = (
            "Direct RF"
            if station.hop_count == 0
            else (
                f"{station.hop_count} hop{'s' if station.hop_count != 1 else ''}"
                if station.hop_count < 999
                else "Unknown"
            )
        )
        lines.append(f"  Minimum hops: {hop_str}")
        if station.relay_paths:
            lines.append(f"  Relayed via: {', '.join(station.relay_paths)}")

        return "\n".join(lines)

    @staticmethod
    def _format_temperature_chart(
        weather_history: List[APRSWeather], width: int = 60
    ) -> str:
        """Create a text-based temperature chart from weather history.

        Args:
            weather_history: List of weather reports (should be sorted newest first)
            width: Width of the chart in characters

        Returns:
            Multi-line ASCII chart showing temperature over time
        """
        if not weather_history:
            return "  No temperature data available"

        # Filter to only reports with temperature data
        temps = [
            (wx.timestamp, wx.temperature)
            for wx in weather_history
            if wx.temperature is not None
        ]

        if not temps:
            return "  No temperature data available"

        # Sort oldest to newest for chart (left to right = past to present)
        temps.sort(key=lambda x: x[0])

        # Extract values
        timestamps = [t[0] for t in temps]
        values = [t[1] for t in temps]

        min_temp = min(values)
        max_temp = max(values)
        temp_range = max_temp - min_temp if max_temp != min_temp else 1

        # Chart dimensions
        height = 8
        chart_width = min(width, len(values) * 2)

        # Build chart
        lines = []
        lines.append(
            f"  Temperature: {min_temp:.0f}°F - {max_temp:.0f}°F "
            f"({len(temps)} samples)"
        )
        lines.append("  " + "─" * chart_width)

        # Create chart rows (top to bottom = hot to cold)
        for row in range(height):
            threshold = max_temp - (temp_range * row / (height - 1))
            line = "  "
            for i, temp in enumerate(values):
                if i >= chart_width // 2:
                    break
                # Use different characters for above/at/below threshold
                if temp >= threshold - (temp_range / (height * 2)):
                    if temp == values[-1] and i == len(values) - 1:
                        line += "█ "  # Current value
                    else:
                        line += "▓ "  # Historical value above threshold
                else:
                    line += "  "  # Below threshold

            # Add temperature label on right
            if row == 0:
                line += f" {max_temp:.0f}°F"
            elif row == height - 1:
                line += f" {min_temp:.0f}°F"
            elif row == height // 2:
                mid_temp = (max_temp + min_temp) / 2
                line += f" {mid_temp:.0f}°F"

            lines.append(line)

        lines.append("  " + "─" * chart_width)

        # Time labels (oldest ... newest)
        oldest = timestamps[0].strftime("%H:%M")
        newest = timestamps[-1].strftime("%H:%M")
        time_label = f"  {oldest}" + " " * (chart_width - len(oldest) - len(newest)) + newest
        lines.append(time_label)

        return "\n".join(lines)

    @staticmethod
    def _format_wind_rose(
        weather_history: List[APRSWeather]
    ) -> str:
        """Create a text-based wind rose from weather history.

        Args:
            weather_history: List of weather reports

        Returns:
            ASCII art wind rose showing wind direction distribution
        """
        if not weather_history:
            return "  No wind data available"

        # Filter to reports with wind data
        winds = [
            (wx.wind_direction, wx.wind_speed)
            for wx in weather_history
            if wx.wind_direction is not None and wx.wind_speed is not None
        ]

        if not winds:
            return "  No wind data available"

        # Count wind directions in 8 sectors (N, NE, E, SE, S, SW, W, NW)
        sectors = {
            "N": 0, "NE": 0, "E": 0, "SE": 0,
            "S": 0, "SW": 0, "W": 0, "NW": 0,
        }
        sector_speeds = {k: [] for k in sectors.keys()}

        # Map directions to sectors
        for direction, speed in winds:
            # Convert to 8 sectors (0° = N, 45° = NE, etc.)
            sector_index = int((direction + 22.5) / 45) % 8
            sector = APRSFormatters.WIND_DIRECTIONS_8[sector_index]
            sectors[sector] += 1
            sector_speeds[sector].append(speed)

        # Calculate average speed per sector
        avg_speeds = {}
        for sector, speeds in sector_speeds.items():
            avg_speeds[sector] = sum(speeds) / len(speeds) if speeds else 0

        # Find max count for scaling
        max_count = max(sectors.values()) if sectors.values() else 1
        scale = 5  # Max bar length

        # Build wind rose
        lines = []
        lines.append(
            f"  Wind Rose ({len(winds)} samples, avg {sum(s for _, s in winds) / len(winds):.1f} mph)"
        )
        lines.append("  " + "─" * 30)

        n_bar = "█" * int(sectors["N"] / max_count * scale)
        ne_bar = "█" * int(sectors["NE"] / max_count * scale)
        e_bar = "█" * int(sectors["E"] / max_count * scale)
        se_bar = "█" * int(sectors["SE"] / max_count * scale)
        s_bar = "█" * int(sectors["S"] / max_count * scale)
        sw_bar = "█" * int(sectors["SW"] / max_count * scale)
        w_bar = "█" * int(sectors["W"] / max_count * scale)
        nw_bar = "█" * int(sectors["NW"] / max_count * scale)

        # Format as rose
        lines.append(f"       N {n_bar:>5}  ({sectors['N']:2d}, {avg_speeds['N']:.0f}mph)")
        lines.append(
            f"  NW {nw_bar:>5}     {ne_bar:<5} NE  "
            f"({sectors['NW']},{sectors['NE']})"
        )
        lines.append(f"       |       |")
        lines.append(
            f"  W {w_bar:>5}  •  {e_bar:<5} E  "
            f"({sectors['W']},{sectors['E']})"
        )
        lines.append(f"       |       |")
        lines.append(
            f"  SW {sw_bar:>5}     {se_bar:<5} SE  "
            f"({sectors['SW']},{sectors['SE']})"
        )
        lines.append(f"       S {s_bar:>5}  ({sectors['S']:2d}, {avg_speeds['S']:.0f}mph)")

        lines.append("  " + "─" * 30)

        return "\n".join(lines)
