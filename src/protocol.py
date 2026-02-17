"""
UV-50PRO Radio Protocol - Encoding/Decoding Functions
"""

import binascii
import struct

from src.constants import *
from src.utils import print_debug, print_error

# KISS TNC framing constants
FEND = 0xC0   # Frame End delimiter
FESC = 0xDB   # Frame Escape
TFEND = 0xDC  # Transposed Frame End (after FESC)
TFESC = 0xDD  # Transposed Frame Escape (after FESC)


def encode_ax25_address(call, is_last=False):
    """Encode a callsign into a 7-byte AX.25 address field.

    Handles SSID parsing, has-been-used (H) bit for digipeater paths,
    and the last-address extension bit.

    Args:
        call: Callsign string, optionally with -SSID and/or trailing '*'
        is_last: If True, set the extension bit marking last address

    Returns:
        7-byte encoded AX.25 address field
    """
    has_been_used = call.endswith('*')
    call = call.rstrip('*')

    if "-" in call:
        callsign, ssid = call.split("-", 1)
        ssid = int(ssid)
    else:
        callsign = call
        ssid = 0

    callsign = callsign.upper().ljust(6)[:6]
    encoded = bytearray([ord(c) << 1 for c in callsign])
    ssid_byte = 0x60 | ((ssid & 0x0F) << 1)

    if has_been_used:
        ssid_byte |= 0x80

    if is_last:
        ssid_byte |= 0x01
    encoded.append(ssid_byte)
    return bytes(encoded)


def build_ax25_address_field(dest, source, path):
    """Build the complete AX.25 address field (dest + source + digipeaters).

    Sets the extension bit on the last address in the field.

    Args:
        dest: Destination callsign
        source: Source callsign
        path: List of digipeater callsigns

    Returns:
        Encoded address field as bytearray
    """
    field = bytearray()
    field.extend(encode_ax25_address(dest, is_last=False))
    is_last = len(path) == 0
    field.extend(encode_ax25_address(source, is_last=is_last))
    for i, digi in enumerate(path):
        is_last = i == len(path) - 1
        field.extend(encode_ax25_address(digi, is_last=is_last))
    return field


def build_message(command_group, is_reply, command_id, body=b""):
    """Build a benlink message."""
    header_value = (command_group << 16) | (int(is_reply) << 15) | command_id
    header_bytes = struct.pack(">I", header_value)
    return header_bytes + body


def parse_message(data):
    """Parse a benlink message."""
    if len(data) < 4:
        return None

    header_value = struct.unpack(">I", data[0:4])[0]

    return {
        "command_group": (header_value >> 16) & 0xFFFF,
        "is_reply": bool((header_value >> 15) & 0x1),
        "command_id": header_value & 0x7FFF,
        "body": data[4:],
    }


def encode_aprs_packet(from_call, to_call, path, message):
    """Encode an APRS message as a KISS frame.

    Builds an AX.25 UI frame with the given addressing and wraps it
    in a KISS data frame (port 0).
    """
    packet = build_ax25_address_field(to_call, from_call, path)
    packet.append(0x03)   # UI control byte
    packet.append(0xF0)   # No Layer 3 PID
    packet.extend(message.encode("ascii"))
    return wrap_kiss(bytes(packet))


def decode_ax25_address(data, offset):
    """Decode a single AX.25 address field (7 bytes)."""
    if len(data) < offset + 7:
        return None, offset, False

    # Extract callsign (6 bytes, shifted right by 1)
    callsign = ""
    for i in range(6):
        c = (data[offset + i] >> 1) & 0x7F
        if c != 0x20:  # Not space
            callsign += chr(c)

    # Extract SSID byte
    ssid_byte = data[offset + 6]
    ssid = (ssid_byte >> 1) & 0x0F

    # Check if this is the last address
    is_last = bool(ssid_byte & 0x01)

    # Check if has been repeated (H-bit)
    has_been_repeated = bool(ssid_byte & 0x80)

    # Format callsign with SSID if not 0
    if ssid > 0:
        call_str = f"{callsign.strip()}-{ssid}"
    else:
        call_str = callsign.strip()

    if has_been_repeated:
        call_str += "*"

    return call_str, offset + 7, is_last


