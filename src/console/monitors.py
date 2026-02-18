"""Background monitor coroutines for TNC, GPS, heartbeat, and auto-save."""

import asyncio
import traceback

from src import constants
from src.aprs.formatters import APRSFormatters
from src.constants import HEARTBEAT_INTERVAL, DEBUG
from src.protocol import parse_ax25_addresses_and_control
from src.utils import (
    print_debug,
    print_error,
    print_info,
    print_tnc,
    print_warning,
)

from .parsers import parse_and_track_aprs_frame


async def tnc_monitor(tnc_queue, radio):
    """Monitor TNC data and display/forward to TCP."""
    frame_buffer = bytearray()

    while True:
        try:
            data = await tnc_queue.get()

            # Update activity tracker
            radio.update_tnc_activity()

            # Show raw data in debug mode
            if constants.DEBUG:
                print_debug(
                    f"TNC RX ({len(data)} bytes): {data.hex()}", level=4
                )
                ascii_str = "".join(
                    chr(b) if 32 <= b <= 126 else "." for b in data
                )
                if ascii_str.strip("."):
                    print_debug(f"TNC ASCII: {ascii_str}", level=5)

            # Add data to buffer
            frame_buffer.extend(data)
            if constants.DEBUG:
                print_debug(f"Buffer now {len(frame_buffer)} bytes", level=5)

            # Process complete KISS frames in buffer
            frames_processed = 0
            while True:
                # Look for frame start (0xC0)
                if len(frame_buffer) == 0:
                    if constants.DEBUG and frames_processed > 0:
                        print_debug(
                            f"Buffer empty after processing {frames_processed} frames",
                            level=5,
                        )
                    break

                # If buffer doesn't start with KISS frame delimiter, find it
                if frame_buffer[0] != 0xC0:
                    try:
                        start_idx = frame_buffer.index(0xC0)
                        if constants.DEBUG:
                            discarded = frame_buffer[:start_idx]
                            print_debug(
                                f"Discarded {len(discarded)} bytes of non-KISS data: {bytes(discarded).hex()}",
                                level=4,
                            )
                        frame_buffer = frame_buffer[start_idx:]
                    except ValueError:
                        if constants.DEBUG:
                            print_debug(
                                f"No KISS frame start found, discarding {len(frame_buffer)} bytes",
                                level=5,
                            )
                        frame_buffer.clear()
                        break

                # Now we have a frame that starts with 0xC0
                if len(frame_buffer) < 2:
                    if constants.DEBUG:
                        print_debug(
                            f"Buffer too small ({len(frame_buffer)} bytes), waiting for more data",
                            level=5,
                        )
                    break

                try:
                    # Find next 0xC0 after the first one
                    end_idx = frame_buffer.index(0xC0, 1)

                    # Collapse immediate duplicate FENDs introduced by
                    # chunk boundaries. If the next fence is at index 1
                    # and there are more bytes, drop the first fence and
                    # continue parsing so we don't interpret a 2-byte
                    # c0,c0 sequence as an empty frame and lose the real
                    # payload that follows.
                    if end_idx == 1 and len(frame_buffer) > 2:
                        if DEBUG:
                            print_debug(
                                "Collapsing duplicate leading FEND (0xC0); skipping one"
                            )
                        frame_buffer = frame_buffer[1:]
                        continue

                    # Extract complete frame
                    complete_frame = bytes(frame_buffer[: end_idx + 1])
                    frame_buffer = frame_buffer[end_idx + 1 :]

                    # Capture frame for history (if processor available)
                    frame_num = None
                    if hasattr(radio, "cmd_processor") and radio.cmd_processor:
                        radio.cmd_processor.frame_history.add_frame(
                            "RX", complete_frame
                        )
                        # Get the frame number that was just assigned
                        frame_num = radio.cmd_processor.frame_history.frame_counter

                    if constants.DEBUG:
                        print_debug(
                            f"Processing complete frame of {len(complete_frame)} bytes",
                            level=5,
                        )

                    # CRITICAL: Invoke AX25Adapter callback for link-layer processing
                    # This must happen BEFORE display code so adapter can process UA, I-frames, etc.
                    try:
                        if (
                            hasattr(radio, "_kiss_callback")
                            and radio._kiss_callback
                        ):
                            if asyncio.iscoroutinefunction(
                                radio._kiss_callback
                            ):
                                await radio._kiss_callback(complete_frame)
                            else:
                                radio._kiss_callback(complete_frame)
                    except Exception as e:
                        if constants.DEBUG:
                            print_debug(f"KISS callback error: {e}", level=2)

                    # Parse APRS and update database (works in all modes)
                    parsed_aprs = parse_and_track_aprs_frame(complete_frame, radio)

                    # Digipeat if enabled and criteria met
                    if parsed_aprs['is_aprs'] and not parsed_aprs['is_duplicate'] and hasattr(radio, 'digipeater'):
                        try:
                            # Check if source is a known digipeater
                            src_call_upper = parsed_aprs['src_call'].upper().rstrip('*')
                            is_source_digi = radio.aprs_manager.stations.get(src_call_upper, None)
                            is_source_digipeater = is_source_digi.is_digipeater if is_source_digi else False

                            # Debug: Show digipeater evaluation
                            if constants.DEBUG_LEVEL >= 4:
                                print_debug(
                                    f"Digipeater eval: {parsed_aprs['src_call']} "
                                    f"hop={parsed_aprs['hop_count']} "
                                    f"path={parsed_aprs['digipeater_path']} "
                                    f"enabled={radio.digipeater.enabled}",
                                    level=4
                                )

                            # Check if we should digipeat
                            if radio.digipeater.should_digipeat(
                                parsed_aprs['src_call'],
                                parsed_aprs['dst_call'],
                                parsed_aprs['hop_count'],
                                parsed_aprs['digipeater_path'],
                                is_source_digipeater,
                                parsed_aprs.get('info_str', '')
                            ):
                                # Create digipeated frame
                                digi_frame, path_type = radio.digipeater.digipeat_frame(complete_frame, parsed_aprs)
                                if digi_frame:
                                    # Transmit the digipeated frame via radio
                                    await radio.write_kiss_frame(digi_frame, response=False)
                                    print_info(
                                        f"🔁 Digipeated {parsed_aprs['src_call']} "
                                        f"({radio.digipeater.packets_digipeated} total)"
                                    )

                                    # Track digipeater statistics
                                    if hasattr(radio, 'aprs_manager') and radio.aprs_manager:
                                        try:
                                            radio.aprs_manager.record_digipeater_activity(
                                                station_call=parsed_aprs['src_call'],
                                                path_type=path_type,
                                                original_path=parsed_aprs.get('digipeater_path', []),
                                                frame_number=frame_num
                                            )
                                        except AttributeError:
                                            # record_digipeater_activity method not yet implemented
                                            pass
                                        except Exception as e:
                                            if constants.DEBUG_LEVEL >= 3:
                                                print_debug(f"Digipeater stats error: {e}", level=3)
                        except Exception as e:
                            if constants.DEBUG_LEVEL >= 2:
                                print_debug(f"Digipeater error: {e}", level=2)
                                print_debug(traceback.format_exc(), level=3)

                    # Display ASCII-decoded frame at debug level 1 (all modes)
                    if constants.DEBUG_LEVEL >= 1 and not radio.tnc_mode_active:
                        try:
                            payload = complete_frame[1:-1]  # Remove KISS delimiters
                            if len(payload) > 0 and payload[0] == 0x00:  # Data frame
                                payload = payload[1:]  # Remove KISS command byte
                                addresses, control_byte, offset = parse_ax25_addresses_and_control(payload)

                                if addresses and len(addresses) >= 2:
                                    # addresses is a list: [dest, src, digi1, digi2, ...]
                                    dst = addresses[0]
                                    src = addresses[1]
                                    path = addresses[2:] if len(addresses) > 2 else []

                                    # Get info field if present
                                    if offset < len(payload):
                                        pid = payload[offset]
                                        if pid == 0xF0 and offset + 1 < len(payload):  # No layer 3
                                            info_bytes = payload[offset + 1:]
                                            # Try to decode as ASCII
                                            info_text = info_bytes.decode('ascii', errors='replace')

                                            # Build path string
                                            path_str = ','.join(path) if path else ''
                                            path_display = f',{path_str}' if path_str else ''

                                            # Display in gray (monitor style) with frame number
                                            header = f"{src}>{dst}{path_display}"
                                            print_tnc(f"{header}:{info_text}", frame_num=frame_num)
                        except Exception:
                            pass  # Silent fail for malformed frames

                    # Display emoji pins (console mode only, not for duplicates)
                    if parsed_aprs['is_aprs'] and not parsed_aprs['is_duplicate'] and not radio.tnc_mode_active:
                        buffer_mode = hasattr(radio, "cmd_processor") and radio.cmd_processor and radio.cmd_processor.frame_history.buffer_mode
                        aprs = parsed_aprs['aprs_types']
                        relay = parsed_aprs['relay']

                        # MIC-E
                        if aprs['mic_e']:
                            mice_pos = aprs['mic_e']
                            cleaned_comment = APRSFormatters.clean_position_comment(mice_pos.comment)
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            if cleaned_comment:
                                print_info(
                                    f"📍 MIC-E from {mice_pos.station}{relay_part}: {mice_pos.grid_square} - {cleaned_comment}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                            else:
                                print_info(
                                    f"📍 MIC-E from {mice_pos.station}{relay_part}: {mice_pos.grid_square}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )

                        # Object
                        elif aprs['object']:
                            obj_pos = aprs['object']
                            cleaned_comment = APRSFormatters.clean_position_comment(obj_pos.comment)
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            if cleaned_comment:
                                print_info(
                                    f"📍 Object {obj_pos.station}{relay_part}: {obj_pos.grid_square} - {cleaned_comment}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                            else:
                                print_info(
                                    f"📍 Object {obj_pos.station}{relay_part}: {obj_pos.grid_square}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )

                        # Item
                        elif aprs['item']:
                            item_pos = aprs['item']
                            cleaned_comment = APRSFormatters.clean_position_comment(item_pos.comment)
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            if cleaned_comment:
                                print_info(
                                    f"📦 Item {item_pos.station}{relay_part}: {item_pos.grid_square} - {cleaned_comment}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )
                            else:
                                print_info(
                                    f"📦 Item {item_pos.station}{relay_part}: {item_pos.grid_square}",
                                    frame_num=frame_num,
                                    buffer_mode=buffer_mode
                                )

                        # Status
                        elif aprs['status']:
                            status = aprs['status']
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            print_info(
                                f"💬 Status from {status.station}{relay_part}: {status.status_text}",
                                frame_num=frame_num,
                                buffer_mode=buffer_mode
                            )

                        # Telemetry
                        elif aprs['telemetry']:
                            telemetry = aprs['telemetry']
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            analog_str = ",".join(str(v) for v in telemetry.analog)
                            print_info(
                                f"📊 Telemetry from {telemetry.station}{relay_part}: seq={telemetry.sequence} analog=[{analog_str}] digital={telemetry.digital}",
                                frame_num=frame_num,
                                buffer_mode=buffer_mode
                            )

                        # Message
                        elif aprs['message']:
                            msg = aprs['message']
                            relay_part = f" [📡 via {relay}]" if relay else ""
                            print_info(
                                f"📨 New APRS message from {msg.from_call}{relay_part}",
                                frame_num=frame_num,
                                buffer_mode=buffer_mode
                            )

                            # Send automatic ACK if message has ID and AUTO_ACK is enabled
                            if msg.message_id and radio.cmd_processor.tnc_config.get("AUTO_ACK") == "ON":
                                try:
                                    await radio.cmd_processor._send_aprs_ack(msg.from_call, msg.message_id)
                                except Exception as e:
                                    print_debug(f"Failed to send ACK: {e}", level=2)

                        # Weather and/or Position
                        else:
                            wx = aprs['weather']
                            pos = aprs['position']
                            relay_part = f" [📡 via {relay}]" if relay else ""

                            if wx and pos:
                                # Combined
                                combined = radio.aprs_manager.format_combined_notification(pos, wx, relay)
                                print_info(f"📍🌤️  {combined}", frame_num=frame_num, buffer_mode=buffer_mode)
                            elif wx:
                                # Weather only
                                print_info(f"🌤️  Weather update from {wx.station}{relay_part}", frame_num=frame_num, buffer_mode=buffer_mode)
                            elif pos:
                                # Position only
                                cleaned_comment = APRSFormatters.clean_position_comment(pos.comment)
                                if cleaned_comment:
                                    print_info(
                                        f"📍 Position from {pos.station}{relay_part}: {pos.grid_square} - {cleaned_comment}",
                                        frame_num=frame_num,
                                        buffer_mode=buffer_mode
                                    )
                                else:
                                    print_info(
                                        f"📍 Position from {pos.station}{relay_part}: {pos.grid_square}",
                                        frame_num=frame_num,
                                        buffer_mode=buffer_mode
                                    )

                    # Forward to bridges (all modes)
                    if radio.tnc_bridge:
                        try:
                            await radio.tnc_bridge.send_to_client(complete_frame)
                        except Exception as e:
                            print_error(f"TCP bridge error: {e}")

                    if getattr(radio, "agwpe_bridge", None):
                        try:
                            await radio.agwpe_bridge.send_monitored_frame(complete_frame)
                        except Exception as e:
                            print_error(f"AGWPE bridge error: {e}")

                    frames_processed += 1

                except ValueError:
                    # No closing delimiter found yet
                    if len(frame_buffer) > 2048:
                        if constants.DEBUG:
                            print_debug(
                                f"Buffer overflow ({len(frame_buffer)} bytes), discarding",
                                level=5,
                            )
                        frame_buffer.clear()
                    else:
                        if constants.DEBUG:
                            print_debug(
                                f"Incomplete frame in buffer ({len(frame_buffer)} bytes), waiting for more data",
                                level=5,
                            )
                    break

        except Exception as e:
            print_error(f"TNC monitor error: {e}")
            traceback.print_exc()
            # Clear buffer to prevent corruption from cascading
            frame_buffer.clear()
            if constants.DEBUG:
                print_debug("Buffer cleared due to error", level=2)


async def heartbeat_monitor(radio):
    """Periodic connection health check."""
    while radio.running:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            if not radio.running:
                break

            healthy = await radio.check_connection_health()

            if not healthy:
                print_error(
                    "Connection health check failed - consider restarting"
                )

        except Exception as e:
            print_error(f"Heartbeat monitor error: {e}")


async def autosave_monitor(radio):
    """Periodic auto-save of APRS database and frame buffer every 2 minutes.

    Uses async saves to avoid blocking the event loop. Saves run in thread pool.
    """
    AUTOSAVE_INTERVAL = 120  # 2 minutes (increased frequency for better data safety)

    while radio.running:
        try:
            await asyncio.sleep(AUTOSAVE_INTERVAL)

            if not radio.running:
                break

            # Save both database and frame buffer asynchronously (non-blocking)
            save_tasks = []

            # Save APRS database
            if hasattr(radio, 'aprs_manager') and radio.aprs_manager:
                save_tasks.append(radio.aprs_manager.save_database_async())

            # Save frame buffer
            if hasattr(radio, 'cmd_processor') and radio.cmd_processor and hasattr(radio.cmd_processor, 'frame_history'):
                save_tasks.append(radio.cmd_processor.frame_history.save_to_disk_async())

            # Run both saves concurrently
            if save_tasks:
                results = await asyncio.gather(*save_tasks, return_exceptions=True)
                # Check results (first is station count, second is None)
                if len(results) >= 1 and isinstance(results[0], int) and results[0] > 0:
                    print_debug(f"Auto-saved APRS database and frame buffer", level=3)

        except Exception as e:
            print_error(f"Auto-save monitor error: {e}")


async def gps_monitor(radio):
    """Monitor GPS and send beacons when enabled.

    Supervises the GPS polling task and automatically restarts it when needed
    for auto-recovery from GPS communication failures.
    """
    # Wait for command processor to be initialized
    while radio.running:
        if hasattr(radio, "cmd_processor") and radio.cmd_processor:
            break
        await asyncio.sleep(1)

    if not radio.running:
        return

    # Run GPS polling task with automatic restart support
    while radio.running:
        # Run GPS polling and beacon task
        await radio.cmd_processor.gps_poll_and_beacon_task()

        # Check if task exited due to restart request
        if radio.cmd_processor.gps_needs_restart:
            print_debug("GPS monitor: Task restart requested, performing recovery...", level=2)

            # Step 1: Flush stale GPS responses from rx_queue
            # This clears any error responses that might be stuck in the queue
            print_debug("Flushing rx_queue to clear stale GPS responses...", level=2)
            flushed_count = 0
            while not radio.rx_queue.empty():
                try:
                    _ = radio.rx_queue.get_nowait()
                    flushed_count += 1
                except asyncio.QueueEmpty:
                    break

            if flushed_count > 0:
                print_debug(f"Flushed {flushed_count} stale response(s) from queue", level=2)

            # Step 2: Reset restart flag and failure counter for fresh start
            radio.cmd_processor.gps_needs_restart = False
            radio.cmd_processor.gps_consecutive_failures = 0

            print_info("GPS auto-recovery: Task restarted with clean state")

            # Step 3: Wait a moment before restarting to let BLE settle
            await asyncio.sleep(2)

            # Loop will restart the task automatically
        else:
            # Task exited normally (radio.running = False), don't restart
            break


async def message_retry_monitor(radio):
    """Monitor sent messages and retry those that haven't been acknowledged."""

    while radio.running:
        try:
            await asyncio.sleep(5)  # Check every 5 seconds

            if not radio.running:
                break

            # Get command processor if available
            if (
                not hasattr(radio, "cmd_processor")
                or radio.cmd_processor is None
            ):
                continue

            aprs_mgr = radio.cmd_processor.aprs_manager

            # Check for messages that have expired after final attempt
            expired = aprs_mgr.check_expired_messages()
            for msg in expired:
                aprs_mgr.mark_message_failed(msg)
                print_warning(
                    f"Message to {msg.to_call} failed after {msg.retry_count} attempts"
                )

            # Get messages that need retry
            pending = aprs_mgr.get_pending_retries()

            for msg in pending:
                # Format the APRS message
                padded_to = msg.to_call.ljust(9)

                # Check if this is an ACK (no message ID) or regular message
                if msg.message_id is None:
                    # ACK message - format as :CALL___:ackXXXXX (no message ID on ACK itself)
                    aprs_message = f":{padded_to}:{msg.message}"
                    print_debug(
                        f"Retrying ACK to {msg.to_call} (attempt {msg.retry_count + 1}/{aprs_mgr.max_retries}): {msg.message}",
                        level=5,
                    )
                else:
                    # Regular message - format with message ID
                    aprs_message = f":{padded_to}:{msg.message}{{{msg.message_id}"
                    print_debug(
                        f"Retrying message to {msg.to_call} (attempt {msg.retry_count + 1}/{aprs_mgr.max_retries}): {msg.message[:30]}...",
                        level=5,
                    )

                # Resend the message
                await radio.send_aprs(
                    aprs_mgr.my_callsign, aprs_message, to_call="APFSYC"
                )

                # Update retry tracking
                aprs_mgr.update_message_retry(msg)

        except Exception as e:
            print_debug(f"Message retry monitor error: {e}", level=2)


async def connection_watcher(radio):
    """Aggressively monitor BLE connection state."""
    while radio.running:
        try:
            await asyncio.sleep(5)  # Check every 5 seconds

            if not radio.running:
                break

            # Check if we're still actually connected (BLE mode only)
            if radio.client and not radio.client.is_connected:
                print_error("Connection watcher: BLE disconnected!")
                radio.running = False
                break

        except Exception as e:
            print_error(f"Connection watcher error: {e}")
