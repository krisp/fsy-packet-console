"""
Transport abstraction layer for KISS TNC communication.

Provides a unified interface for both BLE (UV-50PRO) and serial KISS TNCs.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional, Callable, Any
from src.constants import TNC_TX_UUID, TNC_RX_UUID, TNC_COMMAND_UUID, TNC_INDICATION_UUID
from src.utils import print_info, print_debug, print_warning, print_error

logger = logging.getLogger(__name__)


class TransportBase(ABC):
    """Abstract base class for TNC transport mechanisms."""

    def __init__(self):
        self._rx_callback: Optional[Callable] = None
        self._connected = False

    @abstractmethod
    async def write_kiss_frame(self, data: bytes, response: bool = True) -> bool:
        """
        Write a KISS frame to the TNC.

        Args:
            data: KISS frame data to send
            response: Whether to expect a response (for BLE mode)

        Returns:
            bool: True if write successful
        """
        pass

    @abstractmethod
    async def send_tnc_data(self, data: bytes) -> None:
        """
        Send raw data to the TNC.

        Args:
            data: Raw bytes to send
        """
        pass

    def register_rx_callback(self, callback: Callable) -> None:
        """
        Register a callback for received data.

        Args:
            callback: Function to call when data is received
        """
        self._rx_callback = callback

    def is_connected(self) -> bool:
        """Check if transport is connected."""
        return self._connected

    @abstractmethod
    async def close(self) -> None:
        """Close the transport connection."""
        pass


class BLETransport(TransportBase):
    """BLE transport for UV-50PRO radio's built-in TNC."""
    # Uses TNC_TX_UUID and TNC_RX_UUID from src.constants

    def __init__(self, client, rx_queue: asyncio.Queue, tnc_queue: asyncio.Queue):
        """
        Initialize BLE transport.

        Args:
            client: BleakClient instance
            rx_queue: Queue for radio command responses
            tnc_queue: Queue for TNC data
        """
        super().__init__()
        self.client = client
        self.rx_queue = rx_queue
        self.tnc_queue = tnc_queue
        self._connected = client.is_connected if client else False
        self._response_event = asyncio.Event()
        self._last_response = None

    async def write_kiss_frame(self, data: bytes, response: bool = True) -> bool:
        """Write KISS frame via BLE TNC_TX characteristic."""
        try:
            if not self.client or not self.client.is_connected:
                print_error("BLE client not connected")
                return False

            # Reset response event if expecting a response
            if response:
                self._response_event.clear()
                self._last_response = None

            # Write to TNC TX characteristic
            await self.client.write_gatt_char(TNC_TX_UUID, data, response=response)

            # Wait for response if expected
            if response:
                try:
                    await asyncio.wait_for(self._response_event.wait(), timeout=5.0)
                    return self._last_response is not None
                except asyncio.TimeoutError:
                    print_warning("Timeout waiting for BLE response")
                    return False

            return True

        except Exception as e:
            print_error(f"BLE write error: {e}")
            return False

    async def send_tnc_data(self, data: bytes) -> None:
        """Send raw TNC data via BLE."""
        if self.client and self.client.is_connected:
            await self.client.write_gatt_char(TNC_TX_UUID, data, response=False)

    async def send_command(self, command: int, data: bytes = b'') -> Optional[bytes]:
        """
        Send a radio command via BLE.

        Args:
            command: Command ID
            data: Optional command data

        Returns:
            Response bytes or None
        """
        try:
            if not self.client or not self.client.is_connected:
                return None

            # Clear response queue
            while not self.rx_queue.empty():
                try:
                    self.rx_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            # Build command packet
            cmd_data = bytes([command]) + data

            # Send command
            await self.client.write_gatt_char(TNC_COMMAND_UUID, cmd_data, response=True)

            # Wait for response
            try:
                response = await asyncio.wait_for(self.rx_queue.get(), timeout=2.0)
                return response
            except asyncio.TimeoutError:
                print_warning(f"Command {command} timeout")
                return None

        except Exception as e:
            print_error(f"Command error: {e}")
            return None

    async def get_status(self) -> Optional[dict]:
        """Get radio status (BLE-specific)."""
        response = await self.send_command(5)  # GET_RADIO_STATUS
        if response and len(response) >= 6:
            return {
                'battery': response[0],
                'rssi': response[1],
                'channel': response[2],
                'power': response[3],
                'squelch': response[4],
                'volume': response[5]
            }
        return None

    async def get_gps_position(self) -> Optional[dict]:
        """Get GPS position (BLE-specific)."""
        response = await self.send_command(7)  # GET_GPS_POSITION
        if response and len(response) >= 12:
            lat = int.from_bytes(response[0:4], 'little', signed=True) / 1e7
            lon = int.from_bytes(response[4:8], 'little', signed=True) / 1e7
            alt = int.from_bytes(response[8:10], 'little', signed=True)
            return {
                'latitude': lat,
                'longitude': lon,
                'altitude': alt
            }
        return None

    def handle_indication(self, sender, data: bytearray) -> None:
        """Handle BLE indication responses."""
        self._last_response = bytes(data)
        self._response_event.set()

    def handle_rx(self, sender, data: bytearray) -> None:
        """Handle BLE radio command responses."""
        try:
            self.rx_queue.put_nowait(bytes(data))
        except asyncio.QueueFull:
            print_warning("RX queue full, dropping data")

    async def close(self) -> None:
        """Close BLE connection."""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self._connected = False