def decode_ax25_packet(data):
    """Decode AX.25 packet into human-readable format."""
    if len(data) < 16:  # Minimum: dest(7) + src(7) + control(1) + pid(1)
        return None

    try:
        offset = 0

        # Decode destination
        dest, offset, is_last = decode_ax25_address(data, offset)
        if dest is None:
            return None

        # Decode source
        src, offset, is_last = decode_ax25_address(data, offset)
        if src is None:
            return None

        # Decode digipeater path
        digis = []
        while not is_last and offset + 7 <= len(data):
            digi, offset, is_last = decode_ax25_address(data, offset)
            if digi:
                digis.append(digi)
            else:
                break

        # Control and PID
        if offset + 2 > len(data):
            return None

        control = data[offset]
        pid = data[offset + 1]
        offset += 2

        # Information field (the actual message)
        info = data[offset:].decode("ascii", errors="replace")

        # Format the output
        if digis:
            path = ",".join(digis)
            result = f"{src}>{dest},{path}: {info}"
        else:
            result = f"{src}>{dest}: {info}"

        return result

    except Exception as e:
        return None


def decode_kiss_aprs(data):
    """Decode KISS/APRS packet for display."""
    if len(data) < 2:
        return "Invalid KISS frame"

    # Skip KISS framing
    if data[0] == 0xC0:
        data = data[1:]
    if len(data) > 0 and data[0] == 0x00:
        data = data[1:]
    if len(data) > 0 and data[-1] == 0xC0:
        data = data[:-1]

    # Un-escape KISS escapes
    unescaped = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0xDB and i + 1 < len(data):
            if data[i + 1] == 0xDC:
                unescaped.append(0xC0)
                i += 2
            elif data[i + 1] == 0xDD:
                unescaped.append(0xDB)
                i += 2
            else:
                unescaped.append(data[i])
                i += 1
        else:
            unescaped.append(data[i])
            i += 1

    # Try to decode as AX.25
    decoded = decode_ax25_packet(bytes(unescaped))
    if decoded:
        return decoded

    # Fallback to simple text display
    try:
        text = ""
        for byte in unescaped:
            if 32 <= byte <= 126:
                text += chr(byte)

        if text:
            return f"{text[:100]}"
        else:
            return f"KISS packet: {len(unescaped)} bytes"
    except Exception:
        return f"KISS packet: {len(unescaped)} bytes"


