"""
AGWPE-compatible TCP bridge for amateur radio packet applications.

Implements the AGWPE (SV2AGW Packet Engine) TCP/IP protocol as used by
Direwolf, YAAC, Outpost, and other packet applications.

Supports both UI (unconnected) and connected mode AX.25 operations.

Protocol reference: http://www.on7lds.net/42/sites/default/files/AGWPEAPI.HTM
Based on Direwolf implementation: https://github.com/wb2osz/direwolf
"""

import asyncio
import struct
import sys
from typing import List, Optional, Dict, Tuple
from datetime import datetime

from src.utils import print_info, print_debug, print_error
from src.protocol import kiss_unwrap

AGWPE_PORT = 8000

# AGWPE protocol constants
AGWPE_HEADER_SIZE = 36
MAX_INFO_LEN = 2048  # Maximum info field length


class AGWPEFrame:
    """AGWPE protocol frame structure (36-byte header + variable data)."""

    def __init__(self):
        self.portx: int = 0  # Port number (0-based)
        self.datakind: str = ""  # Frame type (single letter)
        self.pid: int = 0xF0  # Protocol ID
        self.call_from: str = ""  # Source callsign (10 bytes max)
        self.call_to: str = ""  # Destination callsign (10 bytes max)
        self.data: bytes = b""  # Variable-length data

    def pack(self) -> bytes:
        """Pack frame into 36-byte header + data for transmission."""
        # Build header - exactly 36 bytes
        header = bytearray(36)

        # Bytes 0-3: Port, Reserved, Reserved, Reserved
        header[0] = self.portx & 0xFF
        header[1] = 0
        header[2] = 0
        header[3] = 0

        # Bytes 4-7: DataKind, Reserved, PID, Reserved
        header[4] = ord(self.datakind[0]) if self.datakind else 0
        header[5] = 0
        header[6] = self.pid & 0xFF
        header[7] = 0

        # Bytes 8-17: CallFrom (10 bytes, null-padded)
        call_from_bytes = self.call_from.encode("ascii")[:10]
        header[8 : 8 + len(call_from_bytes)] = call_from_bytes

        # Bytes 18-27: CallTo (10 bytes, null-padded)
        call_to_bytes = self.call_to.encode("ascii")[:10]
        header[18 : 18 + len(call_to_bytes)] = call_to_bytes

        # Bytes 28-31: DataLen (32-bit little-endian)
        data_len = len(self.data)
        struct.pack_into("<I", header, 28, data_len)

        # Bytes 32-35: User reserved (32-bit little-endian)
        struct.pack_into("<I", header, 32, 0)

        return bytes(header) + self.data

    @classmethod
    def unpack(cls, header: bytes) -> Optional["AGWPEFrame"]:
        """Unpack 36-byte header into AGWPEFrame object."""
        if len(header) < 36:
            return None

        frame = cls()
        frame.portx = header[0]
        frame.datakind = chr(header[4]) if header[4] != 0 else ""
        frame.pid = header[6]

        # Extract callsigns (null-terminated)
        call_from_bytes = header[8:18]
        null_pos = call_from_bytes.find(b"\x00")
        if null_pos >= 0:
            call_from_bytes = call_from_bytes[:null_pos]
        frame.call_from = call_from_bytes.decode("ascii", errors="ignore")

        call_to_bytes = header[18:28]
        null_pos = call_to_bytes.find(b"\x00")
        if null_pos >= 0:
            call_to_bytes = call_to_bytes[:null_pos]
        frame.call_to = call_to_bytes.decode("ascii", errors="ignore")

        # Extract data length
        data_len = struct.unpack_from("<I", header, 28)[0]

        return frame, data_len


