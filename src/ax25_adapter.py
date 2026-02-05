"""
AX.25 adapter implemented using local protocol builders.
This module exposes helpers used by the TNC mode.

Provided items:
- `parse_ax25_frame(kiss_frame)`
- `build_ui_kiss_frame(source, dest, path, info)`
- `AX25Adapter` class with `connect`, `disconnect`, `send_info`, and
    `handle_incoming` methods.
"""

from typing import Optional, Dict, List
import asyncio
import random

from src.protocol import (
    kiss_unwrap,
    wrap_kiss,
    parse_ax25_addresses_and_control,
    build_sabm,
    build_hdlc_uframe,
    build_ua,
    build_iframe,
    SABM_CONTROL,
    UA_CONTROL,
)
from src.utils import print_debug, print_info, print_error

# Additional control byte constants
DISC_CONTROL = 0x43  # Disconnect
DM_CONTROL = 0x0F  # Disconnected Mode
FRMR_CONTROL = 0x87  # Frame Reject


def parse_ax25_frame(kiss_frame: bytes) -> Dict:
    """Parse a KISS-wrapped AX.25 frame into a dict: src,dst,path,control,pid,info"""
    result = {
        "src": None,
        "dst": None,
        "path": [],
        "control": None,
        "pid": None,
        "info": b"",
    }
    try:
        payload = kiss_unwrap(kiss_frame)
    except Exception:
        return result

    try:
        addresses, control, offset = parse_ax25_addresses_and_control(payload)
        pid = None
        info = b""

        if offset < len(payload):
            # Standard AX.25: I-frames and UI frames have PID byte
            # U-frames without PID: SABM, DISC, UA, DM, FRMR (have control & 0x03 == 0x03 but aren't UI)
            if control is not None and (
                (control & 0x01) == 0  # I-frame: has PID
                or (control == 0x03)  # UI frame specifically: has PID
            ):
                # I-frame or UI frame: has PID byte
                pid = payload[offset]
                info = payload[offset + 1 :]
            else:
                # Other frame types (S-frames, U-frames except UI): no PID byte, info starts at offset
                info = payload[offset:]

        result["control"] = control
        result["pid"] = pid
        if len(addresses) >= 2:
            result["dst"] = addresses[0]
            result["src"] = addresses[1]
        elif len(addresses) == 1:
            result["dst"] = addresses[0]
        if len(addresses) > 2:
            result["path"] = addresses[2:]
        result["info"] = info
    except Exception:
        pass

    return result


def build_ui_kiss_frame(
    source: str, dest: str, path: Optional[List[str]], info: bytes
) -> bytes:
    """Build a UI KISS frame using the local protocol builder.

    Uses `build_sabm`/`build_iframe`/`build_ua` helpers where appropriate
    and always wraps the raw AX.25 bytes into a KISS frame.
    """
    frame = build_iframe(source, dest, path or [], info=info)
    # DON'T add FCS - the radio adds it automatically!
    return wrap_kiss(frame, port=0)