def decode_bss_settings(payload):
    """Decode BSS/APRS settings."""
    # Response format: [group, cmd, len_hi, len_lo, status, ...data...]
    # BSS data starts at byte 5 (after 5-byte header)
    print_debug(f"decode_bss_settings: received {len(payload)} bytes", level=6)
    print_debug(f"decode_bss_settings: raw payload = {payload.hex()}", level=6)

    # Some radios return 47 bytes (42 data) instead of 51 bytes (46 data)
    min_length = 47  # 5 header + at least 42 data bytes
    if len(payload) < min_length:
        print_error(
            f"BSS payload too short: got {len(payload)} bytes, need at least {min_length}"
        )
        print_error(f"First 10 bytes: {payload[:min(10, len(payload))].hex()}")
        return None

    # Check status byte (byte 4)
    if payload[4] != 0:
        print_error(f"BSS read failed with status code: {payload[4]}")
        return None

    print_debug(f"decode_bss_settings: status OK, parsing data...", level=6)
    # Print all bytes with their positions for analysis
    for i in range(0, len(payload), 10):
        chunk = payload[i : min(i + 10, len(payload))]
        print_debug(
            f"  Bytes {i:2d}-{min(i+9, len(payload)-1):2d}: {chunk.hex()} | {chunk}",
            level=6,
        )

    settings = {}

    # Based on analysis, in 47-byte response format:
    # Byte 3 (in header) might contain SSID
    # The actual data seems compressed/different from 51-byte format

    # Try parsing SSID from byte 3 high nibble (where we see 0x90 with SSID=9)
    ssid_candidate = (payload[3] >> 4) & 0x0F
    print_debug(f"  SSID candidate from byte 3: {ssid_candidate}", level=6)

    # Parse BSS data starting from byte 5 (0-indexed)
    settings["max_fwd_times"] = (payload[5] >> 4) & 0x0F
    settings["time_to_live"] = payload[5] & 0x0F
    settings["ptt_release_send_location"] = bool(payload[6] & 0x80)
    settings["ptt_release_send_id_info"] = bool(payload[6] & 0x40)
    settings["ptt_release_send_bss_user_id"] = bool(payload[6] & 0x20)
    settings["should_share_location"] = bool(payload[6] & 0x10)
    settings["send_pwr_voltage"] = bool(payload[6] & 0x08)
    settings["packet_format"] = (payload[6] >> 2) & 0x01
    settings["allow_position_check"] = bool(payload[6] & 0x02)

    # Use the SSID from byte 3 instead of byte 7 for 47-byte format
    settings["aprs_ssid"] = ssid_candidate

    settings["location_share_interval"] = payload[8] * 10
    settings["bss_user_id_lower"] = struct.unpack("<I", payload[9:13])[0]

    settings["ptt_release_id_info"] = (
        payload[13:25].decode("ascii", errors="ignore").rstrip("\x00")
    )

    # In 47-byte response, the data layout appears compressed:
    # Beacon message is shorter and callsign comes earlier
    if len(payload) >= 47:
        # Based on actual data: callsign is at bytes 41-46 (K1FSY\x00)
        # Symbol appears to be at bytes 38-40
        settings["aprs_symbol"] = (
            payload[38:40].decode("ascii", errors="ignore").rstrip("\x00")
        )
        settings["aprs_callsign"] = (
            payload[41:46].decode("ascii", errors="ignore").rstrip("\x00")
        )
        # Beacon message is before symbol
        settings["beacon_message"] = (
            payload[25:38].decode("ascii", errors="ignore").rstrip("\x00")
        )
    else:
        settings["beacon_message"] = ""
        settings["aprs_symbol"] = ""
        settings["aprs_callsign"] = ""

    # Escape values for safe debug printing (avoid HTML parsing issues)
    safe_callsign = (
        settings["aprs_callsign"].replace("<", "&lt;").replace(">", "&gt;")
    )
    safe_symbol = (
        settings["aprs_symbol"].replace("<", "&lt;").replace(">", "&gt;")
    )
    safe_beacon = (
        settings["beacon_message"].replace("<", "&lt;").replace(">", "&gt;")
    )

    print_debug(f"decode_bss_settings: parsed successfully", level=6)
    print_debug(f"  APRS: {safe_callsign}-{settings['aprs_ssid']}", level=6)
    print_debug(f"  Symbol: {safe_symbol}", level=6)
    print_debug(f"  Beacon: {safe_beacon}", level=6)

    # Store the raw data starting from byte 5 (the actual BSS settings)
    settings["raw_data"] = bytes(payload[5:])
    return settings


def encode_bss_settings(settings):
    """Encode BSS/APRS settings."""
    if "raw_data" not in settings:
        data = bytearray(46)
    else:
        data = bytearray(settings["raw_data"][:46])

    data[0] = ((settings["max_fwd_times"] & 0x0F) << 4) | (
        settings["time_to_live"] & 0x0F
    )

    data[1] = (
        (0x80 if settings["ptt_release_send_location"] else 0)
        | (0x40 if settings["ptt_release_send_id_info"] else 0)
        | (0x20 if settings["ptt_release_send_bss_user_id"] else 0)
        | (0x10 if settings["should_share_location"] else 0)
        | (0x08 if settings["send_pwr_voltage"] else 0)
        | ((settings["packet_format"] & 0x01) << 2)
        | (0x02 if settings["allow_position_check"] else 0)
    )

    data[2] = (settings["aprs_ssid"] & 0x0F) << 4
    data[3] = settings["location_share_interval"] // 10

    struct.pack_into("<I", data, 4, settings["bss_user_id_lower"])

    id_info = settings["ptt_release_id_info"][:12].ljust(12, "\x00")
    data[8:20] = id_info.encode("ascii")

    beacon = settings["beacon_message"][:18].ljust(18, "\x00")
    data[20:38] = beacon.encode("ascii")

    symbol = settings["aprs_symbol"][:2].ljust(2, "\x00")
    data[38:40] = symbol.encode("ascii")

    callsign = settings["aprs_callsign"][:6].ljust(6, "\x00")
    data[40:46] = callsign.encode("ascii")

    return bytes(data)


