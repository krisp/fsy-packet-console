"""Main application entry point: command_loop, main, run."""

import asyncio
import signal
import traceback

from bleak import BleakClient, BleakScanner
from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from src import constants
from src.aprs_manager import APRSManager
from src.ax25_adapter import AX25Adapter
from src.constants import (
    RADIO_INDICATE_UUID,
    TNC_RX_UUID,
)
from src.digipeater import Digipeater
from src.radio import RadioController
from src.tnc_bridge import TNCBridge
from src.utils import (
    print_debug,
    print_error,
    print_header,
    print_info,
    print_pt,
    print_warning,
)
from src.web_server import WebServer

from .completers import CommandCompleter
from .monitors import (
    autosave_monitor,
    connection_watcher,
    gps_monitor,
    heartbeat_monitor,
    message_retry_monitor,
    tnc_monitor,
)
from .processor import CommandProcessor
from .tnc_config import TNCConfig


async def command_loop(radio, processor=None, auto_tnc=False, auto_connect=None, serial_mode=False):
    """Command input loop with pinned prompt."""
    # Use provided processor or create new one
    if processor is None:
        processor = CommandProcessor(radio, serial_mode=serial_mode)

    # Register command processor with APRS manager for GPS access
    if hasattr(radio, 'aprs_manager') and radio.aprs_manager:
        radio.aprs_manager._cmd_processor = processor

    # Auto-connect to weather station if enabled
    if hasattr(processor, 'weather_manager') and processor.weather_manager.enabled:
        wx_address = processor.tnc_config.get("WX_ADDRESS")
        if wx_address:
            await processor.weather_manager.connect()

    # Display initial status screen
    await processor.show_startup_status()

    # Auto-enter TNC mode if requested
    if auto_tnc:
        await processor.cmd_tnc([], auto_connect=auto_connect)
        # After TNC mode exits, continue to regular command loop
        print_pt("")  # Add a blank line for spacing

    # Create tab completer
    completer = CommandCompleter(processor)

    # Create key bindings for "?" to show help
    kb = KeyBindings()

    @kb.add("?")
    def show_help(event):
        """Show context-sensitive help when '?' is pressed (IOS-style)."""

        buffer = event.current_buffer
        text_before_cursor = buffer.text[: buffer.cursor_position]

        # IOS-style context help: "command ?" shows options for next token
        # If text ends with space, show completions. Otherwise insert "?" literally
        # This allows: "debug ?" (show help) vs "msg K1MAL are you there?" (literal ?)
        if text_before_cursor.strip() and not text_before_cursor.endswith(' '):
            # Not asking for help - insert ? as regular character
            buffer.insert_text('?')
            return

        # Get completions at current position
        document = Document(
            text=buffer.text, cursor_position=buffer.cursor_position
        )
        completions = list(
            completer.get_completions(document, CompleteEvent())
        )

        # Display available options
        if completions:
            print_pt("\n<Available options>")
            for comp in completions:
                if comp.display_meta:
                    # Convert FormattedText to plain string
                    meta_text = (
                        to_plain_text(comp.display_meta)
                        if hasattr(comp.display_meta, "__iter__")
                        else str(comp.display_meta)
                    )
                    print_pt(f"  {comp.text:<20} {meta_text}")
                else:
                    print_pt(f"  {comp.text}")
            print_pt("")  # Blank line after help
        else:
            # No completions - show general help
            print_pt("\n<Type 'help' for command list>")
            print_pt("")

        # Redisplay the prompt with current text intact
        # This is done automatically by not calling validate_and_handle()

    session = PromptSession(
        completer=completer,
        complete_while_typing=False,  # Only complete on Tab
        key_bindings=kb,
    )

    with patch_stdout():
        while radio.running:
            try:
                # Build prompt with mode and unread message indicator
                mode_name = processor.console_mode
                unread = processor.aprs_manager.get_unread_count()
                if unread > 0:
                    prompt_html = f"<b><green>{mode_name}</green><yellow>({unread} msg)</yellow><green>&gt;</green></b> "
                else:
                    prompt_html = f"<b><green>{mode_name}&gt;</green></b> "

                line = await session.prompt_async(HTML(prompt_html))

                if line:
                    await processor.process(line)

            except EOFError:
                print_pt("")
                await processor.cmd_quit([])
                break
            except KeyboardInterrupt:
                print_pt("")
                await processor.cmd_quit([])
                break
            except Exception as e:
                print_error(f"Input error: {e}")


# === Main Application ===


