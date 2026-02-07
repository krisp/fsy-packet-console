"""Digipeater statistics tracking.

Provides data structures for tracking digipeater activity and performance:
- DigipeaterActivity: Individual digipeater activity event
- DigipeaterStats: Aggregated statistics with time-series retention
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class DigipeaterActivity:
    """Represents a single digipeater activity event."""

    timestamp: datetime
    station_call: str  # Station that was digipeated
    # Path classification: "WIDE1-1", "WIDE2-1", "Direct", "Other"
    path_type: str
    original_path: List[str]  # Original path from packet
    frame_number: Optional[int] = None  # Frame buffer reference

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dictionary.

        Returns:
            Dictionary with ISO-formatted timestamps
        """
        return {
            "timestamp": self.timestamp.isoformat(),
            "station_call": self.station_call,
            "path_type": self.path_type,
            "original_path": self.original_path,
            "frame_number": self.frame_number,
        }

    @staticmethod
    def from_dict(data: dict) -> 'DigipeaterActivity':
        """Create from dictionary.

        Args:
            data: Dictionary with timestamp as ISO string

        Returns:
            DigipeaterActivity instance
        """
        return DigipeaterActivity(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            station_call=data["station_call"],
            path_type=data["path_type"],
            original_path=data.get("original_path", []),
            frame_number=data.get("frame_number"),
        )


@dataclass
class DigipeaterStats:
    """Aggregated digipeater statistics with time-series retention."""

    session_start: datetime  # When the current session started
    packets_digipeated: int = 0  # Total count of digipeated packets
    activities: List[DigipeaterActivity] = field(
        default_factory=list
    )  # Recent activity events (last 500)
    top_stations: Dict[str, int] = field(
        default_factory=dict
    )  # station_call -> count
    path_usage: Dict[str, int] = field(
        default_factory=dict
    )  # path_type -> count

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dictionary.

        Returns:
            Dictionary with ISO-formatted timestamps and serialized activities
        """
        return {
            "session_start": self.session_start.isoformat(),
            "packets_digipeated": self.packets_digipeated,
            "activities": [act.to_dict() for act in self.activities],
            "top_stations": self.top_stations,
            "path_usage": self.path_usage,
        }

    @staticmethod
    def from_dict(data: dict) -> 'DigipeaterStats':
        """Create from dictionary.

        Args:
            data: Dictionary with timestamp as ISO string

        Returns:
            DigipeaterStats instance
        """
        return DigipeaterStats(
            session_start=datetime.fromisoformat(data["session_start"]),
            packets_digipeated=data.get("packets_digipeated", 0),
            activities=[
                DigipeaterActivity.from_dict(act)
                for act in data.get("activities", [])
            ],
            top_stations=data.get("top_stations", {}),
            path_usage=data.get("path_usage", {}),
        )