def decode_channel(payload):
    """Decode channel/RF settings."""
    if len(payload) < 2:
        return None

    data = payload[1:] if payload[0] == 0 else payload

    if len(data) < 25:
        return None

    channel = {}

    # Radio uses 0-based indexing (0-29), convert to 1-based for display (1-30)
    channel["channel_id"] = data[0] + 1

    # TX/RX Frequencies
    tx_bits = struct.unpack(">I", data[1:5])[0]
    channel["tx_mod"] = (tx_bits >> 30) & 0x03
    channel["tx_freq_mhz"] = (tx_bits & 0x3FFFFFFF) / 1e6

    rx_bits = struct.unpack(">I", data[5:9])[0]
    channel["rx_mod"] = (rx_bits >> 30) & 0x03
    channel["rx_freq_mhz"] = (rx_bits & 0x3FFFFFFF) / 1e6

    # Sub-audio
    tx_sub = struct.unpack(">H", data[9:11])[0]
    channel["tx_sub_audio_raw"] = tx_sub
    if tx_sub == 0:
        channel["tx_tone"] = "None"
    elif tx_sub < 6700:
        channel["tx_tone"] = f"D{tx_sub:03d}"
    else:
        channel["tx_tone"] = f"{tx_sub / 100:.1f}"

    rx_sub = struct.unpack(">H", data[11:13])[0]
    channel["rx_sub_audio_raw"] = rx_sub
    if rx_sub == 0:
        channel["rx_tone"] = "None"
    elif rx_sub < 6700:
        channel["rx_tone"] = f"D{rx_sub:03d}"
    else:
        channel["rx_tone"] = f"{rx_sub / 100:.1f}"

    # Flags
    flags1 = data[13]
    flags2 = data[14]

    channel["tx_at_max_power"] = bool(flags1 & 0x40)
    channel["tx_at_med_power"] = bool(flags1 & 0x02)
    channel["bandwidth"] = 1 if (flags1 & 0x10) else 0
    channel["bandwidth_str"] = "W" if channel["bandwidth"] else "N"
    channel["scan"] = bool(flags1 & 0x80)

    channel["flags1"] = flags1
    channel["flags2"] = flags2

    # Power level string
    if channel["tx_at_max_power"]:
        channel["power"] = "High"
    elif channel["tx_at_med_power"]:
        channel["power"] = "Med"
    else:
        channel["power"] = "Low"

    # Name
    name_bytes = data[15:25]
    null_index = name_bytes.find(b"\x00")
    if null_index >= 0:
        name_bytes = name_bytes[:null_index]
    channel["name"] = name_bytes.decode("ascii", errors="ignore")

    return channel


def encode_channel(channel):
    """Encode channel data for writing."""
    data = bytearray(25)

    # Convert from 1-based display (1-30) to 0-based internal (0-29)
    data[0] = channel["channel_id"] - 1

    # TX Frequency + Modulation
    tx_freq_raw = int(channel["tx_freq_mhz"] * 1e6)
    tx_mod = channel.get("tx_mod", 0)
    tx_bits = ((tx_mod & 0x03) << 30) | (tx_freq_raw & 0x3FFFFFFF)
    data[1:5] = struct.pack(">I", tx_bits)

    # RX Frequency + Modulation
    rx_freq_raw = int(channel["rx_freq_mhz"] * 1e6)
    rx_mod = channel.get("rx_mod", 0)
    rx_bits = ((rx_mod & 0x03) << 30) | (rx_freq_raw & 0x3FFFFFFF)
    data[5:9] = struct.pack(">I", rx_bits)

    # Sub-audio
    data[9:11] = struct.pack(">H", channel.get("tx_sub_audio_raw", 0))
    data[11:13] = struct.pack(">H", channel.get("rx_sub_audio_raw", 0))

    # Flags
    data[13] = channel.get("flags1", 0x40)
    data[14] = channel.get("flags2", 0x00)

    # Name
    name = channel.get("name", "")[:10]
    name_bytes = name.encode("ascii")
    data[15 : 15 + len(name_bytes)] = name_bytes

    return bytes(data)


