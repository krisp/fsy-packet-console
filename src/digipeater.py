"""
APRS Digipeater Implementation

Implements standard APRS/TNC-2 digipeater behavior with new-paradigm support.
Only digipeats packets heard directly (not already-digipeated packets).
"""

from src.protocol import parse_ax25_addresses_and_control, kiss_unwrap, encode_aprs_packet
from src.utils import print_info, print_debug
import src.constants as constants


class Digipeater:
    """APRS digipeater following new-paradigm WIDEn-N rules."""

    def __init__(self, my_callsign: str, my_alias: str = "", enabled: bool = False):
        """Initialize digipeater.

        Args:
            my_callsign: Our callsign (e.g., "K1FSY-9")
            my_alias: Our alias (e.g., "WIDE1", "GATE", "RELAY")
            enabled: Whether digipeating is enabled
        """
        self.my_callsign = my_callsign.upper()
        self.my_alias = my_alias.upper() if my_alias else None
        self.enabled = enabled
        self.packets_digipeated = 0

    def _matches_my_calls(self, digi_call: str) -> bool:
        """Check if digipeater call matches our MYCALL or MYALIAS.

        Args:
            digi_call: Digipeater callsign to check (e.g., "WIDE1-1", "K1FSY-9")

        Returns:
            True if matches our callsign or alias

        Examples:
            MYCALL=K1FSY-9, MYALIAS=WIDE1
            - "K1FSY-9" → True (exact MYCALL match)
            - "WIDE1" → True (exact MYALIAS match)
            - "WIDE1-1" → True (MYALIAS with SSID)
            - "WIDE2-1" → False (different alias)
        """
        digi_upper = digi_call.upper()

        # Check exact MYCALL match
        if digi_upper == self.my_callsign:
            return True

        # No alias configured
        if not self.my_alias:
            return False

        # Check exact MYALIAS match
        if digi_upper == self.my_alias:
            return True

        # Check MYALIAS with SSID (e.g., WIDE1 matches WIDE1-1)
        if '-' in digi_upper:
            base_call = digi_upper.split('-')[0]
            if base_call == self.my_alias:
                return True

        return False

    def should_digipeat(
        self,
        src_call: str,
        hop_count: int,
        digipeater_path: list,
        is_source_digipeater: bool
    ) -> bool:
        """Check if we should digipeat this packet.

        Args:
            src_call: Source callsign
            hop_count: Number of hops (0 = direct, >0 = already digipeated)
            digipeater_path: List of digipeater callsigns in path
            is_source_digipeater: True if source is a known digipeater

        Returns:
            True if we should digipeat
        """
        if not self.enabled:
            return False

        # Rule 1: Only digipeat packets heard DIRECTLY (hop_count == 0)
        # Don't re-digipeat already-digipeated packets
        if hop_count != 0:
            if constants.DEBUG:
                print_debug(
                    f"Digipeater: Skip {src_call} - already digipeated (hop_count={hop_count})",
                    level=3
                )
            return False

        # Rule 2: Don't digipeat packets from known digipeaters
        # (prevents digipeater-to-digipeater ping-pong)
        if is_source_digipeater:
            if constants.DEBUG:
                print_debug(
                    f"Digipeater: Skip {src_call} - source is a digipeater",
                    level=3
                )
            return False

        # Rule 3: Don't digipeat our own packets (exact match including SSID)
        # Different SSIDs are different stations (K1MAL-5 != K1MAL-6)
        if src_call.upper() == self.my_callsign.upper():
            if constants.DEBUG:
                print_debug(
                    f"Digipeater: Skip {src_call} - our own packet",
                    level=3
                )
            return False

        # Rule 4: Check if path contains WIDEn-N or our callsign
        has_viable_hop = False
        for digi in digipeater_path:
            # Remove asterisk if present
            digi_clean = digi.rstrip('*').upper()

            # Skip already-used hops (marked with *)
            if digi.endswith('*'):
                continue

            # Check for WIDE1-1, WIDE2-2, etc.
            if digi_clean.startswith('WIDE') and '-' in digi_clean:
                has_viable_hop = True
                break

            # Check for our callsign or alias in path
            if self._matches_my_calls(digi_clean):
                has_viable_hop = True
                break

        if not has_viable_hop:
            if constants.DEBUG:
                print_debug(
                    f"Digipeater: Skip {src_call} - no viable hop in path {digipeater_path}",
                    level=3
                )
            return False

        return True

    def process_path(self, digipeater_path: list) -> tuple:
        """Process digipeater path according to new-paradigm rules.

        Args:
            digipeater_path: Original digipeater path

        Returns:
            Tuple of (new_path, used_alias) where:
                new_path: Updated path with our hop marked
                used_alias: The alias we filled (e.g., "WIDE1-1" or our callsign)
        """
        new_path = []
        used_alias = None
        filled = False

        for digi in digipeater_path:
            # Skip already-used hops (marked with *)
            if digi.endswith('*'):
                new_path.append(digi)
                continue

            digi_clean = digi.upper()

            # Process WIDEn-N
            if digi_clean.startswith('WIDE') and '-' in digi_clean and not filled:
                try:
                    parts = digi_clean.split('-')
                    if len(parts) == 2:
                        ssid = int(parts[1])

                        # Insert our callsign before this hop (mark as used)
                        new_path.append(f"{self.my_callsign}*")

                        # Decrement SSID (no asterisk - still unused)
                        if ssid > 1:
                            # WIDE2-2 → K1MAL*,WIDE2-1 (WIDE2-1 available for next digi)
                            new_path.append(f"{parts[0]}-{ssid-1}")
                        else:
                            # WIDE1-1 → K1MAL*,WIDE1* (WIDE1-0 shown as WIDE1* when consumed)
                            new_path.append(f"{parts[0]}*")

                        used_alias = digi
                        filled = True
                        continue
                except ValueError:
                    pass

            # Process our callsign or alias
            if self._matches_my_calls(digi_clean) and not filled:
                new_path.append(f"{self.my_callsign}*")
                used_alias = digi_clean  # Track what triggered it (could be MYCALL or MYALIAS)
                filled = True
                continue

            # Keep other hops unchanged
            new_path.append(digi)

        return new_path, used_alias

    def _extract_path_type(self, used_alias: str) -> str:
        """Extract path type from the used alias.

        Args:
            used_alias: The alias we filled (e.g., "WIDE1-1", "WIDE2-2", "K1FSY-9")

        Returns:
            Path type: "WIDE1-1", "WIDE2-2", "WIDE2-1", "Direct", or "Other"
        """
        if not used_alias:
            return "Other"

        alias_upper = used_alias.upper().rstrip('*')

        # Check for WIDEn-N patterns
        if alias_upper.startswith('WIDE') and '-' in alias_upper:
            # Return the exact WIDE pattern (e.g., "WIDE1-1", "WIDE2-2", "WIDE2-1")
            return alias_upper
        elif self._matches_my_calls(alias_upper):
            # Direct addressing to our callsign or alias
            return "Direct"
        else:
            return "Other"

    def digipeat_frame(self, complete_frame: bytes, aprs_data: dict) -> tuple:
        """Create digipeated frame with updated path.

        Args:
            complete_frame: Original KISS frame
            aprs_data: Parsed APRS data from parse_and_track_aprs_frame()

        Returns:
            Tuple of (new_frame, path_type) where:
                new_frame: New KISS frame with updated path, or None if processing failed
                path_type: Path type used ("WIDE1-1", "WIDE2-2", "Direct", etc.)
        """
        try:
            # Extract components
            src_call = aprs_data['src_call']
            dst_call = aprs_data['dst_call']
            info_str = aprs_data['info_str']
            original_path = aprs_data['digipeater_path']

            # Process the path
            new_path, used_alias = self.process_path(original_path)

            if used_alias is None:
                # Couldn't fill any hop - shouldn't happen if should_digipeat() returned True
                return None, None

            # Extract path type for statistics
            path_type = self._extract_path_type(used_alias)

            # Build new frame with updated path
            new_frame = encode_aprs_packet(src_call.rstrip('*'), dst_call.rstrip('*'), new_path, info_str)

            self.packets_digipeated += 1

            if constants.DEBUG:
                # Extract callsigns for filtering (src, dst, and path)
                filter_stations = [src_call.rstrip('*'), dst_call.rstrip('*')] + [
                    p.rstrip('*') for p in original_path
                ]
                print_debug(
                    f"Digipeater: Repeating {src_call} via {used_alias}",
                    level=2,
                    stations=filter_stations
                )
                print_debug(
                    f"  Original path: {original_path}",
                    level=3,
                    stations=filter_stations
                )
                print_debug(
                    f"  New path: {new_path}",
                    level=3,
                    stations=filter_stations
                )

            return new_frame, path_type

        except Exception as e:
            print_debug(f"Digipeater: Error creating digipeated frame: {e}", level=1)
            return None, None