class AGWPEClient:
    """Represents a connected AGWPE client."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        bridge: "AGWPEBridge",
    ):
        self.reader = reader
        self.writer = writer
        self.bridge = bridge
        self.addr = writer.get_extra_info("peername")

        # Client state
        self.mon_enabled = False  # Monitor mode enabled
        self.raw_enabled = False  # Raw frame mode enabled
        self.registered_calls = []  # Registered callsigns for connected mode

        # Connected mode state - tracks active AX.25 connections
        # Key: (call_from, call_to), Value: connection state
        self.connections: Dict[Tuple[str, str], dict] = {}

    async def send_frame(self, frame: AGWPEFrame):
        """Send AGWPE frame to client."""
        try:
            data = frame.pack()
            self.writer.write(data)
            await self.writer.drain()
            print_debug(
                f"AGWPE -> {self.addr}: {frame.datakind} port={frame.portx} len={len(frame.data)}",
                level=4,
            )
        except Exception as e:
            print_error(f"AGWPE: failed to send to {self.addr}: {e}")

    async def close(self):
        """Close client connection."""
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


class AGWPEBridge:
    """AGWPE protocol server implementation with connected mode support."""

    def __init__(
        self, radio, get_mycall=None, get_txdelay=None, ax25_adapter=None
    ):
        self.radio = radio
        self.server = None
        self.clients: List[AGWPEClient] = []

        # Port configuration
        self.num_ports = 1
        self.port_names = ["UV50PRO-TNC"]
        self.version_string = "UV50PRO AGWPE 2006.127"

        # AX.25 adapter for connected mode
        # Use provided adapter if available (shares with Console), otherwise create new one
        if ax25_adapter:
            self.ax25 = ax25_adapter
            print_debug("AGWPE: Using shared AX25Adapter instance", level=4)
        else:
            from src.ax25_adapter import AX25Adapter

            self.ax25 = AX25Adapter(
                radio, get_mycall=get_mycall, get_txdelay=get_txdelay
            )
            print_debug("AGWPE: Created new AX25Adapter instance", level=4)

        self.ax25.register_callback(self._handle_ax25_incoming)

        # Track active connection: (call_from, call_to) -> client
        self.active_connection: Optional[Tuple[str, str]] = None
        self.connection_owner: Optional[AGWPEClient] = None

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Handle incoming client connection."""
        client = AGWPEClient(reader, writer, self)
        self.clients.append(client)
        print_info(f"AGWPE: Client connected from {client.addr}")

        try:
            while True:
                # Read 36-byte header
                header_data = await reader.readexactly(AGWPE_HEADER_SIZE)
                frame, data_len = AGWPEFrame.unpack(header_data)

                if frame is None:
                    print_error(f"AGWPE: Invalid header from {client.addr}")
                    break

                # Read variable-length data if present
                if data_len > 0:
                    if data_len > MAX_INFO_LEN:
                        print_error(
                            f"AGWPE: Data length {data_len} exceeds maximum from {client.addr}"
                        )
                        break
                    frame.data = await reader.readexactly(data_len)

                # Process the frame
                await self._process_frame(client, frame)

        except asyncio.IncompleteReadError:
            print_debug(f"AGWPE: Client {client.addr} disconnected", level=4)
        except Exception as e:
            print_error(f"AGWPE: Error handling client {client.addr}: {e}")
        finally:
            # Cleanup any connections owned by this client
            if self.connection_owner == client and self.active_connection:
                try:
                    print_info(
                        f"AGWPE: Cleaning up connection {self.active_connection} (client disconnected)"
                    )
                    await self.ax25.disconnect()
                    self.active_connection = None
                    self.connection_owner = None
                except Exception as e:
                    print_debug(
                        f"AGWPE: Error cleaning up connection: {e}", level=4
                    )

            try:
                await client.close()
            except Exception:
                pass
            try:
                self.clients.remove(client)
            except ValueError:
                pass
            print_info(f"AGWPE: Client {client.addr} disconnected")

    async def _process_frame(self, client: AGWPEClient, frame: AGWPEFrame):
        """Process incoming AGWPE frame from client."""
        dk = frame.datakind

        print_debug(
            f"AGWPE <- {client.addr}: {dk} port={frame.portx} from={frame.call_from} to={frame.call_to} len={len(frame.data)}",
            level=4,
        )

        # Version query
        if dk == "R":
            await self._send_version(client)

        # Port information query
        elif dk == "G":
            await self._send_port_info(client)

        # Port capabilities query
        elif dk == "g":
            await self._send_port_capabilities(client, frame.portx)

        # Enable monitoring
        elif dk == "m":
            client.mon_enabled = True
            print_debug(
                f"AGWPE: Monitoring enabled for {client.addr}", level=4
            )

        # Enable raw frames
        elif dk == "k":
            client.raw_enabled = True
            print_debug(
                f"AGWPE: Raw frames enabled for {client.addr}", level=4
            )

        # Register callsign
        elif dk == "X":
            if (
                frame.call_from
                and frame.call_from not in client.registered_calls
            ):
                client.registered_calls.append(frame.call_from)
                print_info(
                    f"AGWPE: Registered {frame.call_from} for {client.addr}"
                )

        # Unregister callsign
        elif dk == "x":
            if frame.call_from in client.registered_calls:
                client.registered_calls.remove(frame.call_from)
                print_info(
                    f"AGWPE: Unregistered {frame.call_from} for {client.addr}"
                )

        # Send unproto (UI) frame
        elif dk == "M":
            await self._handle_send_unproto(client, frame)

        # Send unproto with via path
        elif dk == "V":
            await self._handle_send_unproto_via(client, frame)

        # Send raw AX.25 frame
        elif dk == "K":
            await self._handle_send_raw(client, frame)

        # Outstanding frames query (connected mode) - lowercase 'y' and uppercase 'Y'
        elif dk == "y" or dk == "Y":
            # Return number of outstanding frames in TX queue
            # Check both directions due to client quirks
            count = 0
            if self.active_connection == (
                frame.call_from,
                frame.call_to,
            ) or self.active_connection == (frame.call_to, frame.call_from):
                count = (
                    len(self.ax25._tx_queue)
                    if hasattr(self.ax25, "_tx_queue")
                    else 0
                )
            resp = AGWPEFrame()
            resp.portx = frame.portx
            resp.datakind = dk  # Echo back the same case
            resp.call_from = frame.call_from
            resp.call_to = frame.call_to
            resp.data = struct.pack("<I", count)
            await client.send_frame(resp)

        # Connect to remote station
        elif dk == "C":
            await self._handle_connect(client, frame, [])

        # Connect with via path
        elif dk == "v":
            await self._handle_connect_via(client, frame)

        # Connect with custom PID
        elif dk == "c":
            await self._handle_connect(client, frame, [], custom_pid=frame.pid)

        # Send connected data
        elif dk == "D":
            await self._handle_send_connected_data(client, frame)

        # Disconnect
        elif dk == "d":
            await self._handle_disconnect(client, frame)

        else:
            print_debug(
                f"AGWPE: Unknown datakind '{dk}' from {client.addr}", level=4
            )

    async def _send_version(self, client: AGWPEClient):
        """Send version information (R frame)."""
        resp = AGWPEFrame()
        resp.datakind = "R"
        resp.data = (self.version_string + "\r\n").encode("ascii")
        await client.send_frame(resp)

    async def _send_port_info(self, client: AGWPEClient):
        """Send port information (G frame)."""
        # Format: "NumPorts;Port1 description;Port2 description;..."
        info_parts = [str(self.num_ports)]
        for i in range(self.num_ports):
            info_parts.append(f"Port{i+1} {self.port_names[i]}")
        info_str = ";".join(info_parts) + ";"

        resp = AGWPEFrame()
        resp.datakind = "G"
        resp.data = info_str.encode("ascii")
        await client.send_frame(resp)

    async def _send_port_capabilities(self, client: AGWPEClient, port: int):
        """Send port capabilities (g frame)."""
        # Format: 8 bytes - baud rate, traffic level, tx delay, tx tail, persist, slottime, maxframe, active connections
        capabilities = struct.pack(
            "BBBBBBBB",
            96,  # Baud rate code (9600 bps)
            0,  # Traffic level
            30,  # TX delay (300ms)
            10,  # TX tail (100ms)
            63,  # Persist (25%)
            10,  # Slot time (100ms)
            7,  # Max frame
            0,  # Active connections
        )

        resp = AGWPEFrame()
        resp.portx = port
        resp.datakind = "g"
        resp.data = capabilities
        await client.send_frame(resp)

    async def _handle_send_unproto(
        self, client: AGWPEClient, frame: AGWPEFrame
    ):
        """Handle 'M' - Send unproto (UI) frame."""
        try:
            # Data contains the info field to send as UI frame
            # We need to build a complete AX.25 UI frame
            from src.ax25_adapter import build_ui_kiss_frame

            # Extract path from call_to if it contains via path (separated by commas)
            dest = frame.call_to
            path = []
            if "," in dest:
                parts = dest.split(",")
                dest = parts[0]
                path = parts[1:]

            kiss_frame = build_ui_kiss_frame(
                source=frame.call_from, dest=dest, path=path, info=frame.data
            )

            await self.radio.write_kiss_frame(kiss_frame)
            print_debug(
                f"AGWPE: Sent UI frame {frame.call_from}>{dest} ({len(frame.data)} bytes)",
                level=5,
            )

        except Exception as e:
            print_error(f"AGWPE: Failed to send unproto: {e}")

    async def _handle_send_unproto_via(
        self, client: AGWPEClient, frame: AGWPEFrame
    ):
        """Handle 'V' - Send unproto with via path."""
        try:
            # Data format: "DEST,VIA1,VIA2\0INFO_DATA"
            # Parse the via path from data
            null_pos = frame.data.find(b"\x00")
            if null_pos < 0:
                print_error(
                    "AGWPE: Invalid V frame format - no null terminator"
                )
                return

            via_string = frame.data[:null_pos].decode("ascii", errors="ignore")
            info_data = frame.data[null_pos + 1 :]

            # Parse destination and path
            parts = via_string.split(",")
            if len(parts) < 1:
                print_error("AGWPE: Invalid V frame - no destination")
                return

            dest = parts[0]
            path = parts[1:] if len(parts) > 1 else []

            from src.ax25_adapter import build_ui_kiss_frame

            kiss_frame = build_ui_kiss_frame(
                source=frame.call_from, dest=dest, path=path, info=info_data
            )

            await self.radio.write_kiss_frame(kiss_frame)
            print_debug(
                f"AGWPE: Sent UI frame {frame.call_from}>{via_string} ({len(info_data)} bytes)",
                level=5,
            )

        except Exception as e:
            print_error(f"AGWPE: Failed to send unproto via: {e}")

    async def _handle_send_raw(self, client: AGWPEClient, frame: AGWPEFrame):
        """Handle 'K' - Send raw AX.25 frame."""
        try:
            # Data contains raw AX.25 frame - wrap in KISS
            kiss_frame = bytes([0xC0, 0x00]) + frame.data + bytes([0xC0])
            await self.radio.write_kiss_frame(kiss_frame)
            print_debug(
                f"AGWPE: Sent raw frame ({len(frame.data)} bytes)", level=5
            )
        except Exception as e:
            print_error(f"AGWPE: Failed to send raw frame: {e}")

    async def _handle_connect(
        self,
        client: AGWPEClient,
        frame: AGWPEFrame,
        path: List[str],
        custom_pid: int = 0xF0,
    ):
        """Handle 'C' or 'c' - Connect to remote station."""
        try:
            call_from = frame.call_from
            call_to = frame.call_to

            # Check if already connected
            if self.active_connection is not None:
                print_error(
                    f"AGWPE: Connection refused - already connected to {self.active_connection}"
                )
                # Send failure (immediate disconnect)
                resp = AGWPEFrame()
                resp.portx = frame.portx
                resp.datakind = "d"
                resp.call_from = call_from
                resp.call_to = call_to
                await client.send_frame(resp)
                return

            # Initialize AX.25 adapter if needed
            if (
                not hasattr(self.ax25, "_src_call")
                or self.ax25._src_call is None
            ):
                self.ax25.init_ax25()

            # Override source call from frame
            self.ax25._src_call = call_from

            # Format path for display
            path_str = ",".join(path) if path else "direct"
            print_info(
                f"AGWPE: Connecting {call_from} to {call_to} via {path_str}"
            )

            # Attempt connection (5 second timeout per attempt to allow for slow responses)
            success = await self.ax25.connect(
                call_to, path=path, timeout=5.0, max_retries=5
            )

            if success:
                # Connection established
                self.active_connection = (call_from, call_to)
                self.connection_owner = client
                client.connections[(call_from, call_to)] = {"connected": True}

                print_info(f"AGWPE: Connected {call_from} to {call_to}")

                # Send connection confirmation to client
                # NOTE: Some AGWPE clients expect the callsigns SWAPPED in the response
                # to indicate which end is remote. Direwolf sends: from=REMOTE to=LOCAL
                # This tells the client "REMOTE is now connected to LOCAL"
                resp = AGWPEFrame()
                resp.portx = frame.portx
                resp.datakind = "C"
                resp.call_from = (
                    call_to  # Remote station (the one we connected to)
                )
                resp.call_to = call_from  # Local station (us)
                await client.send_frame(resp)

            else:
                # Connection failed
                print_error(
                    f"AGWPE: Connection failed {call_from} to {call_to}"
                )

                # Send disconnect to indicate failure
                resp = AGWPEFrame()
                resp.portx = frame.portx
                resp.datakind = "d"
                resp.call_from = call_from
                resp.call_to = call_to
                await client.send_frame(resp)

        except Exception as e:
            import traceback

            print_error(f"AGWPE: Connect error: {e}")
            print_debug(
                f"AGWPE: Connect traceback: {traceback.format_exc()}", level=5
            )

    async def _handle_connect_via(
        self, client: AGWPEClient, frame: AGWPEFrame
    ):
        """Handle 'v' - Connect with via path."""
        try:
            # Data format: "DEST,VIA1,VIA2\0" (null-terminated via path)
            # Some clients may send leading control characters - skip them
            print_debug(
                f"AGWPE: Connect via data ({len(frame.data)} bytes): {frame.data.hex()}",
                level=4,
            )

            # Skip leading non-printable bytes
            data = frame.data
            start_pos = 0
            while start_pos < len(data) and data[start_pos] < 0x20:
                start_pos += 1

            if start_pos >= len(data):
                print_error(
                    "AGWPE: Invalid v frame - no valid data after control characters"
                )
                return

            # Find null terminator
            null_pos = data.find(b"\x00", start_pos)
            if null_pos < 0:
                via_string = data[start_pos:].decode("ascii", errors="ignore")
            else:
                via_string = data[start_pos:null_pos].decode(
                    "ascii", errors="ignore"
                )

            print_debug(
                f"AGWPE: Connect via parsed string (after skipping {start_pos} control bytes): {repr(via_string)}",
                level=5,
            )

            # Parse destination and path
            parts = via_string.split(",")
            if len(parts) < 1:
                print_error("AGWPE: Invalid v frame - no destination")
                return

            dest = parts[0].strip()
            path = [p.strip() for p in parts[1:]] if len(parts) > 1 else []

            print_debug(
                f"AGWPE: Connect via dest='{dest}' path={path}", level=5
            )

            # Use the parsed destination instead of call_to
            # Update frame and call regular connect
            frame.call_to = dest
            await self._handle_connect(client, frame, path)

        except Exception as e:
            import traceback

            print_error(f"AGWPE: Connect via error: {e}")
            print_debug(
                f"AGWPE: Connect via traceback: {traceback.format_exc()}",
                level=5,
            )
            # Send disconnect to client on error
            try:
                resp = AGWPEFrame()
                resp.portx = frame.portx
                resp.datakind = "d"
                resp.call_from = frame.call_from
                resp.call_to = frame.call_to
                await client.send_frame(resp)
            except Exception:
                pass

    async def _handle_send_connected_data(
        self, client: AGWPEClient, frame: AGWPEFrame
    ):
        """Handle 'D' - Send data on connected session."""
        try:
            call_from = frame.call_from
            call_to = frame.call_to
            conn_key = (call_from, call_to)
            conn_key_reversed = (call_to, call_from)

            # Verify connection exists (check both directions due to client quirks)
            # Some AGWPE clients send call_from/call_to in reverse order for 'D' frames
            if self.active_connection == conn_key:
                source = call_from
                dest = call_to
            elif self.active_connection == conn_key_reversed:
                # Client sent reversed callsigns - swap them back
                source = call_to
                dest = call_from
                print_debug(
                    f"AGWPE: Client sent reversed callsigns in D frame, correcting",
                    level=5,
                )
            else:
                print_error(
                    f"AGWPE: No active connection for {call_from} to {call_to} (active: {self.active_connection})"
                )
                return

            if self.connection_owner != client:
                print_error(
                    f"AGWPE: Client {client.addr} doesn't own connection"
                )
                return

            # Send info using AX.25 adapter
            print_debug(
                f"AGWPE: Sending {len(frame.data)} bytes from {source} to {dest}",
                level=5,
            )
            success = await self.ax25.send_info(
                source=source,
                dest=dest,
                path=[],  # Path already used in connection setup
                info=frame.data,
            )

            if not success:
                print_error(
                    f"AGWPE: Failed to send connected data {source} to {dest} (link_established={self.ax25._link_established})"
                )
            else:
                print_debug(f"AGWPE: Successfully sent data", level=5)

        except Exception as e:
            import traceback

            print_error(f"AGWPE: Send connected data error: {e}")
            print_debug(f"AGWPE: Traceback: {traceback.format_exc()}", level=5)

    async def _handle_disconnect(self, client: AGWPEClient, frame: AGWPEFrame):
        """Handle 'd' - Disconnect from remote station."""
        try:
            call_from = frame.call_from
            call_to = frame.call_to
            conn_key = (call_from, call_to)
            conn_key_reversed = (call_to, call_from)

            # Verify this is the active connection (check both directions)
            if self.active_connection == conn_key:
                actual_key = conn_key
            elif self.active_connection == conn_key_reversed:
                actual_key = conn_key_reversed
            else:
                print_debug(
                    f"AGWPE: Disconnect request for non-existent connection {call_from} to {call_to} (active: {self.active_connection})",
                    level=5,
                )
                return

            # Disconnect via AX.25 adapter
            await self.ax25.disconnect()

            # Clear connection state
            self.active_connection = None
            self.connection_owner = None
            if actual_key in client.connections:
                del client.connections[actual_key]

            print_info(f"AGWPE: Disconnected {call_from} from {call_to}")

            # Send disconnect confirmation to client (echo back the callsigns as sent)
            resp = AGWPEFrame()
            resp.portx = frame.portx
            resp.datakind = "d"
            resp.call_from = call_from
            resp.call_to = call_to
            await client.send_frame(resp)

        except Exception as e:
            print_error(f"AGWPE: Disconnect error: {e}")

    async def _handle_ax25_incoming(self, parsed: dict):
        """Callback from AX25Adapter when connected data arrives."""
        try:
            control = parsed.get("control")
            control_str = f"0x{control:02x}" if control is not None else "None"
            print_debug(
                f"AGWPE._handle_ax25_incoming: control={control_str}, active={self.active_connection}, owner={self.connection_owner is not None}",
                level=5,
            )

            # Check for DISC or DM frame FIRST (before active connection check)
            # These frames signal disconnection and should always be processed
            if control == 0x43 or control == 0x0F:  # DISC or DM
                src = parsed.get("src") or "NOCALL"
                dst = parsed.get("dst") or "NOCALL"
                src_str = str(src) if src is not None else "NOCALL"
                dst_str = str(dst) if dst is not None else "NOCALL"

                if control == 0x43:
                    print_info(
                        f"AGWPE: Remote station {src_str} sent DISC, notifying client"
                    )
                else:
                    print_info(
                        f"AGWPE: Remote station {src_str} sent DM, notifying client"
                    )

                # Send disconnect notification to client if we have one
                if self.connection_owner:
                    resp = AGWPEFrame()
                    resp.portx = 0
                    resp.datakind = "d"  # Lowercase d = disconnected
                    resp.call_from = src_str  # Remote station
                    resp.call_to = dst_str  # Local station
                    await self.connection_owner.send_frame(resp)

                # Clear connection state
                self.active_connection = None
                self.connection_owner = None
                print_info(
                    f"AGWPE: Cleared connection state after remote {'DISC' if control == 0x43 else 'DM'}"
                )
                return

            # Only process data frames if we have an active connection and owner
            if not self.active_connection or not self.connection_owner:
                return

            src = parsed.get("src") or "NOCALL"
            dst = parsed.get("dst") or "NOCALL"

            # Ensure callsigns are strings
            src_str = str(src) if src is not None else "NOCALL"
            dst_str = str(dst) if dst is not None else "NOCALL"

            # Extract info field for data frames
            info = parsed.get("info", b"")
            if not info:
                return

            # Send to connection owner as 'D' frame
            # The callsigns should match how the client expects them based on the 'C' response
            # Since we sent 'C' with from=REMOTE to=LOCAL, we send data the same way
            resp = AGWPEFrame()
            resp.portx = 0
            resp.datakind = "D"
            resp.call_from = src_str  # Remote station (source of data)
            resp.call_to = dst_str  # Local station (destination)
            resp.pid = parsed.get("pid", 0xF0)
            resp.data = info

            await self.connection_owner.send_frame(resp)
            print_debug(
                f"AGWPE: Forwarded connected data from {src_str} to {dst_str} ({len(info)} bytes)",
                level=5,
            )

        except Exception as e:
            print_error(f"AGWPE: Error handling incoming AX.25 data: {e}")

    async def send_monitored_frame(
        self, kiss_frame: bytes, timestamp: Optional[datetime] = None
    ):
        """Send monitored frame to all clients with monitoring enabled."""
        if not self.clients:
            return

        # Parse the frame to get addresses
        try:
            from src.ax25_adapter import parse_ax25_frame

            parsed = parse_ax25_frame(kiss_frame)

            if not parsed:
                return

            # Ensure src and dst are never None (safety check before use)
            src = parsed.get("src") or "NOCALL"
            dst = parsed.get("dst") or "NOCALL"

            # Unwrap KISS to get raw AX.25
            raw_ax25 = kiss_unwrap(kiss_frame)

            # Build monitor frame header
            if timestamp is None:
                timestamp = datetime.now()

            time_str = timestamp.strftime("%H:%M:%S")

            # Format: "Channel Port: SRC>DST,PATH <UI Len=123> [HH:MM:SS]\r\nINFO_DATA"
            path = (
                ",".join(parsed.get("path", [])) if parsed.get("path") else ""
            )
            path_str = f",{path}" if path else ""

            info = parsed.get("info", b"")
            pid = parsed.get("pid")

            # Determine frame type based on control byte if available
            frame_type = "UI"  # Default fallback
            control = parsed.get("control")
            if control is not None and isinstance(control, int):
                # Check frame type from control byte
                if (control & 0x01) == 0:
                    # I-frame
                    ns = (control >> 1) & 0x07
                    nr = (control >> 5) & 0x07
                    frame_type = f"I N(S)={ns} N(R)={nr}"
                elif (control & 0x03) == 0x01:
                    # S-frame
                    nr = (control >> 5) & 0x07
                    frame_types = {0: "RR", 1: "RNR", 2: "REJ", 3: "SREJ"}
                    stype = frame_types.get((control >> 2) & 0x03, "S")
                    frame_type = f"{stype} N(R)={nr}"
                else:
                    # U-frame (UA, SABM, DISC, etc.)
                    uframe_types = {
                        0x2F: "SABM",
                        0x63: "UA",
                        0x43: "DISC",
                        0x0F: "DM",
                        0x03: "UI",
                    }
                    utype = uframe_types.get(control, "U")
                    if pid is not None and utype == "UI":
                        frame_type = f"UI pid={pid:02X}"
                    else:
                        frame_type = utype
            elif pid is not None:
                # Fallback: assume UI if we have PID
                frame_type = f"UI pid={pid:02X}"

            # Ensure all components are strings before formatting
            src_str = str(src) if src is not None else "NOCALL"
            dst_str = str(dst) if dst is not None else "NOCALL"
            path_str = str(path_str) if path_str is not None else ""
            frame_type_str = (
                str(frame_type) if frame_type is not None else "UI"
            )

            header_line = f"0: {src_str}>{dst_str}{path_str} <{frame_type_str} Len={len(raw_ax25)}> [{time_str}]\r\n"

            # Build data portion (header + info if present)
            if info:
                try:
                    # Try to decode as text
                    info_text = info.decode("ascii", errors="replace")
                    data = header_line.encode("ascii") + info.encode(
                        "ascii", errors="replace"
                    )
                except Exception:
                    data = header_line.encode("ascii") + info
            else:
                data = header_line.encode("ascii")

            # Send to monitoring clients
            for client in list(self.clients):
                if client.mon_enabled:
                    try:
                        mon_frame = AGWPEFrame()
                        mon_frame.portx = 0
                        mon_frame.datakind = "U"  # Unproto monitor
                        mon_frame.call_from = src_str
                        mon_frame.call_to = dst_str
                        mon_frame.pid = pid if pid is not None else 0xF0
                        mon_frame.data = data
                        await client.send_frame(mon_frame)
                    except Exception as e:
                        print_error(
                            f"AGWPE: Failed to send monitor frame to {client.addr}: {e}"
                        )

                # Also send raw frames if enabled
                if client.raw_enabled:
                    try:
                        raw_frame = AGWPEFrame()
                        raw_frame.portx = 0
                        raw_frame.datakind = "K"  # Raw frame
                        raw_frame.data = raw_ax25
                        await client.send_frame(raw_frame)
                    except Exception as e:
                        print_error(
                            f"AGWPE: Failed to send raw frame to {client.addr}: {e}"
                        )

        except Exception as e:
            import traceback

            print_error(f"AGWPE: Error processing monitored frame: {e}")
            print_debug(f"AGWPE: Traceback: {traceback.format_exc()}", level=5)

    async def start(self, host: str = "0.0.0.0", port: int = AGWPE_PORT):
        """Start the AGWPE server.

        Args:
            host: Bind address (0.0.0.0=all interfaces, 127.0.0.1=localhost only)
            port: TCP port number
        """
        try:
            # Initialize AX.25 adapter for connected mode (safe to call multiple times)
            self.ax25.init_ax25()

            # Register AX25Adapter to receive KISS frames
            # If using shared adapter (from Console), this re-registers the same callback (harmless)
            # If using own adapter, this sets up the callback for the first time
            self.radio.register_kiss_callback(self.ax25.handle_incoming)

            # Start TCP server
            self.server = await asyncio.start_server(
                self.handle_client, host, port
            )
            addr = self.server.sockets[0].getsockname()
            print_info(f"AGWPE: Listening on {addr[0]}:{addr[1]}")
            return True
        except OSError:
            # Re-raise bind errors so caller can handle them specifically
            raise
        except Exception as e:
            print_error(f"AGWPE: Failed to start: {e}")
            self.server = None
            return False

    async def stop(self):
        """Stop the AGWPE server and cleanup connections."""
        # Disconnect any active AX.25 connection
        if self.active_connection:
            try:
                await self.ax25.disconnect()
            except Exception as e:
                print_debug(
                    f"AGWPE: Error disconnecting during shutdown: {e}", level=5
                )

        # Close AX.25 adapter
        try:
            if hasattr(self.ax25, "close_ax25"):
                await self.ax25.close_ax25()
        except Exception as e:
            print_debug(f"AGWPE: Error closing AX.25 adapter: {e}", level=5)

        # Close all clients
        for client in list(self.clients):
            try:
                await client.close()
            except Exception:
                pass
        self.clients.clear()

        # Stop server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            print_info("AGWPE: Server stopped")


__all__ = ["AGWPEBridge"]