def decode_settings(payload):
    """Decode radio settings (VFO, scan, squelch, etc)."""
    if len(payload) < 2:
        return None

    data = payload[1:] if payload[0] == 0 else payload

    print_debug(
        f"decode_settings: raw data ({len(data)} bytes) = {data.hex()}",
        level=6,
    )

    if len(data) < 10:
        return None

    settings = {}

    byte0 = data[0]
    byte9 = data[9] if len(data) > 9 else 0

    # Decode channels (0-indexed in memory)
    channel_a_raw = ((byte0 & 0xF0) >> 4) + (byte9 & 0xF0)
    channel_b_raw = (byte0 & 0x0F) + ((byte9 & 0x0F) << 4)

    # Add 1 for display to match radio LCD
    settings["channel_a"] = channel_a_raw + 1
    settings["channel_b"] = channel_b_raw + 1

    print_debug(f"  Byte 0: 0x{byte0:02x}, Byte 9: 0x{byte9:02x}", level=6)
    print_debug(
        f"  VFO A: raw={channel_a_raw} → display={settings['channel_a']}",
        level=6,
    )
    print_debug(
        f"  VFO B: raw={channel_b_raw} → display={settings['channel_b']}",
        level=6,
    )

    # Byte 1 contains various flags
    if len(data) > 1:
        settings["scan"] = bool(data[1] & 0x80)
        settings["aghfp_call_mode"] = bool(data[1] & 0x40)
        settings["double_channel"] = (
            data[1] >> 4
        ) & 0x03  # 0=off, 1=A+B, 2=B+A
        settings["squelch_level"] = data[1] & 0x0F
        print_debug(
            f"  Flags: Scan={settings['scan']}, DualCh={settings['double_channel']}, Squelch={settings['squelch_level']}",
            level=6,
        )

    # Byte 2 contains more flags
    if len(data) > 2:
        settings["tail_elim"] = bool(data[2] & 0x80)
        settings["auto_relay_en"] = bool(data[2] & 0x40)

    # Byte 13: vfo_x determines active VFO (bits 1-2)
    # Direct mapping: 0=VFO A, 1=VFO B (no inversion needed)
    if len(data) > 13:
        settings["vfo_x"] = (data[13] >> 1) & 0x03
        print_debug(
            f"  Byte[13]=0x{data[13]:02x}, vfo_x={settings['vfo_x']} ({'A' if settings['vfo_x'] == 0 else 'B'})",
            level=6,
        )

    settings["raw_data"] = bytes(data)
    print_debug(
        f"  Stored {len(settings['raw_data'])} bytes of raw data", level=6
    )

    return settings


def encode_settings(settings):
    """Encode settings for writing back."""
    if "raw_data" not in settings:
        print_error("Cannot encode settings - no raw_data preserved!")
        return None

    data = bytearray(settings["raw_data"])

    print_debug(f"encode_settings: Starting with {len(data)} bytes", level=6)
    byte9_val = data[9] if len(data) > 9 else 0
    print_debug(
        f"  Original: byte[0]=0x{data[0]:02x}, byte[9]=0x{byte9_val:02x}",
        level=6,
    )

    ch_a_display = settings["channel_a"]
    ch_b_display = settings["channel_b"]

    # Convert from 1-indexed display to 0-indexed memory
    cha = ch_a_display - 1
    chb = ch_b_display - 1

    # Validate ranges
    if cha < 0 or cha > 255:
        print_error(f"VFO A channel {ch_a_display} out of range (1-256)")
        return None
    if chb < 0 or chb > 255:
        print_error(f"VFO B channel {ch_b_display} out of range (1-256)")
        return None

    # Encode channels
    data[0] = ((cha & 0x0F) << 4) | (chb & 0x0F)
    if len(data) > 9:
        data[9] = (cha & 0xF0) | ((chb & 0xF0) >> 4)

    # Encode scan, double_channel, squelch in byte 1
    if len(data) > 1:
        data[1] = (
            (0x80 if settings.get("scan", False) else 0)
            | (0x40 if settings.get("aghfp_call_mode", False) else 0)
            | ((settings.get("double_channel", 0) & 0x03) << 4)
            | (settings.get("squelch_level", 0) & 0x0F)
        )

    # Encode VFO_X (active VFO) in byte 13, bits 1-2
    # Direct mapping: 0=VFO A, 1=VFO B (no inversion needed)
    if len(data) > 13 and "vfo_x" in settings:
        old_byte13 = data[13]
        data[13] = (data[13] & 0xF9) | ((settings["vfo_x"] & 0x03) << 1)
        print_debug(
            f"  VFO_X: {settings['vfo_x']} ({'A' if settings['vfo_x'] == 0 else 'B'}) → byte[13]: 0x{old_byte13:02x} → 0x{data[13]:02x}",
            level=6,
        )

    print_debug(
        f"  VFO A: display={ch_a_display} → raw={cha} (0x{cha:02x})", level=6
    )
    print_debug(
        f"  VFO B: display={ch_b_display} → raw={chb} (0x{chb:02x})", level=6
    )
    byte9_new = data[9] if len(data) > 9 else 0
    print_debug(
        f"  New: byte[0]=0x{data[0]:02x}, byte[9]=0x{byte9_new:02x}", level=6
    )
    print_debug(
        f"  Full bytes 0-9: {data[:min(10, len(data))].hex()}", level=6
    )

    return bytes(data)