class SerialTransport(TransportBase):
    """Serial port transport for external KISS TNCs."""

    VALID_BAUD_RATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]

    def __init__(self, port: str, baud: int, tnc_queue: asyncio.Queue):
        """
        Initialize serial transport.

        Args:
            port: Serial port path (e.g., /dev/ttyUSB0)
            baud: Baud rate (9600, 19200, etc.)
            tnc_queue: Queue for TNC data
        """
        super().__init__()
        self.port = port
        self.baud = baud
        self.tnc_queue = tnc_queue
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None

        # Validate baud rate
        if baud not in self.VALID_BAUD_RATES:
            print_warning(f"Unusual baud rate {baud}, valid rates: {self.VALID_BAUD_RATES}")

    async def connect(self) -> None:
        """Open serial port and start read loop."""
        try:
            import serial_asyncio

            # Open serial port with async I/O
            self.reader, self.writer = await serial_asyncio.open_serial_connection(
                url=self.port,
                baudrate=self.baud,
                bytesize=8,
                parity='N',
                stopbits=1,
                timeout=1
            )

            self._connected = True

            # Start background read task
            self._read_task = asyncio.create_task(self._read_loop())

            print_info(f"Serial port opened: {self.port} @ {self.baud} baud")

        except ImportError:
            print_error("pyserial-asyncio not installed. Run: pip install pyserial-asyncio")
            raise
        except FileNotFoundError:
            print_error(f"Serial port not found: {self.port}")
            self._suggest_available_ports()
            raise
        except PermissionError:
            print_error(f"Permission denied: {self.port}")
            print_info("Try: sudo usermod -a -G dialout $USER")
            print_info("Then log out and log back in")
            raise
        except Exception as e:
            print_error(f"Failed to open serial port: {e}")
            raise

    def _suggest_available_ports(self) -> None:
        """Suggest available serial ports to the user."""
        try:
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
            if ports:
                print_info("Available serial ports:")
                for port in ports:
                    print_info(f"  {port.device}: {port.description}")
            else:
                print_info("No serial ports found")
        except Exception:
            pass

    async def initialize_kiss_mode(self, timeout: float = 5.0) -> bool:
        """
        Initialize TNC into KISS mode if it's in command mode.

        Some TNCs power up in command mode and need to be switched to KISS mode.
        This method detects command mode and sends the initialization sequence.

        Standard sequence for most TNCs:
        1. Send CR to get command prompt
        2. Send "INTFACE KISS" command
        3. Send "RESET" command to restart in KISS mode

        Args:
            timeout: Seconds to wait for responses

        Returns:
            True if TNC is in KISS mode (or successfully initialized)
            False if initialization failed
        """
        if not self.writer or not self.reader:
            print_error("Serial port not connected, cannot initialize KISS mode")
            return False

        print_info("Checking if TNC needs KISS mode initialization...")

        try:
            # Pause the read loop temporarily to avoid queue interference
            # We'll read responses directly during initialization
            read_task_was_running = self._read_task is not None and not self._read_task.done()
            if read_task_was_running:
                self._read_task.cancel()
                try:
                    await self._read_task
                except asyncio.CancelledError:
                    pass

            # Step 1: Send CR to check for command prompt
            print_debug("Sending CR to check for command prompt...")
            self.writer.write(b'\r')
            await self.writer.drain()

            # Wait briefly and read any response
            await asyncio.sleep(0.5)
            response = b''
            try:
                response = await asyncio.wait_for(
                    self.reader.read(1024),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                # No response might mean already in KISS mode
                print_debug("No response to CR - TNC may already be in KISS mode")

            # Check if we got a command prompt
            response_str = response.decode('ascii', errors='ignore').lower()
            print_debug(f"TNC response: {repr(response_str[:100])}")

            # Look for command prompt indicators
            if 'cmd:' in response_str or 'command' in response_str or '>' in response_str:
                print_info("TNC is in command mode - switching to KISS mode...")

                # Step 2: Send INTFACE KISS command
                print_debug("Sending: INTFACE KISS")
                self.writer.write(b'INTFACE KISS\r')
                await self.writer.drain()
                await asyncio.sleep(0.5)

                # Read acknowledgment
                try:
                    ack = await asyncio.wait_for(
                        self.reader.read(1024),
                        timeout=1.0
                    )
                    print_debug(f"INTFACE response: {repr(ack[:100])}")
                except asyncio.TimeoutError:
                    print_debug("No acknowledgment from INTFACE KISS")

                # Step 3: Send RESET command
                print_debug("Sending: RESET")
                self.writer.write(b'RESET\r')
                await self.writer.drain()

                # Wait for TNC to restart (typically takes 2-3 seconds)
                print_info("Waiting for TNC to restart in KISS mode...")
                await asyncio.sleep(3.0)

                # Drain any startup messages
                try:
                    startup_msg = await asyncio.wait_for(
                        self.reader.read(4096),
                        timeout=1.0
                    )
                    print_debug(f"TNC startup messages: {len(startup_msg)} bytes")
                except asyncio.TimeoutError:
                    pass

                print_info("TNC should now be in KISS mode")
                success = True

            elif b'\xc0' in response:
                # KISS frame delimiter detected - already in KISS mode
                print_info("TNC is already in KISS mode (detected KISS frames)")
                # Put the data back in the queue for processing
                if response:
                    try:
                        self.tnc_queue.put_nowait(response)
                    except asyncio.QueueFull:
                        print_warning("TNC queue full during KISS detection")
                success = True

            else:
                # No clear indication - assume KISS mode or unsupported TNC
                print_info("No command prompt detected - assuming TNC is in KISS mode")
                if response:
                    try:
                        self.tnc_queue.put_nowait(response)
                    except asyncio.QueueFull:
                        print_warning("TNC queue full during KISS detection")
                success = True

            # Restart the read loop
            if read_task_was_running:
                self._read_task = asyncio.create_task(self._read_loop())

            return success

        except Exception as e:
            print_error(f"Error during KISS mode initialization: {e}")
            # Restart read loop on error
            if read_task_was_running:
                try:
                    self._read_task = asyncio.create_task(self._read_loop())
                except Exception:
                    pass
            return False

    async def _read_loop(self) -> None:
        """Background task to read from serial port and push to TNC queue."""
        print_info("Serial read loop started")

        try:
            while self._connected and self.reader:
                try:
                    # Read data from serial port
                    data = await self.reader.read(1024)

                    if not data:
                        # EOF or disconnect
                        print_warning("Serial port disconnected")
                        self._connected = False
                        break

                    # Push to TNC queue for processing
                    await self.tnc_queue.put(data)

                except asyncio.CancelledError:
                    print_info("Serial read loop cancelled")
                    break
                except Exception as e:
                    print_error(f"Serial read error: {e}")
                    await asyncio.sleep(1)  # Avoid tight loop on errors

        finally:
            print_info("Serial read loop stopped")

    async def write_kiss_frame(self, data: bytes, response: bool = True) -> bool:
        """
        Write KISS frame to serial port.

        Note: Serial mode ignores the 'response' parameter since serial
        doesn't have the same request/response semantics as BLE.
        """
        try:
            if not self._connected or not self.writer:
                print_error("Serial port not connected")
                return False

            # Write data
            self.writer.write(data)
            await self.writer.drain()

            return True

        except Exception as e:
            print_error(f"Serial write error: {e}")
            self._connected = False
            return False

    async def send_tnc_data(self, data: bytes) -> None:
        """Send raw data to serial TNC."""
        if self._connected and self.writer:
            try:
                self.writer.write(data)
                await self.writer.drain()
            except Exception as e:
                print_error(f"Serial send error: {e}")
                self._connected = False

    async def close(self) -> None:
        """Close serial port."""
        self._connected = False

        # Cancel read task
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        # Close writer
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

        print_info(f"Serial port closed: {self.port}")


class TCPTransport(TransportBase):
    """KISS-over-TCP client transport for external TNCs like Direwolf."""

    def __init__(self, host: str, port: int, tnc_queue: asyncio.Queue):
        """
        Initialize TCP transport.

        Args:
            host: Hostname or IP address of remote KISS TNC
            port: TCP port number (typically 8001 for Direwolf)
            tnc_queue: Queue for received TNC data
        """
        super().__init__()
        self.host = host
        self.port = port
        self.tnc_queue = tnc_queue
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None

    async def connect(self) -> bool:
        """
        Connect to remote KISS TNC server.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            print_info(f"Connecting to KISS TNC at {self.host}:{self.port}...")

            self.reader, self.writer = await asyncio.open_connection(
                self.host,
                self.port
            )

            self._connected = True

            # Start background read loop
            self._read_task = asyncio.create_task(self._read_loop())

            print_info(f"Connected to KISS TNC at {self.host}:{self.port}")
            return True

        except ConnectionRefusedError:
            print_error(f"Connection refused: {self.host}:{self.port}")
            print_error("Is Direwolf or KISS TNC server running?")
            return False
        except OSError as e:
            print_error(f"Cannot connect to {self.host}:{self.port}: {e}")
            return False
        except Exception as e:
            print_error(f"TCP connection error: {e}")
            return False

    async def _read_loop(self) -> None:
        """Background task to read data from TCP socket."""
        print_info("TCP read loop started")

        try:
            while self._connected and self.reader:
                try:
                    # Read data from TCP socket
                    data = await self.reader.read(1024)

                    if not data:
                        # EOF - connection closed by remote
                        print_warning("TCP connection closed by remote TNC")
                        self._connected = False
                        break

                    # Push to TNC queue for processing
                    await self.tnc_queue.put(data)

                except asyncio.CancelledError:
                    print_info("TCP read loop cancelled")
                    break
                except Exception as e:
                    print_error(f"TCP read error: {e}")
                    await asyncio.sleep(1)  # Avoid tight loop on errors

        finally:
            print_info("TCP read loop stopped")

    async def write_kiss_frame(self, data: bytes, response: bool = True) -> bool:
        """
        Write KISS frame to remote TNC.

        Note: TCP mode ignores the 'response' parameter since TCP
        doesn't have the same request/response semantics as BLE.

        Args:
            data: KISS frame data to send
            response: Ignored for TCP (no response mechanism)

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            if not self._connected or not self.writer:
                print_error("TCP connection not active")
                return False

            # Write data
            self.writer.write(data)
            await self.writer.drain()

            return True

        except Exception as e:
            print_error(f"TCP write error: {e}")
            self._connected = False
            return False

    async def send_tnc_data(self, data: bytes) -> None:
        """Send raw data to TCP TNC."""
        if self._connected and self.writer:
            try:
                self.writer.write(data)
                await self.writer.drain()
            except Exception as e:
                print_error(f"TCP send error: {e}")
                self._connected = False

    async def close(self) -> None:
        """Close TCP connection."""
        self._connected = False

        # Cancel read task
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        # Close writer
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

        self.reader = None
        self.writer = None

        print_info(f"TCP connection closed: {self.host}:{self.port}")