class AX25Adapter:
    def __init__(self, radio, get_mycall=None, get_txdelay=None):
        self.radio = radio
        self._cb = []  # Changed to list to support multiple callbacks
        self._frame_debug_cb = None
        self._link_established = False
        self._pending_connect = None
        self._link_event = asyncio.Event()
        # callable to fetch current MYCALL; if not provided, we'll try radio.settings
        self._get_mycall = get_mycall
        # callable to fetch current TXDELAY; if not provided, use default
        self._get_txdelay = get_txdelay
        # Link-layer transmit state
        self._ns = 0
        self._nr = 0
        self._tx_window = 4
        self._tx_queue = (
            []
        )  # list of dicts: {'ns':int,'raw':bytes,'sent_at':float,'retries':int}
        self._tx_lock = asyncio.Lock()
        self._tx_task = None
        self._max_retries = 4
        self._retransmit_timeout = (
            8.0  # Base timeout - increased from 6s to 8s for large frames
        )
        self._retransmit_jitter = (
            2.0  # Random jitter range (0 to 2 seconds) to desynchronize
        )
        self._rx_holdoff = 3.0  # Don't retransmit for 3s after receiving (remote may send more)
        self._last_rx_time = 0.0  # Track last frame reception time
        # Default TXDELAY - will be updated from config
        self._txdelay = 0.3  # Delay before transmitting RR/REJ (seconds) - gives remote time to switch TX->RX
        # configured source call
        self._src_call = None
        # verbose logging
        self.verbose = True

    def register_callback(self, cb):
        """Register a callback for received frames (supports multiple callbacks)."""
        if cb not in self._cb:
            self._cb.append(cb)
            print_debug(
                f"AX25Adapter: Registered callback {cb.__name__ if hasattr(cb, '__name__') else cb} (total: {len(self._cb)})",
                level=3,
            )

    def register_frame_debug(self, cb):
        """Register a callback called with (direction:str, kiss_frame:bytes).

        direction is 'tx' or 'rx'.
        """
        self._frame_debug_cb = cb

    @property
    def txdelay(self):
        """Get current TXDELAY value (in seconds).

        TXDELAY from TNC config is in units of 10ms, so convert to seconds.
        Default to 0.3 seconds (300ms) if not configured.
        """
        if self._get_txdelay:
            try:
                # TNC TXDELAY is in units of 10ms (e.g., 30 = 300ms = 0.3s)
                value = int(self._get_txdelay())
                return value * 0.01  # Convert to seconds
            except (ValueError, TypeError):
                pass
        return self._txdelay  # Fallback to default

    def init_ax25(self, ip: str = None, port: int = None):
        """Initialize adapter state when entering TNC mode.

        We use the protocol builders for frames; we only record the source callsign.
        """
        src = None
        if callable(self._get_mycall):
            try:
                src = self._get_mycall()
            except Exception:
                src = None
        if not src:
            try:
                src = self.radio.settings.get("MYCALL")
            except Exception:
                src = "NOCALL"
        self._src_call = src or "NOCALL"
        if self.verbose:
            print_info(f"AX25Adapter: initialized for {self._src_call}")

    async def close_ax25(self):
        """Clean up state when leaving TNC mode."""
        if self.verbose:
            print_info("AX25Adapter: cleaning up...")

        # 1. Stop TX worker task first
        if self._tx_task:
            try:
                self._tx_task.cancel()
                try:
                    await self._tx_task
                except asyncio.CancelledError:
                    pass
            except Exception:
                pass
            self._tx_task = None

        # 2. Disconnect if still connected
        if self._link_established:
            try:
                await self.disconnect()
            except Exception:
                pass

        # 3. Clear all queues and state
        async with self._tx_lock:
            self._tx_queue.clear()
        self._link_established = False
        self._pending_connect = None
        self._ns = 0
        self._nr = 0

        # 4. Clear source call
        self._src_call = None

        # NOTE: We don't send KISS reset commands as they're not reliably supported
        # and the radio/TNC continues to function normally without them.
        # The radio stays in KISS mode and will resume working when TNC mode is re-entered.

        if self.verbose:
            print_info("AX25Adapter: closed")

    async def connect(
        self,
        dest: str,
        path: Optional[List[str]] = None,
        timeout: float = 5.0,
        max_retries: int = 5,
    ) -> bool:
        """Connect to remote station with automatic SABM retries.

        Args:
            dest: Destination callsign
            path: Digipeater path
            timeout: Timeout per retry attempt (seconds)
            max_retries: Maximum number of SABM attempts

        Returns:
            True if connection established, False if all retries failed
        """
        if path is None:
            path = []
        self._pending_connect = dest
        self._link_event.clear()
        if self.verbose:
            print_debug(
                f"AX25Adapter.connect: _pending_connect set to '{dest}'",
                level=3,
            )

        # Reset sequence numbers for new connection
        self._ns = 0
        self._nr = 0
        async with self._tx_lock:
            self._tx_queue.clear()

        source = self._src_call or "NOCALL"

        # Build SABM frame (reused for retries)
        raw_frame = build_sabm(source, dest, path or [])
        kiss = wrap_kiss(raw_frame, port=0)

        # Try connecting with retries
        for attempt in range(1, max_retries + 1):
            try:
                # Send SABM
                if self.verbose:
                    if attempt == 1:
                        print_debug(
                            f"AX25Adapter.connect: sending SABM to {dest}",
                            level=3,
                        )
                    else:
                        print_debug(
                            f"AX25Adapter.connect: retry {attempt}/{max_retries} - sending SABM to {dest}",
                            level=3,
                        )

                await self.radio.write_kiss_frame(kiss)
                try:
                    if self._frame_debug_cb:
                        self._frame_debug_cb("tx", kiss)
                except Exception:
                    pass

                # Wait for UA response with timeout
                try:
                    await asyncio.wait_for(
                        self._link_event.wait(), timeout=timeout
                    )
                    # Got UA! Connection established
                    self._link_established = True
                    try:
                        self.radio.tnc_link_established = True
                        self.radio.tnc_connected_callsign = dest
                    except Exception:
                        pass
                    # start transmit worker
                    if self._tx_task is None:
                        self._tx_task = asyncio.create_task(self._tx_worker())
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter.connect: connection established on attempt {attempt}",
                            level=3,
                        )
                    return True
                except asyncio.TimeoutError:
                    # No response, will retry unless this was last attempt
                    if attempt < max_retries:
                        if self.verbose:
                            print_debug(
                                f"AX25Adapter.connect: no response, retrying...",
                                level=3,
                            )
                        # Hardware workaround: Cycle squelch to unstick TX queue before retry
                        await self._cycle_squelch_workaround()
                    continue

            except Exception as e:
                print_error(f"AX25Adapter.connect send SABM failed: {e}")
                if attempt < max_retries:
                    continue
                else:
                    return False

        # All retries exhausted
        if self.verbose:
            print_debug(
                f"AX25Adapter.connect: connection failed after {max_retries} attempts",
                level=3,
            )
            print_debug(
                f"AX25Adapter.connect: clearing _pending_connect (was '{self._pending_connect}')",
                level=3,
            )
        # Clean up on failure - ensure we're in disconnected state
        self._link_established = False
        self._pending_connect = None
        return False

    async def _cycle_squelch_workaround(self):
        """Cycle squelch to unstick radio TX queue (hardware workaround).

        Sometimes the radio gets stuck and won't transmit until another packet
        arrives or squelch is cycled. This method saves the current squelch,
        sets it to 0, restores it, and waits briefly for any queued TX to happen.
        """
        try:
            if not hasattr(self.radio, "get_settings") or not hasattr(
                self.radio, "set_squelch"
            ):
                return

            # Get current squelch level
            settings = await self.radio.get_settings()
            if not settings or "squelch_level" not in settings:
                return

            original_squelch = settings["squelch_level"]

            if self.verbose:
                print_debug(
                    f"AX25Adapter: Cycling squelch to unstick TX queue (original={original_squelch})",
                    level=3,
                )

            # Cycle squelch: 0 -> original
            await self.radio.set_squelch(0)
            await asyncio.sleep(0.1)  # Brief delay
            await self.radio.set_squelch(original_squelch)

            # Wait briefly for any queued packets to transmit or arrive
            await asyncio.sleep(0.3)

            if self.verbose:
                print_debug(
                    f"AX25Adapter: Squelch cycle complete, resuming", level=3
                )

        except Exception as e:
            if self.verbose:
                print_debug(
                    f"AX25Adapter: Squelch cycle failed (non-critical): {e}",
                    level=3,
                )

    async def disconnect(self):
        try:
            if self._link_established:
                source = self._src_call or "NOCALL"
                # Send DISC (disconnect) frame instead of UA
                # DISC control byte is 0x43
                raw_frame = build_hdlc_uframe(
                    source, self._pending_connect or "", [], 0x43
                )
                # DON'T add FCS - the radio adds it automatically!
                kiss = wrap_kiss(raw_frame, port=0)
                print_debug(
                    f"AX25Adapter.disconnect: sending DISC to {self._pending_connect}",
                    level=3,
                )
                await self.radio.write_kiss_frame(kiss)
                try:
                    if self._frame_debug_cb:
                        self._frame_debug_cb("tx", kiss)
                except Exception:
                    pass
        except Exception as e:
            print_error(f"AX25Adapter.disconnect failed: {e}")

        self._link_established = False
        self._pending_connect = None
        # Reset sequence numbers
        self._ns = 0
        self._nr = 0
        async with self._tx_lock:
            self._tx_queue.clear()
        # stop tx worker
        try:
            if self._tx_task:
                self._tx_task.cancel()
        except Exception:
            pass
        try:
            self.radio.tnc_link_established = False
            self.radio.tnc_connected_callsign = None
        except Exception:
            pass

    async def send_info(
        self, source: str, dest: str, path: Optional[List[str]], info: bytes
    ) -> bool:
        if path is None:
            path = []
        try:
            # Only send when a link is established
            if self._link_established:
                async with self._tx_lock:
                    # Standard AX.25: N(R) indicates the next frame we expect to receive
                    # P/F bit set to 1 (poll) to request acknowledgment
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter.send_info: N(S)={self._ns}, N(R)={self._nr}, P/F=1",
                            level=5,
                        )
                    frame = build_iframe(
                        source,
                        dest,
                        path or [],
                        info=info,
                        ns=self._ns,
                        nr=self._nr,
                        pf=1,
                    )
                    # DON'T add FCS - the radio adds it automatically!
                    kiss = wrap_kiss(frame, port=0)
                    ok = await self.radio.write_kiss_frame(kiss)
                    try:
                        if self._frame_debug_cb:
                            self._frame_debug_cb("tx", kiss)
                    except Exception:
                        pass
                    # enqueue for retransmit tracking (store raw AX.25 payload, not KISS-wrapped)
                    entry = {
                        "ns": self._ns,
                        "raw": kiss,
                        "sent_at": asyncio.get_event_loop().time(),
                        "retries": 0,
                    }
                    self._tx_queue.append(entry)
                    self._ns = (self._ns + 1) & 0x07
                    return ok
            return False
        except Exception as e:
            print_error(f"AX25Adapter.send_info failed: {e}")
            return False

    async def handle_incoming(self, kiss_frame: bytes):
        # Track last RX time to avoid retransmitting while remote is sending
        self._last_rx_time = asyncio.get_event_loop().time()

        # debug hook for received raw KISS frames
        try:
            if self._frame_debug_cb:
                try:
                    self._frame_debug_cb("rx", kiss_frame)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            payload = kiss_unwrap(kiss_frame)
        except Exception:
            return

        parsed = parse_ax25_frame(kiss_frame)

        try:
            addrs, control, offset = parse_ax25_addresses_and_control(payload)
            if self.verbose and control is not None:
                try:
                    print_debug(
                        f"AX25Adapter: parsed control: 0x{control:02x}",
                        level=5,
                    )
                    # Decode based on frame type
                    if (control & 0x01) == 0:
                        # I-frame
                        ns = (control >> 1) & 0x07
                        nr = (control >> 5) & 0x07
                        pf = bool(control & 0x10)
                        print_debug(
                            f"  I-frame: N(S)={ns}, N(R)={nr}, P/F={pf}",
                            level=5,
                        )
                    elif (control & 0x03) == 0x01:
                        # S-frame
                        nr = (control >> 5) & 0x07
                        pf = bool(control & 0x10)
                        frame_type = (control >> 2) & 0x03
                        types = {0: "RR", 1: "RNR", 2: "REJ", 3: "SREJ"}
                        print_debug(
                            f"  S-frame: {types.get(frame_type, 'Unknown')}, N(R)={nr}, P/F={pf}",
                            level=5,
                        )
                    else:
                        # U-frame
                        print_debug(
                            f"  U-frame: control=0x{control:02x}", level=5
                        )
                except Exception:
                    pass

            # U-frame UA: mark link established
            if control is not None and control == UA_CONTROL:
                # Verify this UA is from the station we're connecting to
                ua_source = parsed.get("src") if parsed else None
                if self._pending_connect and ua_source:
                    if ua_source == self._pending_connect:
                        # Valid UA from expected station
                        self._link_established = True
                        self._link_event.set()
                        try:
                            self.radio.tnc_link_established = True
                            self.radio.tnc_connected_callsign = (
                                self._pending_connect
                            )
                        except Exception:
                            pass
                        if self.verbose:
                            print_info(
                                f"AX25Adapter: Received UA from {ua_source} - connection established"
                            )
                    else:
                        # UA from unexpected station - ignore
                        if self.verbose:
                            print_debug(
                                f"AX25Adapter: Ignoring UA from {ua_source} (expecting {self._pending_connect})",
                                level=5,
                            )
                elif not self._pending_connect:
                    # Not waiting for a connection, but received UA anyway
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Received UA from {ua_source} but not in connecting state",
                            level=5,
                        )
                else:
                    # Can't determine source
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Received UA with unknown source",
                            level=5,
                        )

            # U-frame DISC: remote station wants to disconnect
            if control is not None and control == DISC_CONTROL:
                remote_call = parsed.get("src")
                if self.verbose:
                    print_debug(
                        f"AX25Adapter: Remote station {remote_call} sent DISC",
                        level=3,
                    )
                # Send UA to acknowledge disconnect
                try:
                    src = self._src_call or "NOCALL"
                    if remote_call:
                        ua_frame = build_ua(src, remote_call, [])
                        # DON'T add FCS - the radio adds it automatically!
                        kiss = wrap_kiss(ua_frame, port=0)
                        print_debug(
                            f"AX25Adapter.handle_incoming: sending UA in response to DISC from {remote_call}",
                            level=3,
                        )
                        await self.radio.write_kiss_frame(kiss)
                        if self._frame_debug_cb:
                            self._frame_debug_cb("tx", kiss)
                except Exception:
                    pass
                # Mark link as disconnected and clear connection state
                self._link_established = False
                self._pending_connect = None
                # Reset sequence numbers
                self._ns = 0
                self._nr = 0
                # Clear TX queue
                async with self._tx_lock:
                    self._tx_queue.clear()
                # Stop TX worker
                if self._tx_task:
                    try:
                        self._tx_task.cancel()
                    except Exception:
                        pass
                    self._tx_task = None
                try:
                    self.radio.tnc_link_established = False
                except Exception:
                    pass
                print_info(f"AX25Adapter: Disconnected from {remote_call}")

            # U-frame DM: remote station is in disconnected mode
            if control is not None and control == DM_CONTROL:
                remote_call = parsed.get("src")
                print_info(
                    f"Remote station {remote_call} sent DM (Disconnected Mode)"
                )
                # Clear connection state
                self._link_established = False
                self._pending_connect = None
                self._ns = 0
                self._nr = 0
                async with self._tx_lock:
                    self._tx_queue.clear()
                if self._tx_task:
                    try:
                        self._tx_task.cancel()
                    except Exception:
                        pass
                    self._tx_task = None
                try:
                    self.radio.tnc_link_established = False
                except Exception:
                    pass

            # U-frame FRMR: remote station rejected our frame (protocol error)
            if control is not None and control == FRMR_CONTROL:
                # Extract FRMR info field (3 bytes) for analysis
                frmr_info = parsed.get("info", b"")
                print_debug(
                    f"AX25Adapter: Received FRMR (Frame Reject) from {parsed.get('src')}",
                    level=2,
                )
                print_debug(
                    "  This indicates a serious protocol error - link may be unstable",
                    level=2,
                )
                if len(frmr_info) >= 3:
                    print_debug(
                        f"  FRMR info bytes: {frmr_info[:3].hex()}", level=2
                    )
                    print_debug(
                        f"    Rejected control: 0x{frmr_info[0]:02x}", level=2
                    )
                    print_debug(
                        f"    Byte 1: 0x{frmr_info[1]:02x} (V(S), V(R), C/R)",
                        level=2,
                    )
                    print_debug(
                        f"    Byte 2: 0x{frmr_info[2]:02x} (reason flags)",
                        level=2,
                    )
                    # Decode byte 2 reason flags
                    reasons = []
                    if frmr_info[2] & 0x01:
                        reasons.append("W: Invalid command/response")
                    if frmr_info[2] & 0x02:
                        reasons.append("X: Invalid I-field")
                    if frmr_info[2] & 0x04:
                        reasons.append("Y: I-field too long")
                    if frmr_info[2] & 0x08:
                        reasons.append("Z: Invalid N(R) received")
                    if reasons:
                        print_debug(
                            f"    Reasons: {', '.join(reasons)}", level=2
                        )
                    else:
                        print_debug(
                            f"    No standard reason flags - possible implementation incompatibility",
                            level=2,
                        )

                    # Decode byte 1 to see remote's state
                    remote_vs = (frmr_info[1] >> 1) & 0x07  # Remote's V(S)
                    remote_vr = (frmr_info[1] >> 5) & 0x07  # Remote's V(R)
                    print_debug(
                        f"    Remote state: V(S)={remote_vs}, V(R)={remote_vr}",
                        level=2,
                    )
                    print_debug(
                        f"    Our state: V(S)={self._ns}, V(R)={self._nr}",
                        level=2,
                    )
                else:
                    print_debug(
                        "  Remote station detected a protocol error in our frame",
                        level=2,
                    )

                # Clear the transmit queue and reset sequence numbers to try recovery
                async with self._tx_lock:
                    if len(self._tx_queue) > 0:
                        print_debug(
                            f"AX25Adapter: Clearing {len(self._tx_queue)} pending frame(s) after FRMR",
                            level=2,
                        )
                        self._tx_queue.clear()
                    # Try to resync by resetting our send sequence to what remote expects
                    if len(frmr_info) >= 2:
                        remote_vr = (frmr_info[1] >> 5) & 0x07
                        if remote_vr != self._ns:
                            print_debug(
                                f"AX25Adapter: Resyncing N(S) from {self._ns} to {remote_vr} to match remote expectations",
                                level=2,
                            )
                            self._ns = remote_vr

            # S-frame: RR, RNR, REJ, SREJ (acknowledgment or flow control)
            # S-frames have bits 0-1 = 01
            if control is not None and (control & 0x03) == 0x01:
                # Only process S-frames if we're actually connected
                if not self._link_established:
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Ignoring S-frame while not connected (from {parsed.get('src', 'unknown')})",
                            level=5,
                        )
                    return

                # Verify the S-frame is from the station we're connected to
                frame_src = parsed.get("src", "")
                if (
                    self._pending_connect
                    and frame_src != self._pending_connect
                ):
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Ignoring S-frame from {frame_src} (connected to {self._pending_connect})",
                            level=5,
                        )
                    return

                frame_type = (control >> 2) & 0x03
                nr = (control >> 5) & 0x07
                pf = bool(control & 0x10)
                type_names = {0: "RR", 1: "RNR", 2: "REJ", 3: "SREJ"}
                frame_name = type_names.get(frame_type, "S-frame")

                if self.verbose:
                    print_debug(
                        f"AX25Adapter: Received {frame_name} N(R)={nr} P/F={pf}",
                        level=5,
                    )

                # Handle REJ (Reject) - remote is requesting retransmit
                if frame_type == 2:  # REJ
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Received REJ - remote requesting retransmit from N(S)={nr}",
                            level=5,
                        )
                    async with self._tx_lock:
                        # First, remove frames that were acknowledged (N(S) < nr)
                        self._tx_queue = [
                            f
                            for f in self._tx_queue
                            if ((f["ns"] - nr) & 0x07)
                            < ((self._ns - nr) & 0x07)
                        ]

                        # Now trigger immediate retransmit of remaining frames
                        # Set sent_at to 0 so _tx_worker will retransmit them immediately
                        for entry in self._tx_queue:
                            entry["sent_at"] = 0
                            entry["retries"] = 0  # Reset retry count
                            if self.verbose:
                                print_debug(
                                    f"AX25Adapter: Marking frame N(S)={entry['ns']} for immediate retransmit",
                                    level=5,
                                )
                    return

                # Handle RNR (Receive Not Ready) - remote is busy
                if frame_type == 1:  # RNR
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Received RNR - remote is busy, pausing transmission",
                            level=5,
                        )
                    # TODO: Should pause transmission until we get RR
                    # For now, just acknowledge frames < nr
                    async with self._tx_lock:
                        before = len(self._tx_queue)
                        self._tx_queue = [
                            f
                            for f in self._tx_queue
                            if ((f["ns"] - nr) & 0x07)
                            < ((self._ns - nr) & 0x07)
                        ]
                        after = len(self._tx_queue)
                        if before != after and self.verbose:
                            print_debug(
                                f"AX25Adapter: Removed {before - after} acknowledged frame(s) after RNR (N(R)={nr})",
                                level=5,
                            )
                    return

                # Handle RR (Receive Ready) - standard acknowledgment
                if frame_type == 0:  # RR
                    async with self._tx_lock:
                        before = len(self._tx_queue)
                        # Remove all frames with N(S) < nr (they've been acknowledged)
                        self._tx_queue = [
                            f
                            for f in self._tx_queue
                            if ((f["ns"] - nr) & 0x07)
                            < ((self._ns - nr) & 0x07)
                        ]
                        after = len(self._tx_queue)
                        if before != after:
                            if self.verbose:
                                print_debug(
                                    f"AX25Adapter: Removed {before - after} acknowledged frame(s) after {frame_name} (N(R)={nr})",
                                    level=5,
                                )

            # Fallback: some radios signal connection via UI text rather than UA.
            # If we see a UI/info payload mentioning 'CONNECTED' from the peer
            # we treat that as link-established as well.
            try:
                if (not self._link_established) and parsed.get("info"):
                    info_bytes = parsed.get("info")
                    src_call = parsed.get("src")
                    if (
                        isinstance(info_bytes, (bytes, bytearray))
                        and src_call == self._pending_connect
                    ):
                        try:
                            txt = info_bytes.decode(
                                "ascii", errors="ignore"
                            ).upper()
                            if "CONNECTED" in txt or "LINK ESTABLISHED" in txt:
                                self._link_established = True
                                self._link_event.set()
                                try:
                                    self.radio.tnc_link_established = True
                                    self.radio.tnc_connected_callsign = (
                                        self._pending_connect
                                    )
                                except Exception:
                                    pass
                                if self.verbose:
                                    print_info(
                                        f"AX25Adapter: Inferred link established from UI for {self._pending_connect}"
                                    )
                        except Exception:
                            pass
            except Exception:
                pass

            # I-frame: process acknowledgement N(R) and deliver info
            if control is not None and (control & 0x01) == 0:
                # Only process I-frames if we're actually connected
                if not self._link_established:
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Ignoring I-frame while not connected (from {parsed.get('src', 'unknown')})",
                            level=5,
                        )
                    return

                # Verify the I-frame is from the station we're connected to
                frame_src = parsed.get("src", "")
                if (
                    self._pending_connect
                    and frame_src != self._pending_connect
                ):
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Ignoring I-frame from {frame_src} (connected to {self._pending_connect})",
                            level=5,
                        )
                    return

                # Standard AX.25: Extract N(S), N(R), and P/F from single control byte
                remote_ns = (control >> 1) & 0x07
                nr = (control >> 5) & 0x07
                pf = bool(control & 0x10)

                if self.verbose:
                    print_debug(
                        f"AX25Adapter: Received I-frame N(S)={remote_ns}, N(R)={nr}, P/F={pf}, Expected N(R)={self._nr}",
                        level=5,
                    )

                # Check for duplicate frame (N(S) < our expected N(R))
                # This can happen if our RR acknowledgment was lost
                if (
                    (remote_ns - self._nr) & 0x07
                ) >= 4:  # N(S) is behind our N(R) (modulo 8)
                    # This is a duplicate - we already processed this frame
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: Duplicate I-frame N(S)={remote_ns} (already processed, expecting {self._nr})",
                            level=5,
                        )
                    # Re-send RR to acknowledge (in case our previous RR was lost)
                    try:
                        src = self._src_call or "NOCALL"
                        dest = (
                            addrs[1]
                            if len(addrs) >= 2
                            else (self._pending_connect or "")
                        )
                        rr_control = (
                            0x01
                            | ((self._nr & 0x07) << 5)
                            | (0x10 if pf else 0)
                        )
                        rr_frame = build_hdlc_uframe(src, dest, [], rr_control)
                        kiss_rr = wrap_kiss(rr_frame, port=0)
                        if self.verbose:
                            print_debug(
                                f"AX25Adapter: Re-sending RR N(R)={self._nr} for duplicate frame",
                                level=5,
                            )
                        # Calculate intelligent delay: avoid colliding with potential multi-frame transmission
                        now = asyncio.get_event_loop().time()
                        time_since_rx = (
                            now - self._last_rx_time
                            if self._last_rx_time > 0
                            else 999
                        )
                        min_wait_from_rx = 1.5  # seconds - enough for large frame + turnaround
                        if time_since_rx < min_wait_from_rx:
                            intelligent_delay = (
                                min_wait_from_rx - time_since_rx
                            )
                            if self.verbose:
                                print_debug(
                                    f"AX25Adapter: waiting {intelligent_delay:.2f}s before RR (avoiding collision)",
                                    level=5,
                                )
                            await asyncio.sleep(intelligent_delay)
                        else:
                            await asyncio.sleep(self.txdelay)

                        # Carrier sense: Check if channel is busy
                        if hasattr(self.radio, "is_channel_busy"):
                            busy = await self.radio.is_channel_busy(
                                max_age=0.2
                            )
                            if busy:
                                if self.verbose:
                                    print_debug(
                                        f"AX25Adapter: Channel busy, delaying duplicate RR",
                                        level=5,
                                    )
                                for _ in range(10):
                                    await asyncio.sleep(0.2)
                                    busy = await self.radio.is_channel_busy(
                                        max_age=0.1
                                    )
                                    if not busy:
                                        break

                        await self.radio.write_kiss_frame(kiss_rr)
                        if self._frame_debug_cb:
                            self._frame_debug_cb("tx", kiss_rr)
                    except Exception as e:
                        print_debug(
                            f"AX25Adapter: Failed to re-send RR: {e}", level=2
                        )
                    # Don't process duplicate data, but do process the N(R) acknowledgment
                    async with self._tx_lock:
                        before = len(self._tx_queue)
                        self._tx_queue = [
                            f
                            for f in self._tx_queue
                            if ((f["ns"] - nr) & 0x07)
                            < ((self._ns - nr) & 0x07)
                        ]
                        after = len(self._tx_queue)
                        if before != after and self.verbose:
                            print_debug(
                                f"AX25Adapter: Removed {before - after} acknowledged frame(s) via duplicate I-frame N(R)",
                                level=5,
                            )
                    return

                # Validate N(S) - must match our expected receive sequence
                if remote_ns != self._nr:
                    # Out of sequence! Send REJ to request retransmit
                    print_debug(
                        f"AX25Adapter: Sequence error! Expected N(S)={self._nr}, got {remote_ns}",
                        level=2,
                    )
                    try:
                        src = self._src_call or "NOCALL"
                        dest = (
                            addrs[1]
                            if len(addrs) >= 2
                            else (self._pending_connect or "")
                        )
                        # REJ control byte: 0x01 (S-frame) | 0x08 (REJ type) | (N(R) << 5)
                        # We're requesting retransmit starting from _nr
                        rej_control = (
                            0x09
                            | ((self._nr & 0x07) << 5)
                            | (0x10 if pf else 0)
                        )
                        rej_frame = build_hdlc_uframe(
                            src, dest, [], rej_control
                        )
                        kiss_rej = wrap_kiss(rej_frame, port=0)
                        print_debug(
                            f"AX25Adapter: Sending REJ requesting N(S)={self._nr} (control=0x{rej_control:02x})",
                            level=2,
                        )
                        # Calculate intelligent delay: avoid colliding with potential multi-frame transmission
                        now = asyncio.get_event_loop().time()
                        time_since_rx = (
                            now - self._last_rx_time
                            if self._last_rx_time > 0
                            else 999
                        )
                        min_wait_from_rx = 1.5  # seconds - enough for large frame + turnaround
                        if time_since_rx < min_wait_from_rx:
                            intelligent_delay = (
                                min_wait_from_rx - time_since_rx
                            )
                            if self.verbose:
                                print_debug(
                                    f"AX25Adapter: waiting {intelligent_delay:.2f}s before REJ (avoiding collision)",
                                    level=5,
                                )
                            await asyncio.sleep(intelligent_delay)
                        else:
                            await asyncio.sleep(self.txdelay)

                        # Carrier sense: Check if channel is busy
                        if hasattr(self.radio, "is_channel_busy"):
                            busy = await self.radio.is_channel_busy(
                                max_age=0.2
                            )
                            if busy:
                                if self.verbose:
                                    print_debug(
                                        f"AX25Adapter: Channel busy, delaying REJ",
                                        level=5,
                                    )
                                for _ in range(10):
                                    await asyncio.sleep(0.2)
                                    busy = await self.radio.is_channel_busy(
                                        max_age=0.1
                                    )
                                    if not busy:
                                        break

                        await self.radio.write_kiss_frame(kiss_rej)
                        if self._frame_debug_cb:
                            self._frame_debug_cb("tx", kiss_rej)
                    except Exception as e:
                        print_debug(
                            f"AX25Adapter: Failed to send REJ: {e}", level=2
                        )
                    # Don't process this frame's data
                    return

                # Validate N(R) - must be within our send window
                # N(R) should acknowledge frames we've actually sent
                async with self._tx_lock:
                    # Check if N(R) is valid (within our sent but unacknowledged frames)
                    oldest_unacked = (
                        self._tx_queue[0]["ns"] if self._tx_queue else self._ns
                    )
                    # N(R) should be in range [oldest_unacked, _ns] (modulo 8)
                    nr_offset = (nr - oldest_unacked) & 0x07
                    ns_offset = (self._ns - oldest_unacked) & 0x07
                    if nr_offset > ns_offset:
                        # Invalid N(R) - acknowledging frames we never sent!
                        print_debug(
                            f"AX25Adapter: Invalid N(R)={nr}, we only sent up to {self._ns}",
                            level=2,
                        )
                        # This is a protocol error - could send FRMR, but just log for now
                        return

                    # Remove acknowledged frames from tx_queue
                    before = len(self._tx_queue)
                    self._tx_queue = [
                        f
                        for f in self._tx_queue
                        if ((f["ns"] - nr) & 0x07) < ((self._ns - nr) & 0x07)
                    ]
                    after = len(self._tx_queue)
                    if before != after:
                        if self.verbose:
                            print_debug(
                                f"AX25Adapter: Removed {before - after} acknowledged frame(s) from tx queue (N(R)={nr})",
                                level=5,
                            )
                        if hasattr(self.radio, "tnc_acked"):
                            try:
                                self.radio.tnc_acked = nr
                            except Exception:
                                pass

                # Extract info field and PID
                if offset < len(payload):
                    pid = payload[offset]
                    info_field = payload[offset + 1 :]
                    # Radio handles FCS, so info_field should not include it
                    parsed["info"] = info_field
                    parsed["pid"] = pid

                # Update receive sequence number (we expect next N(S))
                self._nr = (remote_ns + 1) & 0x07

                # Send RR (Receive Ready) S-frame to acknowledge received I-frame
                # Standard AX.25 S-frame uses SINGLE control byte:
                # Bits 0-1: 01 (S-frame marker)
                # Bits 2-3: 00 (RR type), 10 (RNR), 01 (REJ), 11 (SREJ)
                # Bit 4: P/F (poll/final)
                # Bits 5-7: N(R)
                # Formula: 0x01 | (type << 2) | (pf << 4) | (nr << 5)
                try:
                    src = self._src_call or "NOCALL"
                    dest = (
                        addrs[1]
                        if len(addrs) >= 2
                        else (self._pending_connect or "")
                    )
                    # Build RR control byte: 0x01 (S-frame) | (N(R) << 5)
                    # Use P/F=0 for normal acknowledgments
                    rr_control = 0x01 | ((self._nr & 0x07) << 5)
                    rr_frame = build_hdlc_uframe(src, dest, [], rr_control)
                    # DON'T add FCS - the radio adds it automatically!
                    kiss_rr = wrap_kiss(rr_frame, port=0)
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: sending RR S-frame nr={self._nr} (control=0x{rr_control:02x})",
                            level=5,
                        )
                    # Calculate intelligent delay: longer wait to avoid colliding with multi-frame responses
                    # Use max of TXDELAY or time-since-last-RX to ensure channel is clear
                    now = asyncio.get_event_loop().time()
                    time_since_rx = (
                        now - self._last_rx_time
                        if self._last_rx_time > 0
                        else 999
                    )
                    # Wait at least 1.5 seconds total from last RX to allow large frames to complete
                    # and remote to switch to RX mode
                    min_wait_from_rx = 1.5  # seconds - enough for ~180 byte frame + turnaround
                    if time_since_rx < min_wait_from_rx:
                        intelligent_delay = min_wait_from_rx - time_since_rx
                        if self.verbose:
                            print_debug(
                                f"AX25Adapter: waiting {intelligent_delay:.2f}s before RR (avoiding collision)",
                                level=5,
                            )
                        await asyncio.sleep(intelligent_delay)
                    else:
                        # Already waited long enough, just use standard TXDELAY
                        await asyncio.sleep(self.txdelay)

                    # Carrier sense: Check if channel is busy before transmitting
                    if hasattr(self.radio, "is_channel_busy"):
                        busy = await self.radio.is_channel_busy(max_age=0.2)
                        if busy:
                            if self.verbose:
                                print_debug(
                                    f"AX25Adapter: Channel busy (receiving data), delaying RR transmission",
                                    level=5,
                                )
                            # Wait for channel to clear
                            for _ in range(10):  # Try for up to 2 seconds
                                await asyncio.sleep(0.2)
                                busy = await self.radio.is_channel_busy(
                                    max_age=0.1
                                )
                                if not busy:
                                    break
                            if busy and self.verbose:
                                print_debug(
                                    f"AX25Adapter: Channel still busy, transmitting anyway (timeout)",
                                    level=5,
                                )

                    await self.radio.write_kiss_frame(kiss_rr)
                    try:
                        if self._frame_debug_cb:
                            self._frame_debug_cb("tx", kiss_rr)
                    except Exception:
                        pass
                except Exception as e:
                    if self.verbose:
                        print_debug(
                            f"AX25Adapter: RR send failed: {e}", level=5
                        )
        except Exception:
            pass

        # Invoke all registered callbacks (supports multiple callbacks for shared adapter)
        for cb in self._cb:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(parsed)
                else:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, cb, parsed)
            except Exception as e:
                print_error(f"AX25Adapter.callback failed: {e}")

    async def _tx_worker(self):
        try:
            while True:
                await asyncio.sleep(0.5)
                if not self._link_established:
                    continue
                now = asyncio.get_event_loop().time()

                # RX holdoff: Don't retransmit if we recently received a frame
                # (remote station might be sending a multi-frame response)
                if (
                    self._last_rx_time > 0
                    and (now - self._last_rx_time) < self._rx_holdoff
                ):
                    time_left = self._rx_holdoff - (now - self._last_rx_time)
                    if self.verbose and self._tx_queue:
                        print_debug(
                            f"_tx_worker: RX holdoff active ({time_left:.1f}s remaining)",
                            level=5,
                        )
                    continue

                async with self._tx_lock:
                    for entry in list(self._tx_queue):
                        elapsed = now - entry["sent_at"]

                        # Calculate adaptive timeout with exponential backoff and random jitter
                        # First attempt: base timeout + random jitter
                        # Subsequent attempts: base * (1.5 ^ retries) + random jitter
                        backoff_multiplier = 1.5 ** entry["retries"]
                        jitter = random.uniform(0, self._retransmit_jitter)
                        adaptive_timeout = (
                            self._retransmit_timeout * backoff_multiplier
                        ) + jitter

                        if elapsed >= adaptive_timeout:
                            if entry["retries"] >= self._max_retries:
                                # drop frame
                                print_debug(
                                    f"_tx_worker: dropping frame N(S)={entry['ns']} after {entry['retries']} retries",
                                    level=5,
                                )
                                try:
                                    self._tx_queue.remove(entry)
                                except Exception:
                                    pass
                                continue
                            # retransmit
                            print_debug(
                                f"_tx_worker: retransmitting N(S)={entry['ns']} (elapsed={elapsed:.1f}s >= {adaptive_timeout:.1f}s, retry#{entry['retries']+1})",
                                level=5,
                            )

                            # Hardware workaround: Cycle squelch to unstick TX queue
                            # This addresses a radio bug where transmissions get stuck
                            await self._cycle_squelch_workaround()

                            # Carrier sense: Check if channel is busy before retransmitting
                            if hasattr(self.radio, "is_channel_busy"):
                                busy = await self.radio.is_channel_busy(
                                    max_age=0.2
                                )
                                if busy:
                                    if self.verbose:
                                        print_debug(
                                            f"_tx_worker: Channel busy, delaying retransmit",
                                            level=5,
                                        )
                                    # Wait for channel to clear (up to 2 seconds)
                                    for _ in range(10):
                                        await asyncio.sleep(0.2)
                                        busy = (
                                            await self.radio.is_channel_busy(
                                                max_age=0.1
                                            )
                                        )
                                        if not busy:
                                            break
                                    if busy and self.verbose:
                                        print_debug(
                                            f"_tx_worker: Channel still busy, retransmitting anyway (timeout)",
                                            level=5,
                                        )

                            try:
                                await self.radio.write_kiss_frame(entry["raw"])
                                try:
                                    if self._frame_debug_cb:
                                        self._frame_debug_cb(
                                            "tx", entry["raw"]
                                        )
                                except Exception:
                                    pass
                                entry["retries"] += 1
                                entry["sent_at"] = now
                            except Exception as e:
                                print_error(
                                    f"AX25Adapter retransmit failed: {e}"
                                )
        except asyncio.CancelledError:
            return
        except Exception as e:
            print_error(f"AX25Adapter _tx_worker error: {e}")