def decode_ht_status(payload):
    """Decode HT status."""
    if len(payload) < 3:
        return None

    data = payload[1:] if payload[0] == 0 else payload

    if len(data) < 2:
        return None

    status = {}
    status["is_power_on"] = bool(data[0] & 0x80)
    status["is_in_tx"] = bool(data[0] & 0x40)
    status["is_sq"] = bool(data[0] & 0x20)  # Squelch open (carrier detected)
    status["is_in_rx"] = bool(data[0] & 0x10)
    status["is_scan"] = bool(data[0] & 0x02)
    status["curr_ch_id_lower"] = data[1] >> 4

    if len(data) >= 4:
        status["curr_channel_id_upper"] = (data[3] & 0x3C) >> 2
        status["curr_ch_id"] = (status["curr_channel_id_upper"] << 4) + status[
            "curr_ch_id_lower"
        ]
        status["rssi"] = data[2] >> 4
    else:
        status["curr_ch_id"] = status["curr_ch_id_lower"]
        status["rssi"] = 0

    return status


def kiss_escape(payload: bytes) -> bytes:
    """Escape FEND/FESC bytes for KISS transmission.

    Replaces:
      FEND (0xC0) -> FESC TFEND (0xDB 0xDC)
      FESC (0xDB) -> FESC TFESC (0xDB 0xDD)
    """
    out = bytearray()
    for b in payload:
        if b == FEND:
            out.append(FESC)
            out.append(TFEND)
        elif b == FESC:
            out.append(FESC)
            out.append(TFESC)
        else:
            out.append(b)
    return bytes(out)


def kiss_unescape(data: bytes) -> bytes:
    """Reverse KISS escape sequences in raw data.

    Replaces:
      FESC TFEND (0xDB 0xDC) -> FEND (0xC0)
      FESC TFESC (0xDB 0xDD) -> FESC (0xDB)
    """
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == FESC and i + 1 < len(data):
            if data[i + 1] == TFEND:
                out.append(FEND)
                i += 2
            elif data[i + 1] == TFESC:
                out.append(FESC)
                i += 2
            else:
                out.append(data[i])
                i += 1
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def wrap_kiss(frame: bytes, port: int = 0) -> bytes:
    """Wrap raw AX.25 frame in a KISS data frame (with escaping).

    KISS frame format: FEND, CMD, <escaped payload>, FEND
    CMD = (port << 4) | 0x00  (0x00 = data frame)
    """
    cmd = (port & 0x0F) << 4
    return bytes([FEND, cmd]) + kiss_escape(frame) + bytes([FEND])


# HDLC / Link-layer helpers (basic, minimal implementation)
# Control field values commonly used for AX.25 link management
SABM_CONTROL = 0x2F
UA_CONTROL = 0x63


def kiss_unwrap(kiss_frame: bytes) -> bytes:
    """Unwrap a KISS frame and return the raw payload (unescaped, without CMD)."""
    if not kiss_frame:
        return b""

    data = bytes(kiss_frame)
    # Strip leading FEND
    if data[0] == FEND:
        data = data[1:]
    if len(data) == 0:
        return b""
    # Strip CMD byte
    data = data[1:]
    # Strip trailing FEND if present
    if len(data) > 0 and data[-1] == FEND:
        data = data[:-1]

    return kiss_unescape(data)


def parse_ax25_addresses_and_control(payload: bytes):
    """Parse AX.25 address fields then return (addresses_list, control_byte, offset).

    Returns (addresses, control, offset) where addresses is list of callsign strings,
    control is the control byte (int) or None, and offset is the index in
    payload after the control byte.

    Supports standard AX.25 modulo-8 (single control byte).
    """
    addresses = []
    offset = 0
    try:
        while offset + 7 <= len(payload):
            call, new_off, is_last = decode_ax25_address(payload, offset)
            if call is None:
                break
            addresses.append(call)
            offset = new_off
            if is_last:
                break

        # control byte should follow addresses
        if offset < len(payload):
            control = payload[offset]
            offset += 1
            return addresses, control, offset
        else:
            return addresses, None, offset

    except Exception:
        return addresses, None, offset


