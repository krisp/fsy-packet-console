"""APRS device identification based on hessu/aprs-deviceid database.

This module identifies APRS devices and software from:
- Destination callsigns (tocalls)
- MIC-E comment suffixes (mice new-style)
- MIC-E prefix+suffix (micelegacy old Kenwood)

Database source: https://github.com/hessu/aprs-deviceid
License: CC BY-SA 2.0
"""

from src.utils import print_warning, print_error

import os
import yaml
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass


@dataclass
class DeviceInfo:
    """Information about an identified APRS device."""
    vendor: str
    model: str
    class_type: str = None  # wx, tracker, ht, rig, app, software, etc.
    os: str = None          # Android, Windows, Linux/Unix, embedded, ios
    features: List[str] = None  # messaging, item-in-msg, etc.

    def __str__(self):
        """Human-readable device string."""
        if self.vendor and self.model:
            return f"{self.vendor} {self.model}"
        elif self.model:
            return self.model
        return "Unknown"


class DeviceIdentifier:
    """APRS device identification engine."""

    def __init__(self, database_path: str = None):
        """Initialize device identifier.

        Args:
            database_path: Optional path to tocalls.yaml. If None, uses data/tocalls.yaml
        """
        if database_path is None:
            # Default to data/tocalls.yaml relative to project root
            project_root = Path(__file__).parent.parent
            database_path = project_root / "data" / "tocalls.yaml"

        self.database_path = Path(database_path)
        self.tocalls: List[Dict] = []
        self.mice: List[Dict] = []
        self.micelegacy: List[Dict] = []
        self.classes: Dict[str, Dict] = {}

        self._load_database()

    def _load_database(self):
        """Load and parse the YAML device database."""
        if not self.database_path.exists():
            print_warning(f"Device database not found at {self.database_path}")
            return

        try:
            with open(self.database_path, 'r') as f:
                data = yaml.safe_load(f)

            self.tocalls = data.get('tocalls', [])
            self.mice = data.get('mice', [])
            self.micelegacy = data.get('micelegacy', [])

            # Build classes lookup
            for cls in data.get('classes', []):
                self.classes[cls['class']] = {
                    'shown': cls.get('shown'),
                    'description': cls.get('description')
                }

        except Exception as e:
            print_error(f"Error loading device database: {e}")

    def _match_tocall(self, pattern: str, tocall: str) -> bool:
        """Match a tocall against a pattern with wildcards.

        Wildcard rules:
        - ? matches any single character
        - * matches any characters
        - n matches a single digit

        Args:
            pattern: Pattern from database (e.g., "APY???", "APZ*")
            tocall: Actual tocall to match (e.g., "APY500")

        Returns:
            True if tocall matches pattern
        """
        if pattern == tocall:
            return True

        # Convert pattern to regex-like matching
        i = j = 0
        while i < len(pattern) and j < len(tocall):
            if pattern[i] == '?':
                # ? matches any single character
                i += 1
                j += 1
            elif pattern[i] == 'n':
                # n matches a single digit
                if not tocall[j].isdigit():
                    return False
                i += 1
                j += 1
            elif pattern[i] == '*':
                # * matches rest of string
                return True
            elif pattern[i] == tocall[j]:
                # Exact character match
                i += 1
                j += 1
            else:
                return False

        # Pattern and tocall must be same length (unless pattern ended with *)
        if i == len(pattern) and j == len(tocall):
            return True
        if i < len(pattern) and pattern[i:] == '*':
            return True

        return False

    def identify_by_tocall(self, destination: str) -> Optional[DeviceInfo]:
        """Identify device by APRS destination callsign (tocall).

        Args:
            destination: Destination address (e.g., "APY500", "APRS")

        Returns:
            DeviceInfo if identified, None otherwise
        """
        # Remove SSID if present
        dest_call = destination.split('-')[0] if '-' in destination else destination
        dest_call = dest_call.upper()

        # Try exact matches first (no wildcards)
        for entry in self.tocalls:
            tocall_pattern = entry['tocall'].upper()
            if '?' not in tocall_pattern and '*' not in tocall_pattern and 'n' not in tocall_pattern.lower():
                if tocall_pattern == dest_call:
                    return DeviceInfo(
                        vendor=entry.get('vendor', ''),
                        model=entry.get('model', ''),
                        class_type=entry.get('class'),
                        os=entry.get('os'),
                        features=entry.get('features')
                    )

        # Try wildcarded matches, longest match first
        matches = []
        for entry in self.tocalls:
            tocall_pattern = entry['tocall'].upper()
            if self._match_tocall(tocall_pattern, dest_call):
                # Calculate match quality (number of non-wildcard chars)
                quality = sum(1 for c in tocall_pattern if c not in '?*n')
                matches.append((quality, entry))

        if matches:
            # Return highest quality (longest) match
            matches.sort(key=lambda x: x[0], reverse=True)
            entry = matches[0][1]
            return DeviceInfo(
                vendor=entry.get('vendor', ''),
                model=entry.get('model', ''),
                class_type=entry.get('class'),
                os=entry.get('os'),
                features=entry.get('features')
            )

        return None

    def identify_by_mice(self, comment: str) -> Optional[DeviceInfo]:
        """Identify device by MIC-E comment suffix.

        MIC-E devices encode their type in the last 2 characters of the comment.
        The first character of the suffix indicates messaging capability:
        - ` (backtick, 0x60) = messaging capable
        - ' (apostrophe, 0x27) = no messaging

        Args:
            comment: MIC-E comment field (raw, before cleaning)

        Returns:
            DeviceInfo if identified, None otherwise
        """
        if not comment or len(comment) < 2:
            return None

        # Try new-style 2-character suffix
        suffix = comment[-2:]
        for entry in self.mice:
            if entry['suffix'] == suffix:
                return DeviceInfo(
                    vendor=entry.get('vendor', ''),
                    model=entry.get('model', ''),
                    class_type=entry.get('class'),
                    os=entry.get('os'),
                    features=entry.get('features')
                )

        # Try legacy prefix+suffix (old Kenwood)
        if len(comment) >= 2:
            prefix = comment[0]
            suffix = comment[-1]

            for entry in self.micelegacy:
                if entry.get('prefix') == prefix and entry.get('suffix') == suffix:
                    return DeviceInfo(
                        vendor=entry.get('vendor', ''),
                        model=entry.get('model', ''),
                        class_type=entry.get('class'),
                        os=entry.get('os'),
                        features=entry.get('features')
                    )

        return None

    def get_class_description(self, class_type: str) -> str:
        """Get human-readable description for a device class.

        Args:
            class_type: Class identifier (e.g., 'ht', 'rig', 'wx')

        Returns:
            Human-readable class name
        """
        if class_type in self.classes:
            return self.classes[class_type].get('shown', class_type)
        return class_type


# Global instance
_device_identifier = None


def get_device_identifier() -> DeviceIdentifier:
    """Get or create the global device identifier instance."""
    global _device_identifier
    if _device_identifier is None:
        _device_identifier = DeviceIdentifier()
    return _device_identifier
