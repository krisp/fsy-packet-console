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

    def __init__(self, my_callsign: str, my_alias: str = "", mode: str = "OFF", enabled: bool = None):
        """Initialize digipeater.

        Args:
            my_callsign: Our callsign (e.g., "K1FSY-9")
            my_alias: Our alias (e.g., "WIDE1", "GATE", "RELAY")
            mode: Digipeater mode - "ON", "OFF", or "SELF"
                  "ON" = repeat all direct packets
                  "OFF" = don't repeat anything
                  "SELF" = only repeat packets from our callsign (any SSID)
            enabled: (deprecated) Legacy boolean parameter for backward compatibility
        """
        self.my_callsign = my_callsign.upper()
        self.my_alias = my_alias.upper() if my_alias else None

        # Handle legacy 'enabled' parameter for backward compatibility
        if enabled is not None:
            self.mode = "ON" if enabled else "OFF"
        elif isinstance(mode, bool):
            # Legacy: mode passed as boolean
            self.mode = "ON" if mode else "OFF"
        elif isinstance(mode, str):
            self.mode = mode.upper()
        else:
            self.mode = "OFF"

        self.packets_digipeated = 0

    @property
    def enabled(self) -> bool:
        """Backward compatibility property for enabled check."""
        return self.mode in ("ON", "SELF")

    @enabled.setter
    def enabled(self, value: bool):
        """Backward compatibility setter for enabled property."""
        self.mode = "ON" if value else "OFF"

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

    def _extract_aprs_message_addressee(self, info_str: str) -> str:
        """Extract APRS message addressee from info field.

        Args:
            info_str: APRS info field

        Returns:
            Addressee callsign if this is a message, empty string otherwise
        """
        if not info_str or not info_str.startswith(':'):
            return ""

        # APRS message format: :ADDRESSEE:message text
        # ADDRESSEE is padded to 9 characters
        try:
            # Extract addressee (between first : and second :)
            parts = info_str[1:].split(':', 1)
            if len(parts) >= 1:
                # Strip whitespace padding
                addressee = parts[0].strip()
                return addressee
        except Exception:
            pass

        return ""

    def should_digipeat(
        self,
        src_call: str,
        dst_call: str,
        hop_count: int,
        digipeater_path: list,
        is_source_digipeater: bool,
        info_str: str = ""
    ) -> bool:
        """Check if we should digipeat this packet.

        Args:
            src_call: Source callsign
            dst_call: Destination callsign (AX.25 address)
            hop_count: Number of hops (0 = direct, >0 = already digipeated)
            digipeater_path: List of digipeater callsigns in path
            is_source_digipeater: True if source is a known digipeater
            info_str: APRS info field (for message addressee extraction)

        Returns:
            True if we should digipeat
        """
        if self.mode == "OFF":
            return False

        # Rule 1: Only digipeat packets heard DIRECTLY (hop_count == 0)
        # EXCEPTION: SELF mode allows any hop_count for inbound packets to our callsign
        allow_already_digipeated = False
        if self.mode == "SELF" and hop_count > 0:
            # Check if this is an inbound packet to our callsign (different SSID)
            my_base = self.my_callsign.upper().split('-')[0]
            src_base = src_call.upper().split('-')[0]

            # Check both AX.25 destination and APRS message addressee
            dst_base = dst_call.upper().split('-')[0]
            is_ax25_match = (dst_base == my_base and dst_call.upper() != self.my_callsign.upper())

            # For APRS messages, also check addressee in info field
            message_addressee = self._extract_aprs_message_addressee(info_str)
            is_message_match = False
            if message_addressee:
                msg_base = message_addressee.upper().split('-')[0]
                is_message_match = (msg_base == my_base and message_addressee.upper() != self.my_callsign.upper())

            # Inbound if either AX.25 destination or message addressee matches our base (not exact SSID), and not from us
            if (is_ax25_match or is_message_match) and src_base != my_base:
                # Check we're not already in path (loop prevention)
                if not any(my_base in hop.rstrip('*').upper() for hop in digipeater_path):
                    allow_already_digipeated = True
                    if constants.DEBUG:
                        target = message_addressee if is_message_match else dst_call
                        print_debug(
                            f"Digipeater: SELF courtesy relay - inbound to {target} (hop_count={hop_count})",
                            level=3
                        )

        if hop_count != 0 and not allow_already_digipeated:
            if constants.DEBUG:
                print_debug(
                    f"Digipeater: Skip {src_call} - already digipeated (hop_count={hop_count})",
                    level=3
                )
            return False

        # Rule 2: Don't digipeat packets from known digipeaters
        # (prevents digipeater-to-digipeater ping-pong)
        # EXCEPTION: SELF mode inbound allows packets FROM digipeaters if addressed TO us
        if is_source_digipeater:
            # Check if SELF mode inbound (packet addressed to our callsign)
            skip_rule2 = False
            if self.mode == "SELF":
                my_base = self.my_callsign.upper().split('-')[0]

                # Check AX.25 destination
                dst_base = dst_call.upper().split('-')[0]
                is_inbound_ax25 = (dst_base == my_base and dst_call.upper() != self.my_callsign.upper())

                # Check APRS message addressee
                message_addressee = self._extract_aprs_message_addressee(info_str)
                is_inbound_message = False
                if message_addressee:
                    msg_base = message_addressee.upper().split('-')[0]
                    is_inbound_message = (msg_base == my_base and message_addressee.upper() != self.my_callsign.upper())

                # Skip Rule 2 if addressed to our callsign (digipeater sending TO us is OK)
                if is_inbound_ax25 or is_inbound_message:
                    skip_rule2 = True
                    if constants.DEBUG:
                        target = message_addressee if is_inbound_message else dst_call
                        print_debug(
                            f"Digipeater: SELF mode - allowing {src_call} (digipeater) because addressed to {target}",
                            level=3
                        )

            if not skip_rule2:
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

        # Rule 3.5: SELF mode - only digipeat packets from/to our base callsign (any SSID)
        if self.mode == "SELF":
            # Extract base callsigns (without SSID)
            src_base = src_call.upper().split('-')[0]
            my_base = self.my_callsign.upper().split('-')[0]
            dst_base = dst_call.upper().split('-')[0]

            # Check if packet involves our callsign (outbound OR inbound)
            is_outbound = (src_base == my_base and hop_count == 0)  # FROM our callsign, direct only

            # Check AX.25 destination
            is_inbound_ax25 = (dst_base == my_base and dst_call.upper() != self.my_callsign.upper() and
                               src_base != my_base)

            # Check APRS message addressee
            is_inbound_message = False
            message_addressee = self._extract_aprs_message_addressee(info_str)
            if message_addressee:
                msg_base = message_addressee.upper().split('-')[0]
                is_inbound_message = (msg_base == my_base and message_addressee.upper() != self.my_callsign.upper() and
                                      src_base != my_base)

            is_inbound = is_inbound_ax25 or is_inbound_message

            if not is_outbound and not is_inbound:
                if constants.DEBUG:
                    print_debug(
                        f"Digipeater: Skip {src_call} - SELF mode, not our callsign (src: {src_base}, dst: {dst_base} != {my_base})",
                        level=3
                    )
                return False

            if is_outbound and constants.DEBUG:
                print_debug(
                    f"Digipeater: SELF mode outbound - will digipeat {src_call} (matches base {my_base})",
                    level=3
                )
            if is_inbound and hop_count == 0 and constants.DEBUG:
                print_debug(
                    f"Digipeater: SELF mode inbound - will digipeat for {dst_call} (direct packet)",
                    level=3
                )
            # Continue to path checks below

        # Rule 4: Check if path contains WIDEn-N or our callsign
        # EXCEPTION: SELF mode inbound doesn't require viable hop (last mile delivery)
        if self.mode == "SELF":
            my_base = self.my_callsign.upper().split('-')[0]
            src_base = src_call.upper().split('-')[0]

            # Check AX.25 destination
            dst_base = dst_call.upper().split('-')[0]
            is_inbound_ax25 = (dst_base == my_base and dst_call.upper() != self.my_callsign.upper() and src_base != my_base)

            # Check APRS message addressee
            is_inbound_message = False
            message_addressee = self._extract_aprs_message_addressee(info_str)
            if message_addressee:
                msg_base = message_addressee.upper().split('-')[0]
                is_inbound_message = (msg_base == my_base and message_addressee.upper() != self.my_callsign.upper() and
                                      src_base != my_base)

            # If this is inbound to our callsign (either way), skip viable hop check
            if is_inbound_ax25 or is_inbound_message:
                return True  # Allow regardless of path state

        # For all other cases, check for viable hop
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

    def process_path(self, digipeater_path: list, courtesy_relay: bool = False) -> tuple:
        """Process digipeater path according to new-paradigm rules.

        Args:
            digipeater_path: Original digipeater path
            courtesy_relay: If True, insert our callsign without consuming WIDE2 (SELF mode inbound)

        Returns:
            Tuple of (new_path, used_alias) where:
                new_path: Updated path with our hop marked
                used_alias: The alias we filled (e.g., "WIDE1-1" or our callsign)
        """
        new_path = []
        used_alias = None
        filled = False

        # SELF mode courtesy relay - insert ourselves (preserve WIDE if present, or append if exhausted)
        if courtesy_relay:
            # Find position: after last * (digipeated) hop, before first unconsumed hop
            # If all hops are consumed (*), append at end
            insert_pos = 0
            found_unconsumed = False

            for i, hop in enumerate(digipeater_path):
                if hop.endswith('*'):
                    insert_pos = i + 1
                else:
                    # Found first unconsumed hop, insert before it
                    found_unconsumed = True
                    break

            # If no unconsumed hops found, insert_pos is already at the end
            if not found_unconsumed:
                insert_pos = len(digipeater_path)

            # Build new path: insert ourselves at the determined position
            new_path = (
                digipeater_path[:insert_pos] +
                [f"{self.my_callsign}*"] +
                digipeater_path[insert_pos:]
            )
            used_alias = "Courtesy"  # Special marker for courtesy relay

            return new_path, used_alias

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
            used_alias: The alias we filled (e.g., "WIDE1-1", "WIDE2-2", "K1FSY-9", "Courtesy")

        Returns:
            Path type: "WIDE1-1", "WIDE2-2", "WIDE2-1", "Direct", "Courtesy", or "Other"
        """
        if not used_alias:
            return "Other"

        alias_upper = used_alias.upper().rstrip('*')

        # Check for courtesy relay marker
        if alias_upper == "COURTESY":
            return "Courtesy"

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
                path_type: Path type used ("WIDE1-1", "WIDE2-2", "Direct", "Courtesy", etc.)
        """
        try:
            # Extract components
            src_call = aprs_data['src_call']
            dst_call = aprs_data['dst_call']
            info_str = aprs_data['info_str']
            original_path = aprs_data['digipeater_path']

            # Determine if this is a courtesy relay (SELF mode inbound with hop_count > 0)
            courtesy_relay = False
            hop_count = aprs_data.get('hop_count', 0)

            if self.mode == "SELF" and hop_count > 0:
                dst_base = dst_call.upper().split('-')[0]
                my_base = self.my_callsign.upper().split('-')[0]
                src_base = src_call.upper().split('-')[0]

                # Courtesy relay if: destination is our base (different SSID), source is not ours, already digipeated
                # No WIDE requirement - we handle both preserved and exhausted paths
                if dst_base == my_base and dst_call.upper() != self.my_callsign.upper() and src_base != my_base:
                    courtesy_relay = True

            # Process the path
            new_path, used_alias = self.process_path(original_path, courtesy_relay=courtesy_relay)

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