def build_hdlc_uframe(source, dest, path, control_byte):
    """Build a minimal HDLC U-frame (addresses + control byte)."""
    frame = build_ax25_address_field(dest, source, path)
    frame.append(control_byte & 0xFF)
    return bytes(frame)


def build_sabm(source, dest, path=None):
    if path is None:
        path = []
    return build_hdlc_uframe(source, dest, path, SABM_CONTROL)


def build_ua(source, dest, path=None):
    if path is None:
        path = []
    return build_hdlc_uframe(source, dest, path, UA_CONTROL)


def build_iframe(
    source, dest, path=None, info=b"", ns=0, nr=0, pf=0, pid=0xF0
):
    """Build a standard AX.25 modulo-8 I-frame (Information frame).

    Follows the AX.25 v2.0 specification with a single control byte.

    Args:
        source: Source callsign (e.g., "K1ABC" or "K1ABC-1")
        dest: Destination callsign
        path: List of digipeater callsigns (optional)
        info: Information field payload (bytes)
        ns: Send sequence number N(S) [0-7]
        nr: Receive sequence number N(R) [0-7]
        pf: Poll/Final bit (0 or 1)
        pid: Protocol ID (default 0xF0 = no layer 3 protocol)

    Returns:
        Complete I-frame (addresses + control + PID + info, NO FCS)

    Standard Control Field Format (1 octet for modulo-8):
        Bit 0: 0 (I-frame marker)
        Bits 3-1: N(S) send sequence number
        Bit 4: P/F poll/final bit
        Bits 7-5: N(R) receive sequence number
    """
    if path is None:
        path = []

    frame = bytearray(build_hdlc_uframe(source, dest, path, 0x00))
    # build_hdlc_uframe appended a single control byte placeholder; remove it

    if len(frame) > 0 and frame[-1] == 0x00:
        frame = frame[:-1]

    # Standard AX.25 modulo-8 control byte:
    # Bit 0: 0 (I-frame marker)
    # Bits 3-1: N(S) send sequence number
    # Bit 4: P/F poll/final bit
    # Bits 7-5: N(R) receive sequence number
    control = (ns & 0x07) << 1  # N(S) in bits 3-1
    control |= (nr & 0x07) << 5  # N(R) in bits 7-5
    if pf:
        control |= 0x10  # P/F in bit 4
    # Bit 0 is already 0 (I-frame marker)

    frame.append(control)

    # Standard AX.25: PID byte required for I-frames
    frame.append(pid)

    # Info field
    if isinstance(info, str):
        info = info.encode("ascii", errors="replace")
    frame.extend(info)

    # DEBUG: Show what we built
    print_debug(
        f"build_iframe: Final frame ({len(frame)} bytes): {bytes(frame).hex()}",
        level=4,
    )
    print_debug(f"  Addresses: {len(frame)-len(info)-2} bytes", level=5)
    print_debug(
        f"  Control: 0x{control:02x} (N(S)={ns}, N(R)={nr}, P/F={pf})", level=5
    )
    print_debug(f"  PID: 0x{pid:02x}", level=5)
    print_debug(f"  Info: {len(info)} bytes", level=5)

    return bytes(frame)


def crc_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """Compute CRC-16-CCITT (X.25) over data.

    Uses binascii.crc_hqx with initial value 0xFFFF and returns a 16-bit integer.
    The returned value is the CRC (no final XOR applied here) - callers
    should follow AX.25 convention of transmitting the 1's complement if needed.
    """
    crc = binascii.crc_hqx(data, init)
    # Common AX.25 convention is to append the 1's complement of the CRC
    return crc ^ 0xFFFF


def append_fcs(frame: bytes) -> bytes:
    """Append 16-bit FCS (little-endian) to the provided AX.25 frame bytes.

    The function computes CRC-16-CCITT and appends low-byte then high-byte.
    """
    crc = crc_ccitt(frame)
    lo = crc & 0xFF
    hi = (crc >> 8) & 0xFF
    return frame + bytes([lo, hi])
