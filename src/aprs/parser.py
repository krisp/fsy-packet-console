"""APRS packet parsing.

Provides the :class:`APRSParserMixin`, which supplies the packet-parsing
behaviour for :class:`~src.aprs.manager.APRSManager`.

Responsibilities (all parse the raw APRS information field into model
objects and update station tracking state):
- Third-party packet unwrapping (``parse_third_party``)
- MIC-E compressed position/telemetry decoding (``parse_aprs_mice``)
- Text messages, ACKs and rejects (``parse_aprs_message``)
- Weather reports (``parse_aprs_weather``)
- Base-91 compressed positions (``_parse_compressed_position``)
- Uncompressed position reports (``parse_aprs_position``)
- Objects, items, status and telemetry (``parse_aprs_object``,
  ``parse_aprs_item``, ``parse_aprs_status``, ``parse_aprs_telemetry``)

These methods read and mutate state that lives on the ``APRSManager``
instance (``self.stations``, ``self.monitored_messages`` and the shared
tracking helpers ``self._get_or_create_station``,
``self._add_position_to_history``, ``self._add_weather_to_history`` and
``self._broadcast_update``). The mixin is therefore composed into the
manager via multiple inheritance rather than delegation, which keeps the
public parsing API unchanged.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src.device_id import get_device_identifier
from src.utils import print_debug

from .models import (
    APRSMessage, APRSPosition, APRSWeather, APRSStatus,
    APRSTelemetry, ensure_utc_aware
)


class APRSParserMixin:
    """APRS packet parsing behaviour for :class:`APRSManager`."""

    def parse_third_party(
        self, relay_call: str, info: str
    ) -> Optional[Tuple[str, str, str]]:
        """Parse third-party APRS packet.

        Third-party format: }SOURCE>DEST,PATH:info_field

        Args:
            relay_call: Callsign of the relay station
            info: APRS information field

        Returns:
            Tuple of (source_call, relay_call, inner_info) if third-party, None otherwise
        """
        if not info.startswith("}"):
            return None

        try:
            # Remove leading }
            inner = info[1:]

            # Extract source callsign (before >)
            gt_pos = inner.find(">")
            if gt_pos == -1:
                return None

            source_call = inner[:gt_pos].strip()

            # Find the FIRST : after the > which separates header from info
            # (Can't use rfind because info field may contain colons, e.g., APRS messages)
            colon_pos = inner.find(":", gt_pos)
            if colon_pos == -1:
                return None

            inner_info = inner[colon_pos + 1 :]

            print_debug(
                f"parse_third_party: source={source_call}, relay={relay_call}, inner_info='{inner_info[:50]}...'",
                level=5,
            )

            return (source_call, relay_call, inner_info)

        except Exception as e:
            print_debug(f"parse_third_party: exception {e}", level=5)
            return None

    def parse_aprs_mice(
        self,
        station: str,
        dest_addr: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None,
    ) -> Optional[APRSPosition]:
        """Parse MIC-E format APRS position.

        MIC-E encodes position data in the destination address and first 9 bytes of info.

        Args:
            station: Station callsign
            dest_addr: Destination address (contains encoded latitude)
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops (0 = direct RF)
            digipeater_path: List of digipeater callsigns from AX.25 path

        Returns:
            APRSPosition if valid MIC-E, None otherwise
        """
        # MIC-E packets start with specific data types
        if not info or len(info) < 9:
            return None

        # Check for MIC-E indicator (', `, or 0x1c-0x1f)
        first_byte = ord(info[0]) if isinstance(info[0], str) else info[0]
        if first_byte not in (0x27, 0x60, 0x1C, 0x1D, 0x1E, 0x1F):
            return None

        try:
            print_debug(
                f"MIC-E parsing: {station} dest={dest_addr} info={repr(info[:20])}...",
                level=5,
                stations=[station]
            )

            # Remove SSID from dest_addr if present
            dest_call = (
                dest_addr.split("-")[0] if "-" in dest_addr else dest_addr
            )

            # Destination address must be 6 characters for MIC-E
            if len(dest_call) != 6:
                return None

            # Decode latitude from destination address
            # Each character encodes a digit plus message/position info
            lat_digits = []
            north_south = None
            msg_bits = []

            for i, ch in enumerate(dest_call):
                if "0" <= ch <= "9":
                    lat_digits.append(ch)
                    msg_bits.append(0)
                elif "A" <= ch <= "J":
                    lat_digits.append(str(ord(ch) - ord("A")))
                    msg_bits.append(1)
                elif "P" <= ch <= "Y":
                    lat_digits.append(str(ord(ch) - ord("P")))
                    msg_bits.append(1)
                elif ch == "K" or ch == "L" or ch == "Z":
                    # Space characters represent zero
                    lat_digits.append("0")
                    msg_bits.append(0 if ch == "L" else 1)
                else:
                    return None

            # Extract latitude
            if len(lat_digits) != 6:
                return None

            # Format: DDMM.HH (degrees, minutes, hundredths)
            lat_str = "".join(lat_digits)

            lat_deg = int(lat_str[0:2])
            lat_min = float(lat_str[2:4] + "." + lat_str[4:6])
            latitude = lat_deg + (lat_min / 60.0)

            # N/S is encoded in message bits (bit 3)
            # Per APRS spec: 0 = South, 1 = North
            if msg_bits[3] == 0:
                latitude = -latitude  # South

            # Decode longitude from info bytes 1-3
            lon_deg = ord(info[1]) - 28
            lon_min = ord(info[2]) - 28
            lon_min_frac = ord(info[3]) - 28

            # Longitude offset is in message bits (bit 4)
            if msg_bits[4] == 1:
                lon_deg += 100  # +100 for longitude >= 100 degrees

            # E/W is in message bits (bit 5)
            longitude = lon_deg + ((lon_min + lon_min_frac / 100.0) / 60.0)
            if msg_bits[5] == 1:
                longitude = -longitude  # West

            print_debug(
                f"MIC-E decoded position: {latitude:.6f}, {longitude:.6f}",
                level=5,
                stations=[station]
            )

            # Extract speed and course from bytes 4-6
            speed_course = ord(info[4]) - 28
            speed = ((ord(info[5]) - 28) * 10) + ((speed_course // 10) % 10)
            course = ((speed_course % 10) * 100) + (ord(info[6]) - 28)

            # Symbol table and code
            symbol_code = info[7] if len(info) > 7 else ">"
            symbol_table = info[8] if len(info) > 8 else "/"

            # Status text (everything after byte 8)
            # MIC-E status format: T......Mv where:
            #   T = Type indicator (1 byte): space, >, ], `, '
            #   . = Free text (7 bytes, can include altitude as "}aaa" base-91 encoding)
            #   M = Manufacturer code (1 byte)
            #   v = Version code (1 byte)
            raw_comment = info[9:] if len(info) > 9 else ""

            # Strip MIC-E type indicator (first byte)
            # Known type indicators: space (0x20), > (0x3E), ] (0x5D), ` (0x60), ' (0x27)
            if raw_comment and ord(raw_comment[0]) in (
                0x20,
                0x3E,
                0x5D,
                0x60,
                0x27,
            ):
                raw_comment = raw_comment[1:]

            # Keep only printable characters (0x20-0x7E = space through tilde)
            printable_comment = "".join(
                c for c in raw_comment if 0x20 <= ord(c) <= 0x7E
            )

            # Strip MIC-E altitude encoding if present: }xyz (base-91)
            # Altitude format: } followed by 2-3 base-91 characters
            if "}" in printable_comment:
                # Find the } and remove it plus following characters that look like altitude
                brace_idx = printable_comment.find("}")
                # Base-91 uses chars 0x21-0x7B (! through {)
                end_idx = brace_idx + 1
                while (
                    end_idx < len(printable_comment)
                    and end_idx < brace_idx + 4
                ):
                    ch = printable_comment[end_idx]
                    if 0x21 <= ord(ch) <= 0x7B:  # Base-91 character range
                        end_idx += 1
                    else:
                        break
                # Remove the altitude encoding
                printable_comment = (
                    printable_comment[:brace_idx] + printable_comment[end_idx:]
                )

            # Identify device type from MIC-E comment suffix BEFORE stripping
            # MIC-E devices encode type in last 2 characters (new-style) or prefix+suffix (legacy)
            device_str = None
            try:
                device_id = get_device_identifier()
                device_info = device_id.identify_by_mice(printable_comment)
                if device_info:
                    device_str = str(device_info)
                    print_debug(
                        f"MIC-E device identified: {device_str} (comment: {repr(printable_comment[-10:])})",
                        level=3,
                        stations=[station]
                    )
                else:
                    print_debug(
                        f"MIC-E device not found for comment: {repr(printable_comment[-10:])}",
                        level=4,
                        stations=[station]
                    )
            except Exception as e:
                print_debug(f"MIC-E device ID error: {e}", level=3, stations=[station])

            # Strip trailing manufacturer/version codes (last 1-2 chars)
            # These are typically symbols (non-alphanumeric except space)
            while (
                len(printable_comment) > 0
                and printable_comment[-1]
                in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
            ):
                printable_comment = printable_comment[:-1]

            # Strip trailing whitespace
            printable_comment = printable_comment.rstrip()

            # If result is mostly symbols/gibberish (>60% non-alphanumeric), suppress it
            if printable_comment:
                alphanumeric_count = sum(
                    1 for c in printable_comment if c.isalnum() or c == " "
                )
                if (
                    len(printable_comment) > 0
                    and (alphanumeric_count / len(printable_comment)) < 0.4
                ):
                    printable_comment = ""  # Suppress gibberish

            # Apply standard APRS comment cleaning to remove data fields (PHG, weather codes, etc.)
            comment = self.clean_position_comment(printable_comment)

            print_debug(
                f"MIC-E symbol: {symbol_table}{symbol_code}, comment: {repr(comment)}",
                level=5,
                stations=[station]
            )

            # Convert to Maidenhead grid
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Filter out invalid "Null Island" coordinates (0.0, 0.0)
            if latitude == 0.0 and longitude == 0.0:
                print_debug(
                    f"parse_mice_position: Rejecting Null Island coordinates from {station}",
                    level=5,
                    stations=[station]
                )
                return None

            # Create position object
            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
                device=device_str,
            )

            # Store position
            self.position_reports[station.upper()] = pos

            # Track station
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="mic_e", timestamp=timestamp, frame_number=frame_number)
            sta.last_position = pos
            if device_str:
                sta.device = device_str
            self._add_position_to_history(sta, pos)

            # Broadcast station update to web clients
            self._broadcast_update('station_update', sta)

            print_debug(
                f"MIC-E position stored: {station} @ {grid_square} ({latitude:.6f}, {longitude:.6f})",
                level=5,
                stations=[station]
            )

            return pos

        except Exception as e:
            print_debug(f"parse_aprs_mice exception for {station}: {e}", level=5, stations=[station])
            import traceback
            print_debug(traceback.format_exc(), level=6, stations=[station])
            return None

    def parse_aprs_message(
        self,
        from_call: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSMessage]:
        """Parse APRS message format.

        APRS message format: :CALLSIGN :message text{12345
        Where CALLSIGN is 9 chars padded with spaces, {12345 is optional message ID

        Args:
            from_call: Source callsign
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops (0 = direct RF)
            digipeater_path: List of digipeater callsigns from AX.25 path

        Returns:
            APRSMessage if this is a message, None otherwise
        """
        if not info.startswith(":"):
            return None

        print_debug(
            f"parse_aprs_message: from={from_call}, info='{info[:50]}...'",
            level=5,
        )

        try:
            # Format: :CALLSIGN :message{id
            # CALLSIGN is 9 chars (padded with spaces)
            if len(info) < 11:  # Minimum: ":" + 9-char call + ":"
                print_debug(
                    f"parse_aprs_message: info too short ({len(info)} chars)",
                    level=5,
                )
                return None

            to_call = info[1:10].strip()  # Extract 9-char callsign field
            if info[10] != ":":
                print_debug(
                    f"parse_aprs_message: missing colon at position 10",
                    level=5,
                )
                return None

            message_part = info[11:]

            print_debug(
                f"parse_aprs_message: to_call='{to_call}', message='{message_part[:30]}...'",
                level=5,
            )

            # Check for message ID: {12345
            message_id = None
            message_text = message_part
            if "{" in message_part:
                parts = message_part.split("{", 1)
                message_text = parts[0]
                if len(parts) > 1:
                    message_id = parts[1].strip()

            # Filter out telemetry definition messages (not user messages)
            # These are configuration broadcasts: PARM., UNIT., EQNS., BITS.
            if message_text.startswith(("PARM.", "UNIT.", "EQNS.", "BITS.")):
                # Track station activity but don't treat as a message
                sender_station = self._get_or_create_station(
                    from_call, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="telemetry_config", timestamp=timestamp, frame_number=frame_number
                )
                # Note: packets_heard incremented by _get_or_create_station
                print_debug(
                    f"parse_aprs_message: filtered out telemetry config message",
                    level=5,
                )
                return None  # Don't notify - these are telemetry config, not messages

            # Handle ACK/REJ messages (protocol acknowledgments)
            # Format: "ack12345" or "rej12345"
            if message_text.lower().startswith(("ack", "rej")):
                # Track station activity
                sender_station = self._get_or_create_station(
                    from_call, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="message_ack", timestamp=timestamp, frame_number=frame_number
                )
                # Note: packets_heard incremented by _get_or_create_station

                # Check if this ACK is for one of our sent messages
                if message_text.lower().startswith("ack"):
                    # Extract ID after "ack", handling multi-line format: ack12345}line_num
                    acked_msg_id = message_text[3:].strip()
                    if "}" in acked_msg_id:
                        acked_msg_id = acked_msg_id.split("}")[0]
                    print_debug(
                        f"parse_aprs_message: received ACK for message ID '{acked_msg_id}' from {from_call}",
                        level=5,
                    )

                    # Extract base callsign from ACK sender (strip SSID)
                    from_call_base = from_call.upper().split("-")[0]

                    # Find and mark our sent message as acknowledged
                    found = False
                    for sent_msg in self.messages:
                        if sent_msg.direction == "sent":
                            print_debug(
                                f"  Checking sent msg: to={sent_msg.to_call}, msg_id={sent_msg.message_id}, ack={sent_msg.ack_received}",
                                level=6,
                            )

                        # Match on message ID and base callsign (to handle different SSIDs)
                        sent_to_base = sent_msg.to_call.upper().split("-")[0]
                        if (
                            sent_msg.direction == "sent"
                            and sent_msg.message_id == acked_msg_id
                            and (sent_msg.to_call.upper() == from_call.upper()
                                 or sent_to_base == from_call_base)
                        ):
                            sent_msg.ack_received = True
                            found = True
                            print_debug(
                                f"parse_aprs_message: ✓ MATCHED - marked message ID '{acked_msg_id}' to {sent_msg.to_call} as acknowledged (ACK from {from_call})",
                                level=5,
                            )
                            break

                    if not found:
                        print_debug(
                            f"parse_aprs_message: ACK for '{acked_msg_id}' from {from_call} - no matching sent message found",
                            level=5,
                        )

                return None  # Don't notify or add ACK messages to list

            # Check if this is our own message digipeated back to us
            # If so, treat it as proof of successful transmission (implicit ACK)
            # NOTE: Only match exact callsign (with SSID). Different SSIDs are different
            # stations (e.g., K1MAL-5 is HT, K1MAL-6 is console) and should communicate.
            is_our_message = from_call.upper() == self.my_callsign

            if is_our_message and digipeater_path:
                # This is our own message coming back via digipeater(s)
                # Could be a regular message (with message_id) or an ACK (without message_id)

                if message_id:
                    # Regular message with message ID
                    print_debug(
                        f"parse_aprs_message: received our own message via digipeater (ID={message_id}, path={digipeater_path})",
                        level=5,
                    )

                    # Find and mark our sent message as digipeated
                    found = False
                    for sent_msg in self.messages:
                        if (
                            sent_msg.direction == "sent"
                            and sent_msg.message_id == message_id
                            and not sent_msg.digipeated  # Don't re-mark if already digipeated
                        ):
                            sent_msg.digipeated = True
                            found = True
                            print_debug(
                                f"parse_aprs_message: ✓ DIGIPEATED - marked message ID '{message_id}' as digipeated (heard via {','.join(digipeater_path)})",
                                level=5,
                            )
                            break

                    if not found:
                        print_debug(
                            f"parse_aprs_message: Digipeated message ID '{message_id}' - no matching sent message found",
                            level=5,
                        )
                else:
                    # No message ID - could be an ACK we sent
                    # ACKs have message text like "ackXXXXX" and are sent to the original sender
                    print_debug(
                        f"parse_aprs_message: received our own message via digipeater (no ID, to={to_call}, msg='{message_text}', path={digipeater_path})",
                        level=5,
                    )

                    # Find matching ACK by to_call and message text
                    found = False
                    for sent_msg in self.messages:
                        if (
                            sent_msg.direction == "sent"
                            and sent_msg.message_id is None  # ACKs don't have message IDs
                            and sent_msg.to_call.upper() == to_call.upper()
                            and sent_msg.message == message_text
                            and not sent_msg.digipeated  # Don't re-mark if already digipeated
                        ):
                            sent_msg.digipeated = True
                            # ACKs are considered "acknowledged" once digipeated (no ACK for ACKs)
                            sent_msg.ack_received = True
                            found = True
                            print_debug(
                                f"parse_aprs_message: ✓ DIGIPEATED - marked ACK to {to_call} as digipeated (heard via {','.join(digipeater_path)})",
                                level=5,
                            )
                            break

                    if not found:
                        print_debug(
                            f"parse_aprs_message: Digipeated message to {to_call} (no ID) - no matching sent ACK found",
                            level=5,
                        )

                return None  # Don't add our own messages to the received list

            # Create message object
            msg = APRSMessage(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                from_call=from_call.upper(),
                to_call=to_call.upper(),
                message=message_text,
                message_id=message_id,
                read=False,
            )

            # Track station activity
            sender_station = self._get_or_create_station(
                from_call, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="message", timestamp=timestamp, frame_number=frame_number
            )
            sender_station.messages_sent += 1

            # Track receiver if it's us
            to_call_upper = to_call.upper()
            if self.is_message_for_me(to_call):
                # Message is to us - track as received by the sender
                sender_station.messages_received += 1

            # Always add to monitored messages (for monitoring all traffic)
            self.monitored_messages.append(msg)

            # Add to personal messages if addressed to us, ALL, or BSS callsign
            is_for_me = self.is_message_for_me(to_call)
            is_all = to_call_upper == "ALL"
            is_bss = to_call_upper.startswith("BSS")
            is_base = to_call_upper == self.my_callsign_base

            print_debug(
                f"parse_aprs_message: filtering - is_for_me={is_for_me}, is_all={is_all}, is_bss={is_bss}, is_base={is_base}",
                level=5,
            )

            if is_for_me or is_all or is_bss or is_base:
                # Check for duplicate before adding
                is_duplicate = False
                for existing_msg in self.messages:
                    # Check if duplicate: same sender + (same message_id OR same content OR fuzzy match)
                    if existing_msg.from_call == msg.from_call:
                        if (
                            message_id
                            and existing_msg.message_id == message_id
                        ):
                            # Same sender, same message ID = duplicate
                            is_duplicate = True
                            print_debug(
                                f"parse_aprs_message: duplicate detected (same message_id={message_id})",
                                level=5,
                            )
                            break
                        elif existing_msg.message == msg.message:
                            # Same sender, same content = duplicate (for messages without IDs)
                            is_duplicate = True
                            print_debug(
                                f"parse_aprs_message: duplicate detected (same content)",
                                level=5,
                            )
                            break
                        else:
                            # Fuzzy duplicate detection: catches corrupted iGate packets
                            # Check if message content is similar (one starts with the other)
                            # and within a time window (30 seconds)
                            time_diff = abs((msg.timestamp - existing_msg.timestamp).total_seconds())
                            min_match_len = 20  # Minimum characters to match

                            if time_diff < 30:  # Within 30 seconds
                                # Check if messages have enough content to compare
                                if len(existing_msg.message) >= min_match_len and len(msg.message) >= min_match_len:
                                    # Check if one message starts with the other (fuzzy match)
                                    if (existing_msg.message.startswith(msg.message[:min_match_len]) or
                                        msg.message.startswith(existing_msg.message[:min_match_len])):
                                        is_duplicate = True
                                        print_debug(
                                            f"parse_aprs_message: duplicate detected (fuzzy match, time_diff={time_diff:.1f}s)",
                                            level=5,
                                        )
                                        break

                if not is_duplicate:
                    self.messages.append(msg)
                    print_debug(
                        f"parse_aprs_message: added to personal messages (count={len(self.messages)})",
                        level=5,
                    )

                    # Broadcast message received to web clients
                    self._broadcast_update('message_received', msg)

                    return msg  # Return for notification
                else:
                    print_debug(
                        f"parse_aprs_message: skipped duplicate message",
                        level=5,
                    )
                    return None  # Don't notify for duplicates

            print_debug(
                f"parse_aprs_message: NOT added to personal messages (not for us)",
                level=5,
            )
            return None  # Don't notify for messages not to us

        except Exception:
            return None

    def parse_aprs_weather(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSWeather]:
        """Parse APRS weather data.

        Supports position-with-weather formats:
        - ! (position without timestamp)
        - @ (position with timestamp)
        - / (position with timestamp, no messaging)
        - _ (weather report without position)

        Args:
            station: Station callsign
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops
            digipeater_path: List of digipeater callsigns from AX.25 path

        Returns:
            APRSWeather if weather data found, None otherwise
        """
        if not info or info[0] not in ("!", "@", "/", "_"):
            return None

        try:
            wx = APRSWeather(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                raw_data=info,
            )

            # Check for weather data indicators
            has_weather = False

            # Look for weather fields (these are the typical indicators)
            # Wind: c...s... (direction/speed) or g... (gust)
            # Temp: t... (F)
            # Rain: r... (last hour), p... (last 24h), P... (since midnight)
            # Humidity: h... (00-99, 00 means 100%)
            # Pressure: b..... (tenths of mbar)

            # Simple pattern matching for common weather fields
            # Allow variable digit counts and negative signs for temperature (t-3, t003, etc.)
            if re.search(r"[csghpPb]\d{3}|t-?\d{1,3}|r\d{3}", info):
                has_weather = True

            if not has_weather:
                return None

            # Try to extract lat/lon (simplified - just check for valid position format)
            # Full parsing would require comprehensive APRS position decoding
            # For now, we'll extract what we can

            # Extract weather values using regex

            # Wind - two formats supported:
            # Format 1: _ddd/sss (underscore, direction/speed)
            match = re.search(r"_(\d{3})/(\d{3})", info)
            if match:
                wx.wind_direction = int(match.group(1))
                wx.wind_speed = int(match.group(2))
            else:
                # Format 2: cdddsddd (compact form)
                match = re.search(r"c(\d{3})s(\d{3})", info)
                if match:
                    wx.wind_direction = int(match.group(1))
                    wx.wind_speed = int(match.group(2))

            # Wind gust (g...) - mph
            match = re.search(r"g(\d{3})", info)
            if match:
                wx.wind_gust = int(match.group(1))

            # Temperature (t...) - Fahrenheit
            # Allow 1-3 digits with optional minus sign (e.g., t-3, t-03, t-003, t003)
            match = re.search(r"t(-?\d{1,3})", info)
            if match:
                temp = int(match.group(1))
                # Negative temps in standard APRS use two's complement (e.g., 253 = -3)
                # But some stations send explicit minus sign (e.g., -3)
                if temp > 200:
                    temp = temp - 256
                wx.temperature = temp

            # Rain last hour (r...) - hundredths of inches
            match = re.search(r"r(\d{3})", info)
            if match:
                wx.rain_1h = int(match.group(1)) / 100.0

            # Rain last 24h (p...) - hundredths of inches
            match = re.search(r"p(\d{3})", info)
            if match:
                wx.rain_24h = int(match.group(1)) / 100.0

            # Rain since midnight (P...) - hundredths of inches
            match = re.search(r"P(\d{3})", info)
            if match:
                wx.rain_since_midnight = int(match.group(1)) / 100.0

            # Humidity (h...) - percent (00 = 100%)
            match = re.search(r"h(\d{2})", info)
            if match:
                humidity = int(match.group(1))
                wx.humidity = 100 if humidity == 0 else humidity

            # Barometric pressure (b.....) - auto-detect format
            # Some stations use tenths of mb (b10130 = 1013.0 mb)
            # Others use hundredths of inHg (b02979 = 29.79 inHg)
            match = re.search(r"b(\d{5})", info)
            if match:
                raw_value = int(match.group(1))

                # Try as tenths of millibars first
                pressure_mb = raw_value / 10.0

                # Sanity check: valid atmospheric pressure is 900-1100 mb
                if 900 <= pressure_mb <= 1100:
                    # Valid as millibars, use directly
                    wx.pressure = pressure_mb
                else:
                    # Try as hundredths of inHg (US format)
                    pressure_inhg = raw_value / 100.0

                    # Sanity check: valid inHg range is 25-32 inHg
                    if 25 <= pressure_inhg <= 32:
                        # Valid as inHg, convert to millibars
                        wx.pressure = pressure_inhg * 33.8639
                    # else: invalid pressure, leave as None

            # Store latest weather for this station
            self.weather_reports[station.upper()] = wx

            # Track station activity
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="weather", timestamp=timestamp, frame_number=frame_number)
            sta.last_weather = wx
            self._add_weather_to_history(sta, wx)

            # Broadcast weather update to web clients
            self._broadcast_update('weather_update', sta)

            return wx

        except Exception:
            return None

    def _parse_compressed_position(
        self,
        info: str,
        offset: int,
        station: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        dest_addr: str = None,
        timestamp: datetime = None,
        frame_number: int = None,
    ) -> Optional[APRSPosition]:
        r"""Parse APRS compressed position format.

        Compressed format: /YYYYXXXX$csT
        - / or \ = symbol table (1 byte)
        - YYYY = compressed latitude (4 bytes, base-91)
        - XXXX = compressed longitude (4 bytes, base-91)
        - $ = symbol code (1 byte)
        - cs = compressed course/speed or other data (1-2 bytes)
        - T = compression type byte (1 byte)

        Args:
            info: APRS info field
            offset: Start offset of position data
            station: Station callsign
            relay_call: Optional relay station
            hop_count: Number of digipeater hops
            digipeater_path: List of digipeater callsigns
            dest_addr: Destination address

        Returns:
            APRSPosition if valid, None otherwise
        """
        try:
            if len(info) < offset + 13:  # Minimum: symbol_table + lat(4) + lon(4) + symbol + cs + T
                return None

            # Extract components
            symbol_table = info[offset]
            lat_compressed = info[offset + 1:offset + 5]
            lon_compressed = info[offset + 5:offset + 9]
            symbol_code = info[offset + 9]

            # Optional: compressed course/speed and type byte
            # We'll extract the comment starting after the compression type byte
            # The compression type byte is typically at offset+10, but we'll be lenient

            # Decode base-91 coordinates
            # Base-91 uses ASCII 33-124 (! to |)
            def base91_decode(s):
                """Decode 4-character base-91 string to integer."""
                result = 0
                for i, c in enumerate(s):
                    val = ord(c) - 33
                    if val < 0 or val > 90:
                        return None
                    result = result * 91 + val
                return result

            lat_val = base91_decode(lat_compressed)
            lon_val = base91_decode(lon_compressed)

            if lat_val is None or lon_val is None:
                return None

            # Convert to decimal degrees
            # Latitude: 90 - (lat_val / 380926)
            # Longitude: -180 + (lon_val / 190463)
            latitude = 90.0 - (lat_val / 380926.0)
            longitude = -180.0 + (lon_val / 190463.0)

            # Validate coordinates
            if latitude < -90 or latitude > 90 or longitude < -180 or longitude > 180:
                return None

            # Filter out Null Island
            if latitude == 0.0 and longitude == 0.0:
                return None

            # Extract comment (skip compression type byte and optional data)
            # Typically comment starts at offset+13
            comment = info[offset + 13:].strip() if len(info) > offset + 13 else ""

            # Convert to Maidenhead grid square
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Identify device type
            device_str = None
            if dest_addr:
                try:
                    device_id = get_device_identifier()
                    device_info = device_id.identify_by_tocall(dest_addr)
                    if device_info:
                        device_str = str(device_info)
                except Exception:
                    pass

            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
                device=device_str,
            )

            # Store latest position
            self.position_reports[station.upper()] = pos

            # Track station activity
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="position", timestamp=timestamp, frame_number=frame_number)
            sta.last_position = pos
            if device_str:
                sta.device = device_str
            self._add_position_to_history(sta, pos)

            # Broadcast update
            self._broadcast_update('station_update', sta)

            return pos

        except Exception as e:
            # Silently fail for invalid compressed data
            return None

    def parse_aprs_position(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        dest_addr: str = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSPosition]:
        """Parse APRS position report.

        Supports formats:
        - ! (position without timestamp)
        - @ (position with timestamp)
        - / (position with timestamp, no messaging)
        - = (position without timestamp, with messaging)

        Supports both uncompressed and compressed position formats.

        Args:
            station: Station callsign
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)
            hop_count: Number of digipeater hops
            digipeater_path: List of digipeater callsigns from AX.25 path
            dest_addr: Destination address (for device identification)

        Returns:
            APRSPosition if position data found, None otherwise
        """
        if not info or info[0] not in ("!", "@", "/", "="):
            return None

        try:
            # Skip timestamp if present (@ and / formats have 7-char timestamp)
            offset = 0
            if info[0] in ("@", "/"):
                offset = 8  # 1 (type) + 7 (timestamp)
            else:
                offset = 1  # Just skip type character

            # Check if this is compressed format
            # Compressed format: symbol_table(1) + lat(4) + lon(4) + symbol(1) + compressed_type(1) = 11 bytes minimum
            # Uncompressed format: lat(8) + symbol_table(1) + lon(9) + symbol(1) = 19 bytes minimum
            if len(info) >= offset + 13:  # Minimum for compressed
                # Check for compressed format: symbol table char followed by base-91 chars
                symbol_table_char = info[offset]
                # Compressed format uses symbol tables / or \, followed by base-91 encoded data
                if symbol_table_char in ('/', '\\') and len(info) >= offset + 13:
                    # Try to parse as compressed
                    result = self._parse_compressed_position(
                        info, offset, station, relay_call, hop_count,
                        digipeater_path, dest_addr,
                        timestamp=timestamp, frame_number=frame_number)
                    if result:
                        return result
                    # If compressed parsing failed, fall through to try uncompressed

            if len(info) < offset + 19:  # Need at least lat/lon/symbol for uncompressed
                return None

            # Parse position data
            # Format: DDMMmmN$DDDMMmmW# where $ is symbol table, # is symbol code
            # Example: 4210.45N/07153.00W> (/ is symbol table, > is symbol code)
            # Symbol table can be / \ or any printable ASCII character
            lat_str = info[offset : offset + 8]  # DDMMmmN or DDMMmmS
            lon_str = info[offset + 9 : offset + 18]  # DDDMMmmW or DDDMMmmE

            # Extract symbol table and code
            symbol_table = info[offset + 8] if offset + 8 < len(info) else "/"
            symbol_code = info[offset + 18] if offset + 18 < len(info) else ">"

            # Parse latitude (DDMMmmN/S format)
            try:
                lat_deg = int(lat_str[0:2])
                lat_min = float(lat_str[2:7])
                lat_dir = lat_str[7]
                latitude = lat_deg + (lat_min / 60.0)
                if lat_dir in ("S", "s"):
                    latitude = -latitude
            except (ValueError, IndexError):
                return None

            # Parse longitude (DDDMMmmW/E format)
            try:
                lon_deg = int(lon_str[0:3])
                lon_min = float(lon_str[3:8])
                lon_dir = lon_str[8]
                longitude = lon_deg + (lon_min / 60.0)
                if lon_dir in ("W", "w"):
                    longitude = -longitude
            except (ValueError, IndexError):
                return None

            # Extract comment (everything after symbol code)
            comment = (
                info[offset + 19 :].strip() if len(info) > offset + 19 else ""
            )

            # Convert to Maidenhead grid square
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Filter out invalid "Null Island" coordinates (0.0, 0.0)
            if latitude == 0.0 and longitude == 0.0:
                print_debug(
                    f"parse_position: Rejecting Null Island coordinates from {station}",
                    level=5,
                )
                return None

            # Identify device type from destination callsign (tocall)
            device_str = None
            if dest_addr:
                try:
                    device_id = get_device_identifier()
                    device_info = device_id.identify_by_tocall(dest_addr)
                    if device_info:
                        device_str = str(device_info)
                except Exception:
                    pass  # Silently ignore device ID errors

            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
                device=device_str,
            )

            # Store latest position for this station
            self.position_reports[station.upper()] = pos

            # Track station activity
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="position", timestamp=timestamp, frame_number=frame_number)
            sta.last_position = pos
            if device_str:
                sta.device = device_str
            self._add_position_to_history(sta, pos)

            # Broadcast station update to web clients
            self._broadcast_update('station_update', sta)

            return pos

        except Exception:
            return None

    def parse_aprs_object(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSPosition]:
        """Parse APRS object report.

        Format: ;OBJECTNAM*DDHHMMzLATITUDEsLONGITUDEsCOMMENT
        Object name is 9 characters (padded with spaces)
        Status is * (live) or _ (killed)

        Args:
            station: Station that sent the object
            info: APRS information field
            relay_call: Optional relay station (for third-party packets)

        Returns:
            APRSPosition if object contains position data, None otherwise
        """
        if not info or info[0] != ";":
            return None

        try:
            # Extract object name (9 chars) and status (* or _)
            if len(info) < 11:  # ; + 9-char name + *
                return None

            object_name = info[1:10].strip()  # 9-character object name
            status = info[10]  # * (live) or _ (killed)

            if status not in ("*", "_"):
                return None

            # Killed objects just announce removal, no position data needed
            if status == "_":
                return None

            # Parse timestamp (7 chars: DDHHMMz)
            if len(info) < 18:  # Need at least: ; + 9 + * + 7
                return None

            timestamp_str = info[11:18]  # DDHHMMz format
            offset = 18  # Start of position data

            if len(info) < offset + 19:  # Need at least lat/lon/symbol
                return None

            # Parse position data (same format as regular position reports)
            lat_str = info[offset : offset + 8]  # DDMMmmN or DDMMmmS
            lon_str = info[offset + 9 : offset + 18]  # DDDMMmmW or DDDMMmmE

            # Extract symbol table and code
            symbol_table = info[offset + 8] if offset + 8 < len(info) else "/"
            symbol_code = info[offset + 18] if offset + 18 < len(info) else ">"

            # Parse latitude (DDMMmmN/S format)
            try:
                lat_deg = int(lat_str[0:2])
                lat_min = float(lat_str[2:7])
                lat_dir = lat_str[7]
                latitude = lat_deg + (lat_min / 60.0)
                if lat_dir in ("S", "s"):
                    latitude = -latitude
            except (ValueError, IndexError):
                return None

            # Parse longitude (DDDMMmmW/E format)
            try:
                lon_deg = int(lon_str[0:3])
                lon_min = float(lon_str[3:8])
                lon_dir = lon_str[8]
                longitude = lon_deg + (lon_min / 60.0)
                if lon_dir in ("W", "w"):
                    longitude = -longitude
            except (ValueError, IndexError):
                return None

            # Extract comment (everything after symbol code)
            comment = (
                info[offset + 19 :].strip() if len(info) > offset + 19 else ""
            )

            # Convert to Maidenhead grid square
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Create position object using the OBJECT name as the station
            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=object_name.upper(),  # Use object name, not sender
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
            )

            # Store latest position for this object
            self.position_reports[object_name.upper()] = pos

            # Track as station (objects are tracked like stations)
            sta = self._get_or_create_station(
                object_name, relay_call, hop_count, packet_type="object", timestamp=timestamp, frame_number=frame_number
            )
            sta.last_position = pos
            self._add_position_to_history(sta, pos)

            # Broadcast station update to web clients
            self._broadcast_update('station_update', sta)

            return pos

        except Exception:
            return None

    def parse_aprs_status(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSStatus]:
        """Parse APRS status report.

        Status format: >Status text here

        Args:
            station: Station callsign
            info: APRS info field
            relay_call: Optional relay station callsign

        Returns:
            APRSStatus object if valid, None otherwise
        """
        try:
            if not info or info[0] != ">":
                return None

            # Extract status text (everything after >)
            status_text = info[1:].strip()

            if not status_text:
                return None

            # Create status object
            status = APRSStatus(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                status_text=status_text,
            )

            # Track as station
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="status", timestamp=timestamp, frame_number=frame_number)
            sta.last_status = status

            return status

        except Exception:
            return None

    def parse_aprs_item(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSPosition]:
        """Parse APRS item report.

        Item format: )NAME!lat/lonSymbol...
        Items are like objects but with 3-9 character names (not fixed at 9)

        Args:
            station: Station callsign that placed the item
            info: APRS info field
            relay_call: Optional relay station callsign

        Returns:
            APRSPosition object if valid, None otherwise
        """
        try:
            if not info or info[0] != ")":
                return None

            # Find the position marker (! or _)
            pos_marker_idx = -1
            for i, c in enumerate(info[1:], 1):
                if c in ("!", "_"):
                    pos_marker_idx = i
                    break

            if pos_marker_idx == -1:
                return None

            # Extract item name (3-9 chars between ) and !)
            item_name = info[1:pos_marker_idx].strip()
            if not item_name or len(item_name) < 3 or len(item_name) > 9:
                return None

            # Parse position (same format as standard position)
            # Position starts after the marker
            offset = pos_marker_idx + 1

            # Need at least lat (8) + symbol table (1) + lon (9) + symbol code (1) = 19 chars
            if len(info) < offset + 19:
                return None

            # Parse latitude: DDMM.MMN (8 chars)
            lat_str = info[offset : offset + 8]
            symbol_table = info[offset + 8]
            lon_str = info[offset + 9 : offset + 18]
            symbol_code = info[offset + 18]

            # Convert lat/lon
            try:
                lat_deg = int(lat_str[0:2])
                lat_min = float(lat_str[2:7])
                lat_dir = lat_str[7]
                latitude = lat_deg + (lat_min / 60.0)
                if lat_dir in ("S", "s"):
                    latitude = -latitude

                lon_deg = int(lon_str[0:3])
                lon_min = float(lon_str[3:8])
                lon_dir = lon_str[8]
                longitude = lon_deg + (lon_min / 60.0)
                if lon_dir in ("W", "w"):
                    longitude = -longitude
            except (ValueError, IndexError):
                return None

            # Extract comment (everything after symbol code)
            comment = (
                info[offset + 19 :].strip() if len(info) > offset + 19 else ""
            )

            # Convert to Maidenhead grid square
            grid_square = self.latlon_to_maidenhead(latitude, longitude)

            # Create position object using the ITEM name as the station
            pos = APRSPosition(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=item_name.upper(),  # Use item name, not sender
                latitude=latitude,
                longitude=longitude,
                symbol_table=symbol_table,
                symbol_code=symbol_code,
                comment=comment,
                grid_square=grid_square,
            )

            # Store latest position for this item
            self.position_reports[item_name.upper()] = pos

            # Track as station (items are tracked like stations)
            sta = self._get_or_create_station(item_name, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="item", timestamp=timestamp, frame_number=frame_number)
            sta.last_position = pos

            return pos

        except Exception:
            return None

    def parse_aprs_telemetry(
        self,
        station: str,
        info: str,
        relay_call: str = None,
        hop_count: int = 999,
        digipeater_path: List[str] = None,
        timestamp: datetime = None,
        frame_number: int = None
    ) -> Optional[APRSTelemetry]:
        """Parse APRS telemetry packet.

        Telemetry format: T#SSS,A1,A2,A3,A4,A5,BBBBBBBB
        SSS = sequence number (000-999)
        A1-A5 = analog values (000-255)
        BBBBBBBB = 8 digital bits (0/1)

        Args:
            station: Station callsign
            info: APRS info field
            relay_call: Optional relay station callsign

        Returns:
            APRSTelemetry object if valid, None otherwise
        """
        try:
            if not info or not info.startswith("T#"):
                return None

            # Remove T# prefix
            data = info[2:].strip()

            # Split by comma
            parts = data.split(",")

            # Need exactly 7 parts: sequence + 5 analog + digital
            if len(parts) != 7:
                return None

            # Parse sequence number
            try:
                sequence = int(parts[0])
                if sequence < 0 or sequence > 999:
                    return None
            except ValueError:
                return None

            # Parse analog values (5 channels)
            analog = []
            for i in range(1, 6):
                try:
                    val = int(parts[i])
                    if val < 0 or val > 255:
                        return None
                    analog.append(val)
                except ValueError:
                    return None

            # Parse digital bits (8 bits)
            digital = parts[6].strip()
            if len(digital) != 8 or not all(c in "01" for c in digital):
                return None

            # Create telemetry object
            telemetry = APRSTelemetry(
                timestamp=ensure_utc_aware(timestamp) if timestamp else datetime.now(timezone.utc),
                station=station.upper(),
                sequence=sequence,
                analog=analog,
                digital=digital,
            )

            # Track as station
            sta = self._get_or_create_station(station, relay_call, hop_count, digipeater_path=digipeater_path, packet_type="telemetry", timestamp=timestamp, frame_number=frame_number)
            sta.last_telemetry = telemetry

            # Keep recent telemetry history (last 20 packets)
            sta.telemetry_sequence.append(telemetry)
            if len(sta.telemetry_sequence) > 20:
                sta.telemetry_sequence.pop(0)

            return telemetry

        except Exception:
            return None
