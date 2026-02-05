"""Thin Radio adapter for aprslib integration.

This module provides a standalone `RadioController` used by the
`src` package.
"""

import asyncio
from datetime import datetime
from src.utils import print_debug, print_error, print_warning
from src.constants import *
from src.aprs.geo_utils import maidenhead_to_latlon
from src.protocol import (
    wrap_kiss,
    build_message,
    parse_message,
    decode_ht_status,
    decode_settings,
    encode_settings,
    encode_bss_settings,
    decode_bss_settings,
    decode_channel,
    encode_channel,
    encode_aprs_packet,
)


class RadioController:
    """Radio controller wrapper used by `src` modules."""

    def __init__(self, transport, rx_queue, tnc_queue):
        """Initialize RadioController with a transport layer.

        Args:
            transport: TransportBase instance (BLETransport or SerialTransport)
            rx_queue: Queue for radio command responses
            tnc_queue: Queue for TNC data
        """
        self.transport = transport
        # For BLE mode, client is the BleakClient; for serial mode, it's None
        self.client = getattr(transport, 'client', None)
        self.rx_queue = rx_queue
        self.tnc_queue = tnc_queue
        self.running = True
        self.tnc_bridge = None
        self.last_tnc_packet = datetime.now()
        self.last_heartbeat = datetime.now()
        self.tnc_packet_count = 0
        self.heartbeat_failures = 0
        # KISS callback (AX25Adapter registers here)
        self._kiss_callback = None
        # Flag to disable tnc_monitor display when in TNC mode (AX25Adapter handles it)
        self.tnc_mode_active = False
        # Carrier sense - is_in_rx status (True = channel busy, receiving data)
        # Uses is_in_rx (data carrier detect) instead of is_sq (squelch)
        # This works even with squelch wide open!
        self.channel_busy = False
        self.last_status_check = 0.0

    async def send_command(self, command_id, body=b"", timeout=2.0):
        """Send radio-specific command (BLE only).

        Returns None gracefully in serial mode.
        """
        # Check if BLE client is available (not in serial mode)
        if not self.client:
            print_debug("Radio commands not available in serial mode", level=6)
            return None

        msg = build_message(CMD_GROUP_BASIC, False, command_id, body)
        try:
            await self.client.write_gatt_char(
                RADIO_WRITE_UUID, msg, response=True
            )
        except Exception as e:
            print_error(f"Failed to write to radio: {e}")
            return None

        try:
            resp = await asyncio.wait_for(self.rx_queue.get(), timeout=timeout)
            result = parse_message(resp)
            if result and len(result["body"]) > 0:
                if result["body"][0] == 0:
                    return result["body"]
            return None
        except asyncio.TimeoutError:
            return None

    def update_tnc_activity(self):
        self.last_tnc_packet = datetime.now()
        self.tnc_packet_count += 1

    def get_tnc_idle_time(self):
        return (datetime.now() - self.last_tnc_packet).total_seconds()

    async def check_connection_health(self):
        """Check connection health (BLE only).

        Returns True in serial mode (always healthy).
        """
        try:
            # Serial mode: simple connected check
            if not self.client:
                return self.transport.is_connected()

            # BLE mode: full health check
            if not self.client.is_connected:
                print_warning("BLE connection lost!")
                return False

            status = await self.get_status()
            if status is None:
                self.heartbeat_failures += 1
                print_warning(f"Heartbeat failed ({self.heartbeat_failures})")
                if self.heartbeat_failures >= 3:
                    print_error(
                        "Multiple heartbeat failures - connection may be dead"
                    )
                    return False
            else:
                self.heartbeat_failures = 0
                print_debug(
                    f"Heartbeat OK - RSSI: {status.get('rssi', 0)}/15", level=6
                )

            self.last_heartbeat = datetime.now()
            return True
        except Exception as e:
            print_error(f"Health check failed: {e}")
            return False

    async def get_status(self):
        resp = await self.send_command(CMD_GET_HT_STATUS)
        if resp:
            status = decode_ht_status(resp)
            if status:
                # Track RX status for carrier sense (works even with squelch open!)
                # is_in_rx = True means radio is actively receiving/demodulating AFSK data
                # This is like DCD (Data Carrier Detect) - detects actual packet tones
                self.channel_busy = status.get("is_in_rx", False)
                self.last_status_check = asyncio.get_event_loop().time()
            return status
        return None

    async def is_channel_busy(self, max_age=0.5):
        """Check if channel is busy (radio receiving data = carrier detected).

        Uses is_in_rx status bit which detects actual AFSK data demodulation,
        similar to DCD (Data Carrier Detect). Works independently of squelch
        setting, so you can keep squelch open to monitor traffic while still
        detecting when the channel is busy with packet data.

        Args:
            max_age: Maximum age of cached status in seconds. If status is older,
                    a fresh status check is performed.

        Returns:
            True if channel is busy (actively receiving packet data), False if clear
        """
        now = asyncio.get_event_loop().time()

        # If status is stale, refresh it
        if (now - self.last_status_check) > max_age:
            status = await self.get_status()
            if status is None:
                # If we can't get status, assume channel is busy (conservative approach)
                return True

        return self.channel_busy

    async def get_settings(self):
        resp = await self.send_command(CMD_READ_SETTINGS)
        if resp:
            return decode_settings(resp)
        return None

    async def write_settings(self, settings):
        settings_bytes = encode_settings(settings)
        if settings_bytes is None:
            return False
        resp = await self.send_command(CMD_WRITE_SETTINGS, settings_bytes)
        return resp is not None

    async def get_bss_settings(self):
        print_debug(
            "get_bss_settings: sending READ_BSS_SETTINGS command...", level=6
        )
        resp = await self.send_command(CMD_READ_BSS_SETTINGS)
        if resp:
            print_debug(
                f"get_bss_settings: received response of {len(resp)} bytes",
                level=6,
            )
            return decode_bss_settings(resp)
        else:
            print_error("get_bss_settings: no response from radio")
        return None

    async def write_bss_settings(self, bss):
        bss_bytes = encode_bss_settings(bss)
        resp = await self.send_command(CMD_WRITE_BSS_SETTINGS, bss_bytes)
        return resp is not None

    async def read_channel(self, channel_id):
        internal_id = channel_id - 1
        resp = await self.send_command(CMD_READ_RF_CH, bytes([internal_id]))
        if resp:
            return decode_channel(resp)
        return None

    async def write_channel(self, channel_data):
        channel_bytes = encode_channel(channel_data)
        resp = await self.send_command(CMD_WRITE_RF_CH, channel_bytes)
        return resp is not None

    async def set_channel_power(self, channel_id, power_level):
        """Set transmit power level for a specific channel (High/Med/Low)."""
        channel = await self.read_channel(channel_id)
        if not channel:
            return False
        flags1 = channel["flags1"]
        flags1 &= ~0x42
        if power_level.lower() in ["high", "max"]:
            flags1 |= 0x40
        elif power_level.lower() in ["med", "medium"]:
            flags1 |= 0x02
        channel["flags1"] = flags1
        return await self.write_channel(channel)

    async def set_vfo(self, vfo, channel_id):
        settings = await self.get_settings()
        if not settings:
            print_error("Failed to read current settings")
            return False
        if vfo.lower() == "a":
            print_debug(
                f"Changing VFO A from {settings['channel_a']} to {channel_id}",
                level=6,
            )
            settings["channel_a"] = channel_id
        elif vfo.lower() == "b":
            print_debug(
                f"Changing VFO B from {settings['channel_b']} to {channel_id}",
                level=6,
            )
            settings["channel_b"] = channel_id
        else:
            return False
        return await self.write_settings(settings)

    async def set_active_vfo(self, vfo):
        settings = await self.get_settings()
        if not settings:
            print_error("Failed to read current settings")
            return False
        v = vfo.lower()
        if v == "a":
            settings["double_channel"] = 1
        elif v == "b":
            settings["double_channel"] = 2
        else:
            return False
        return await self.write_settings(settings)

    async def set_dual_watch(self, mode):
        settings = await self.get_settings()
        if not settings:
            return False
        settings["double_channel"] = mode
        return await self.write_settings(settings)

    async def set_scan(self, enabled):
        settings = await self.get_settings()
        if not settings:
            return False
        settings["scan"] = enabled
        return await self.write_settings(settings)

    async def set_squelch(self, level):
        settings = await self.get_settings()
        if not settings:
            return False
        settings["squelch_level"] = level & 0x0F
        return await self.write_settings(settings)

    async def get_volume(self):
        resp = await self.send_command(CMD_GET_VOLUME)
        if resp and len(resp) > 1:
            return resp[1]
        return None

    async def set_volume(self, level):
        if level < 0 or level > 15:
            return False
        resp = await self.send_command(CMD_SET_VOLUME, bytes([level]))
        return resp is not None

    async def get_gps_position(self):
        """Get current GPS position from radio (BLE only).

        Returns:
            Dict with GPS data or None if unavailable:
            {
                'latitude': float,  # Decimal degrees
                'longitude': float,  # Decimal degrees
                'altitude': int or None,  # Meters
                'speed': int or None,  # km/h
                'heading': int or None,  # Degrees
                'timestamp': int,  # Unix timestamp
                'accuracy': int,  # Meters
                'locked': bool  # GPS lock status
            }

        Note: Returns None in serial mode (not supported).
        """
        # GPS only available in BLE mode
        if not self.client:
            return None

        resp = await self.send_command(CMD_GET_POSITION)
        if not resp:
            print_debug("get_gps_position: No response from GPS", level=5)
            return None

        if len(resp) < 8:
            print_debug(f"get_gps_position: Response too short ({len(resp)} bytes)", level=5)
            return None

        # Debug: Show raw response
        print_debug(f"GET_POSITION response ({len(resp)} bytes): {resp.hex()}", level=5)

        try:
            # Status byte (0 = SUCCESS)
            if resp[0] != 0:
                print_debug(f"get_gps_position: Error status {resp[0]}", level=5)
                return None

            # Parse 24-bit latitude (bytes 1-3, signed, two's complement)
            lat_raw = (resp[1] << 16) | (resp[2] << 8) | resp[3]
            if lat_raw >= (1 << 23):  # Check sign bit
                lat_raw -= (1 << 24)  # Convert from two's complement
            latitude = lat_raw / 30000.0

            # Parse 24-bit longitude (bytes 4-6, signed, two's complement)
            lon_raw = (resp[4] << 16) | (resp[5] << 8) | resp[6]
            if lon_raw >= (1 << 23):  # Check sign bit
                lon_raw -= (1 << 24)  # Convert from two's complement
            longitude = lon_raw / 30000.0

            result = {
                'latitude': latitude,
                'longitude': longitude,
                'altitude': None,
                'speed': None,
                'heading': None,
                'timestamp': 0,
                'accuracy': 0,
                'locked': True  # Assume locked if we got valid data
            }

            # Parse optional fields (firmware 0.8.5+)
            if len(resp) >= 9:
                # Altitude (bytes 7-8, signed 16-bit, -32768 = None)
                altitude = (resp[7] << 8) | resp[8]
                if altitude != 0x8000:  # Not -32768
                    if altitude >= 0x8000:
                        altitude -= 0x10000
                    result['altitude'] = altitude

            if len(resp) >= 11:
                # Speed (bytes 9-10, unsigned 16-bit, 0xFFFF = None)
                speed = (resp[9] << 8) | resp[10]
                if speed != 0xFFFF:
                    result['speed'] = speed

            if len(resp) >= 13:
                # Heading (bytes 11-12, unsigned 16-bit, 0xFFFF = None)
                heading = (resp[11] << 8) | resp[12]
                if heading != 0xFFFF:
                    result['heading'] = heading

            if len(resp) >= 17:
                # Timestamp (bytes 13-16, unsigned 32-bit)
                timestamp = (resp[13] << 24) | (resp[14] << 16) | (resp[15] << 8) | resp[16]
                result['timestamp'] = timestamp

            if len(resp) >= 19:
                # Accuracy (bytes 17-18, unsigned 16-bit)
                accuracy = (resp[17] << 8) | resp[18]
                result['accuracy'] = accuracy

            return result

        except Exception as e:
            print_error(f"Failed to parse GPS position: {e}")
            return None

    async def check_gps_lock(self):
        """Check if GPS has a lock.

        Note: GET_HT_STATUS (command 20) doesn't work on all radio firmware versions.
        This method always returns True as a fallback - actual lock status is determined
        by whether get_gps_position() returns valid data.

        Returns:
            bool: True (always, since lock check command doesn't work)
        """
        # Try GET_HT_STATUS but don't fail if it doesn't work
        try:
            resp = await self.send_command(CMD_GET_HT_STATUS, timeout=1.0)
            if resp and len(resp) >= 7:
                # Debug: Show raw response if we get one
                print_debug(f"GET_HT_STATUS response ({len(resp)} bytes): {resp.hex()}", level=5)
                print_debug(f"  Byte 6: 0x{resp[6]:02x} (bit 3: {bool(resp[6] & 0x08)})", level=5)

                # GPS lock is supposedly at byte 6, bit 3
                gps_locked = (resp[6] & 0x08) != 0
                return gps_locked
        except Exception as e:
            print_debug(f"check_gps_lock: GET_HT_STATUS failed: {e}", level=5)

        # Fallback: Command doesn't work on this firmware, assume locked
        # Real lock status will be determined by get_gps_position() success
        print_debug("check_gps_lock: GET_HT_STATUS not supported, returning True", level=6)
        return True

    def get_position_with_fallback(self, tnc_config=None):
        """Get current position from GPS or manual location (MYLOCATION).

        Args:
            tnc_config: Optional TNCConfig instance to read MYLOCATION from

        Returns:
            Tuple of (latitude, longitude, source) or (None, None, None) if unavailable
            source is 'GPS', 'Grid <gridsquare>', or None
        """
        # Try GPS first (if available)
        if hasattr(self, 'gps_position') and self.gps_position:
            return (
                self.gps_position['latitude'],
                self.gps_position['longitude'],
                'GPS'
            )

        # Fall back to manual location
        if tnc_config:
            mylocation = tnc_config.get("MYLOCATION")
            if mylocation:
                try:
                    lat, lon = maidenhead_to_latlon(mylocation)
                    return (lat, lon, f"Grid {mylocation.upper()}")
                except ValueError:
                    pass

        return (None, None, None)

    async def write_kiss_frame(
        self, data: bytes, response: bool = True
    ) -> bool:
        """Write KISS frame to TNC (transport-agnostic).

        Args:
            data: KISS frame bytes to send
            response: Use write-with-response (True) for BLE,
                     ignored in serial mode.
                     Default True to ensure frames transmit immediately over the air.
        """
        try:
            # Capture frame for history before transmission
            if hasattr(self, "cmd_processor") and self.cmd_processor:
                self.cmd_processor.frame_history.add_frame("TX", data)

            # Delegate to transport layer
            result = await self.transport.write_kiss_frame(data, response=response)

            print_debug(
                f"write_kiss_frame: wrote {len(data)} bytes (response={response})",
                level=6,
            )
            return result
        except Exception as e:
            print_error(f"write_kiss_frame failed: {e}")
            return False

    async def send_tnc_data(self, data):
        """Send raw TNC data (transport-agnostic)."""
        try:
            # Delegate to transport layer
            await self.transport.send_tnc_data(data)
            print_debug(
                f"Sent {len(data)} bytes to TNC: {data.hex()}",
                level=6,
            )
        except Exception as e:
            print_error(f"Failed to send TNC data: {e}")

    async def set_hardware_power(self, power_on):
        """Hardware power control - turn radio ON/OFF using command 21."""
        power_byte = 0x01 if power_on else 0x00
        resp = await self.send_command(CMD_SET_HT_POWER, bytes([power_byte]))
        return resp is not None

    async def send_aprs(self, from_call, message, to_call="APRS", path=None):
        if path is None:
            path = ["WIDE1-1", "WIDE2-1"]

        print_debug(
            f"send_aprs: from={from_call}, to={to_call}, path={path}", level=5
        )
        print_debug(f"send_aprs: message='{message}'", level=5)

        packet = encode_aprs_packet(from_call, to_call, path, message)

        print_debug(
            f"TX KISS frame ({len(packet)} bytes): {packet.hex()}", level=4
        )
        # Decode the packet to show what will be transmitted
        try:
            from src.protocol import (
                kiss_unwrap,
                parse_ax25_addresses_and_control,
            )

            unwrapped = kiss_unwrap(packet)
            addresses, control, offset = parse_ax25_addresses_and_control(
                unwrapped
            )
            info_field = (
                unwrapped[offset + 2 :] if offset + 2 < len(unwrapped) else b""
            )
            path_str = ",".join(addresses[2:]) if len(addresses) > 2 else ""
            full_path = f"{addresses[1]}>{addresses[0]}"
            if path_str:
                full_path += f",{path_str}"
            print_debug(
                f"TX decoded: {full_path}: {info_field.decode('ascii', errors='replace')}",
                level=5,
            )
        except Exception as e:
            print_debug(f"TX decode error: {e}", level=5)

        await self.send_tnc_data(packet)

    def register_kiss_callback(self, cb):
        self._kiss_callback = cb


__all__ = ["RadioController"]