async def main(auto_tnc=False, auto_connect=None, auto_debug=False,
               serial_port=None, serial_baud=9600, init_kiss=False,
               tcp_host=None, tcp_port=8001, radio_mac=None):
    # Enable debug mode if requested via command line
    if auto_debug:
        constants.DEBUG_LEVEL = 2
        constants.DEBUG = True
        print_info("Debug mode enabled at startup")

    print_header(f"FSY Packet Console v{constants.VERSION}")
    print_info("Starting initialization...")

    rx_queue = asyncio.Queue()
    tnc_queue = asyncio.Queue()

    # Transport and client setup
    transport = None
    client = None
    is_shutting_down = False

    # Load TNC config early (all modes) for parallel startup
    tnc_config = TNCConfig()

    # Determine BLE MAC address if in BLE mode
    if not serial_port and not tcp_host:
        # BLE mode - need to determine MAC address
        # Command-line overrides config
        if radio_mac:
            ble_mac = radio_mac
        elif tnc_config.settings.get("RADIO_MAC"):
            ble_mac = tnc_config.settings.get("RADIO_MAC")
        else:
            print_error("No radio MAC address configured")
            print_error("Set via command line: -r/--radio-mac MAC_ADDRESS")
            print_error("Or in TNC mode: RADIO_MAC 38:D2:00:01:62:C2")
            return

    # Create APRS manager early so database can load in parallel with radio connection
    mycall = tnc_config.get("MYCALL") or "NOCALL"
    retry_count = int(tnc_config.get("RETRY") or "3")
    retry_fast = int(tnc_config.get("RETRY_FAST") or "20")
    retry_slow = int(tnc_config.get("RETRY_SLOW") or "600")
    aprs_manager = APRSManager(mycall, max_retries=retry_count,
                               retry_fast=retry_fast, retry_slow=retry_slow)

    # Start database load immediately (runs in parallel with radio connection below)
    database_load_task = asyncio.create_task(aprs_manager.load_database_async())

    # Serial mode
    if serial_port:
        print_info(f"Serial KISS Mode: {serial_port} @ {serial_baud} baud..")

        try:
            from src.transport import SerialTransport

            transport = SerialTransport(serial_port, serial_baud, tnc_queue)
            await transport.connect()
            print_info(f"Serial port connected: {serial_port}")

            # Initialize KISS mode if requested
            if init_kiss:
                print_info("Initializing TNC into KISS mode...")
                success = await transport.initialize_kiss_mode()
                if success:
                    print_info("TNC is in KISS mode and ready")
                else:
                    print_warning("KISS mode initialization may have failed - continuing anyway")

        except Exception as e:
            print_error(f"Failed to open serial port: {e}")
            return

    # TCP KISS TNC Client Mode
    elif tcp_host:
        print_info(f"TCP KISS Mode: {tcp_host}:{tcp_port}...")

        try:
            from src.transport import TCPTransport

            transport = TCPTransport(
                host=tcp_host,
                port=tcp_port,
                tnc_queue=tnc_queue
            )

            if not await transport.connect():
                # Error already printed by transport layer
                return

            print_info(f"TCP KISS client ready")

        except Exception as e:
            print_error(f"Failed to connect to TCP TNC: {e}")
            return

    # BLE mode
    else:
        print_info(f"Connecting to {ble_mac}...")

        device = await BleakScanner.find_device_by_address(
            ble_mac, timeout=10.0
        )
        if not device:
            print_error(f"Device not found: {ble_mac}")
            print_error("Use 'bluetoothctl scan on' to find your radio's MAC address")
            return

        print_info(f"Found: {device.name}")

        async def handle_indication(sender, data):
            if constants.DEBUG:
                print_debug(
                    f"Radio notification from {sender.uuid}: {len(data)} bytes",
                    level=6,
                )
            await rx_queue.put(data)

        async def handle_tnc(sender, data):
            if constants.DEBUG:
                print_debug(
                    f"TNC notification from {sender.uuid}: {len(data)} bytes",
                    level=6,
                )
            await tnc_queue.put(data)

        def disconnected_callback(ble_client):
            """Called when BLE disconnects."""
            if not is_shutting_down:
                print_error("BLE disconnected!")

        try:
            client = BleakClient(
                device, timeout=20.0, disconnected_callback=disconnected_callback
            )
            await client.connect()
            print_info("Connected")

            # Pair with device for encrypted characteristics (required on Windows)
            # On Linux, pairing is handled externally via bluetoothctl
            # On Windows, Bleak requires programmatic pairing for encrypted writes
            try:
                await client.pair()
                print_info("Device paired (encrypted connection established)")
            except NotImplementedError:
                # pair() not available on this platform (e.g., Linux/BlueZ)
                print_debug("Pairing not implemented on this platform (using system pairing)", level=3)
            except Exception as e:
                # Pairing failed - might already be paired or not required
                print_debug(f"Pairing attempt: {e}", level=3)

            await client.start_notify(RADIO_INDICATE_UUID, handle_indication)
            await client.start_notify(TNC_RX_UUID, handle_tnc)

            print_info("Notifications enabled")

            await asyncio.sleep(0.5)
            while not rx_queue.empty():
                await rx_queue.get()

            # Create BLE transport
            from src.transport import BLETransport
            transport = BLETransport(client, rx_queue, tnc_queue)

            # Auto-save the working MAC address to config (if different or not set)
            stored_mac = tnc_config.settings.get("RADIO_MAC")
            if stored_mac != ble_mac:
                print_info(f"Saving radio MAC {ble_mac} to config")
                tnc_config.set("RADIO_MAC", ble_mac)
                tnc_config.save()

        except Exception as e:
            print_error(f"BLE connection failed: {e}")
            return

    # Create radio controller with transport
    try:
        radio = RadioController(transport, rx_queue, tnc_queue)

        # Attach APRS manager (database loading in background)
        radio.aprs_manager = aprs_manager

        # Create shared AX25Adapter that will be used by both CommandProcessor and AGWPE
        # This prevents the two-adapter conflict where one sets _pending_connect
        # but the other receives the UA frames
        shared_ax25 = AX25Adapter(
            radio,
            get_mycall=lambda: tnc_config.get("MYCALL"),
            get_txdelay=lambda: tnc_config.get("TXDELAY"),
        )
        # Store on radio object so CommandProcessor can access it
        radio.shared_ax25 = shared_ax25

        # Create digipeater (read state from TNC config)
        digipeat_mode = (tnc_config.get("DIGIPEAT") or "OFF").upper()
        # Validate mode (ON, OFF, SELF)
        if digipeat_mode not in ("ON", "OFF", "SELF"):
            digipeat_mode = "OFF"
        myalias = tnc_config.get("MYALIAS") or ""
        radio.digipeater = Digipeater(mycall, my_alias=myalias, mode=digipeat_mode)

        # ========================================================================
        # PARALLEL STARTUP: Complete all initialization
        # ========================================================================
        startup_tasks = []

        # Task 1: Wait for database load (already running in background)
        startup_tasks.append(database_load_task)

        # Task 2: Start TNC bridge (if not TCP client mode)
        async def start_tnc_bridge():
            if tcp_host:
                return  # Bridges disabled in TCP client mode
            try:
                tnc_host = tnc_config.get("TNC_HOST") or "0.0.0.0"
                tnc_port = int(tnc_config.get("TNC_PORT") or "8001")
                radio.tnc_bridge = TNCBridge(radio, port=tnc_port)
                await radio.tnc_bridge.start(host=tnc_host)
            except OSError as e:
                print_error(f"TNC bridge failed to bind to {tnc_host}:{tnc_port} - {e}")
                radio.tnc_bridge = None
            except Exception as e:
                print_error(f"Failed to start TNC bridge: {e}")
                radio.tnc_bridge = None

        startup_tasks.append(start_tnc_bridge())

        # Task 3: Start AGWPE bridge (if not TCP client mode)
        async def start_agwpe_bridge():
            if tcp_host:
                return  # Bridges disabled in TCP client mode
            try:
                from src.agwpe_bridge import AGWPEBridge
                agwpe_host = tnc_config.get("AGWPE_HOST") or "0.0.0.0"
                agwpe_port = int(tnc_config.get("AGWPE_PORT") or "8000")
                radio.agwpe_bridge = AGWPEBridge(
                    radio,
                    get_mycall=lambda: tnc_config.get("MYCALL"),
                    get_txdelay=lambda: tnc_config.get("TXDELAY"),
                    ax25_adapter=shared_ax25,
                )
                started = await radio.agwpe_bridge.start(host=agwpe_host, port=agwpe_port)
                if not started:
                    print_error("AGWPE bridge failed to start")
                    radio.agwpe_bridge = None
            except OSError as e:
                print_error(f"AGWPE bridge failed to bind to {agwpe_host}:{agwpe_port} - {e}")
                radio.agwpe_bridge = None
            except Exception as e:
                print_error(f"Failed to start AGWPE bridge: {e}")
                radio.agwpe_bridge = None

        startup_tasks.append(start_agwpe_bridge())

        # Task 4: Start Web UI server
        async def start_web_ui():
            try:
                webui_host = tnc_config.get("WEBUI_HOST") or "0.0.0.0"
                webui_port = int(tnc_config.get("WEBUI_PORT") or "8002")
                radio.web_server = WebServer(
                    radio=radio,
                    aprs_manager=radio.aprs_manager,
                    get_mycall=lambda: tnc_config.get("MYCALL"),
                    get_mylocation=lambda: tnc_config.get("MYLOCATION"),
                    get_wxtrend=lambda: tnc_config.get("WXTREND"),
                    tnc_config=tnc_config
                )
                started = await radio.web_server.start(host=webui_host, port=webui_port)
                if started:
                    print_info(f"Web UI started on http://{webui_host}:{webui_port}")
                else:
                    print_error("Web UI failed to start")
                    radio.web_server = None
            except OSError as e:
                print_error(f"Web UI failed to bind to {webui_host}:{webui_port} - {e}")
                radio.web_server = None
            except Exception as e:
                print_error(f"Failed to start Web UI: {e}")
                radio.web_server = None

        startup_tasks.append(start_web_ui())

        # Wait for all startup tasks to complete
        await asyncio.gather(*startup_tasks, return_exceptions=True)

        if tcp_host:
            print_info("TNC/AGWPE bridges disabled (TCP client mode)")

        # Create CommandProcessor (loads frame buffer)
        # Pass tnc_config so command processor and web server share the same instance
        serial_mode = (serial_port is not None or tcp_host is not None)
        processor = CommandProcessor(radio, serial_mode=serial_mode, tnc_config=tnc_config)

        # Register command processor with APRS manager for GPS access
        if hasattr(radio, 'aprs_manager') and radio.aprs_manager:
            radio.aprs_manager._cmd_processor = processor

        # Now ALL initialization is truly complete (including frame buffer)
        print_info("All components initialized")
        print_info("Monitoring TNC traffic...")

        # Create background task list
        background_tasks = [
            asyncio.create_task(tnc_monitor(tnc_queue, radio)),
            asyncio.create_task(message_retry_monitor(radio)),
            asyncio.create_task(autosave_monitor(radio)),
        ]

        # Add BLE-only monitors (GPS, connection, heartbeat)
        if not serial_port and not tcp_host:
            background_tasks.extend([
                asyncio.create_task(gps_monitor(radio)),  # GPS only available in BLE mode
                asyncio.create_task(connection_watcher(radio)),
                asyncio.create_task(heartbeat_monitor(radio)),
            ])

        # Store background tasks on radio for graceful shutdown
        radio.background_tasks = background_tasks

        # Create full task list including command loop
        tasks = background_tasks.copy()

        # Add command loop with pre-initialized processor
        tasks.append(
            asyncio.create_task(
                command_loop(
                    radio, processor=processor,
                    auto_tnc=auto_tnc, auto_connect=auto_connect,
                    serial_mode=serial_mode
                )
            )
        )

        # Wait for command loop to finish (last task)
        await tasks[-1]

        # Mark as shutting down to suppress disconnect error
        is_shutting_down = True

        # Cancel other tasks
        for task in tasks[:-1]:  # All except command_loop
            task.cancel()

        # Stop TNC bridge (if started)
        if hasattr(radio, 'tnc_bridge') and radio.tnc_bridge:
            await radio.tnc_bridge.stop()

        # Stop AGWPE bridge (if started)
        if hasattr(radio, 'agwpe_bridge') and radio.agwpe_bridge:
            await radio.agwpe_bridge.stop()

        # Shutdown web server
        if hasattr(radio, 'web_server') and radio.web_server:
            print_info("Shutting down Web UI...")
            await radio.web_server.stop()

        # Note: Frame buffer and database already saved by cmd_quit() or autosave
        # No need to save again here (would be redundant with async saves)

        print_info("Disconnecting...")

        # Close transport
        if transport:
            await transport.close()

    except Exception as e:
        print_error(f"{type(e).__name__}: {e}")

        traceback.print_exc()


def run(auto_tnc=False, auto_connect=None, auto_debug=False,
        serial_port=None, serial_baud=9600, tcp_host=None, tcp_port=8001,
        radio_mac=None, init_kiss=False):
    """Entry point for the console application."""
    def sigterm_handler(signum, frame):
        """Handle SIGTERM by raising SIGINT to interrupt the prompt."""
        # Raise SIGINT to trigger KeyboardInterrupt in the blocking prompt
        signal.raise_signal(signal.SIGINT)

    # Register SIGTERM handler
    signal.signal(signal.SIGTERM, sigterm_handler)

    try:
        asyncio.run(
            main(
                auto_tnc=auto_tnc,
                auto_connect=auto_connect,
                auto_debug=auto_debug,
                serial_port=serial_port,
                serial_baud=serial_baud,
                tcp_host=tcp_host,
                tcp_port=tcp_port,
                radio_mac=radio_mac,
                init_kiss=init_kiss,
            )
        )
    except KeyboardInterrupt:
        print_pt(HTML("\n<yellow>Interrupted by user</yellow>"))

    print_pt(HTML("<gray>Goodbye!</gray>"))
