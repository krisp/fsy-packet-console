"""
UV-50PRO TNC TCP Bridge
"""

import asyncio
from src.utils import print_info, print_error, print_debug
from src.constants import TNC_TCP_PORT
from src.protocol import kiss_unwrap
from src.ax25_adapter import parse_ax25_frame


class TNCBridge:
    """TCP bridge for TNC serial port."""

    def __init__(self, radio, port=TNC_TCP_PORT):
        self.radio = radio
        self.port = port
        self.server = None
        self.client_writer = None
        self.client_reader = None
        self.client_address = None

    async def handle_client(self, reader, writer):
        """Handle incoming TCP connection."""
        addr = writer.get_extra_info("peername")

        # Only allow one connection at a time
        if self.client_writer is not None:
            print_error(
                f"TNC Bridge: Rejected connection from {addr} (already connected)"
            )
            writer.close()
            await writer.wait_closed()
            return

        self.client_reader = reader
        self.client_writer = writer
        self.client_address = addr
        print_info(f"TNC Bridge: Client connected from {addr}")

        try:
            while True:
                # Read data from TCP client
                data = await reader.read(4096)
                if not data:
                    break

                # Send to TNC
                self._debug_frame("TCP → TNC", data)
                await self.radio.send_tnc_data(data)

        except Exception as e:
            print_error(f"TNC Bridge: Error: {e}")

        finally:
            print_info(f"TNC Bridge: Client disconnected from {addr}")

            # Forcefully shutdown the connection to ensure client detects disconnect
            try:
                # Get the underlying socket to set SO_LINGER for hard reset
                transport = writer.transport
                if transport and hasattr(transport, "get_extra_info"):
                    sock = transport.get_extra_info("socket")
                    if sock:
                        import socket

                        # Set SO_LINGER to (1, 0) to send RST instead of FIN
                        # This forcefully resets the connection - client WILL notice!
                        try:
                            sock.setsockopt(
                                socket.SOL_SOCKET,
                                socket.SO_LINGER,
                                bytes([1, 0, 0, 0, 0, 0, 0, 0]),
                            )  # struct linger {1, 0}
                        except Exception:
                            pass

                # Close the connection (will send RST due to SO_LINGER)
                writer.close()

                # Wait for close to complete (with timeout)
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass  # Force close anyway

            except Exception as e:
                print_debug(
                    f"TNC Bridge: Error during disconnect: {e}", level=5
                )
            finally:
                self.client_writer = None
                self.client_reader = None
                self.client_address = None

    async def send_to_client(self, data):
        """Send data from TNC to TCP client."""
        if self.client_writer is not None:
            try:
                self.client_writer.write(data)
                await self.client_writer.drain()
                self._debug_frame("TNC → TCP", data)
            except Exception as e:
                print_error(f"TNC Bridge: Failed to send to client: {e}")
                self.client_writer = None
                self.client_reader = None
                self.client_address = None

    def _debug_frame(self, direction, data):
        """Debug output for bridged frames using parse_ax25_frame."""
        try:
            print_debug(
                f"TNC Bridge: {direction} ({len(data)} bytes)", level=5
            )
            print_debug(f"  HEX: {data.hex()}", level=4)

            # Try to show ASCII
            try:
                ascii_repr = "".join(
                    chr(b) if 32 <= b <= 126 else "." for b in data
                )
                print_debug(f"  ASCII: {ascii_repr}", level=5)
            except Exception:
                pass

            # Try to parse as KISS frame
            try:
                if len(data) >= 3 and data[0] == 0xC0:
                    # Looks like KISS frame
                    cmd = data[1]
                    port = (cmd >> 4) & 0x0F
                    cmd_type = cmd & 0x0F
                    print_debug(f"  KISS: port={port} cmd={cmd_type}", level=5)

                    # Use parse_ax25_frame to get parsed data
                    try:
                        parsed = parse_ax25_frame(data)

                        if parsed["src"]:
                            print_debug(f"    SRC: {parsed['src']}", level=5)
                        if parsed["dst"]:
                            print_debug(f"    DST: {parsed['dst']}", level=5)
                        if parsed["path"]:
                            print_debug(
                                f"    PATH: {', '.join(parsed['path'])}",
                                level=5,
                            )

                        # Get control byte from raw unwrapped frame to determine type
                        try:
                            ax25_frame = kiss_unwrap(data)
                            # Find control byte by counting address fields
                            # Each address is 7 bytes, and we have at least src+dst
                            num_addrs = 2 + len(parsed.get("path", []))
                            ctrl_offset = num_addrs * 7

                            if ctrl_offset < len(ax25_frame):
                                control = ax25_frame[ctrl_offset]

                                # Determine frame type
                                if (control & 0x01) == 0:
                                    # I-frame
                                    ns = (control >> 1) & 0x07
                                    nr = (control >> 5) & 0x07
                                    pf = bool(control & 0x10)
                                    print_debug(
                                        f"    I-FRAME: N(S)={ns}, N(R)={nr}, P/F={pf}",
                                        level=5,
                                    )
                                    print_debug(
                                        f"      Control: 0x{control:02x}",
                                        level=5,
                                    )

                                    # Show PID and info
                                    if parsed.get("pid") is not None:
                                        print_debug(
                                            f"      PID: 0x{parsed['pid']:02x}",
                                            level=5,
                                        )
                                    if parsed.get("info"):
                                        info = parsed["info"]
                                        print_debug(
                                            f"      Info ({len(info)} bytes): {info.hex()}",
                                            level=4,
                                        )
                                        try:
                                            info_text = info.decode(
                                                "ascii", errors="replace"
                                            )
                                            print_debug(
                                                f"      Info text: {repr(info_text)}",
                                                level=5,
                                            )
                                        except Exception:
                                            pass

                                elif (control & 0x03) == 0x01:
                                    # S-frame
                                    frame_type = (control >> 2) & 0x03
                                    type_names = {
                                        0: "RR",
                                        1: "RNR",
                                        2: "REJ",
                                        3: "SREJ",
                                    }
                                    nr = (control >> 5) & 0x07
                                    pf = bool(control & 0x10)
                                    print_debug(
                                        f"    S-FRAME: {type_names.get(frame_type, 'UNKNOWN')}, N(R)={nr}, P/F={pf}",
                                        level=5,
                                    )
                                    print_debug(
                                        f"      Control: 0x{control:02x}",
                                        level=5,
                                    )

                                else:
                                    # U-frame
                                    uframe_types = {
                                        0x2F: "SABM",
                                        0x63: "UA",
                                        0x43: "DISC",
                                        0x0F: "DM",
                                        0x87: "FRMR",
                                        0x03: "UI",
                                    }
                                    frame_name = uframe_types.get(
                                        control, f"U-FRAME(0x{control:02x})"
                                    )
                                    print_debug(f"    {frame_name}", level=5)
                                    print_debug(
                                        f"      Control: 0x{control:02x}",
                                        level=5,
                                    )

                                    # For FRMR or UI with info, show it
                                    if parsed.get("info"):
                                        info = parsed["info"]
                                        print_debug(
                                            f"      Info ({len(info)} bytes): {info.hex()}",
                                            level=4,
                                        )
                                        if control == 0x87:  # FRMR
                                            if len(info) >= 3:
                                                print_debug(
                                                    f"        Rejected control: 0x{info[0]:02x}",
                                                    level=5,
                                                )
                                                print_debug(
                                                    f"        V(S)/V(R): 0x{info[1]:02x}",
                                                    level=5,
                                                )
                                                reason = info[2]
                                                print_debug(
                                                    f"        Reason: 0x{reason:02x} (W={bool(reason&1)} X={bool(reason&2)} Y={bool(reason&4)} Z={bool(reason&8)})",
                                                    level=5,
                                                )

                        except Exception as e:
                            print_debug(
                                f"    Control byte parse error: {e}", level=5
                            )

                    except Exception as e:
                        print_debug(f"  AX.25 parse error: {e}", level=5)

            except Exception:
                pass

        except Exception as e:
            print_debug(f"TNC Bridge debug error: {e}", level=5)

    async def start(self, host: str = "0.0.0.0"):
        """Start the TCP server.

        Args:
            host: Bind address (0.0.0.0=all interfaces, 127.0.0.1=localhost only)
        """
        self.server = await asyncio.start_server(
            self.handle_client, host, self.port
        )

        addr = self.server.sockets[0].getsockname()
        print_info(f"TNC Bridge: Listening on {addr[0]}:{addr[1]}")

    async def stop(self):
        """Stop the TCP server and disconnect any clients."""
        # First, forcefully disconnect any connected client
        if self.client_writer is not None:
            try:
                # Get the underlying socket to force RST
                transport = self.client_writer.transport
                if transport and hasattr(transport, "get_extra_info"):
                    sock = transport.get_extra_info("socket")
                    if sock:
                        import socket

                        # Set SO_LINGER to (1, 0) for hard RST reset
                        try:
                            sock.setsockopt(
                                socket.SOL_SOCKET,
                                socket.SO_LINGER,
                                bytes([1, 0, 0, 0, 0, 0, 0, 0]),
                            )
                        except Exception:
                            pass

                # Close the connection (sends RST due to SO_LINGER)
                self.client_writer.close()

                # Wait for it to close
                try:
                    await asyncio.wait_for(
                        self.client_writer.wait_closed(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    pass  # Force close even if timeout

                print_info(
                    f"TNC Bridge: Disconnected client {self.client_address}"
                )
            except Exception as e:
                print_debug(f"TNC Bridge: Error closing client: {e}", level=5)
            finally:
                self.client_writer = None
                self.client_reader = None
                self.client_address = None

        # Then close the server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            print_info("TNC Bridge: Server stopped")
