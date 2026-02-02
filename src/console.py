"""
FSY Packet Console - Command Processor and Main Application
"""

import asyncio
import base64
import gzip
import hashlib
import html
import json
import os
import re
import socket
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import List

from bleak import BleakClient, BleakScanner
from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from src import constants
from src.aprs_manager import APRSManager
from src.ax25_adapter import AX25Adapter
from src.device_id import get_device_identifier
from src.constants import *
from src.digipeater import Digipeater
from src.protocol import (
    build_iframe,
    decode_kiss_aprs,
    kiss_unwrap,
    parse_ax25_addresses_and_control,
    wrap_kiss,
)
from src.radio import RadioController
from src.tnc_bridge import TNCBridge
from src.utils import *
from src.web_server import WebServer


def sanitize_for_xml(text: str) -> str:
    """Sanitize text for XML/HTML display by escaping HTML entities and filtering invalid XML characters.

    Valid XML 1.0 characters are:
    - Tab (0x09), LF (0x0A), CR (0x0D)
    - Printable characters (0x20 and above)

    Args:
        text: Input string to sanitize

    Returns:
        Sanitized string safe for XML/HTML parsing
    """
    # First escape HTML entities (&, <, >, etc.)
    escaped = html.escape(text)

    # Then filter out characters that are invalid in XML
    # Valid XML chars: \x09 (tab), \x0A (LF), \x0D (CR), and \x20-\uD7FF
    valid_chars = []
    for char in escaped:
        code = ord(char)
        if code == 0x09 or code == 0x0A or code == 0x0D or (code >= 0x20 and code <= 0xD7FF):
            valid_chars.append(char)
        else:
            # Replace invalid chars with '.'
            valid_chars.append('.')

    return ''.join(valid_chars)


def decode_ax25_address_field(data, is_dest=False):
    """
    Decode AX.25 address field (7 bytes).

    Returns dict with callsign, ssid, has_been_repeated, command, last_address
    """
    if len(data) < 7:
        return None

    # Decode callsign (6 bytes, shifted right by 1)
    # Filter to printable ASCII only (space to ~)
    callsign = ''.join(
        chr(b >> 1) if 32 <= (b >> 1) <= 126 else '?'
        for b in data[:6]
    ).strip()

    # Decode SSID byte
    ssid_byte = data[6]
    ssid = (ssid_byte >> 1) & 0x0F
    has_been_repeated = bool(ssid_byte & 0x80)  # H-bit
    reserved_bits = (ssid_byte >> 5) & 0x03
    last_address = bool(ssid_byte & 0x01)  # Extension bit

    # Command/Response bits (in reserved field)
    if is_dest:
        command_bit = bool(reserved_bits & 0x02)
    else:
        command_bit = bool(reserved_bits & 0x01)

    return {
        'callsign': callsign,
        'ssid': ssid,
        'full': f"{callsign}-{ssid}" if ssid else callsign,
        'has_been_repeated': has_been_repeated,
        'command': command_bit,
        'last_address': last_address,
    }


def decode_control_field(control):
    """
    Decode AX.25 control byte.

    Returns dict with frame_type, ns, nr, pf, description
    """
    # Check frame type
    if (control & 0x01) == 0:
        # I-frame (Information)
        frame_type = "I"
        ns = (control >> 1) & 0x07
        nr = (control >> 5) & 0x07
        pf = bool(control & 0x10)
        desc = f"I-frame: N(S)={ns}, N(R)={nr}, P/F={pf}"
        return {'type': 'I', 'ns': ns, 'nr': nr, 'pf': pf, 'desc': desc, 'raw': control}
    elif (control & 0x03) == 0x01:
        # S-frame (Supervisory)
        nr = (control >> 5) & 0x07
        pf = bool(control & 0x10)
        s_type = (control >> 2) & 0x03
        s_names = {0: "RR", 1: "RNR", 2: "REJ", 3: "SREJ"}
        s_desc = {0: "Receive Ready", 1: "Receive Not Ready", 2: "Reject", 3: "Selective Reject"}
        name = s_names.get(s_type, 'Unknown')
        desc = f"S-frame: {name} ({s_desc.get(s_type, 'Unknown')}), N(R)={nr}, P/F={pf}"
        return {'type': 'S', 'subtype': name, 'nr': nr, 'pf': pf, 'desc': desc, 'raw': control}
    else:
        # U-frame (Unnumbered)
        pf = bool(control & 0x10)
        u_types = {
            0x2F: "SABM (Set Async Balanced Mode)",
            0x63: "UA (Unnumbered Ack)",
            0x43: "DISC (Disconnect)",
            0x0F: "DM (Disconnected Mode)",
            0x87: "FRMR (Frame Reject)",
            0x03: "UI (Unnumbered Information)"
        }
        desc = f"U-frame: {u_types.get(control, f'Unknown (0x{control:02X})')}, P/F={pf}"
        return {'type': 'U', 'pf': pf, 'desc': desc, 'raw': control}


def decode_aprs_packet_type(info_str, dest_addr=None):
    """
    Decode APRS information field and identify packet type.

    Args:
        info_str: APRS information field
        dest_addr: Destination address (needed for MIC-E decoding)

    Returns dict with type and details
    """
    if not info_str:
        return {'type': 'Empty', 'details': {}}

    first_char = info_str[0] if len(info_str) > 0 else ''

    # APRS Message
    if first_char == ':':
        if len(info_str) >= 11:
            to_call = info_str[1:10].strip()
            message_text = info_str[11:]
            msg_id = None
            if '{' in message_text:
                text, msg_id = message_text.split('{', 1)
                message_text = text
            is_ack = message_text.startswith('ack')
            is_rej = message_text.startswith('rej')
            return {
                'type': 'APRS Message',
                'to': to_call,
                'message': message_text,
                'message_id': msg_id,
                'is_ack': is_ack,
                'is_rej': is_rej
            }

    # Position Reports
    elif first_char in ('!', '=', '@', '/'):
        result = {'type': 'APRS Position'}
        try:
            # Determine offset based on timestamp presence
            offset = 8 if first_char in ('@', '/') else 1

            if len(info_str) >= offset + 19:
                # Parse lat/lon
                lat_str = info_str[offset:offset+8]
                lon_str = info_str[offset+9:offset+18]
                symbol_table = info_str[offset+8] if offset+8 < len(info_str) else '/'
                symbol_code = info_str[offset+18] if offset+18 < len(info_str) else '>'
                comment = info_str[offset+19:].strip() if len(info_str) > offset+19 else ''

                # Parse latitude (DDMMmmN/S)
                lat_deg = int(lat_str[0:2])
                lat_min = float(lat_str[2:7])
                lat_dir = lat_str[7]
                latitude = lat_deg + (lat_min / 60.0)
                if lat_dir in ('S', 's'):
                    latitude = -latitude

                # Parse longitude (DDDMMmmW/E)
                lon_deg = int(lon_str[0:3])
                lon_min = float(lon_str[3:8])
                lon_dir = lon_str[8]
                longitude = lon_deg + (lon_min / 60.0)
                if lon_dir in ('W', 'w'):
                    longitude = -longitude

                result['latitude'] = latitude
                result['longitude'] = longitude
                result['symbol'] = f"{symbol_table}{symbol_code}"
                result['comment'] = comment

                # Check for weather data in comment
                if re.search(r"[cstgrhpPb]\d{2,3}", comment):
                    result['has_weather'] = True

                    # Parse weather fields
                    match = re.search(r"c(\d{3})s(\d{3})", comment)
                    if match:
                        result['wind_dir'] = int(match.group(1))
                        result['wind_speed'] = int(match.group(2))

                    match = re.search(r"g(\d{3})", comment)
                    if match:
                        result['wind_gust'] = int(match.group(1))

                    match = re.search(r"t(-?\d{3})", comment)
                    if match:
                        temp = int(match.group(1))
                        if temp > 200:
                            temp = temp - 256
                        result['temperature'] = temp

                    match = re.search(r"h(\d{2})", comment)
                    if match:
                        humidity = int(match.group(1))
                        result['humidity'] = 100 if humidity == 0 else humidity

                    match = re.search(r"b(\d{5})", comment)
                    if match:
                        result['pressure'] = int(match.group(1)) / 10.0
        except:
            pass
        return result

    # MIC-E
    elif first_char == '`' or first_char == "'":
        result = {'type': 'APRS MIC-E Position', 'format': 'compressed'}

        # Decode MIC-E if we have destination address
        if dest_addr and len(dest_addr) >= 6 and len(info_str) >= 9:
            try:
                # Remove SSID from dest_addr if present
                dest_call = dest_addr.split("-")[0] if "-" in dest_addr else dest_addr

                # Decode latitude from destination address
                lat_digits = []
                msg_bits = []

                for ch in dest_call[:6]:
                    if "0" <= ch <= "9":
                        lat_digits.append(ch)
                        msg_bits.append(0)
                    elif "A" <= ch <= "J":
                        lat_digits.append(str(ord(ch) - ord("A")))
                        msg_bits.append(1)
                    elif "P" <= ch <= "Y":
                        lat_digits.append(str(ord(ch) - ord("P")))
                        msg_bits.append(1)
                    elif ch in ("K", "L", "Z"):
                        lat_digits.append("0")  # Space = zero
                        msg_bits.append(0 if ch == "L" else 1)

                if len(lat_digits) == 6 and len(msg_bits) >= 6:
                    # Decode latitude
                    lat_str = "".join(lat_digits)
                    lat_deg = int(lat_str[0:2])
                    lat_min = float(lat_str[2:4] + "." + lat_str[4:6])
                    latitude = lat_deg + (lat_min / 60.0)

                    # N/S from message bit 3
                    if msg_bits[3] == 0:
                        latitude = -latitude  # South

                    # Decode longitude from info bytes 1-3
                    lon_deg = ord(info_str[1]) - 28
                    lon_min = ord(info_str[2]) - 28
                    lon_min_frac = ord(info_str[3]) - 28

                    # Longitude offset from message bit 4
                    if msg_bits[4] == 1:
                        lon_deg += 100

                    longitude = lon_deg + ((lon_min + lon_min_frac / 100.0) / 60.0)

                    # E/W from message bit 5
                    if msg_bits[5] == 1:
                        longitude = -longitude  # West

                    # Extract speed and course
                    speed_course = ord(info_str[4]) - 28
                    speed = ((ord(info_str[5]) - 28) * 10) + ((speed_course // 10) % 10)
                    course = ((speed_course % 10) * 100) + (ord(info_str[6]) - 28)

                    # Symbol
                    symbol_code = info_str[7] if len(info_str) > 7 else ">"
                    symbol_table = info_str[8] if len(info_str) > 8 else "/"

                    # Comment (after position data)
                    comment = info_str[9:] if len(info_str) > 9 else ""

                    # Strip type indicator
                    if comment and ord(comment[0]) in (0x20, 0x3E, 0x5D, 0x60, 0x27):
                        comment = comment[1:]

                    # Keep only printable chars
                    comment = "".join(c for c in comment if 0x20 <= ord(c) <= 0x7E)

                    # Decode MIC-E message type from message bits
                    msg_type_bits = (msg_bits[0] << 2) | (msg_bits[1] << 1) | msg_bits[2]
                    msg_types = {
                        0: "Emergency",
                        1: "Priority",
                        2: "Special",
                        3: "Committed",
                        4: "Returning",
                        5: "In Service",
                        6: "En Route",
                        7: "Off Duty"
                    }
                    message_type = msg_types.get(msg_type_bits, f"Unknown ({msg_type_bits})")

                    # Store all decoded fields
                    result['latitude'] = latitude
                    result['longitude'] = longitude
                    result['speed'] = speed
                    result['course'] = course
                    result['symbol'] = f"{symbol_table}{symbol_code}"
                    result['comment'] = comment.strip()
                    result['message_type'] = message_type
                    result['dest_encoded'] = dest_call

            except Exception as e:
                # If decode fails, just show raw data
                result['data'] = info_str[:40]
                result['decode_error'] = str(e)

        return result

    # Weather
    elif first_char == '_':
        result = {'type': 'APRS Weather'}
        try:
            # Parse weather fields from standalone weather report
            match = re.search(r"_(\d{3})/(\d{3})", info_str)
            if match:
                result['wind_dir'] = int(match.group(1))
                result['wind_speed'] = int(match.group(2))

            match = re.search(r"g(\d{3})", info_str)
            if match:
                result['wind_gust'] = int(match.group(1))

            match = re.search(r"t(-?\d{3})", info_str)
            if match:
                temp = int(match.group(1))
                if temp > 200:
                    temp = temp - 256
                result['temperature'] = temp

            match = re.search(r"h(\d{2})", info_str)
            if match:
                humidity = int(match.group(1))
                result['humidity'] = 100 if humidity == 0 else humidity

            match = re.search(r"b(\d{5})", info_str)
            if match:
                result['pressure'] = int(match.group(1)) / 10.0

            match = re.search(r"r(\d{3})", info_str)
            if match:
                result['rain_1h'] = int(match.group(1)) / 100.0

            match = re.search(r"p(\d{3})", info_str)
            if match:
                result['rain_24h'] = int(match.group(1)) / 100.0
        except:
            pass
        return result

    # Telemetry
    elif info_str.startswith('T#'):
        return {'type': 'APRS Telemetry', 'data': info_str}

    # Status
    elif first_char == '>':
        return {'type': 'APRS Status', 'status': info_str[1:]}

    # Object
    elif first_char == ';':
        return {'type': 'APRS Object', 'data': info_str[:40]}

    # Item
    elif first_char == ')':
        return {'type': 'APRS Item', 'data': info_str[:40]}

    # Third-party
    elif first_char == '}':
        return {'type': 'APRS Third-Party', 'data': info_str[:50]}

    else:
        return {'type': 'Unknown APRS', 'data': info_str[:50]}


def format_detailed_frame(frame, index=1):
    """
    Format a frame with detailed Wireshark-style protocol analysis.

    Args:
        frame: FrameHistoryEntry object
        index: Frame number in sequence

    Returns:
        List of HTML-formatted strings for display
    """
    lines = []
    time_str = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]
    direction_color = "green" if frame.direction == "TX" else "cyan"

    # Header
    lines.append(HTML(f"<b>{'=' * 80}</b>"))
    lines.append(HTML(f"<b>Frame {index}: <{direction_color}>{frame.direction}</{direction_color}> {time_str} ({len(frame.raw_bytes)} bytes)</b>"))
    lines.append(HTML(f"<b>{'=' * 80}</b>"))

    raw_bytes = frame.raw_bytes

    # Check KISS framing
    if len(raw_bytes) < 3 or raw_bytes[0] != 0xC0 or raw_bytes[-1] != 0xC0:
        lines.append(HTML("<red>ERROR: Invalid KISS framing</red>"))
        return lines

    # KISS Layer
    kiss_cmd = raw_bytes[1]
    kiss_type = "Data Frame" if kiss_cmd == 0 else f"Command {kiss_cmd}"
    lines.append(HTML(f"\n<cyan><b>KISS Layer</b></cyan>"))
    lines.append(HTML(f"  Command: 0x{kiss_cmd:02X} ({kiss_type})"))

    # AX.25 payload
    ax25_payload = raw_bytes[2:-1]

    if len(ax25_payload) < 16:
        lines.append(HTML("<red>ERROR: AX.25 payload too short</red>"))
        return lines

    # AX.25 Layer
    lines.append(HTML(f"\n<cyan><b>AX.25 Layer</b></cyan>"))

    # Destination
    dest = decode_ax25_address_field(ax25_payload[0:7], is_dest=True)
    if dest:
        lines.append(HTML(f"  Destination: <green><b>{sanitize_for_xml(dest['full'])}</b></green>"))
        lines.append(HTML(f"    Callsign: {sanitize_for_xml(dest['callsign'])}, SSID: {dest['ssid']}, Command: {dest['command']}"))

    # Source
    src = decode_ax25_address_field(ax25_payload[7:14], is_dest=False)
    if src:
        lines.append(HTML(f"  Source: <green><b>{sanitize_for_xml(src['full'])}</b></green>"))
        lines.append(HTML(f"    Callsign: {sanitize_for_xml(src['callsign'])}, SSID: {src['ssid']}, Command: {src['command']}"))

    # Digipeater path
    offset = 14
    digipeaters = []
    while not ax25_payload[offset - 1] & 0x01:
        if offset + 7 > len(ax25_payload):
            break
        digi = decode_ax25_address_field(ax25_payload[offset:offset+7])
        if digi:
            digipeaters.append(digi)
        offset += 7

    if digipeaters:
        lines.append(HTML(f"  Digipeater Path: ({len(digipeaters)} hop{'s' if len(digipeaters) > 1 else ''})"))
        for i, digi in enumerate(digipeaters, 1):
            repeated = "<yellow>*</yellow>" if digi['has_been_repeated'] else ""
            lines.append(HTML(f"    [{i}] <blue>{sanitize_for_xml(digi['full'])}{repeated}</blue> (repeated: {digi['has_been_repeated']})"))
    else:
        lines.append(HTML(f"  Digipeater Path: (none)"))

    # Control
    if offset < len(ax25_payload):
        ctrl = decode_control_field(ax25_payload[offset])
        lines.append(HTML(f"  Control: 0x{ctrl['raw']:02X} - <yellow>{ctrl['desc']}</yellow>"))
        if 'ns' in ctrl and ctrl['ns'] is not None:
            lines.append(HTML(f"    N(S): {ctrl['ns']}"))
        if 'nr' in ctrl and ctrl['nr'] is not None:
            lines.append(HTML(f"    N(R): {ctrl['nr']}"))
        offset += 1

    # PID
    if offset < len(ax25_payload):
        pid = ax25_payload[offset]
        pid_desc = "No layer 3" if pid == 0xF0 else f"Protocol 0x{pid:02X}"
        lines.append(HTML(f"  PID: 0x{pid:02X} ({pid_desc})"))
        offset += 1

    # Information field
    if offset < len(ax25_payload):
        info_bytes = ax25_payload[offset:]
        info_str = info_bytes.decode('ascii', errors='replace').rstrip('\r\n\x00')

        lines.append(HTML(f"\n<cyan><b>Information Field ({len(info_bytes)} bytes)</b></cyan>"))
        lines.append(HTML(f"  <magenta>{sanitize_for_xml(info_str)}</magenta>"))

        # APRS Layer
        dest_call = dest['callsign'] if dest else None
        aprs = decode_aprs_packet_type(info_str, dest_addr=dest_call)
        if aprs:
            lines.append(HTML(f"\n<cyan><b>APRS Layer</b></cyan>"))
            lines.append(HTML(f"  Packet Type: <green><b>{sanitize_for_xml(aprs['type'])}</b></green>"))

            if aprs.get('is_ack'):
                lines.append(HTML(f"  <red><b>>>> ACK MESSAGE <<<</b></red>"))
            if aprs.get('is_rej'):
                lines.append(HTML(f"  <red><b>>>> REJ MESSAGE <<<</b></red>"))

            # Display MIC-E specific fields first
            if aprs['type'] == 'APRS MIC-E Position':
                if 'dest_encoded' in aprs:
                    lines.append(HTML(f"  <b>MIC-E Encoded:</b>"))
                    lines.append(HTML(f"    Destination: <yellow>{sanitize_for_xml(aprs['dest_encoded'])}</yellow> (encodes lat/msg)"))

                if 'message_type' in aprs:
                    lines.append(HTML(f"    Message Type: <green>{sanitize_for_xml(aprs['message_type'])}</green>"))

            # Display position data if present
            if 'latitude' in aprs and 'longitude' in aprs:
                lines.append(HTML(f"  <b>Position:</b>"))
                lines.append(HTML(f"    Latitude:  {aprs['latitude']:.6f}°"))
                lines.append(HTML(f"    Longitude: {aprs['longitude']:.6f}°"))

                # Calculate grid square
                try:
                    from src.aprs_manager import APRSManager
                    grid = APRSManager.latlon_to_maidenhead(aprs['latitude'], aprs['longitude'])
                    lines.append(HTML(f"    Grid Square: <cyan>{grid}</cyan>"))
                except:
                    pass

                if 'symbol' in aprs:
                    lines.append(HTML(f"    Symbol: {sanitize_for_xml(aprs['symbol'])}"))

                # MIC-E speed and course
                if 'speed' in aprs:
                    lines.append(HTML(f"    Speed: <green>{aprs['speed']} mph</green>"))
                if 'course' in aprs:
                    lines.append(HTML(f"    Course: <green>{aprs['course']}°</green>"))

                if 'comment' in aprs and aprs['comment']:
                    # Display comment without weather data if present
                    comment = aprs['comment']
                    if aprs.get('has_weather'):
                        # Strip weather data for cleaner display
                        comment = re.sub(r'[cstgrhpPb]\d{2,5}', '', comment).strip()
                    if comment:
                        lines.append(HTML(f"    Comment: {sanitize_for_xml(comment)}"))

            # Display weather data if present
            if aprs.get('has_weather') or aprs['type'] == 'APRS Weather':
                has_data = False
                wx_lines = []

                if 'temperature' in aprs:
                    wx_lines.append(f"    Temperature: {aprs['temperature']}°F")
                    has_data = True
                if 'wind_speed' in aprs:
                    wind_dir = aprs.get('wind_dir', 0)
                    wx_lines.append(f"    Wind: {wind_dir}° @ {aprs['wind_speed']} mph")
                    has_data = True
                if 'wind_gust' in aprs:
                    wx_lines.append(f"    Gust: {aprs['wind_gust']} mph")
                    has_data = True
                if 'humidity' in aprs:
                    wx_lines.append(f"    Humidity: {aprs['humidity']}%")
                    has_data = True
                if 'pressure' in aprs:
                    wx_lines.append(f"    Pressure: {aprs['pressure']:.1f} mbar")
                    has_data = True
                if 'rain_1h' in aprs:
                    wx_lines.append(f"    Rain (1h): {aprs['rain_1h']:.2f} in")
                    has_data = True
                if 'rain_24h' in aprs:
                    wx_lines.append(f"    Rain (24h): {aprs['rain_24h']:.2f} in")
                    has_data = True

                if has_data:
                    lines.append(HTML(f"  <b>Weather Data:</b>"))
                    for wx_line in wx_lines:
                        lines.append(HTML(wx_line))

            # Display message data
            if 'to' in aprs:
                lines.append(HTML(f"  <b>Message To:</b> {sanitize_for_xml(aprs['to'])}"))
            if 'message' in aprs:
                lines.append(HTML(f"  <b>Message Text:</b> {sanitize_for_xml(aprs['message'])}"))
            if 'message_id' in aprs and aprs['message_id']:
                lines.append(HTML(f"  <b>Message ID:</b> {sanitize_for_xml(aprs['message_id'])}"))

            # Display status
            if 'status' in aprs:
                lines.append(HTML(f"  <b>Status:</b> {sanitize_for_xml(aprs['status'])}"))

            # Display any remaining fields not handled above
            skip_keys = {'type', 'is_ack', 'is_rej', 'latitude', 'longitude', 'symbol',
                        'comment', 'has_weather', 'temperature', 'wind_dir', 'wind_speed',
                        'wind_gust', 'humidity', 'pressure', 'rain_1h', 'rain_24h',
                        'to', 'message', 'message_id', 'status', 'speed', 'course',
                        'message_type', 'dest_encoded', 'format', 'data', 'decode_error'}
            for key, value in aprs.items():
                if key not in skip_keys and value is not None:
                    lines.append(HTML(f"  {key.capitalize()}: {sanitize_for_xml(str(value))}"))

            # Device identification
            device_info = None
            try:
                device_id = get_device_identifier()

                # Try to identify by tocall (destination address) for normal APRS
                if dest_call:
                    device_info = device_id.identify_by_tocall(dest_call)

                # For MIC-E, try to identify by comment suffix
                if not device_info and aprs['type'] == 'APRS MIC-E Position' and 'comment' in aprs:
                    device_info = device_id.identify_by_mice(aprs.get('comment', ''))

                if device_info:
                    lines.append(HTML(f"\n<cyan><b>Device Identification</b></cyan>"))
                    lines.append(HTML(f"  Vendor: <green>{sanitize_for_xml(device_info.vendor)}</green>"))
                    lines.append(HTML(f"  Model: <green>{sanitize_for_xml(device_info.model)}</green>"))
                    if device_info.version:
                        lines.append(HTML(f"  Version: <green>{sanitize_for_xml(device_info.version)}</green>"))
                    if device_info.class_type:
                        lines.append(HTML(f"  Class: <yellow>{sanitize_for_xml(device_info.class_type)}</yellow>"))

                    # Show detection method
                    if aprs['type'] == 'APRS MIC-E Position':
                        lines.append(HTML(f"  <gray>(detected from MIC-E comment suffix)</gray>"))
                    else:
                        lines.append(HTML(f"  <gray>(detected from destination: {sanitize_for_xml(dest_call)})</gray>"))

            except Exception as e:
                # Silently fail device detection - not critical
                pass

    # Hex dump
    lines.append(HTML(f"\n<cyan><b>Hex Dump (AX.25 Payload)</b></cyan>"))
    for i in range(0, len(ax25_payload), 16):
        chunk = ax25_payload[i:i+16]
        offset_str = f"{i:04x}"
        hex_part = ' '.join(f"{b:02x}" for b in chunk).ljust(47)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(HTML(f"  {offset_str}  {hex_part}  {sanitize_for_xml(ascii_part)}"))

    # Full frame
    lines.append(HTML(f"\n<gray>Full KISS Frame (hex): {raw_bytes.hex()}</gray>"))

    return lines


def calculate_hop_count(addresses: List[str]) -> int:
    """Calculate hop count from AX.25 address path.

    Counts the number of digipeaters that have been used (marked with asterisk).
    In AX.25, digipeaters beyond the destination and source (addresses[2:])
    represent the path. Each used digipeater adds 1 hop.

    Args:
        addresses: List of callsigns from AX.25 header (includes destination, source, digipeaters)

    Returns:
        Number of hops (0 = direct RF, higher = more digipeaters used)
    """
    # Direct RF if no digipe path or only dst/src
    if len(addresses) <= 2:
        return 0

    # Count USED digipeaters in path (those marked with asterisk)
    # addresses[2:] contains the digipeater path
    # Only count digipeaters with * (the "used" bit set in AX.25)
    used_count = sum(1 for digi in addresses[2:] if digi.endswith('*'))
    return used_count


@dataclass
class FrameHistoryEntry:
    """Represents a captured frame for debugging."""

    timestamp: datetime
    direction: str  # 'RX' or 'TX'
    raw_bytes: bytes
    frame_number: int  # Sequential frame number

    def format_hex(self) -> str:
        """Format frame as hex dump."""
        hex_str = self.raw_bytes.hex()
        # Format as groups of 2 hex chars (bytes)
        formatted = " ".join(
            hex_str[i : i + 2] for i in range(0, len(hex_str), 2)
        )
        return formatted

    def format_ascii(self, chunk: bytes) -> str:
        """Format bytes as ASCII (printable chars or dots)."""
        return "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)

    def format_hex_lines(self) -> List[str]:
        """Format frame as hex editor style lines (hex + ASCII)."""
        lines = []
        for i in range(0, len(self.raw_bytes), 16):
            chunk = self.raw_bytes[i : i + 16]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            # Pad hex part to align ASCII column (16 bytes * 3 chars - 1 space = 47 chars)
            hex_part = hex_part.ljust(47)
            ascii_part = self.format_ascii(chunk)
            lines.append(f"  {hex_part}  {ascii_part}")
        return lines


class FrameHistory:
    """Tracks recent frames for debugging."""

    # File path for persistent storage
    BUFFER_FILE = os.path.expanduser("~/.console_frame_buffer.json.gz")
    AUTO_SAVE_INTERVAL = 100  # Save every N frames

    def __init__(self, max_size_mb: int = 10, buffer_mode: bool = True):
        self.buffer_mode = buffer_mode  # True = MB-based, False = simple 10-frame buffer
        self.frame_counter = 0  # Global frame counter (never resets)
        self.frames_since_save = 0  # Track frames added since last save

        if buffer_mode:
            self.frames = deque()  # No maxlen
            self.max_size_bytes = max_size_mb * 1024 * 1024  # Convert MB to bytes
            self.current_size_bytes = 0
        else:
            # Simple mode: just keep last 10 frames
            self.frames = deque(maxlen=10)
            self.max_size_bytes = 0
            self.current_size_bytes = 0

        # Note: load_from_disk() called explicitly after creation to display load info

    def add_frame(self, direction: str, raw_bytes: bytes):
        """Add a frame to history.

        Args:
            direction: 'RX' or 'TX'
            raw_bytes: Complete KISS frame bytes
        """
        self.frame_counter += 1
        entry = FrameHistoryEntry(
            timestamp=datetime.now(),
            direction=direction,
            raw_bytes=raw_bytes,
            frame_number=self.frame_counter
        )
        self.frames.append(entry)

        if self.buffer_mode:
            self.current_size_bytes += len(raw_bytes)
            # Remove old frames if we exceed size limit
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)

        # Auto-save periodically
        self.frames_since_save += 1
        if self.frames_since_save >= self.AUTO_SAVE_INTERVAL:
            self.save_to_disk()
            self.frames_since_save = 0

    def get_recent(self, count: int = None) -> List[FrameHistoryEntry]:
        """Get recent frames.

        Args:
            count: Number of frames to return (None = all)

        Returns:
            List of frames (most recent last)
        """
        if count is None:
            return list(self.frames)
        else:
            # Return last N frames
            return list(self.frames)[-count:]

    def get_by_number(self, frame_number: int) -> FrameHistoryEntry:
        """Get a specific frame by its number.

        Args:
            frame_number: Frame number to retrieve

        Returns:
            FrameHistoryEntry or None if not found
        """
        for frame in self.frames:
            if frame.frame_number == frame_number:
                return frame
        return None

    def set_buffer_mode(self, buffer_mode: bool, size_mb: int = 10):
        """Switch between buffer modes.

        Args:
            buffer_mode: True = MB-based, False = simple 10-frame
            size_mb: Size in MB for buffer mode
        """
        self.buffer_mode = buffer_mode

        if buffer_mode:
            # Convert to MB-based mode
            self.max_size_bytes = size_mb * 1024 * 1024
            # Recreate deque without maxlen
            old_frames = list(self.frames)
            self.frames = deque()
            for frame in old_frames:
                self.frames.append(frame)
            # Calculate current size
            self.current_size_bytes = sum(len(f.raw_bytes) for f in self.frames)
            # Trim if needed
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)
        else:
            # Convert to simple mode
            old_frames = list(self.frames)[-10:]  # Keep last 10
            self.frames = deque(old_frames, maxlen=10)
            self.max_size_bytes = 0
            self.current_size_bytes = 0

    def set_max_size_mb(self, size_mb: int):
        """Change buffer size limit.

        Args:
            size_mb: New size in MB
        """
        if self.buffer_mode:
            self.max_size_bytes = size_mb * 1024 * 1024
            # Trim if needed
            while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                removed = self.frames.popleft()
                self.current_size_bytes -= len(removed.raw_bytes)

    def save_to_disk(self):
        """Save frame buffer to disk (compressed JSON format).

        Saves frames to ~/.console_frame_buffer.json.gz for persistence
        across restarts.
        """
        try:
            # Serialize frames to JSON-compatible format
            frames_data = []
            for frame in self.frames:
                frames_data.append({
                    'timestamp': frame.timestamp.isoformat(),
                    'direction': frame.direction,
                    'raw_bytes': base64.b64encode(frame.raw_bytes).decode('ascii'),
                    'frame_number': frame.frame_number
                })

            data = {
                'frame_counter': self.frame_counter,
                'buffer_mode': self.buffer_mode,
                'max_size_mb': self.max_size_bytes // (1024 * 1024) if self.buffer_mode else 0,
                'frames': frames_data,
                'saved_at': datetime.now().isoformat()
            }

            # Write compressed JSON
            temp_file = self.BUFFER_FILE + ".tmp"
            with gzip.open(temp_file, 'wt', encoding='utf-8') as f:
                json.dump(data, f)

            # Atomic rename
            os.replace(temp_file, self.BUFFER_FILE)

        except Exception as e:
            # Silently fail - don't disrupt operation if save fails
            # Could add optional logging here
            pass

    def load_from_disk(self) -> dict:
        """Load frame buffer from disk if available.

        Restores frames from ~/.console_frame_buffer.json.gz to maintain
        debugging history across restarts.

        Returns:
            dict with keys: loaded (bool), frame_count (int), start_frame (int),
            file_size_kb (float), corrupted_frames (int)
        """
        result = {
            'loaded': False,
            'frame_count': 0,
            'start_frame': 0,
            'file_size_kb': 0.0,
            'corrupted_frames': 0
        }

        if not os.path.exists(self.BUFFER_FILE):
            return result

        try:
            # Get file size before loading
            result['file_size_kb'] = os.path.getsize(self.BUFFER_FILE) / 1024

            with gzip.open(self.BUFFER_FILE, 'rt', encoding='utf-8') as f:
                data = json.load(f)

            # Restore frame counter (important to maintain sequential numbering)
            self.frame_counter = data.get('frame_counter', 0)

            # Restore frames
            corrupted = 0
            for frame_data in data.get('frames', []):
                try:
                    entry = FrameHistoryEntry(
                        timestamp=datetime.fromisoformat(frame_data['timestamp']),
                        direction=frame_data['direction'],
                        raw_bytes=base64.b64decode(frame_data['raw_bytes']),
                        frame_number=frame_data['frame_number']
                    )
                    self.frames.append(entry)

                    # Update size tracking for buffer mode
                    if self.buffer_mode:
                        self.current_size_bytes += len(entry.raw_bytes)

                except Exception:
                    # Skip corrupted frames but continue loading others
                    corrupted += 1
                    continue

            # Trim to current buffer size if needed
            trimmed = 0
            if self.buffer_mode:
                while self.current_size_bytes > self.max_size_bytes and len(self.frames) > 1:
                    removed = self.frames.popleft()
                    self.current_size_bytes -= len(removed.raw_bytes)
                    trimmed += 1

            # Calculate start frame number (lowest frame in buffer)
            start_frame = self.frames[0].frame_number if self.frames else self.frame_counter

            result['loaded'] = True
            result['frame_count'] = len(self.frames)
            result['start_frame'] = start_frame
            result['corrupted_frames'] = corrupted

            return result

        except Exception as e:
            # If load fails (corrupted file, format change, etc.), start fresh
            # Don't disrupt startup - just start with empty buffer
            self.frames.clear()
            self.frame_counter = 0
            self.current_size_bytes = 0
            return result


class TNCConfig:
    """TNC-2 style configuration management."""

    def __init__(self, config_file=None):
        # Default to user's home directory
        if config_file is None:
            config_file = os.path.expanduser("~/.tnc_config.json")

        self.config_file = config_file
        self.legacy_file = "tnc_config.json"  # Old location in project directory

        self.settings = {
            "MYCALL": "NOCALL",
            "MYALIAS": "",
            "MYLOCATION": "",  # Maidenhead grid square (2-10 chars) for manual position
            "UNPROTO": "CQ",
            "DIGIPEAT": "OFF",
            "MONITOR": "ON",
            "DEBUGFRAMES": "OFF",
            "TXDELAY": "30",
            "PERSIST": "63",
            "SLOTTIME": "10",
            "TXTAIL": "10",
            "DUPLEX": "0",
            "MAXFRAME": "7",
            "FRACK": "3000",
            "PACLEN": "128",
            "USERS": "1",
            "RETRY": "3",
            "RETRY_FAST": "20",  # Fast retry timeout (seconds) for non-digipeated messages
            "RETRY_SLOW": "600",  # Slow retry timeout (seconds) for digipeated but not ACKed - 10 minutes
            "AUTO_ACK": "ON",  # Automatic ACK for APRS messages with IDs
            "BEACON": "OFF",
            "BEACON_INTERVAL": "10",
            "BEACON_PATH": "WIDE1-1",
            "BEACON_SYMBOL": "/[",
            "BEACON_COMMENT": "FSY Packet Console",
            "LAST_BEACON": "",  # Timestamp of last beacon sent (ISO format)
            "DEBUG_BUFFER": "10",  # Frame history buffer size in MB (or "OFF" for simple 10-frame mode)
            "AGWPE_HOST": "0.0.0.0",  # AGWPE bind address (0.0.0.0=all, 127.0.0.1=localhost)
            "AGWPE_PORT": "8000",  # AGWPE-compatible server port
            "TNC_HOST": "0.0.0.0",    # TNC bridge bind address (0.0.0.0=all, 127.0.0.1=localhost)
            "TNC_PORT": "8001",    # TNC TCP bridge port
            "WEBUI_HOST": "0.0.0.0",  # Web UI bind address (0.0.0.0=all, 127.0.0.1=localhost)
            "WEBUI_PORT": "8002",  # Web UI HTTP server port
            "WX_ENABLE": "OFF",  # Enable weather station integration
            "WX_BACKEND": "ecowitt",  # Backend type: ecowitt, davis, ambient, etc.
            "WX_ADDRESS": "",  # IP address (http) or serial port path (serial)
            "WX_PORT": "",  # Port number for network stations (blank = auto)
            "WX_INTERVAL": "300",  # Update interval in seconds (300 = 5 minutes)
            "WX_AVERAGE_WIND": "ON",  # Average wind over beacon interval (ON/OFF)
        }
        self.load()

    def load(self):
        """Load configuration from file, with migration from legacy location."""
        try:
            # Check if we need to migrate from old location
            if not os.path.exists(self.config_file) and os.path.exists(self.legacy_file):
                print_info(f"Migrating config from {self.legacy_file} to {self.config_file}")
                try:
                    # Copy the file to new location
                    import shutil
                    shutil.copy2(self.legacy_file, self.config_file)
                    print_info(f"Migration complete. You can safely delete {self.legacy_file}")
                except Exception as e:
                    print_error(f"Could not migrate config file: {e}")
                    print_info(f"Will use legacy file at {self.legacy_file}")
                    # Fall back to legacy file
                    self.config_file = self.legacy_file

            # Load from config file
            if os.path.exists(self.config_file):
                with open(self.config_file, "r") as f:
                    saved = json.load(f)
                    self.settings.update(saved)
                print_debug(
                    f"Loaded TNC config from {self.config_file}", level=6
                )
        except Exception as e:
            print_debug(f"Could not load TNC config: {e}", level=6)

    def save(self):
        """Save configuration to file."""
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.settings, f, indent=2)
            print_debug(f"Saved TNC config to {self.config_file}", level=6)
        except Exception as e:
            print_error(f"Could not save TNC config: {e}")

    def set(self, key, value):
        """Set a configuration value."""
        key = key.upper()
        if key in self.settings:
            # Validate MYLOCATION (Maidenhead grid square)
            if key == "MYLOCATION" and value:
                from src.aprs_manager import APRSManager
                try:
                    # Test if valid grid square by converting to lat/lon
                    lat, lon = APRSManager.maidenhead_to_latlon(value)
                    # Verify we got sensible coordinates
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        print_error(f"Invalid grid square '{value}': coordinates out of range")
                        return False
                    print_info(f"MYLOCATION set to {value.upper()} ({lat:.6f}, {lon:.6f})")
                except ValueError as e:
                    print_error(f"Invalid grid square '{value}': {e}")
                    return False
                # Store in uppercase
                value = value.upper()

            # Validate port numbers
            if key in ["AGWPE_PORT", "TNC_PORT", "WEBUI_PORT"]:
                try:
                    port = int(value)
                    if not (1 <= port <= 65535):
                        print_error(f"Invalid port '{value}': must be between 1 and 65535")
                        return False
                    print_info(f"{key} set to {port} (restart required to take effect)")
                    value = str(port)
                except ValueError:
                    print_error(f"Invalid port '{value}': must be a number")
                    return False

            # Validate weather station backend
            if key == "WX_BACKEND":
                from src.weather_manager import WeatherStationManager
                backends = WeatherStationManager.list_backends()
                if value.lower() not in backends:
                    valid = ', '.join(backends.keys())
                    print_error(f"Invalid backend '{value}'. Valid: {valid}")
                    return False
                value = value.lower()

            # Validate weather station interval
            if key == "WX_INTERVAL":
                try:
                    interval = int(value)
                    if not (30 <= interval <= 3600):
                        print_error(f"Invalid interval '{value}': must be 30-3600 seconds")
                        return False
                except ValueError:
                    print_error(f"Invalid interval '{value}': must be a number")
                    return False

            # Validate weather station port
            if key == "WX_PORT" and value:
                try:
                    port = int(value)
                    if not (1 <= port <= 65535):
                        print_error(f"Invalid port '{value}': must be 1-65535")
                        return False
                except ValueError:
                    print_error(f"Invalid port '{value}': must be a number")
                    return False

            self.settings[key] = value
            self.save()
            return True
        return False

    def get(self, key):
        """Get a configuration value."""
        return self.settings.get(key.upper(), "")

    def display(self):
        """Display all settings."""
        print_header("TNC-2 Configuration")
        for key in sorted(self.settings.keys()):
            value = self.settings[key]
            if value:
                print_pt(HTML(f"<b>{key:12s}</b> {value}"))
            else:
                print_pt(HTML(f"<gray>{key:12s} (not set)</gray>"))
        print_pt("")


class TNCCompleter(Completer):
    """Tab completion for TNC mode commands."""

    def get_completions(self, document, complete_event):
        """Generate completions for TNC commands.

        Args:
            document: Current document (input text)
            complete_event: Completion event

        Yields:
            Completion objects for matching TNC commands
        """
        text = document.text_before_cursor.upper()
        words = text.split()

        # TNC-2 commands
        tnc_commands = [
            "CONNECT",
            "DISCONNECT",
            "CONVERSE",
            "MYCALL",
            "MYALIAS",
            "MYLOCATION",
            "UNPROTO",
            "MONITOR",
            "AUTO_ACK",
            "BEACON",
            "DIGIPEATER",
            "DIGI",
            "RETRY",
            "RETRY_FAST",
            "RETRY_SLOW",
            "DISPLAY",
            "STATUS",
            "RESET",
            "HARDRESET",
            "POWERCYCLE",
            "DEBUGFRAMES",
            "AGWPE_HOST",
            "AGWPE_PORT",
            "TNC_HOST",
            "TNC_PORT",
            "WEBUI_HOST",
            "WEBUI_PORT",
            "WX_ENABLE",
            "WX_BACKEND",
            "WX_ADDRESS",
            "WX_PORT",
            "WX_INTERVAL",
            "WX_AVERAGE_WIND",
            "QUIT",
            "EXIT",
        ]

        if not words or (len(words) == 1 and not text.endswith(" ")):
            word = words[0] if words else ""
            for cmd in tnc_commands:
                if cmd.startswith(word):
                    yield Completion(
                        cmd,
                        start_position=-len(word),
                        display=cmd,
                        display_meta=self._get_tnc_help(cmd),
                    )

    def _get_tnc_help(self, cmd):
        """Get brief help for TNC command.

        Args:
            cmd: TNC command name

        Returns:
            Brief help string
        """
        help_text = {
            "CONNECT": "Connect to station",
            "DISCONNECT": "Disconnect from station",
            "CONVERSE": "Enter conversation mode",
            "MYCALL": "Set/show my callsign",
            "MYALIAS": "Set/show my alias",
            "MYLOCATION": "Set manual position (Maidenhead grid, e.g., FN31pr)",
            "UNPROTO": "Set unproto destination",
            "MONITOR": "Toggle monitor mode",
            "AUTO_ACK": "Auto-acknowledge APRS messages (ON/OFF)",
            "BEACON": "GPS beacon (ON/OFF/INTERVAL/PATH/SYMBOL/COMMENT/NOW)",
            "DIGIPEATER": "Digipeater mode (ON/OFF/STATUS) - repeats direct packets only",
            "DIGI": "Digipeater mode (ON/OFF/STATUS) - short alias",
            "RETRY": "Set max retry attempts (1-10)",
            "RETRY_FAST": "Fast retry timeout in seconds (5-300) for non-digipeated messages",
            "RETRY_SLOW": "Slow retry timeout in seconds (60-86400) for digipeated messages",
            "DISPLAY": "Toggle display mode",
            "STATUS": "Show TNC status",
            "RESET": "Reset TNC settings",
            "HARDRESET": "Hard reset radio",
            "POWERCYCLE": "Power cycle radio",
            "DEBUGFRAMES": "Toggle frame debugging",
            "AGWPE_HOST": "Set AGWPE bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "AGWPE_PORT": "Set AGWPE server port (default: 8000)",
            "TNC_HOST": "Set TNC bridge bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "TNC_PORT": "Set TNC bridge port (default: 8001)",
            "WEBUI_HOST": "Set Web UI bind address (0.0.0.0=all, 127.0.0.1=localhost)",
            "WEBUI_PORT": "Set Web UI port (default: 8002)",
            "WX_ENABLE": "Enable/disable weather station (ON/OFF)",
            "WX_BACKEND": "Set weather station backend (ecowitt, davis, etc.)",
            "WX_ADDRESS": "Set weather station IP or serial port",
            "WX_PORT": "Set weather station port (blank = auto)",
            "WX_INTERVAL": "Set update interval in seconds (30-3600)",
            "WX_AVERAGE_WIND": "Average wind over beacon interval (ON/OFF)",
            "QUIT": "Exit TNC mode",
            "EXIT": "Exit TNC mode",
        }
        return help_text.get(cmd, "")


class CommandCompleter(Completer):
    """Tab completion for radio console commands."""

    def __init__(self, command_processor):
        """Initialize with reference to command processor.

        Args:
            command_processor: CommandProcessor instance to get available commands
        """
        self.command_processor = command_processor

    def get_completions(self, document, complete_event):
        """Generate completions for the current input.

        Args:
            document: Current document (input text)
            complete_event: Completion event

        Yields:
            Completion objects for matching commands
        """
        text = document.text_before_cursor
        words = text.split()

        # If empty or just whitespace, show all commands
        if not words or (len(words) == 1 and not text.endswith(" ")):
            # Completing the first word (command)
            word = words[0] if words else ""

            # Get base commands
            commands = sorted(self.command_processor.commands.keys())

            # Mode-specific filtering
            if self.command_processor.console_mode == "aprs":
                # APRS mode: add APRS subcommands as top-level, hide radio commands
                aprs_subcommands = ["message", "station", "wx"]
                commands = sorted(set(commands + aprs_subcommands))

                # Hide radio-specific commands (keep "radio" for mode switching if BLE)
                radio_commands = ["status", "health", "vfo", "setvfo", "active", "dual",
                                "scan", "squelch", "volume", "channel", "list", "power",
                                "freq", "bss", "setbss", "poweron", "poweroff", "scan_ble",
                                "notifications"]
                commands = [c for c in commands if c not in radio_commands]

                # In serial mode, also hide the "radio" command (can't switch to radio mode)
                if self.command_processor.serial_mode:
                    commands = [c for c in commands if c != "radio"]

            elif self.command_processor.console_mode == "radio":
                # Radio mode: don't show APRS subcommands as top-level (keep "aprs" for mode switching)
                pass  # APRS subcommands stay hidden, full commands shown normally

            # Filter and yield matching commands
            for cmd in commands:
                if cmd.startswith(word.lower()):
                    yield Completion(
                        cmd,
                        start_position=-len(word),
                        display=cmd,
                        display_meta=self._get_command_help(cmd),
                    )

        # Special completion for multi-word commands
        elif len(words) >= 1:
            first_word = words[0].lower()

            # APRS command completions
            if first_word == "aprs":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    # Complete aprs subcommands
                    subcommands = [
                        "message",
                        "msg",
                        "wx",
                        "weather",
                        "position",
                        "pos",
                        "station",
                        "database",
                        "db",
                    ]
                    word = words[1] if len(words) == 2 else ""
                    for sub in subcommands:
                        if sub.startswith(word):
                            yield Completion(
                                sub, start_position=-len(word), display=sub
                            )
                elif len(words) >= 2:
                    subcmd = words[1].lower()
                    if subcmd in ("message", "msg"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete message actions
                            actions = ["read", "send", "clear", "monitor"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3:
                            action = words[2].lower()
                            if action == "monitor":
                                if len(words) == 3 or (
                                    len(words) == 4 and not text.endswith(" ")
                                ):
                                    # Complete monitor subactions
                                    subactions = ["list"]
                                    word = words[3] if len(words) == 4 else ""
                                    for subaction in subactions:
                                        if subaction.startswith(word):
                                            yield Completion(
                                                subaction,
                                                start_position=-len(word),
                                                display=subaction,
                                            )
                    elif subcmd in ("wx", "weather"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete wx actions
                            actions = ["list"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3:
                            # Complete sort options for "aprs wx list"
                            action = words[2].lower()
                            if action == "list":
                                if len(words) == 3 or (
                                    len(words) == 4 and not text.endswith(" ")
                                ):
                                    sort_options = [
                                        "last",
                                        "name",
                                        "temp",
                                        "humidity",
                                        "pressure",
                                    ]
                                    word = words[3] if len(words) == 4 else ""
                                    for option in sort_options:
                                        if option.startswith(word):
                                            # Add descriptive meta text
                                            meta = {
                                                "last": "Most recent first",
                                                "name": "Alphabetically by callsign",
                                                "temp": "Highest temperature first",
                                                "humidity": "Highest humidity first",
                                                "pressure": "Highest pressure first",
                                            }.get(option, "")
                                            yield Completion(
                                                option,
                                                start_position=-len(word),
                                                display=option,
                                                display_meta=meta,
                                            )
                    elif subcmd in ("position", "pos"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete position actions
                            actions = ["list"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                    elif subcmd == "station":
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete station actions
                            actions = ["list", "show"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )
                        elif len(words) >= 3 and words[2].lower() == "show":
                            # Complete with known station callsigns
                            if len(words) == 3 or (
                                len(words) == 4 and not text.endswith(" ")
                            ):
                                word = words[3] if len(words) == 4 else ""
                                stations = (
                                    self.command_processor.aprs_manager.get_all_stations()
                                )
                                for station in stations:
                                    if station.callsign.lower().startswith(
                                        word.lower()
                                    ):
                                        yield Completion(
                                            station.callsign,
                                            start_position=-len(word),
                                            display=station.callsign,
                                        )
                        elif len(words) >= 3 and words[2].lower() == "list":
                            # Complete sort order options for station list
                            if len(words) == 3 or (
                                len(words) == 4 and not text.endswith(" ")
                            ):
                                word = words[3] if len(words) == 4 else ""
                                sort_options = [
                                    (
                                        "name",
                                        "Sort alphabetically by callsign",
                                    ),
                                    (
                                        "packets",
                                        "Sort by packet count (highest first)",
                                    ),
                                    (
                                        "last",
                                        "Sort by last heard (most recent first)",
                                    ),
                                    (
                                        "hops",
                                        "Sort by hop count (direct RF first)",
                                    ),
                                ]
                                for option, meta in sort_options:
                                    if option.startswith(word.lower()):
                                        yield Completion(
                                            option,
                                            start_position=-len(word),
                                            display=option,
                                            display_meta=meta,
                                        )
                    elif subcmd in ("database", "db"):
                        if len(words) == 2 or (
                            len(words) == 3 and not text.endswith(" ")
                        ):
                            # Complete database actions
                            actions = ["clear", "prune"]
                            word = words[2] if len(words) == 3 else ""
                            for action in actions:
                                if action.startswith(word):
                                    yield Completion(
                                        action,
                                        start_position=-len(word),
                                        display=action,
                                    )

            # APRS subcommands as top-level commands (in APRS mode)
            # Handle "message ?", "station ?", etc. when used without "aprs" prefix
            elif self.command_processor.console_mode == "aprs" and first_word in ("message", "msg", "station", "wx", "weather"):
                # Redirect to the same logic as "aprs <subcommand>"
                # Treat first_word as if it were the second word after "aprs"
                subcmd = first_word

                if subcmd in ("message", "msg"):
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete message actions
                        actions = ["read", "send", "clear", "monitor"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                meta = {
                                    "read": "Read messages addressed to you",
                                    "send": "Send APRS message to callsign",
                                    "clear": "Clear read messages",
                                    "monitor": "View all monitored messages"
                                }.get(action, "")
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta=meta,
                                )
                    elif len(words) >= 2:
                        action = words[1].lower()
                        if action == "monitor":
                            if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                                # Complete monitor subactions
                                subactions = ["list"]
                                word = words[2] if len(words) == 3 else ""
                                for subaction in subactions:
                                    if subaction.startswith(word):
                                        yield Completion(
                                            subaction,
                                            start_position=-len(word),
                                            display=subaction,
                                            display_meta="List all monitored messages",
                                        )

                elif subcmd in ("wx", "weather"):
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete wx actions
                        actions = ["list"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta="List weather stations",
                                )
                    elif len(words) >= 2:
                        # Complete sort options for "wx list"
                        action = words[1].lower()
                        if action == "list":
                            if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                                sort_options = ["last", "name", "temp", "humidity", "pressure"]
                                word = words[2] if len(words) == 3 else ""
                                for option in sort_options:
                                    if option.startswith(word):
                                        meta = {
                                            "last": "Most recent first",
                                            "name": "Alphabetically by callsign",
                                            "temp": "Highest temperature first",
                                            "humidity": "Highest humidity first",
                                            "pressure": "Highest pressure first",
                                        }.get(option, "")
                                        yield Completion(
                                            option,
                                            start_position=-len(word),
                                            display=option,
                                            display_meta=meta,
                                        )

                elif subcmd == "station":
                    if len(words) == 1 or (len(words) == 2 and not text.endswith(" ")):
                        # Complete station actions
                        actions = ["list", "show"]
                        word = words[1] if len(words) == 2 else ""
                        for action in actions:
                            if action.startswith(word):
                                meta = {
                                    "list": "List all heard stations",
                                    "show": "Show detailed station info",
                                }.get(action, "")
                                yield Completion(
                                    action,
                                    start_position=-len(word),
                                    display=action,
                                    display_meta=meta,
                                )
                    elif len(words) >= 2 and words[1].lower() == "show":
                        # Complete with known station callsigns
                        if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                            word = words[2] if len(words) == 3 else ""
                            stations = self.command_processor.aprs_manager.get_all_stations()
                            for station in stations:
                                if station.callsign.lower().startswith(word.lower()):
                                    yield Completion(
                                        station.callsign,
                                        start_position=-len(word),
                                        display=station.callsign,
                                    )
                    elif len(words) >= 2 and words[1].lower() == "list":
                        # Complete sort options for "station list"
                        if len(words) == 2 or (len(words) == 3 and not text.endswith(" ")):
                            sort_options = ["last", "name", "packets", "hops"]
                            word = words[2] if len(words) == 3 else ""
                            for option in sort_options:
                                if option.startswith(word):
                                    meta = {
                                        "last": "Most recent first",
                                        "name": "Alphabetically by callsign",
                                        "packets": "Most packets first",
                                        "hops": "Fewest hops first",
                                    }.get(option, "")
                                    yield Completion(
                                        option,
                                        start_position=-len(word),
                                        display=option,
                                        display_meta=meta,
                                    )

            # VFO completions
            elif first_word in ("vfo", "setvfo"):
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    vfos = ["a", "b"]
                    word = words[1] if len(words) == 2 else ""
                    for vfo in vfos:
                        if vfo.startswith(word.lower()):
                            yield Completion(
                                vfo.upper(),
                                start_position=-len(word),
                                display=vfo.upper(),
                            )

            # Power completions
            elif first_word == "power":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    levels = ["high", "medium", "low"]
                    word = words[1] if len(words) == 2 else ""
                    for level in levels:
                        if level.startswith(word.lower()):
                            yield Completion(
                                level, start_position=-len(word), display=level
                            )

            # Debug level completions
            elif first_word == "debug":
                if len(words) == 1 or (
                    len(words) == 2 and not text.endswith(" ")
                ):
                    level_meta = {
                        "0": "Off (no debug output)",
                        "1": "TNC monitor",
                        "2": "Critical errors and events",
                        "3": "Connection state changes",
                        "4": "Frame transmission/reception",
                        "5": "Protocol details, retransmissions",
                        "6": "Everything (BLE, config, hex dumps)",
                        "dump": "Dump frame history",
                        "filter": "Show/set station-specific debug filters",
                    }
                    # Add 'dump' and 'filter' to completions
                    options = ["0", "1", "2", "3", "4", "5", "6", "dump", "filter"]
                    word = words[1] if len(words) == 2 else ""
                    for option in options:
                        if option.startswith(word.lower()):
                            yield Completion(
                                option,
                                start_position=-len(word),
                                display=option,
                                display_meta=level_meta[option],
                            )
                elif len(words) >= 2 and words[1].lower() == "dump":
                    # After "debug dump", suggest "brief", "detail", or "watch"
                    if len(words) == 2 or (
                        len(words) >= 3 and not text.endswith(" ")
                    ):
                        word = words[-1] if len(words) >= 3 else ""
                        if "brief".startswith(word.lower()):
                            yield Completion(
                                "brief",
                                start_position=-len(word),
                                display="brief",
                                display_meta="compact hex output",
                            )
                        if "detail".startswith(word.lower()):
                            yield Completion(
                                "detail",
                                start_position=-len(word),
                                display="detail",
                                display_meta="Wireshark-style protocol analysis",
                            )
                        if "watch".startswith(word.lower()):
                            yield Completion(
                                "watch",
                                start_position=-len(word),
                                display="watch",
                                display_meta="live frame analysis (ESC to exit)",
                            )
                elif len(words) >= 2 and words[1].lower() == "filter":
                    # After "debug filter", suggest "clear"
                    if len(words) == 2 or (
                        len(words) == 3 and not text.endswith(" ")
                    ):
                        word = words[2] if len(words) == 3 else ""
                        if "clear".startswith(word.lower()):
                            yield Completion(
                                "clear",
                                start_position=-len(word),
                                display="clear",
                                display_meta="Clear all station filters",
                            )

    def _get_command_help(self, cmd):
        """Get brief help text for a command.

        Args:
            cmd: Command name

        Returns:
            Brief help string
        """
        help_text = {
            "help": "Show available commands",
            "status": "Show radio status",
            "health": "Show radio health",
            "notifications": "Toggle notifications",
            "vfo": "Select VFO (A/B)",
            "setvfo": "Set VFO frequency",
            "active": "Set active channel",
            "dual": "Toggle dual watch",
            "scan": "Toggle scan mode",
            "squelch": "Set squelch level",
            "volume": "Set volume level",
            "bss": "Show BSS status",
            "setbss": "Set BSS user ID",
            "poweron": "Power on radio",
            "poweroff": "Power off radio",
            "power": "Set TX power",
            "channel": "Show channel info",
            "list": "List channels",
            "freq": "Show/set frequency",
            "dump": "Dump config/status",
            "debug": "Set debug level (0-6), filter by station, or dump frames (dump/filter)",
            "tncsend": "Send TNC data",
            "aprs": "APRS commands / Switch to APRS mode",
            "radio": "Radio commands / Switch to radio mode",
            "scan_ble": "Scan BLE characteristics",
            "tnc": "Enter TNC mode",
            "quit": "Exit console",
            "exit": "Exit console",
            # APRS subcommands (when shown as top-level in APRS mode)
            "message": "APRS messaging",
            "station": "Station database",
            "wx": "Weather stations",
        }
        return help_text.get(cmd, "")


class CommandProcessor:
    def __init__(self, radio, serial_mode=False):
        self.radio = radio
        self.serial_mode = serial_mode  # True if using serial TNC (no radio control)
        self.console_mode = "aprs" if serial_mode else "radio"  # Start in APRS mode for serial

        self.commands = {
            "help": self.cmd_help,
            "status": self.cmd_status,
            "health": self.cmd_health,
            "notifications": self.cmd_notifications,
            "vfo": self.cmd_vfo,
            "setvfo": self.cmd_setvfo,
            "active": self.cmd_active,
            "dual": self.cmd_dual,
            "scan": self.cmd_scan,
            "squelch": self.cmd_squelch,
            "volume": self.cmd_volume,
            "bss": self.cmd_bss,
            "setbss": self.cmd_setbss,
            "poweron": self.cmd_poweron,
            "poweroff": self.cmd_poweroff,
            "channel": self.cmd_channel,
            "list": self.cmd_list,
            "power": self.cmd_power,
            "freq": self.cmd_freq,
            "dump": self.cmd_dump,
            "debug": self.cmd_debug,
            "tncsend": self.cmd_tncsend,
            "aprs": self.cmd_aprs,
            "wx": self.cmd_wx,
            "scan_ble": self.cmd_scan_ble,
            "tnc": self.cmd_tnc,
            "quit": self.cmd_quit,
            "exit": self.cmd_quit,
        }
        # TNC configuration and state
        self.tnc_config = TNCConfig()
        self.tnc_connected_to = None
        self.tnc_mode = False
        self.tnc_conversation_mode = (
            False  # Track if in conversation mode (vs command mode)
        )
        self.tnc_debug_frames = False
        self._original_debug_state = (
            None  # Save original DEBUG state for restoration
        )
        self._tnc_text_buffer = (
            ""  # Buffer for accumulating text across frames
        )

        # APRS message and weather tracking
        # Use existing APRS manager if already created (e.g., by web server)
        # Otherwise create a new one
        if hasattr(self.radio, 'aprs_manager') and self.radio.aprs_manager:
            self.aprs_manager = self.radio.aprs_manager
            # Update retry config from TNC config if it changed
            retry_count = int(self.tnc_config.get("RETRY") or "3")
            retry_fast = int(self.tnc_config.get("RETRY_FAST") or "20")
            retry_slow = int(self.tnc_config.get("RETRY_SLOW") or "600")
            self.aprs_manager.max_retries = retry_count
            self.aprs_manager.retry_fast = retry_fast
            self.aprs_manager.retry_slow = retry_slow
        else:
            mycall = self.tnc_config.get("MYCALL") or "NOCALL"
            retry_count = int(self.tnc_config.get("RETRY") or "3")
            retry_fast = int(self.tnc_config.get("RETRY_FAST") or "20")
            retry_slow = int(self.tnc_config.get("RETRY_SLOW") or "600")
            self.aprs_manager = APRSManager(mycall, max_retries=retry_count,
                                           retry_fast=retry_fast, retry_slow=retry_slow)
            # Attach to radio so tnc_monitor() can access it
            self.radio.aprs_manager = self.aprs_manager

        # Frame history for debugging
        debug_buffer_setting = self.tnc_config.get("DEBUG_BUFFER") or "10"
        if debug_buffer_setting.upper() == "OFF":
            self.frame_history = FrameHistory(buffer_mode=False)
            load_info = self.frame_history.load_from_disk()
            if load_info['loaded']:
                print_info(f"Frame buffer: Simple mode (last 10 frames), loaded {load_info['frame_count']} frames")
                print_info(f"  Starting at frame #{load_info['start_frame']}, next frame will be #{self.frame_history.frame_counter + 1}")
            else:
                print_info("Frame buffer: Simple mode (last 10 frames), starting fresh at frame #1")
        else:
            debug_buffer_mb = int(debug_buffer_setting)
            self.frame_history = FrameHistory(max_size_mb=debug_buffer_mb, buffer_mode=True)
            load_info = self.frame_history.load_from_disk()
            if load_info['loaded']:
                size_kb = load_info['file_size_kb']
                print_info(f"Frame buffer: {debug_buffer_mb} MB buffer, loaded {load_info['frame_count']} frames ({size_kb:.1f} KB)")
                print_info(f"  Starting at frame #{load_info['start_frame']}, next frame will be #{self.frame_history.frame_counter + 1}")
                if load_info['corrupted_frames'] > 0:
                    print_warning(f"  Skipped {load_info['corrupted_frames']} corrupted frames during load")
            else:
                print_info(f"Frame buffer: {debug_buffer_mb} MB buffer, starting fresh at frame #1")

        # GPS state
        self.gps_position = None  # Current GPS position from radio
        self.gps_locked = False  # GPS lock status

        # Initialize weather station manager
        from src.weather_manager import WeatherStationManager
        self.weather_manager = WeatherStationManager()

        # Configure from saved settings
        backend = self.tnc_config.get("WX_BACKEND")
        address = self.tnc_config.get("WX_ADDRESS")
        port_str = self.tnc_config.get("WX_PORT")
        interval_str = self.tnc_config.get("WX_INTERVAL") or "300"
        enabled = self.tnc_config.get("WX_ENABLE") == "ON"

        port = int(port_str) if port_str else None
        interval = int(interval_str) if interval_str else 300

        self.weather_manager.configure(
            backend=backend if backend else None,
            address=address if address else None,
            port=port,
            enabled=enabled,
            update_interval=interval
        )

        # Configure wind averaging
        average_wind = self.tnc_config.get("WX_AVERAGE_WIND") == "ON"
        self.weather_manager.average_wind = average_wind

        # Load last beacon time from config
        last_beacon_str = self.tnc_config.get("LAST_BEACON")
        if last_beacon_str:
            try:
                self.last_beacon_time = datetime.fromisoformat(last_beacon_str)
                print_debug(f"Loaded last beacon time: {self.last_beacon_time}", level=6)
            except (ValueError, TypeError):
                self.last_beacon_time = None
        else:
            self.last_beacon_time = None

        self.gps_poll_task = None  # Background GPS polling task
        # Attach to radio so frame hooks can access it
        self.radio.cmd_processor = self

        # AX.25 adapter - use shared adapter if available, otherwise create new one
        try:
            if hasattr(self.radio, "shared_ax25") and self.radio.shared_ax25:
                # Use the shared adapter (created in main() for AGWPE compatibility)
                self.ax25 = self.radio.shared_ax25
                print_debug(
                    "CommandProcessor: Using shared AX25Adapter instance",
                    level=6,
                )
            else:
                # Create new adapter (fallback for standalone use)
                self.ax25 = AX25Adapter(
                    self.radio,
                    get_mycall=lambda: self.tnc_config.get("MYCALL"),
                    get_txdelay=lambda: self.tnc_config.get("TXDELAY"),
                )
                print_debug(
                    "CommandProcessor: Created new AX25Adapter instance",
                    level=6,
                )

            # Register callback to display received data
            self.ax25.register_callback(self._tnc_receive_callback)
            try:
                self.radio.register_kiss_callback(self.ax25.handle_incoming)
            except Exception:
                pass
        except Exception as e:
            print_error(f"Failed to initialize AX25Adapter: {e}")
            sys.exit(1)

    async def process(self, line):
        """Process a command line with mode-aware dispatching."""
        parts = line.strip().split()
        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        # Handle mode switching commands
        if cmd == "aprs":
            if not args:
                # Switch to APRS mode
                self.console_mode = "aprs"
                print_info("Switched to APRS mode (APRS commands no longer need 'aprs' prefix)")
                return
            # Fall through to handle "aprs" command with subcommands

        elif cmd == "radio":
            if not args:
                # Switch to radio mode
                if self.serial_mode:
                    print_error("Radio mode not available in serial mode (no radio control)")
                    return
                self.console_mode = "radio"
                print_info("Switched to radio mode (radio commands no longer need 'radio' prefix)")
                return
            # Fall through to handle "radio" prefix in APRS mode

        # Mode-aware command routing
        if self.console_mode == "aprs":
            # In APRS mode:
            # - APRS subcommands work without "aprs" prefix
            # - Radio commands need "radio" prefix (if not in serial mode)

            # Check if it's an APRS subcommand without prefix
            aprs_subcommands = ["message", "station", "wx"]
            if cmd in aprs_subcommands:
                # Rewrite as "aprs <subcommand> ..."
                cmd = "aprs"
                args = [parts[0]] + args  # Prepend original command as first arg

            # Handle "radio" prefix for radio commands
            elif cmd == "radio" and args:
                if self.serial_mode:
                    print_error("Radio commands not available in serial mode")
                    return
                # Remove "radio" prefix and dispatch
                cmd = args[0].lower()
                args = args[1:]

        elif self.console_mode == "radio":
            # In radio mode:
            # - Radio commands work without prefix
            # - "aprs" prefix required for APRS commands (handled normally)
            pass

        # Dispatch command
        if cmd in self.commands:
            try:
                await self.commands[cmd](args)
            except Exception as e:
                print_error(f"Command failed: {e}")
                import traceback
                traceback.print_exc()
        else:
            if self.console_mode == "aprs":
                print_error(
                    f"Unknown command: {cmd}. Type 'help' for available commands."
                )
            else:
                print_error(
                    f"Unknown command: {cmd}. Type 'help' for available commands."
                )

    async def show_startup_status(self):
        """Display startup status screen with VFO info."""
        # In serial mode, skip radio status and show APRS mode info
        if self.serial_mode:
            print_header("Console Ready")
            print_pt("")
            print_info(f"Mode: APRS (serial KISS TNC)")
            print_info(f"MYCALL: {self.tnc_config.get('MYCALL')}")
            mylocation = self.tnc_config.get('MYLOCATION')
            if mylocation:
                print_info(f"MYLOCATION: {mylocation}")

                # Broadcast MYLOCATION position to web UI on startup
                try:
                    from src.aprs_manager import APRSManager
                    lat, lon = APRSManager.maidenhead_to_latlon(mylocation)
                    if self.aprs_manager._web_broadcast:
                        await self.aprs_manager._web_broadcast('gps_update', {
                            'latitude': lat,
                            'longitude': lon,
                            'altitude': None,
                            'locked': True,
                            'source': 'MYLOCATION'
                        })
                except Exception as e:
                    print_debug(f"Failed to broadcast MYLOCATION on startup: {e}", level=6)

            print_pt("")
            print_pt(HTML("<gray>Type 'help' for available commands</gray>"))
            print_pt(HTML(f"<gray>Console mode: <b>{self.console_mode}</b> (use APRS commands without prefix)</gray>"))
            print_pt("")
            return

        try:
            # Get radio settings and status
            settings = await self.radio.get_settings()
            status = await self.radio.get_status()
            volume = await self.radio.get_volume()

            if not settings:
                print_error("Unable to read radio settings")
                return

            # Get channel details for both VFOs
            ch_a = await self.radio.read_channel(settings["channel_a"])
            ch_b = await self.radio.read_channel(settings["channel_b"])

            print_header("Radio Status")
            print_pt("")

            # Determine active VFO: prefer `double_channel` (radio's dual-watch
            # mode) when present, otherwise fall back to `vfo_x`.
            mode = settings.get("double_channel", None)
            if mode is not None and mode in (1, 2):
                active_vfo = "A" if mode != 2 else "B"
            else:
                vfo_x = settings.get("vfo_x", None)
                active_vfo = "A" if (vfo_x is None or vfo_x == 0) else "B"
            active_a = "●" if active_vfo == "A" else "○"
            active_b = "●" if active_vfo == "B" else "○"

            if ch_a:
                print_pt(
                    HTML(
                        f"<b>VFO A {active_a}</b>  CH{settings['channel_a']:3d}  {ch_a['tx_freq_mhz']:.4f} MHz  {ch_a['power']:4s}  {ch_a['name']}"
                    )
                )
            else:
                print_pt(
                    HTML(
                        f"<b>VFO A {active_a}</b>  CH{settings['channel_a']:3d}"
                    )
                )

            if ch_b:
                print_pt(
                    HTML(
                        f"<b>VFO B {active_b}</b>  CH{settings['channel_b']:3d}  {ch_b['tx_freq_mhz']:.4f} MHz  {ch_b['power']:4s}  {ch_b['name']}"
                    )
                )
            else:
                print_pt(
                    HTML(
                        f"<b>VFO B {active_b}</b>  CH{settings['channel_b']:3d}"
                    )
                )

            print_pt("")

            # Radio state
            if status:
                power_state = "ON" if status["is_power_on"] else "OFF"
                power_color = "green" if status["is_power_on"] else "red"
                print_pt(
                    HTML(
                        f"Power: <{power_color}>{power_state}</{power_color}>"
                    )
                )

            # Dual watch
            dual_mode = settings.get("double_channel", 0)
            if dual_mode == 1:
                print_pt("Dual Watch: A+B")
            elif dual_mode == 2:
                print_pt("Dual Watch: B+A")
            else:
                print_pt("Dual Watch: Off")

            # Volume and squelch
            squelch = settings.get("squelch_level", 0)
            if volume is not None:
                print_pt(f"Volume: {volume}/15    Squelch: {squelch}/15")
            else:
                print_pt(f"Squelch: {squelch}/15")

            print_pt("")
            print_pt(HTML("<gray>Type 'help' for commands</gray>"))
            print_pt(HTML(f"<gray>Console mode: <b>{self.console_mode}</b> (type 'aprs' to switch to APRS mode)</gray>"))
            print_pt("")

        except Exception as e:
            print_error(f"Failed to read status: {e}")

    async def cmd_help(self, args):
        """Show mode-aware help."""
        print_header(f"Available Commands (Mode: {self.console_mode.upper()})")

        if self.console_mode == "aprs":
            # APRS mode help
            print_pt(HTML("<b>Mode Switching:</b>"))
            if not self.serial_mode:
                print_pt(HTML("  <b>radio</b>             - Switch to radio mode"))
            print_pt("")

            print_pt(HTML("<b>APRS Commands (no prefix needed):</b>"))
            print_pt(HTML("  <b>message read</b>       - Read APRS messages"))
            print_pt(HTML("  <b>message send &lt;call&gt; &lt;text&gt;</b> - Send APRS message"))
            print_pt(HTML("  <b>message clear</b>      - Clear all messages"))
            print_pt(HTML("  <b>station list [N]</b>   - List last N heard stations"))
            print_pt(HTML("  <b>station info &lt;call&gt;</b> - Show station details"))
            print_pt(HTML("  <b>wx list [sort]</b>     - List weather stations"))
            print_pt("")

            if not self.serial_mode:
                print_pt(HTML("<b>Radio Commands:</b>"))
                print_pt(HTML("  <gray>Radio commands require 'radio' prefix in APRS mode</gray>"))
                print_pt(HTML("  <gray>(Type 'radio' to switch modes for direct access)</gray>"))
                print_pt("")

            print_pt(HTML("<b>TNC Commands:</b>"))
            print_pt(HTML("  <b>tnc</b>               - Enter TNC-2 terminal mode"))
            print_pt("")

        elif self.console_mode == "radio":
            # Radio mode help
            print_pt(HTML("<b>Mode Switching:</b>"))
            print_pt(HTML("  <b>aprs</b>              - Switch to APRS mode"))
            print_pt("")

            print_pt(HTML("<b>Radio Control:</b>"))
            print_pt(
                HTML("  <b>status</b>            - Show current radio status")
            )
            print_pt(HTML("  <b>health</b>            - Show connection health"))
            print_pt(
                HTML("  <b>notifications</b>     - Check BLE notification status")
            )
            print_pt(
                HTML("  <b>vfo</b>               - Show VFO A/B configuration")
            )
            print_pt(
                HTML(
                    "  <b>setvfo &lt;a|b&gt; &lt;ch&gt;</b>  - Set VFO to channel 1-256"
                )
            )
            print_pt(HTML("  <b>active &lt;a|b&gt;</b>      - Switch active VFO"))
            print_pt(
                HTML(
                    "  <b>dual &lt;off|ab|ba&gt;</b> - Set dual watch (off/A+B/B+A)"
                )
            )
            print_pt(HTML("  <b>scan &lt;on|off&gt;</b>    - Enable/disable scan"))
            print_pt(HTML("  <b>squelch &lt;0-15&gt;</b>   - Set squelch level"))
            print_pt(
                HTML("  <b>volume &lt;0-15&gt;</b>    - Get/set volume level")
            )
            print_pt("")
            print_pt(HTML("<b>Channel Management:</b>"))
            print_pt(
                HTML("  <b>channel &lt;id&gt;</b>     - Show channel details")
            )
            print_pt(HTML("  <b>list [start] [end]</b> - List channels"))
            print_pt(
                HTML(
                    "  <b>power &lt;id&gt; &lt;lvl&gt;</b>  - Set power (low/med/high)"
                )
            )
            print_pt(
                HTML(
                    "  <b>freq &lt;id&gt; &lt;tx&gt; &lt;rx&gt;</b> - Set frequencies"
                )
            )
            print_pt("")
            print_pt(HTML("<b>BSS Settings:</b>"))
            print_pt(
                HTML("  <b>bss</b>                    - Show BSS/APRS settings")
            )
            print_pt(
                HTML(
                    "  <b>setbss &lt;param&gt; &lt;val&gt;</b>  - Set BSS parameter"
                )
            )
            print_pt("")

            print_pt(HTML("<b>APRS Commands:</b>"))
            print_pt(HTML("  <gray>APRS commands require 'aprs' prefix in radio mode</gray>"))
            print_pt(HTML("  <gray>(Type 'aprs' to switch modes for direct access)</gray>"))
            print_pt("")

            print_pt(HTML("<b>TNC:</b>"))
            print_pt(
                HTML("  <b>tncsend &lt;hex&gt;</b>         - Send raw hex to TNC")
            )
            print_pt(HTML("  <b>tnc</b>               - Enter TNC terminal mode"))
            print_pt("")

        # Common commands (both modes)
        print_pt(HTML("<b>Utility:</b>"))
        print_pt(HTML("  <b>dump</b>              - Dump raw settings"))
        print_pt(HTML("  <b>debug</b>             - Toggle debug output"))
        print_pt(HTML("  <b>help</b>              - Show this help"))
        print_pt(HTML("  <b>quit</b> / <b>exit</b>      - Exit application"))
        print_pt("")

        # Show server ports from TNC config
        tnc_port = self.tnc_config.get("TNC_PORT") or "8001"
        agwpe_port = self.tnc_config.get("AGWPE_PORT") or "8000"
        webui_port = self.tnc_config.get("WEBUI_PORT") or "8002"
        print_pt(HTML(f"<b>TNC TCP Bridge:</b> Port {tnc_port} (bidirectional)"))
        print_pt(HTML(f"<b>AGWPE Bridge:</b> Port {agwpe_port}"))
        print_pt(HTML(f"<b>Web UI:</b> Port {webui_port}"))
        print_pt("")

    async def cmd_health(self, args):
        """Show connection health."""
        print_header("Connection Health")

        # Check connection status (BLE mode only)
        if self.radio.client:
            connected = self.radio.client.is_connected
            conn_color = "green" if connected else "red"
            print_pt(
                HTML(
                    f"BLE Connected:     <{conn_color}>{'Yes' if connected else 'No'}</{conn_color}>"
                )
            )
        else:
            print_pt(HTML("Mode:              <green>Serial KISS TNC</green>"))

        idle_time = int(self.radio.get_tnc_idle_time())
        idle_color = (
            "green"
            if idle_time < 60
            else "yellow" if idle_time < 300 else "red"
        )
        print_pt(
            HTML(
                f"TNC Idle Time:     <{idle_color}>{idle_time}s</{idle_color}>"
            )
        )

        print_pt(f"TNC Packets RX:    {self.radio.tnc_packet_count}")
        print_pt(f"Heartbeat Fails:   {self.radio.heartbeat_failures}")

        hb_time = int(
            (datetime.now() - self.radio.last_heartbeat).total_seconds()
        )
        print_pt(f"Last Heartbeat:    {hb_time}s ago")

        print_pt("")
        print_info("Running health check...")
        healthy = await self.radio.check_connection_health()

        if healthy:
            print_info("✓ Connection is healthy")
        else:
            print_error("✗ Connection appears unhealthy")

        print_pt("")

    async def cmd_notifications(self, args):
        """Check notification status."""
        print_header("Notification Status")

        if not self.radio.client:
            print_error("Notifications command not available in serial mode")
            return

        try:
            # Check if notifications are still enabled
            services = self.radio.client.services

            for service in services:
                for char in service.characteristics:
                    if char.uuid == TNC_RX_UUID:
                        print_info(f"TNC RX UUID found: {char.uuid}")
                        print_info(f"  Properties: {char.properties}")

                    if char.uuid == RADIO_INDICATE_UUID:
                        print_info(f"Radio indicate UUID found: {char.uuid}")
                        print_info(f"  Properties: {char.properties}")

            print_pt("")
            print_warning(
                "If TNC packets have stopped, restart the application"
            )

        except Exception as e:
            print_error(f"Failed to check notifications: {e}")

        print_pt("")

    async def cmd_debug(self, args):
        """Set debug level (0-6), filter by station, or dump frame history.

        Usage:
            debug [level]               - Show or set debug level (0-6)
            debug <level> filter <call> - Set debug level only for specific station
            debug filter                - Show active station filters
            debug filter clear          - Clear all station filters
            debug save                  - Save frame buffer to disk now
            debug dump [n] [brief]      - Dump last n frames (default=10)
                                          brief = compact hex output
            debug dump [n] detail       - Dump with Wireshark-style protocol analysis
            debug dump n-m [brief|detail] - Dump frames n through m (range)
            debug dump detail watch     - Watch mode: live protocol analysis of incoming frames
        """
        if not args:
            # Show current level and available levels
            print_info(f"Current debug level: {constants.DEBUG_LEVEL}")
            if constants.DEBUG_STATION_FILTERS:
                print_info("")
                print_info("Active station filters:")
                for call, level in sorted(constants.DEBUG_STATION_FILTERS.items()):
                    print_info(f"  {call}: level {level}")
            print_info("")
            print_info("Debug levels:")
            print_info("  0 = Off (no debug output)")
            print_info("  1 = TNC monitor")
            print_info("  2 = Critical errors and events")
            print_info("  3 = Connection state changes")
            print_info("  4 = Frame transmission/reception")
            print_info("  5 = Protocol details, retransmissions")
            print_info("  6 = Everything (BLE, config, hex dumps)")
            print_info("")
            print_info("Usage: debug <level>  or  debug <level> filter <callsign>")
            print_info("       debug filter  or  debug filter clear")
            print_info("       debug save")
            print_info("       debug dump [n|-n|n-m] [brief|detail|watch]")
            print_info("Example: debug 5 filter k1mal-7  (debug level 5 for K1MAL-7 only)")
            print_info("         debug 2  (sets global level to 2, clears filters)")
            print_info("         debug save  (save frame buffer to disk)")
            print_info("         debug dump 5 brief  or  debug dump 3 detail")
            return

        # Check for filter subcommand
        if args[0].lower() == "filter":
            if len(args) == 1:
                # Show current filters
                if constants.DEBUG_STATION_FILTERS:
                    print_info("Active station filters:")
                    for call, level in sorted(constants.DEBUG_STATION_FILTERS.items()):
                        print_info(f"  {call}: level {level}")
                else:
                    print_info("No station filters active")
                    print_info("Use: debug <level> filter <callsign>")
                return
            elif len(args) == 2 and args[1].lower() == "clear":
                # Clear all filters
                constants.DEBUG_STATION_FILTERS.clear()
                print_info("All station filters cleared")
                return
            else:
                print_error("Usage: debug filter  or  debug filter clear")
                return

        # Check for save subcommand
        if args[0].lower() == "save":
            print_info("Saving frame buffer to disk...")
            self.frame_history.save_to_disk()

            # Show stats
            if os.path.exists(FrameHistory.BUFFER_FILE):
                size = os.path.getsize(FrameHistory.BUFFER_FILE)
                size_kb = size / 1024
                print_info(f"✓ Saved {len(self.frame_history.frames)} frames to {FrameHistory.BUFFER_FILE}")
                print_info(f"  File size: {size_kb:.1f} KB")
            else:
                print_info("✓ Frame buffer saved")
            return

        # Check for dump subcommand
        if args[0].lower() == "dump":
            # Handle debug dump [n] [brief|detail] [watch]
            count = None
            specific_frame = None
            frame_range = None  # (start, end) tuple for range
            brief_mode = False
            detail_mode = False
            watch_mode = False
            format_specified = None  # Track which format was specified

            # Parse arguments (count and/or brief/detail/watch)
            for arg in args[1:]:
                if arg.lower() == "brief":
                    if format_specified == "detail":
                        print_error("Cannot specify both 'brief' and 'detail'")
                        return
                    brief_mode = True
                    detail_mode = False
                    format_specified = "brief"
                elif arg.lower() == "detail":
                    if format_specified == "brief":
                        print_error("Cannot specify both 'brief' and 'detail'")
                        return
                    detail_mode = True
                    brief_mode = False
                    format_specified = "detail"
                elif arg.lower() == "watch":
                    watch_mode = True
                    # If no format specified yet, default to detail for watch mode
                    if not format_specified:
                        detail_mode = True
                        format_specified = "detail"
                else:
                    # Check if this is a range (e.g., "4-13")
                    if '-' in arg and not arg.startswith('-'):
                        try:
                            parts = arg.split('-')
                            if len(parts) == 2:
                                start = int(parts[0])
                                end = int(parts[1])
                                if start > 0 and end > 0 and start <= end:
                                    frame_range = (start, end)
                                else:
                                    print_error("Range must be start-end where start <= end and both > 0")
                                    return
                            else:
                                print_error("Invalid range format (use start-end)")
                                return
                        except ValueError:
                            print_error(f"Invalid range: {arg}")
                            return
                    else:
                        try:
                            num = int(arg)
                            if num < 0:
                                # Negative = last n frames
                                count = abs(num)
                            elif num > 0:
                                # Positive = specific frame number
                                specific_frame = num
                            else:
                                print_error("Frame number must be non-zero")
                                return
                        except ValueError:
                            print_error(f"Unknown argument: {arg}")
                            return

            # Handle watch mode
            if watch_mode:
                await self._debug_watch_mode()
                return

            # Get frames
            if frame_range is not None:
                # Get frames in range
                start, end = frame_range
                frames = []
                for frame_num in range(start, end + 1):
                    frame = self.frame_history.get_by_number(frame_num)
                    if frame:
                        frames.append(frame)

                if not frames:
                    print_error(f"No frames found in range #{start} to #{end}")
                    if len(self.frame_history.frames) > 0:
                        oldest = self.frame_history.frames[0].frame_number
                        newest = self.frame_history.frames[-1].frame_number
                        print_info(f"Available frames: #{oldest} to #{newest}")
                    return
                elif len(frames) < (end - start + 1):
                    missing = (end - start + 1) - len(frames)
                    print_info(f"Found {len(frames)} frames in range #{start}-#{end} ({missing} missing)")
            elif specific_frame is not None:
                # Get specific frame by number
                frame = self.frame_history.get_by_number(specific_frame)
                if not frame:
                    print_error(f"Frame #{specific_frame} not found in history")
                    if len(self.frame_history.frames) > 0:
                        oldest = self.frame_history.frames[0].frame_number
                        newest = self.frame_history.frames[-1].frame_number
                        print_info(f"Available frames: #{oldest} to #{newest}")
                    return
                frames = [frame]
            else:
                # Get last n frames (default 10 if count is None)
                if count is None:
                    count = 10
                frames = self.frame_history.get_recent(count)

            if not frames:
                print_info("No frames in history")
                return

            # Determine header text
            if frame_range:
                header_suffix = f"frames #{frame_range[0]}-#{frame_range[1]}"
            elif specific_frame:
                header_suffix = f"frame #{specific_frame}"
            else:
                header_suffix = f"last {len(frames)} frames"

            if detail_mode:
                # Detail mode - Wireshark-style protocol analysis
                print_header(
                    f"Frame History - Detail ({header_suffix})"
                )

                # Track ACKs
                acks_found = []

                for frame in frames:
                    # Format frame with detailed analysis
                    detail_lines = format_detailed_frame(frame, frame.frame_number)
                    for line in detail_lines:
                        print_pt(line)
                    print_pt("")  # Blank line between frames

                    # Check for ACKs
                    try:
                        raw_bytes = frame.raw_bytes
                        if len(raw_bytes) > 3:
                            ax25_payload = raw_bytes[2:-1]
                            # Quick check for APRS message
                            if len(ax25_payload) > 20:
                                # Extract info field (after addresses, control, pid)
                                offset = 14
                                while offset < len(ax25_payload) and not ax25_payload[offset - 1] & 0x01:
                                    offset += 7
                                if offset + 2 < len(ax25_payload):
                                    info_bytes = ax25_payload[offset + 2:]
                                    info_str = info_bytes.decode('ascii', errors='replace').rstrip('\r\n\x00')
                                    if info_str.startswith(':') and len(info_str) >= 11:
                                        message_text = info_str[11:]
                                        if message_text.startswith('ack'):
                                            src = decode_ax25_address_field(ax25_payload[7:14])
                                            to_call = info_str[1:10].strip()
                                            msg_id = message_text[3:].split('{')[0].strip()
                                            if src:
                                                acks_found.append(f"{src['full']} -> {to_call} (ID: {msg_id})")
                    except:
                        pass

            elif brief_mode:
                # Brief mode - compact output
                print_header(
                    f"Frame History - Brief ({header_suffix})"
                )
                for frame in frames:
                    time_str = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]
                    direction_color = (
                        "green" if frame.direction == "TX" else "cyan"
                    )
                    hex_str = frame.raw_bytes.hex()
                    print_pt(
                        HTML(
                            f"[{frame.frame_number}] <{direction_color}>{frame.direction}</{direction_color}> {time_str} ({len(frame.raw_bytes)}b): {hex_str}"
                        )
                    )
            else:
                # Verbose mode - hex editor style
                print_header(f"Frame History ({header_suffix})")

                for frame in frames:
                    time_str = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]
                    direction_color = (
                        "green" if frame.direction == "TX" else "cyan"
                    )

                    print_pt(
                        HTML(
                            f"<b>[{frame.frame_number}] <{direction_color}>{frame.direction}</{direction_color}> {time_str}</b> ({len(frame.raw_bytes)} bytes)"
                        )
                    )

                    # Print hex dump with ASCII
                    for line in frame.format_hex_lines():
                        print_pt(line)
                    print_pt("")  # Blank line between frames

            return

        # Handle debug level setting
        try:
            level = int(args[0])
            if level < 0 or level > 6:
                print_error("Debug level must be 0-6")
                return

            # Check if this is a per-station filter (debug <level> filter <callsign>)
            if len(args) >= 3 and args[1].lower() == "filter":
                callsign = args[2].upper().strip()
                constants.DEBUG_STATION_FILTERS[callsign] = level
                print_info(f"Station filter set: {callsign} -> debug level {level}")
                print_info(f"Global debug level remains: {constants.DEBUG_LEVEL}")
                print_info("(Frames involving this station will use the higher level)")
                return

            # Setting global level - clear station filters as requested
            if constants.DEBUG_STATION_FILTERS:
                constants.DEBUG_STATION_FILTERS.clear()
                print_info("Station filters cleared (global level set)")

            # Track previous level for file logging management
            prev_level = constants.DEBUG_LEVEL

            constants.DEBUG_LEVEL = level
            # Update legacy DEBUG flag for backward compatibility
            constants.DEBUG = level > 0

            if level == 0:
                print_info("Debug mode: OFF")
            else:
                print_info(f"Debug level set to: {level}")
                level_desc = {
                    1: "Received Frames",
                    2: "Critical errors and events",
                    3: "Connection state changes",
                    4: "Frame TX/RX details",
                    5: "Protocol details, retransmissions + FILE LOGGING",
                    6: "Everything (BLE, config, hex dumps) + FILE LOGGING",
                }
                print_info(f"  {level_desc.get(level, 'Unknown')}")
        except (ValueError, IndexError):
            print_error("Usage: debug <level>  or  debug dump [n]")
            print_error("Example: debug 3  or  debug dump 5")

    async def _debug_watch_mode(self):
        """Watch mode for debug dump - continuously display detailed protocol analysis for incoming frames."""

        print_header("Frame Watch Mode - Live Protocol Analysis")
        print_pt(HTML("<yellow>Monitoring for incoming frames...</yellow>"))
        print_pt(HTML("<gray>Press ESC or type '~~~' to exit</gray>"))
        print_pt("")

        # Track the last frame we've seen
        last_frame_count = len(self.frame_history.frames)
        frame_counter = 0

        # Set up key bindings for escape detection
        kb = KeyBindings()
        exit_requested = asyncio.Event()

        @kb.add('escape')
        def _(event):
            """Handle escape key."""
            exit_requested.set()
            event.app.exit()

        # Create a prompt session for input handling
        session = PromptSession(key_bindings=kb)

        async def monitor_frames():
            """Monitor for new frames and display them."""
            nonlocal last_frame_count, frame_counter

            while not exit_requested.is_set():
                try:
                    # Check if there are new frames
                    current_frame_count = len(self.frame_history.frames)

                    if current_frame_count > last_frame_count:
                        # Get only the new frames
                        new_frames = list(self.frame_history.frames)[last_frame_count:current_frame_count]

                        for frame in new_frames:
                            frame_counter += 1

                            # Format and display the frame with detailed analysis
                            # (format_detailed_frame includes its own header separator)
                            print_pt("")
                            detail_lines = format_detailed_frame(frame, frame_counter)
                            for line in detail_lines:
                                print_pt(line)

                        last_frame_count = current_frame_count

                    # Small delay to avoid busy waiting
                    await asyncio.sleep(0.1)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print_debug(f"Error in frame monitor: {e}", level=2)
                    break

        async def input_handler():
            """Handle user input for ~~~ exit sequence and ESC key."""
            try:
                # Single prompt call - waits for user input (ESC or ~~~)
                # This processes keyboard events (including ESC key binding)
                line = await session.prompt_async(
                    HTML("<gray>[Watching... type '~~~' or press ESC to exit]</gray> "),
                    key_bindings=kb
                )

                # Check if user typed ~~~ (line will be None if ESC was pressed)
                if line is not None and line.strip() == "~~~":
                    exit_requested.set()
                elif line is None:
                    # ESC was pressed (prompt returned None)
                    exit_requested.set()

            except (KeyboardInterrupt, EOFError):
                exit_requested.set()
            except asyncio.CancelledError:
                pass

        # Run both tasks concurrently
        monitor_task = asyncio.create_task(monitor_frames())
        input_task = asyncio.create_task(input_handler())

        try:
            # Wait for either task to complete (exit requested)
            done, pending = await asyncio.wait(
                [monitor_task, input_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except KeyboardInterrupt:
            pass
        finally:
            print_pt("")
            print_pt(HTML("<yellow>Watch mode exited</yellow>"))
            print_pt(HTML(f"<gray>Monitored {frame_counter} frame(s)</gray>"))
            print_pt("")

    async def cmd_dump(self, args):
        """Dump raw settings bytes for analysis."""
        print_info("Reading raw settings...")
        settings = await self.radio.get_settings()

        if settings and "raw_data" in settings:
            data = settings["raw_data"]
            print_header("Raw Settings Data")

            # Print hex dump
            print_pt("Hex dump:")
            for i in range(0, len(data), 16):
                chunk = data[i : i + 16]
                hex_str = " ".join(f"{b:02x}" for b in chunk)
                ascii_str = "".join(
                    chr(b) if 32 <= b <= 126 else "." for b in chunk
                )
                print_pt(f"  {i:04x}: {hex_str:<48} {ascii_str}")

            print_pt(HTML(f"\n<yellow>Decoded VFO settings:</yellow>"))
            print_pt(f"  VFO A: Channel {settings['channel_a']}")
            print_pt(f"  VFO B: Channel {settings['channel_b']}")
            print_pt(
                f"  Active VFO: {'A' if settings.get('vfo_x', 0) == 0 else 'B'}"
            )
            print_pt("")
        else:
            print_error("Failed to read settings")

    async def cmd_status(self, args):
        """Show radio status."""
        print_info("Reading status...")
        status = await self.radio.get_status()
        settings = await self.radio.get_settings()

        if status:
            print_header("Radio Status")
            on_color = "green" if status["is_power_on"] else "red"
            print_pt(
                HTML(
                    f"Power:         <{on_color}>{'ON' if status['is_power_on'] else 'OFF'}</{on_color}>"
                )
            )

            tx_color = "red" if status["is_in_tx"] else "gray"
            print_pt(
                HTML(
                    f"TX:            <{tx_color}>{'TRANSMITTING' if status['is_in_tx'] else 'Idle'}</{tx_color}>"
                )
            )

            rx_color = "green" if status["is_in_rx"] else "gray"
            print_pt(
                HTML(
                    f"RX:            <{rx_color}>{'RECEIVING' if status['is_in_rx'] else 'Idle'}</{rx_color}>"
                )
            )

            scan_color = "yellow" if status["is_scan"] else "gray"
            print_pt(
                HTML(
                    f"Scan:          <{scan_color}>{'Active' if status['is_scan'] else 'Off'}</{scan_color}>"
                )
            )

            # Add 1 to channel ID for display (radio uses 0-based internally, displays 1-based)
            print_pt(f"Channel:       {status['curr_ch_id'] + 1}")
            print_pt(f"RSSI:          {status['rssi']}/15")

            if settings:
                # Prefer double_channel for active VFO selection when available
                mode = settings.get("double_channel", None)
                if mode is not None and mode in (1, 2):
                    active_vfo = "A" if mode != 2 else "B"
                else:
                    vfo_x = settings.get("vfo_x", 0)
                    active_vfo = "A" if vfo_x == 0 else "B"
                print_pt(f"Active VFO:    {active_vfo}")

                dual_mode = settings.get("double_channel", 0)
                dual_str = (
                    "Off"
                    if dual_mode == 0
                    else "A+B" if dual_mode == 1 else "B+A"
                )
                print_pt(f"Dual Watch:    {dual_str}")

            # TNC status
            idle_time = int(self.radio.get_tnc_idle_time())
            print_pt(
                f"TNC Packets:   {self.radio.tnc_packet_count} ({idle_time}s idle)"
            )

            # TNC Bridge status
            if self.radio.tnc_bridge:
                if self.radio.tnc_bridge.client_address:
                    print_pt(
                        HTML(
                            f"TNC Bridge:    <green>Connected ({self.radio.tnc_bridge.client_address})</green>"
                        )
                    )
                else:
                    print_pt(
                        HTML(
                            f"TNC Bridge:    <yellow>Listening on port {TNC_TCP_PORT}</yellow>"
                        )
                    )

            print_pt("")
        else:
            print_error("Failed to read status")

    async def cmd_vfo(self, args):
        """Show VFO A/B configuration."""
        print_info("Reading VFO settings...")
        settings = await self.radio.get_settings()

        if settings:
            print_header("VFO Configuration")

            # Determine active VFO: prefer `double_channel` when present,
            # otherwise fall back to `vfo_x`.
            mode = settings.get("double_channel", None)
            if mode is not None and mode in (1, 2):
                active_vfo = "A" if mode != 2 else "B"
            else:
                vfo_x = settings.get("vfo_x", None)
                active_vfo = "A" if (vfo_x is None or vfo_x == 0) else "B"
            active_marker_a = "●" if active_vfo == "A" else "○"
            active_marker_b = "●" if active_vfo == "B" else "○"

            print_pt(
                HTML(
                    f"VFO A (Main):  {active_marker_a} Channel {settings['channel_a']}"
                )
            )
            print_pt(
                HTML(
                    f"VFO B (Sub):   {active_marker_b} Channel {settings['channel_b']}"
                )
            )

            mode = settings.get("double_channel", 0)
            mode_str = "Off" if mode == 0 else "A+B" if mode == 1 else "B+A"
            print_pt(f"Dual Watch:    {mode_str}")
            print_pt(
                f"Scan:          {'On' if settings.get('scan', False) else 'Off'}"
            )
            print_pt(f"Squelch:       {settings.get('squelch_level', 0)}")
            print_pt("")

            print_info(f"Reading VFO A details (CH{settings['channel_a']})...")
            ch_a = await self.radio.read_channel(settings["channel_a"])
            if ch_a:
                self._print_channel_details(ch_a)

            print_info(f"Reading VFO B details (CH{settings['channel_b']})...")
            ch_b = await self.radio.read_channel(settings["channel_b"])
            if ch_b:
                self._print_channel_details(ch_b)
        else:
            print_error("Failed to read VFO settings")

    async def cmd_setvfo(self, args):
        """Set VFO to a specific channel."""
        if len(args) < 2:
            print_error("Usage: setvfo &lt;a|b&gt; &lt;channel&gt;")
            print_error("Channel must be 1-256")
            return

        vfo = args[0].lower()
        if vfo not in ["a", "b"]:
            print_error("VFO must be 'a' or 'b'")
            return

        try:
            channel_id = int(args[1])
        except ValueError:
            print_error("Channel ID must be a number")
            return

        if channel_id < 1 or channel_id > 256:
            print_error("Channel ID must be 1-256")
            return

        print_info(f"Setting VFO {vfo.upper()} to channel {channel_id}...")
        success = await self.radio.set_vfo(vfo, channel_id)

        if success:
            print_info(f"✓ VFO {vfo.upper()} set to channel {channel_id}")

            channel = await self.radio.read_channel(channel_id)
            if channel:
                print_pt(
                    f"  {channel['name']}: {channel['tx_freq_mhz']:.4f} MHz"
                )

            await asyncio.sleep(0.3)
            settings = await self.radio.get_settings()
            if settings:
                actual_a = settings["channel_a"]
                actual_b = settings["channel_b"]
                print_info(f"Verified: VFO A={actual_a}, VFO B={actual_b}")
        else:
            print_error(f"Failed to set VFO {vfo.upper()}")

    async def cmd_active(self, args):
        """Switch active VFO."""
        if not args:
            print_error("Usage: active &lt;a|b&gt;")
            return

        vfo = args[0].lower()
        if vfo not in ["a", "b"]:
            print_error("VFO must be 'a' or 'b'")
            return

        print_info(f"Switching to VFO {vfo.upper()}...")
        success = await self.radio.set_active_vfo(vfo)

        if success:
            print_info(f"✓ Active VFO set to {vfo.upper()}")
        else:
            print_error("Failed to switch VFO")

    async def cmd_dual(self, args):
        """Set dual watch mode."""
        if not args:
            print_error("Usage: dual &lt;off|ab|ba&gt;")
            return

        mode_str = args[0].lower()
        if mode_str == "off":
            mode = 0
        elif mode_str == "ab":
            mode = 1
        elif mode_str == "ba":
            mode = 2
        else:
            print_error("Mode must be: off, ab, or ba")
            return

        print_info(f"Setting dual watch to {mode_str.upper()}...")
        success = await self.radio.set_dual_watch(mode)

        if success:
            print_info(f"✓ Dual watch set to {mode_str.upper()}")
        else:
            print_error("Failed to set dual watch")

    async def cmd_scan(self, args):
        """Enable/disable scanning."""
        if not args:
            print_error("Usage: scan &lt;on|off&gt;")
            return

        state = args[0].lower()
        if state == "on":
            enabled = True
        elif state == "off":
            enabled = False
        else:
            print_error("State must be: on or off")
            return

        print_info(f"Setting scan to {state.upper()}...")
        success = await self.radio.set_scan(enabled)

        if success:
            print_info(f"✓ Scan {state.upper()}")
        else:
            print_error("Failed to set scan")

    async def cmd_squelch(self, args):
        """Set squelch level."""
        if not args:
            print_error("Usage: squelch &lt;0-15&gt;")
            return

        try:
            level = int(args[0])
        except ValueError:
            print_error("Level must be a number 0-15")
            return

        if level < 0 or level > 15:
            print_error("Level must be 0-15")
            return

        print_info(f"Setting squelch to {level}...")
        success = await self.radio.set_squelch(level)

        if success:
            print_info(f"✓ Squelch set to {level}")
        else:
            print_error("Failed to set squelch")

    async def cmd_volume(self, args):
        """Get or set volume."""
        if not args:
            # Get volume
            print_info("Reading volume...")
            level = await self.radio.get_volume()
            if level is not None:
                print_info(f"Volume: {level}/15")
            else:
                print_error("Failed to read volume")
            return

        # Set volume
        try:
            level = int(args[0])
        except ValueError:
            print_error("Level must be a number 0-15")
            return

        if level < 0 or level > 15:
            print_error("Level must be 0-15")
            return

        print_info(f"Setting volume to {level}...")
        success = await self.radio.set_volume(level)

        if success:
            print_info(f"✓ Volume set to {level}")
        else:
            print_error("Failed to set volume")

    async def cmd_bss(self, args):
        """Show BSS/APRS settings."""
        print_info("Reading BSS settings...")
        bss = await self.radio.get_bss_settings()

        print_debug(f"cmd_bss: received bss = {bss}", level=6)

        if bss:
            print_header("BSS/APRS Settings")
            print_pt(f"APRS Callsign:     {bss['aprs_callsign']}")
            print_pt(f"APRS SSID:         {bss['aprs_ssid']}")
            print_pt(f"APRS Symbol:       {bss['aprs_symbol']}")
            print_pt(f"Beacon Message:    {bss['beacon_message']}")
            print_pt(
                f"Share Location:    {'Yes' if bss['should_share_location'] else 'No'}"
            )
            print_pt(f"Share Interval:    {bss['location_share_interval']}s")
            print_pt(f"PTT Release ID:    {bss['ptt_release_id_info']}")
            print_pt(f"Max Fwd Times:     {bss['max_fwd_times']}")
            print_pt(f"Time To Live:      {bss['time_to_live']}")
            print_pt("")
        else:
            print_error("Failed to read BSS settings")

    async def cmd_setbss(self, args):
        """Set BSS parameter."""
        if len(args) < 2:
            print_error("Usage: setbss &lt;param&gt; &lt;value&gt;")
            print_error("Parameters: callsign, ssid, symbol, beacon, interval")
            return

        param = args[0].lower()
        value = " ".join(args[1:])

        bss = await self.radio.get_bss_settings()
        if not bss:
            print_error("Failed to read BSS settings")
            return

        try:
            if param == "callsign":
                bss["aprs_callsign"] = value.upper()[:6]
            elif param == "ssid":
                bss["aprs_ssid"] = int(value) & 0x0F
            elif param == "symbol":
                bss["aprs_symbol"] = value[:2]
            elif param == "beacon":
                bss["beacon_message"] = value[:18]
            elif param == "interval":
                bss["location_share_interval"] = int(value)
            else:
                print_error(f"Unknown parameter: {param}")
                return

            print_info(f"Setting {param} to {value}...")
            success = await self.radio.write_bss_settings(bss)

            if success:
                print_info(f"✓ BSS {param} updated")
            else:
                print_error("Failed to update BSS settings")

        except ValueError as e:
            print_error(f"Invalid value: {e}")

    async def cmd_channel(self, args):
        """Show channel details."""
        if not args:
            print_error("Usage: channel &lt;id&gt;")
            return

        try:
            channel_id = int(args[0])
        except ValueError:
            print_error("Channel ID must be a number")
            return

        print_info(f"Reading channel {channel_id}...")
        channel = await self.radio.read_channel(channel_id)

        if channel:
            self._print_channel_details(channel)
        else:
            print_error(f"Failed to read channel {channel_id}")

    async def cmd_list(self, args):
        """List channels in table format."""
        if len(args) == 0:
            start = 1
            end = 30
        elif len(args) == 1:
            start = int(args[0])
            end = start + 9
        elif len(args) >= 2:
            start = int(args[0])
            end = int(args[1])

        start = max(1, min(start, 256))
        end = max(1, min(end, 256))

        if start > end:
            print_error("Start channel must be &lt;= end channel")
            return

        print_header(f"Channel List ({start}-{end})")

        widths = [4, 12, 12, 12, 8, 8, 5]
        print_table_row(
            [
                "CH",
                "Name",
                "TX (MHz)",
                "RX (MHz)",
                "TX Tone",
                "RX Tone",
                "Power",
            ],
            widths,
            header=True,
        )

        for ch_id in range(start, end + 1):
            channel = await self.radio.read_channel(ch_id)

            if channel:
                print_table_row(
                    [
                        ch_id,
                        channel["name"][:12],
                        f"{channel['tx_freq_mhz']:.4f}",
                        f"{channel['rx_freq_mhz']:.4f}",
                        channel["tx_tone"][:8],
                        channel["rx_tone"][:8],
                        channel["power"],
                    ],
                    widths,
                )
            else:
                print_table_row(
                    [ch_id, "---", "---", "---", "---", "---", "---"], widths
                )

            await asyncio.sleep(0.05)

        print_pt("")

    async def cmd_power(self, args):
        """Set power level."""
        if len(args) < 2:
            print_error("Usage: power &lt;channel&gt; &lt;level&gt;")
            return

        try:
            channel_id = int(args[0])
        except ValueError:
            print_error("Channel ID must be a number")
            return

        power_level = args[1].lower()
        if power_level not in ["low", "med", "medium", "high", "max"]:
            print_error("Power level must be: low, med, or high")
            return

        print_info(f"Setting channel {channel_id} to {power_level} power...")
        success = await self.radio.set_channel_power(channel_id, power_level)

        if success:
            print_info(f"✓ Channel {channel_id} power set to {power_level}")
        else:
            print_error(f"Failed to set power for channel {channel_id}")

    async def cmd_freq(self, args):
        """Set frequency."""
        if len(args) < 3:
            print_error(
                "Usage: freq &lt;channel&gt; &lt;tx_mhz&gt; &lt;rx_mhz&gt;"
            )
            return

        try:
            channel_id = int(args[0])
            tx_freq = float(args[1])
            rx_freq = float(args[2])
        except ValueError:
            print_error("Invalid frequency values")
            return

        print_info(f"Reading channel {channel_id}...")
        channel = await self.radio.read_channel(channel_id)

        if not channel:
            print_error(f"Failed to read channel {channel_id}")
            return

        channel["tx_freq_mhz"] = tx_freq
        channel["rx_freq_mhz"] = rx_freq

        print_info(f"Writing new frequencies to channel {channel_id}...")
        success = await self.radio.write_channel(channel)

        if success:
            print_info(
                f"✓ Channel {channel_id} updated: TX={tx_freq} MHz, RX={rx_freq} MHz"
            )
        else:
            print_error(f"Failed to update channel {channel_id}")

    async def cmd_tncsend(self, args):
        """Send raw hex data to TNC."""
        if not args:
            print_error("Usage: tncsend &lt;hexdata&gt;")
            print_error("Example: tncsend c000c0")
            return

        hex_str = "".join(args).replace(" ", "")

        try:
            data = bytes.fromhex(hex_str)
        except ValueError:
            print_error("Invalid hex data")
            return

        print_info(f"Sending {len(data)} bytes to TNC...")
        await self.radio.send_tnc_data(data)
        print_info(f"✓ Sent: {data.hex()}")

    async def cmd_aprs(self, args):
        """APRS commands - message handling and weather."""
        if not args:
            print_info("APRS Commands:")
            print_info(
                "  aprs message read [all]                - Read APRS messages (all = include read)"
            )
            print_info(
                "  aprs message send <call> <text>    - Send APRS message"
            )
            print_info(
                "  aprs message clear                 - Clear all messages"
            )
            print_info(
                "  aprs message monitor list [N]      - List monitored messages (last N)"
            )
            print_info(
                "  aprs wx list [last|name|temp|humidity|pressure] - List weather stations"
            )
            print_info(
                "  aprs position list                 - List station positions"
            )
            print_info(
                "  aprs station list [name|packets|last|hops] - List all heard stations (default: last)"
            )
            print_info(
                "  aprs station show <callsign>       - Show detailed station info"
            )
            print_info(
                "  aprs database save                 - Manually save database to disk"
            )
            print_info(
                "  aprs database clear                - Clear entire APRS database"
            )
            print_info(
                "  aprs database prune <days>         - Remove entries older than N days"
            )
            return

        subcmd = args[0].lower()

        # Message commands
        if subcmd == "message" or subcmd == "msg":
            if len(args) < 2:
                print_error(
                    "Usage: aprs message <read|send|clear|monitor> ..."
                )
                return

            action = args[1].lower()

            if action == "read":
                # aprs message read [all]
                show_all = len(args) > 2 and args[2].lower() == "all"
                messages = self.aprs_manager.get_messages(
                    unread_only=not show_all
                )

                if not messages:
                    if show_all:
                        print_info("No APRS messages")
                    else:
                        print_info("No unread APRS messages")
                    return

                print_header(f"APRS Messages ({len(messages)})")
                for idx, msg in enumerate(messages, 1):
                    time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")

                    if msg.direction == "sent":
                        # Sent message - show delivery status and recipient
                        if msg.ack_received:
                            # Acknowledged - green checkmark
                            print_pt(
                                HTML(
                                    f"[{idx}] <green>✓</green> {time_str} To: {msg.to_call}"
                                )
                            )
                        elif msg.failed:
                            # Failed after max retries - red X
                            status_info = " (digipeated)" if msg.digipeated else " (not sent)"
                            retry_info = f" tried {msg.retry_count}x"
                            print_pt(
                                HTML(
                                    f"[{idx}] <red>✗</red> {time_str} To: {msg.to_call}{status_info}{retry_info}"
                                )
                            )
                        elif msg.digipeated:
                            # Digipeated but not ACKed - cyan arrow (on RF, waiting for recipient)
                            retry_info = (
                                f" (retry {msg.retry_count})"
                                if msg.retry_count > 0
                                else ""
                            )
                            print_pt(
                                HTML(
                                    f"[{idx}] <cyan>→</cyan> {time_str} To: {msg.to_call}{retry_info}"
                                )
                            )
                        else:
                            # Not digipeated yet - yellow dots (trying to get on RF)
                            retry_info = (
                                f" (retry {msg.retry_count})"
                                if msg.retry_count > 0
                                else ""
                            )
                            print_pt(
                                HTML(
                                    f"[{idx}] <yellow>⋯</yellow> {time_str} To: {msg.to_call}{retry_info}"
                                )
                            )
                        print_pt(f"  {msg.message}")
                    else:
                        # Received message - show read status and sender
                        status = "NEW" if not msg.read else "READ"
                        print_pt(
                            f"[{idx}] [{status}] {time_str} From: {msg.from_call}"
                        )
                        # Show message ID if present (in braces like APRS protocol)
                        if msg.message_id:
                            print_pt(f"  {msg.message} {{{msg.message_id}}}")
                        else:
                            print_pt(f"  {msg.message}")

                # Mark all as read
                marked = self.aprs_manager.mark_all_read()
                if marked > 0:
                    print_info(f"Marked {marked} message(s) as read")

            elif action == "send":
                # aprs message send <call> <text>
                if len(args) < 4:
                    print_error(
                        "Usage: aprs message send <callsign> <message text>"
                    )
                    print_error("Example: aprs message send K1MAL hello world")
                    return

                to_call = args[2].upper()
                message_text = " ".join(args[3:])

                # Send APRS message
                await self._send_aprs_message(to_call, message_text)

            elif action == "monitor":
                # aprs message monitor list [N]
                if len(args) < 3:
                    print_error("Usage: aprs message monitor list [count]")
                    print_error("Example: aprs message monitor list 20")
                    return

                monitor_action = args[2].lower()
                if monitor_action != "list":
                    print_error(f"Unknown monitor action: {monitor_action}")
                    print_error("Use: list")
                    return

                # Get limit if specified
                limit = None
                if len(args) > 3:
                    try:
                        limit = int(args[3])
                    except ValueError:
                        print_error("Count must be a number")
                        return

                messages = self.aprs_manager.get_monitored_messages(
                    limit=limit
                )

                if not messages:
                    print_info("No monitored messages")
                    return

                if limit:
                    print_header(
                        f"Monitored APRS Messages (last {len(messages)})"
                    )
                else:
                    print_header(
                        f"Monitored APRS Messages ({len(messages)} total)"
                    )

                for idx, msg in enumerate(messages, 1):
                    # Show from/to for monitored messages
                    time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                    msg_id_str = f" {{msg_id}}" if msg.message_id else ""
                    print_pt(
                        f"[{idx}] {time_str} {msg.from_call}>{msg.to_call}: {msg.message}{msg_id_str}"
                    )

            elif action == "clear":
                # aprs message clear
                count = self.aprs_manager.clear_messages()
                if count > 0:
                    print_info(f"Cleared {count} message(s)")
                else:
                    print_info("No messages to clear")

            else:
                print_error(f"Unknown message action: {action}")
                print_error("Use: read, send, monitor, clear")

        # Weather commands
        elif subcmd == "wx" or subcmd == "weather":
            if len(args) < 2:
                print_error(
                    "Usage: aprs wx list [last|name|temp|humidity|pressure]"
                )
                return

            action = args[1].lower()

            if action == "list":
                # Get sort option if provided
                sort_by = "last"  # Default to most recent first
                if len(args) > 2:
                    sort_arg = args[2].lower()
                    if sort_arg in [
                        "last",
                        "name",
                        "temp",
                        "temperature",
                        "humidity",
                        "pressure",
                    ]:
                        sort_by = sort_arg
                    else:
                        print_error(f"Unknown sort option: {sort_arg}")
                        print_error(
                            "Valid options: last, name, temp, humidity, pressure"
                        )
                        return

                stations = self.aprs_manager.get_weather_stations(
                    sort_by=sort_by
                )

                if not stations:
                    print_info("No weather reports received")
                    return

                # Show sort method in header
                sort_name = {
                    "last": "by Last Heard",
                    "name": "by Name",
                    "temp": "by Temperature",
                    "temperature": "by Temperature",
                    "humidity": "by Humidity",
                    "pressure": "by Pressure",
                }.get(sort_by, "")

                print_header(
                    f"APRS Weather Stations ({len(stations)}) {sort_name}"
                )

                # Table header
                print_pt(
                    HTML(
                        "<b>Station      Time     Temp    Humidity  Wind                 Pressure     Rain/1h  History</b>"
                    )
                )
                print_pt(
                    HTML(
                        "<gray>──────────────────────────────────────────────────────────────────────────────────────────────</gray>"
                    )
                )

                # Table rows
                for wx in stations:
                    fmt = self.aprs_manager.format_weather(wx)

                    # Get weather history count for this station
                    station = self.aprs_manager.stations.get(wx.station.upper())
                    history_count = len(station.weather_history) if station and station.weather_history else 0

                    # Note: Wind and later fields have no width limit to avoid truncation
                    print_pt(
                        f"{fmt['station']:<12} {fmt['time']:<8} {fmt['temp']:<7} {fmt['humidity']:<9} {fmt['wind']:<20} {fmt['pressure']:<12} {fmt['rain_1h']:<8} {history_count:>4}"
                    )

            else:
                print_error(f"Unknown wx action: {action}")
                print_error("Use: list [last|name|temp|humidity|pressure]")

        # Position commands
        elif subcmd == "position" or subcmd == "pos":
            if len(args) < 2:
                print_error("Usage: aprs position list")
                return

            action = args[1].lower()

            if action == "list":
                positions = self.aprs_manager.get_position_reports()

                if not positions:
                    print_info("No position reports received")
                    return

                print_header(f"APRS Position Reports ({len(positions)})")

                # Table header
                print_pt(
                    HTML(
                        "<b>Station      Time     Latitude   Longitude   Grid      Symbol  Comment</b>"
                    )
                )
                print_pt(
                    HTML(
                        "<gray>─────────────────────────────────────────────────────────────────────────────────────</gray>"
                    )
                )

                # Table rows
                for pos in positions:
                    fmt = self.aprs_manager.format_position(pos)
                    print_pt(
                        f"{fmt['station']:<12} {fmt['time']:<8} {fmt['latitude']:<10} {fmt['longitude']:<11} {fmt['grid']:<9} {fmt['symbol']:<7} {fmt['comment']}"
                    )

            else:
                print_error(f"Unknown position action: {action}")
                print_error("Use: list")

        # Station commands
        elif subcmd == "station":
            if len(args) < 2:
                print_error(
                    "Usage: aprs station list  or  aprs station show <callsign>"
                )
                return

            action = args[1].lower()

            if action == "list":
                # Parse optional sort parameter
                sort_by = "last"  # Default: most recent first
                if len(args) > 2:
                    sort_arg = args[2].lower()
                    if sort_arg in ("name", "packets", "last", "hops"):
                        sort_by = sort_arg
                    else:
                        print_error(f"Invalid sort order: {sort_arg}")
                        print_error("Valid options: name, packets, last, hops")
                        return

                stations = self.aprs_manager.get_all_stations(sort_by=sort_by)

                if not stations:
                    print_info("No stations heard yet")
                    return

                # Show sort order in header
                sort_labels = {
                    "name": "by Name",
                    "packets": "by Packets",
                    "last": "by Last Heard",
                    "hops": "by Hops",
                }
                sort_label = sort_labels.get(sort_by, "")
                print_header(
                    f"APRS Stations Heard ({len(stations)}) - Sorted {sort_label}"
                )

                # Table header
                print_pt(
                    HTML(
                        "<b>Callsign     Grid      Temp    Last Heard  Packets  Hops</b>"
                    )
                )
                print_pt(
                    HTML(
                        "<gray>────────────────────────────────────────────────────────────────</gray>"
                    )
                )

                # Table rows
                for station in stations:
                    fmt = self.aprs_manager.format_station_table_row(station)
                    hops_str = (
                        "RF"
                        if fmt["hops"] == 0
                        else str(fmt["hops"]) if fmt["hops"] < 999 else "?"
                    )
                    print_pt(
                        f"{fmt['callsign']:<12} {fmt['grid']:<9} {fmt['temp']:<7} {fmt['last_heard']:<11} {fmt['packets']:<8} {hops_str:<4}"
                    )

            elif action == "show":
                # aprs station show <callsign>
                if len(args) < 3:
                    print_error("Usage: aprs station show <callsign>")
                    print_error("Example: aprs station show N1TKS")
                    return

                callsign = args[2].upper()
                station = self.aprs_manager.get_station(callsign)

                if not station:
                    print_error(f"Station {callsign} not found")
                    print_info("Use 'aprs station list' to see all stations")
                    return

                print_header(f"Station Details: {callsign}")
                detail = self.aprs_manager.format_station_detail(station)
                print_pt(detail)

            else:
                print_error(f"Unknown station action: {action}")
                print_error("Use: list, show")

        # Database commands
        elif subcmd == "database" or subcmd == "db":
            if len(args) < 2:
                print_error("Usage: aprs database <save|clear|prune> ...")
                print_error(
                    "  aprs database save          - Manually save database to disk"
                )
                print_error(
                    "  aprs database clear         - Clear entire APRS database"
                )
                print_error(
                    "  aprs database prune <days>  - Remove entries older than N days"
                )
                return

            action = args[1].lower()

            if action == "save":
                # Manually save the database
                print_info("Saving APRS database...")
                count = self.aprs_manager.save_database()
                if count > 0:
                    # Get file size
                    db_file = self.aprs_manager.db_file
                    try:
                        import os
                        size = os.path.getsize(db_file)
                        size_kb = size / 1024
                        print_info(
                            f"✓ Saved {count} station(s) to {db_file}"
                        )
                        print_info(f"  File size: {size_kb:.1f} KB")
                    except:
                        print_info(f"✓ Saved {count} station(s)")
                else:
                    print_error("Failed to save database (check error messages above)")

            elif action == "clear":
                # Confirm before clearing
                print_warning(
                    "This will delete ALL APRS stations and messages from the database!"
                )
                print_info("Type 'yes' to confirm:")
                # We're in async context, but can't easily read input
                # So just do it with a warning
                station_count, message_count = (
                    self.aprs_manager.clear_database()
                )
                print_info(
                    f"Cleared {station_count} station(s) and {message_count} message(s)"
                )
                print_info("Database saved to disk")
                self.aprs_manager.save_database()

            elif action == "prune":
                if len(args) < 3:
                    print_error("Usage: aprs database prune <days>")
                    print_error(
                        "Example: aprs database prune 7  (removes entries >7 days old)"
                    )
                    return

                try:
                    days = int(args[2])
                    if days <= 0:
                        print_error("Days must be positive")
                        return
                except ValueError:
                    print_error("Days must be a number")
                    return

                station_count, message_count = (
                    self.aprs_manager.prune_database(days)
                )
                print_info(
                    f"Pruned {station_count} station(s) and {message_count} message(s) older than {days} days"
                )
                print_info("Database saved to disk")
                self.aprs_manager.save_database()

            else:
                print_error(f"Unknown database action: {action}")
                print_error("Use: save, clear, prune")

        # Unknown subcommand
        else:
            print_error(f"Unknown APRS subcommand: {subcmd}")
            print_error(
                "Valid subcommands: message, wx, position, station, database"
            )
            print_error("Use 'aprs' with no arguments to see help")

    async def _send_aprs_message(self, to_call: str, message: str):
        """Send an APRS message.

        Args:
            to_call: Destination callsign
            message: Message text
        """
        # Validate callsign format
        if "-" in to_call:
            call_base, ssid = to_call.split("-", 1)
            if len(call_base) > 6 or not call_base.isalnum():
                print_error("Invalid callsign format")
                return
            try:
                ssid_num = int(ssid)
                if ssid_num < 0 or ssid_num > 15:
                    print_error("SSID must be 0-15")
                    return
            except ValueError:
                print_error("Invalid SSID")
                return
        else:
            if len(to_call) > 6 or not to_call.isalnum():
                print_error("Invalid callsign format")
                return

        # Format as APRS message: :CALLSIGN :message text{msgid
        # Destination callsign must be exactly 9 characters (right-padded with spaces)
        padded_to = to_call.upper().ljust(9)

        # Generate simple message ID (1-5 chars) - use last 5 digits of timestamp

        msg_id = str(int(time.time() * 1000))[-5:]

        # Build APRS message format
        aprs_message = f":{padded_to}:{message}{{{msg_id}"

        print_debug(
            f"APRS message format: recipient='{to_call}' padded='{padded_to}' msgid={msg_id}",
            level=5,
        )
        print_debug(f"APRS info field: '{aprs_message}'", level=5)

        print_info(f"Sending APRS message to {to_call}: {message}")

        # Track sent message for ACK monitoring BEFORE sending
        # This prevents race condition if ACK arrives very quickly
        self.aprs_manager.add_sent_message(to_call, message, msg_id)

        # Send with our callsign as source, to APRS as destination
        print_debug(
            f"Calling send_aprs: from_call={self.aprs_manager.my_callsign}, to_call=APRS",
            level=5,
        )
        await self.radio.send_aprs(
            self.aprs_manager.my_callsign, aprs_message, to_call="APRS"
        )

        print_info("✓ APRS packet sent")

    async def _send_aprs_ack(self, to_call: str, acked_msg_id: str):
        """Send an APRS ACK message with retry tracking.

        Args:
            to_call: Destination callsign (who sent the original message)
            acked_msg_id: The message ID we're acknowledging
        """
        # Format ACK: :SENDER___:ack{msgid}
        # Sender callsign must be exactly 9 characters (left-padded with spaces)
        padded_to = to_call.ljust(9)
        ack_message = f"ack{acked_msg_id}"
        ack_info = f":{padded_to}:{ack_message}"

        print_debug(
            f"Sending ACK to {to_call} for message ID '{acked_msg_id}'",
            level=5
        )

        # Track sent ACK for retry monitoring BEFORE sending
        # ACKs don't have their own message IDs (prevents ACK loops)
        # We mark them as "ack" messages by using the full ack text as the message
        self.aprs_manager.add_sent_message(to_call, ack_message, message_id=None)

        # Send ACK back to sender
        await self.radio.send_aprs(
            self.aprs_manager.my_callsign,
            ack_info,
            to_call="APRS"
        )

        print_debug(f"✓ ACK sent to {to_call}", level=5)

    async def cmd_wx(self, args):
        """Weather station commands.

        Usage:
            wx                  - Show weather station status
            wx show             - Show current weather data
            wx fetch            - Fetch fresh weather data now
            wx connect          - Connect to weather station
            wx disconnect       - Disconnect from weather station
            wx test             - Test connection to weather station
        """
        if not hasattr(self, 'weather_manager'):
            print_error("Weather station not available")
            return

        if not args:
            # Show status
            status = self.weather_manager.get_status()

            print_header("Weather Station Status")
            print_pt(f"Enabled: {status['enabled']}")
            print_pt(f"Configured: {status['configured']}")
            print_pt(f"Connected: {status['connected']}")

            if status['backend']:
                print_pt(f"Backend: {status['backend']}")
            if status['address']:
                print_pt(f"Address: {status['address']}")
            if status['port']:
                print_pt(f"Port: {status['port']}")

            print_pt(f"Update Interval: {status['update_interval']}s")

            if status['last_update']:
                print_pt(f"Last Update: {status['last_update']}")

            if status['has_data']:
                print_pt("\nUse 'wx show' to see current weather data")

            if not status['configured']:
                print_pt("\nConfiguration:")
                print_pt("  WX_BACKEND <ecowitt|davis|...>")
                print_pt("  WX_ADDRESS <IP or serial port>")
                print_pt("  WX_ENABLE ON")

            return

        subcmd = args[0].lower()

        if subcmd == "show":
            # Show current weather
            data = self.weather_manager.get_cached_weather()
            if not data:
                print_error("No weather data available")
                print_info("Use 'wx fetch' to get fresh data")
                return

            print_header("Current Weather")

            if data.temperature_outdoor is not None:
                print_pt(f"Outdoor Temperature: {data.temperature_outdoor:.1f}°F")
            if data.temperature_indoor is not None:
                print_pt(f"Indoor Temperature: {data.temperature_indoor:.1f}°F")
            if data.dew_point is not None:
                print_pt(f"Dew Point: {data.dew_point:.1f}°F")

            if data.humidity_outdoor is not None:
                print_pt(f"Outdoor Humidity: {data.humidity_outdoor}%")

            if data.pressure_relative is not None:
                print_pt(f"Pressure: {data.pressure_relative:.2f} mb")

            if data.wind_speed is not None:
                print_pt(f"Wind: {data.wind_speed:.1f} mph @ {data.wind_direction}°")
            if data.wind_gust is not None:
                print_pt(f"Gust: {data.wind_gust:.1f} mph")

            if data.rain_daily is not None:
                print_pt(f"Rain (24h): {data.rain_daily:.2f} in")

            print_pt(f"\nLast updated: {data.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")

        elif subcmd == "fetch":
            # Fetch fresh data
            print_info("Fetching weather data...")
            data = await self.weather_manager.get_current_weather()

            if not data:
                print_error("Failed to fetch weather data")
                return

            print_info("✓ Weather data updated")
            # Show the data
            await self.cmd_wx(["show"])

        elif subcmd == "connect":
            # Connect to weather station
            success = await self.weather_manager.connect()
            if not success:
                print_error("Failed to connect to weather station")

        elif subcmd == "disconnect":
            # Disconnect from weather station
            await self.weather_manager.disconnect()

        elif subcmd == "test":
            # Test connection
            print_info("Testing connection...")
            if not self.weather_manager._station:
                print_error("Not connected to weather station")
                print_info("Use 'wx connect' first")
                return

            success = await self.weather_manager._station.test_connection()
            if success:
                print_info("✓ Connection test passed")
            else:
                print_error("Connection test failed")

        else:
            print_error(f"Unknown wx command: {subcmd}")
            print_info("Use 'wx' with no args to see status")

    async def cmd_scan_ble(self, args):
        """Scan all BLE characteristics to find audio channels."""
        print_header("BLE Characteristics Scan")

        if not self.radio.client:
            print_error("BLE scan not available in serial mode")
            return

        try:
            services = self.radio.client.services

            for service in services:
                print_pt(HTML(f"\n<b>Service:</b> {service.uuid}"))
                print_pt(f"  Handle: {service.handle}")

                for char in service.characteristics:
                    props = ", ".join(char.properties)
                    print_pt(
                        HTML(
                            f"\n  <yellow>Characteristic:</yellow> {char.uuid}"
                        )
                    )
                    print_pt(f"    Handle: {char.handle}")
                    print_pt(f"    Properties: {props}")

                    # Check if this is a known UUID
                    if char.uuid == RADIO_WRITE_UUID:
                        print_pt(
                            HTML("    <green>→ RADIO_WRITE (commands)</green>")
                        )
                    elif char.uuid == RADIO_INDICATE_UUID:
                        print_pt(
                            HTML(
                                "    <green>→ RADIO_INDICATE (responses)</green>"
                            )
                        )
                    elif char.uuid == TNC_TX_UUID:
                        print_pt(
                            HTML("    <green>→ TNC_TX (TNC transmit)</green>")
                        )
                    elif char.uuid == TNC_RX_UUID:
                        print_pt(
                            HTML("    <green>→ TNC_RX (TNC receive)</green>")
                        )
                    elif (
                        "notify" in char.properties
                        or "indicate" in char.properties
                    ):
                        print_pt(
                            HTML(
                                "    <cyan>→ Potential audio/data stream</cyan>"
                            )
                        )

                    # Show descriptors
                    if char.descriptors:
                        for desc in char.descriptors:
                            print_pt(f"      Descriptor: {desc.uuid}")

            print_pt("")
            print_info(
                "Look for characteristics with 'notify' property for audio streams"
            )
            print_pt("")

        except Exception as e:
            print_error(f"Failed to scan characteristics: {e}")

    async def cmd_poweron(self, args):
        """Power on the radio."""
        print_info("Powering on radio...")
        success = await self.radio.set_hardware_power(True)
        if success:
            print_info("✓ Radio powered on")
        else:
            print_error("Failed to power on radio")

    async def cmd_poweroff(self, args):
        """Power off the radio."""
        print_info("Powering off radio...")
        success = await self.radio.set_hardware_power(False)
        if success:
            print_info("✓ Radio powered off")
        else:
            print_error("Failed to power off radio")

    async def cmd_quit(self, args):
        """Quit the application."""
        print_info("Exiting...")
        # Save APRS station database to disk
        count = self.aprs_manager.save_database()
        if count > 0:
            print_info(f"Saved {count} station(s) to APRS database")
        self.radio.running = False

    async def cmd_tnc(self, args, auto_connect=None):
        """Enter TNC terminal mode."""

        print_header("TNC Terminal Mode")
        print_pt(
            HTML(
                f"<gray>Current MYCALL: <b>{self.tnc_config.get('MYCALL')}</b></gray>"
            )
        )
        print_pt(
            HTML(
                "<gray>Use '~~~' to toggle between conversation and command mode</gray>"
            )
        )
        print_pt("")

        self.tnc_mode = True

        # Flag to trigger auto-connect after setup
        do_auto_connect = auto_connect
        # Disable tnc_monitor display - AX25Adapter callback handles everything
        self.radio.tnc_mode_active = True
        # Add keybinding for Ctrl+] to toggle between conversation and command mode

        kb = KeyBindings()

        @kb.add("c-]")
        def _escape_tnc(event):
            # Toggle conversation mode and exit prompt to refresh
            print_debug("Ctrl+] keybinding triggered", level=6)
            self.tnc_conversation_mode = not self.tnc_conversation_mode
            # Exit with special marker to trigger mode change message
            event.app.exit(result="<<<TOGGLE_MODE>>>")

        @kb.add("?")
        def _show_tnc_help(event):
            """Show context-sensitive help when '?' is pressed (IOS-style)."""
            from prompt_toolkit.completion import CompleteEvent
            from prompt_toolkit.document import Document
            from prompt_toolkit.formatted_text import to_plain_text

            buffer = event.current_buffer
            text_before_cursor = buffer.text[: buffer.cursor_position]

            # IOS-style context help: "command ?" shows options for next token
            # If text ends with space, show completions. Otherwise insert "?" literally
            if text_before_cursor.strip() and not text_before_cursor.endswith(' '):
                # Not asking for help - insert ? as regular character
                buffer.insert_text('?')
                return

            # Get completions at current position
            document = Document(
                text=buffer.text, cursor_position=buffer.cursor_position
            )
            completions = list(
                tnc_completer.get_completions(document, CompleteEvent())
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
                        print_pt(f"  {comp.text:<15} {meta_text}")
                    else:
                        print_pt(f"  {comp.text}")
                print_pt("")  # Blank line after help
            else:
                # No completions - show general TNC help
                print_pt(
                    "\n<TNC Commands: CONNECT, DISCONNECT, CONVERSE, STATUS, K/UNPROTO, MONITOR, DIGIPEATER, DISPLAY, RESET>"
                )
                print_pt("")

            # Redisplay the prompt with current text intact
            # This is done automatically by not calling validate_and_handle()

        # Create TNC completer for command mode
        tnc_completer = TNCCompleter()

        session = PromptSession(
            completer=tnc_completer,
            complete_while_typing=False,
            key_bindings=kb,
        )
        # initialize pyax25 AX25 instance for TNC mode
        try:
            if getattr(self, "ax25", None) is not None:
                try:
                    self.ax25.init_ax25()
                except Exception as e:
                    print_error(
                        f"Failed to initialize pyax25 AX25 instance: {e}"
                    )
                    return
        except Exception:
            pass
        # Apply DEBUGFRAMES setting from TNC config and register debug callback
        try:
            # Save original DEBUG_LEVEL state before entering TNC mode
            if self._original_debug_state is None:
                self._original_debug_state = constants.DEBUG_LEVEL

            df = (self.tnc_config.get("DEBUGFRAMES") or "").upper()
            self.tnc_debug_frames = df in ("ON", "1", "YES", "TRUE")

            # Set DEBUG_LEVEL based on DEBUGFRAMES and console debug mode
            # If DEBUGFRAMES is ON, enable at least level 1 (frame debugging)
            # If console debug is already higher, keep the higher level
            if self.tnc_debug_frames:
                # Enable frame debugging (level 1) at minimum
                if constants.DEBUG_LEVEL < 1:
                    constants.DEBUG_LEVEL = 1
            else:
                # Restore original level
                constants.DEBUG_LEVEL = self._original_debug_state

            if getattr(self, "ax25", None) is not None:
                if self.tnc_debug_frames:
                    try:
                        self.ax25.register_frame_debug(
                            self._tnc_frame_debug_cb
                        )
                    except Exception:
                        pass
                else:
                    try:
                        self.ax25.register_frame_debug(None)
                    except Exception:
                        pass
        except Exception:
            pass

        with patch_stdout():
            while self.tnc_mode and self.radio.running:
                try:
                    # Handle auto-connect on first iteration
                    if do_auto_connect:
                        await asyncio.sleep(
                            0.5
                        )  # Give TNC mode time to initialize
                        print_info(f"Auto-connecting to {do_auto_connect}...")
                        await self._process_tnc_command(
                            f"connect {do_auto_connect}"
                        )
                        do_auto_connect = None  # Only do this once
                        continue  # Skip to next iteration to show connected prompt

                    # Sync tnc_connected_to with actual link state
                    # If adapter reports link is down, clear our connected state
                    if self.tnc_connected_to and not getattr(
                        self.radio, "tnc_link_established", True
                    ):
                        print_pt("")  # New line to clear prompt
                        print_info(
                            f"*** DISCONNECTED from {self.tnc_connected_to}"
                        )
                        self.tnc_connected_to = None
                        self.tnc_conversation_mode = (
                            False  # Exit conversation mode on disconnect
                        )
                        continue  # Restart loop immediately to show updated prompt

                    # Show different prompt based on connection state and conversation mode
                    if self.tnc_connected_to:
                        if self.tnc_conversation_mode:
                            # No prompt in conversation mode - let BBS/node prompts be visible
                            prompt_text = ""
                        else:
                            prompt_text = f"<b><cyan>TNC({self.tnc_connected_to}:CMD)&gt;</cyan></b> "
                    else:
                        if self.tnc_conversation_mode:
                            prompt_text = (
                                "<b><yellow>TNC(CONV)&gt;</yellow></b> "
                            )
                        else:
                            prompt_text = "<b><cyan>TNC&gt;</cyan></b> "

                    # Create prompt task and disconnect watcher task
                    prompt_task = asyncio.create_task(
                        session.prompt_async(
                            HTML(prompt_text), key_bindings=kb
                        )
                    )

                    async def watch_disconnect():
                        """Monitor connection state and return when disconnected."""
                        initial_connection = self.tnc_connected_to
                        if not initial_connection:
                            # Not connected - wait forever (will be cancelled by prompt)
                            await asyncio.Event().wait()
                            return False
                        # Connected - monitor for disconnect
                        while self.tnc_connected_to == initial_connection:
                            await asyncio.sleep(0.1)  # Check every 100ms
                            if not getattr(
                                self.radio, "tnc_link_established", True
                            ):
                                return True  # Disconnected
                        return False  # Connection changed or cleared

                    watcher_task = asyncio.create_task(watch_disconnect())

                    # Wait for either prompt completion or disconnect detection
                    done, pending = await asyncio.wait(
                        {prompt_task, watcher_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Cancel the pending task
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                    # Check which task completed
                    if watcher_task in done and watcher_task.result():
                        # Disconnected while waiting for input
                        print_pt("")  # New line
                        print_info(
                            f"*** DISCONNECTED from {self.tnc_connected_to}"
                        )
                        self.tnc_connected_to = None
                        self.tnc_conversation_mode = (
                            False  # Exit conversation mode on disconnect
                        )
                        continue  # Show updated prompt

                    # Prompt completed normally, get the result
                    line = prompt_task.result()

                    # Debug: show what we received (only in debug mode)
                    if line and constants.DEBUG_LEVEL >= 1:
                        print_debug(f"Received input: {repr(line)}", level=6)

                    if not line:
                        continue

                    # Check for mode toggle marker from Ctrl+] keybinding
                    if line == "<<<TOGGLE_MODE>>>":
                        # Mode was already toggled in keybinding, just show feedback
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Check for text-based escape sequence: ~~~
                    if line.strip() == "~~~":
                        print_debug("Escape sequence '~~~' detected", level=6)
                        self.tnc_conversation_mode = (
                            not self.tnc_conversation_mode
                        )
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Check for escape sequence (fallback if keybinding doesn't work)
                    if line == "\x1d":  # ^]
                        print_debug(
                            "Ctrl+] received as input character (keybinding fallback)",
                            level=6,
                        )
                        self.tnc_conversation_mode = (
                            not self.tnc_conversation_mode
                        )
                        if self.tnc_conversation_mode:
                            print_pt(
                                HTML(
                                    "<green>[Conversation mode]</green> Type text to send, type '~~~' to exit"
                                )
                            )
                        else:
                            print_pt(
                                HTML(
                                    "<cyan>[Command mode]</cyan> Type commands, type '~~~' for conversation mode"
                                )
                            )
                        continue

                    # Process based on conversation mode
                    if self.tnc_conversation_mode:
                        # In conversation mode - send text (either connected or UI frames)
                        await self._tnc_send_text(line)
                    else:
                        # In command mode - process as TNC command
                        await self._process_tnc_command(line)

                except EOFError:
                    print_pt("")
                    break
                except KeyboardInterrupt:
                    print_pt("")
                    print_info("Interrupt received - cleaning up...")
                    if self.tnc_connected_to:
                        try:
                            await self._tnc_disconnect()
                        except Exception:
                            pass
                    # Exit TNC mode on keyboard interrupt
                    break
                except Exception as e:
                    print_error(f"TNC error: {e}")

        self.tnc_mode = False
        # Re-enable tnc_monitor display for regular console mode
        self.radio.tnc_mode_active = False
        # Restore original DEBUG_LEVEL when exiting TNC mode
        if self._original_debug_state is not None:
            constants.DEBUG_LEVEL = self._original_debug_state
            self._original_debug_state = None
        # close pyax25 AX25 instance when leaving TNC mode
        try:
            if getattr(self, "ax25", None) is not None:
                try:
                    await self.ax25.close_ax25()
                except Exception as e:
                    print_error(f"Error closing AX25 adapter: {e}")
        except Exception:
            pass
        print_info("Exited TNC mode")
        print_pt("")

    def _tnc_frame_debug_cb(self, direction, kiss_frame: bytes):
        try:
            if not self.tnc_debug_frames:
                return
            if direction == "tx":
                print_debug(
                    f"TNC TX KISS ({len(kiss_frame)} bytes): {kiss_frame.hex()}",
                    level=4,
                )
                try:
                    ascii = "".join(
                        chr(b) if 32 <= b <= 126 else "." for b in kiss_frame
                    )
                    print_debug(f"TNC TX ASCII: {ascii}", level=4)
                except Exception:
                    pass
            elif direction == "rx":
                print_debug(
                    f"TNC RX KISS ({len(kiss_frame)} bytes): {kiss_frame.hex()}",
                    level=4,
                )
                try:
                    ascii = "".join(
                        chr(b) if 32 <= b <= 126 else "." for b in kiss_frame
                    )
                    print_debug(f"TNC RX ASCII: {ascii}", level=4)
                except Exception:
                    pass
        except Exception:
            pass

    async def _process_tnc_command(self, line):
        """Process TNC command."""
        parts = line.strip().split()
        if not parts:
            return

        cmd = parts[0].upper()
        args = parts[1:]

        if cmd == "CONNECT" or cmd == "C":
            if not args:
                print_error("Usage: CONNECT <callsign> [via <path>]")
                return
            # Parse callsign and optional via path
            callsign = args[0].upper()
            path = []
            if len(args) > 1 and args[1].upper() == "VIA":
                path = [p.upper() for p in args[2:]]
            await self._tnc_connect(callsign, path)

        elif cmd == "DISCONNECT" or cmd == "D":
            await self._tnc_disconnect()

        elif cmd == "CONV" or cmd == "CONVERSE":
            # Enter conversation mode
            self.tnc_conversation_mode = True
            if self.tnc_connected_to:
                print_info(
                    f"Entering conversation mode with {self.tnc_connected_to}"
                )
                print_pt(
                    HTML(
                        "<gray>Type text to send, type '~~~' to return to command mode</gray>"
                    )
                )
            else:
                print_info(
                    "Entering conversation mode - will send UI frames to UNPROTO address"
                )
                unproto = self.tnc_config.get("UNPROTO")
                print_pt(HTML(f"<gray>UNPROTO: {unproto}</gray>"))
                print_pt(
                    HTML(
                        "<gray>Type text to send, type '~~~' to return to command mode</gray>"
                    )
                )

        elif cmd == "MYCALL":
            if not args:
                print_pt(f"MYCALL: {self.tnc_config.get('MYCALL')}")
                return
            callsign = args[0].upper()
            self.tnc_config.set("MYCALL", callsign)
            print_info(f"MYCALL set to {callsign}")

        elif cmd == "MYALIAS":
            if not args:
                print_pt(f"MYALIAS: {self.tnc_config.get('MYALIAS')}")
                return
            alias = args[0].upper()
            self.tnc_config.set("MYALIAS", alias)
            print_info(f"MYALIAS set to {alias}")

        elif cmd == "MYLOCATION":
            if not args:
                location = self.tnc_config.get('MYLOCATION')
                if location:
                    print_pt(f"MYLOCATION: {location}")
                else:
                    print_pt("MYLOCATION: (not set)")
                return
            grid = args[0].upper()
            # TNCConfig.set() handles validation and displays result
            if self.tnc_config.set("MYLOCATION", grid):
                # Broadcast position to web UI
                from src.aprs_manager import APRSManager
                try:
                    lat, lon = APRSManager.maidenhead_to_latlon(grid)
                    if self.aprs_manager._web_broadcast:
                        await self.aprs_manager._web_broadcast('gps_update', {
                            'latitude': lat,
                            'longitude': lon,
                            'altitude': None,
                            'locked': True,
                            'source': 'MYLOCATION'
                        })
                except Exception as e:
                    print_debug(f"Failed to broadcast MYLOCATION: {e}", level=6)
            else:
                print_error("Failed to set MYLOCATION")

        elif cmd == "UNPROTO":
            if not args:
                print_pt(f"UNPROTO: {self.tnc_config.get('UNPROTO')}")
                return
            # Parse destination and optional via path
            unproto = " ".join(args).upper()
            self.tnc_config.set("UNPROTO", unproto)
            print_info(f"UNPROTO set to {unproto}")

        elif cmd == "MONITOR":
            if not args:
                print_pt(f"MONITOR: {self.tnc_config.get('MONITOR')}")
                return
            value = args[0].upper()
            if value in ("ON", "OFF"):
                self.tnc_config.set("MONITOR", value)
                print_info(f"MONITOR set to {value}")
            else:
                print_error("Usage: MONITOR <ON|OFF>")

        elif cmd == "AUTO_ACK":
            if not args:
                status = self.tnc_config.get("AUTO_ACK")
                print_pt(f"AUTO_ACK: {status}")
                print_pt("  Automatically send ACK for APRS messages with message IDs")
                return
            value = args[0].upper()
            if value in ("ON", "OFF"):
                self.tnc_config.set("AUTO_ACK", value)
                print_info(f"AUTO_ACK set to {value}")
            else:
                print_error("Usage: AUTO_ACK <ON|OFF>")

        elif cmd == "RETRY":
            if not args:
                retry = self.tnc_config.get("RETRY")
                retry_fast = self.tnc_config.get("RETRY_FAST")
                retry_slow = self.tnc_config.get("RETRY_SLOW")
                print_pt(f"RETRY: {retry} attempts")
                print_pt(f"  RETRY_FAST: {retry_fast}s (for non-digipeated messages)")
                print_pt(f"  RETRY_SLOW: {retry_slow}s (for digipeated but not ACKed)")
                return
            try:
                count = int(args[0])
                if count < 1 or count > 10:
                    print_error("RETRY must be between 1 and 10")
                    return
                self.tnc_config.set("RETRY", str(count))
                # Update APRS manager
                self.aprs_manager.max_retries = count
                print_info(f"RETRY set to {count}")
            except ValueError:
                print_error("Usage: RETRY <1-10>")

        elif cmd == "RETRY_FAST":
            if not args:
                value = self.tnc_config.get("RETRY_FAST")
                print_pt(f"RETRY_FAST: {value} seconds")
                print_pt("  Fast retry timeout for non-digipeated messages (trying to get on RF)")
                return
            try:
                timeout = int(args[0])
                if timeout < 5 or timeout > 300:
                    print_error("RETRY_FAST must be between 5 and 300 seconds")
                    return
                self.tnc_config.set("RETRY_FAST", str(timeout))
                # Update APRS manager
                self.aprs_manager.retry_fast = timeout
                print_info(f"RETRY_FAST set to {timeout} seconds")
            except ValueError:
                print_error("Usage: RETRY_FAST <5-300>")

        elif cmd == "RETRY_SLOW":
            if not args:
                value = self.tnc_config.get("RETRY_SLOW")
                print_pt(f"RETRY_SLOW: {value} seconds ({int(value)//60} minutes)")
                print_pt("  Slow retry timeout for digipeated but not ACKed messages")
                return
            try:
                timeout = int(args[0])
                if timeout < 60 or timeout > 86400:
                    print_error("RETRY_SLOW must be between 60 and 86400 seconds (1 min - 24 hours)")
                    return
                self.tnc_config.set("RETRY_SLOW", str(timeout))
                # Update APRS manager
                self.aprs_manager.retry_slow = timeout
                print_info(f"RETRY_SLOW set to {timeout} seconds ({timeout//60} minutes)")
            except ValueError:
                print_error("Usage: RETRY_SLOW <60-86400>")

        elif cmd == "DEBUG_BUFFER":
            if not args:
                # Show current setting
                setting = self.tnc_config.get("DEBUG_BUFFER")
                print_pt(f"DEBUG_BUFFER: {setting}")
                if setting.upper() == "OFF":
                    print_pt("  Mode: Simple (last 10 frames)")
                else:
                    print_pt(f"  Mode: Buffer ({setting} MB)")
                    if self.frame_history.buffer_mode:
                        size_mb = self.frame_history.current_size_bytes / (1024 * 1024)
                        print_pt(f"  Current usage: {size_mb:.2f} MB")
                        print_pt(f"  Frames stored: {len(self.frame_history.frames)}")

                # Show persistence info
                if os.path.exists(FrameHistory.BUFFER_FILE):
                    file_size = os.path.getsize(FrameHistory.BUFFER_FILE)
                    file_size_kb = file_size / 1024
                    print_pt(f"  Saved to: {FrameHistory.BUFFER_FILE} ({file_size_kb:.1f} KB)")
                else:
                    print_pt(f"  Save file: {FrameHistory.BUFFER_FILE} (not saved yet)")
                print_pt(f"  Auto-save: Every {FrameHistory.AUTO_SAVE_INTERVAL} frames")
                return

            value = args[0].upper()
            if value == "OFF":
                self.tnc_config.set("DEBUG_BUFFER", "OFF")
                self.frame_history.set_buffer_mode(False)
                print_info("DEBUG_BUFFER set to OFF (simple mode, last 10 frames)")
            else:
                try:
                    size_mb = int(value)
                    if size_mb < 1 or size_mb > 1000:
                        print_error("Buffer size must be 1-1000 MB")
                        return
                    self.tnc_config.set("DEBUG_BUFFER", str(size_mb))
                    self.frame_history.set_buffer_mode(True, size_mb)
                    print_info(f"DEBUG_BUFFER set to {size_mb} MB")
                except ValueError:
                    print_error("Usage: DEBUG_BUFFER <OFF|1-1000>")

        elif cmd == "BEACON":
            if not args:
                # Show beacon status
                status = self.tnc_config.get("BEACON")
                interval = self.tnc_config.get("BEACON_INTERVAL")
                path = self.tnc_config.get("BEACON_PATH")
                symbol = self.tnc_config.get("BEACON_SYMBOL")
                comment = self.tnc_config.get("BEACON_COMMENT")
                print_pt(f"BEACON: {status}")
                print_pt(f"  Interval: {interval} minutes")
                print_pt(f"  Path: {path}")
                print_pt(f"  Symbol: {symbol}")
                print_pt(f"  Comment: {comment}")

                # Show last beacon time
                if self.last_beacon_time:
                    elapsed = (datetime.now() - self.last_beacon_time).total_seconds()
                    elapsed_min = int(elapsed // 60)
                    elapsed_sec = int(elapsed % 60)
                    time_str = self.last_beacon_time.strftime("%H:%M:%S")
                    print_pt(f"  Last beacon: {time_str} ({elapsed_min}m {elapsed_sec}s ago)")

                    # Show time until next beacon
                    if status == "ON":
                        beacon_interval_sec = int(interval) * 60
                        remaining = max(0, beacon_interval_sec - elapsed)
                        remaining_min = int(remaining // 60)
                        remaining_sec = int(remaining % 60)
                        if remaining > 0:
                            print_pt(f"  Next beacon: in {remaining_min}m {remaining_sec}s")
                        else:
                            print_pt(f"  Next beacon: due now")
                else:
                    print_pt(f"  Last beacon: never")

                if self.gps_locked and self.gps_position:
                    pos = self.gps_position
                    print_pt(f"  GPS: {pos['latitude']:.6f}, {pos['longitude']:.6f} (LOCKED)")
                else:
                    print_pt(f"  GPS: NO LOCK")
                return

            subcmd = args[0].upper()
            if subcmd in ("ON", "OFF"):
                self.tnc_config.set("BEACON", subcmd)
                print_info(f"BEACON set to {subcmd}")
            elif subcmd == "INTERVAL":
                if len(args) < 2:
                    print_error("Usage: BEACON INTERVAL <minutes>")
                    return
                try:
                    interval = int(args[1])
                    if interval < 1:
                        print_error("Interval must be at least 1 minute")
                        return
                    self.tnc_config.set("BEACON_INTERVAL", str(interval))
                    print_info(f"Beacon interval set to {interval} minutes")
                except ValueError:
                    print_error("Invalid interval value")
            elif subcmd == "PATH":
                if len(args) < 2:
                    print_error("Usage: BEACON PATH <path>")
                    return
                path = " ".join(args[1:])
                self.tnc_config.set("BEACON_PATH", path)
                print_info(f"Beacon path set to {path}")
            elif subcmd == "SYMBOL":
                if len(args) < 2:
                    print_error("Usage: BEACON SYMBOL <table><code>")
                    print_error("Example: BEACON SYMBOL /[ (jogger)")
                    return
                symbol = args[1]
                if len(symbol) != 2:
                    print_error("Symbol must be exactly 2 characters (table + code)")
                    return
                self.tnc_config.set("BEACON_SYMBOL", symbol)
                print_info(f"Beacon symbol set to {symbol}")
            elif subcmd == "COMMENT":
                if len(args) < 2:
                    print_error("Usage: BEACON COMMENT <text>")
                    return
                comment = " ".join(args[1:])
                self.tnc_config.set("BEACON_COMMENT", comment)
                print_info(f"Beacon comment set to: {comment}")
            elif subcmd == "NOW":
                # Send beacon immediately
                # Try GPS first, fall back to MYLOCATION
                if self.gps_locked and self.gps_position:
                    print_info("Sending beacon now (GPS)...")
                    await self._send_position_beacon(self.gps_position)
                elif self.tnc_config.get("MYLOCATION"):
                    print_info("Sending beacon now (MYLOCATION)...")
                    await self._send_position_beacon(None)  # Use MYLOCATION
                else:
                    print_error("No position available (GPS unavailable and MYLOCATION not set)")
                    return
            else:
                print_error("Usage: BEACON <ON|OFF|INTERVAL|PATH|SYMBOL|COMMENT|NOW>")

        # Weather station commands
        elif cmd == "WX_ENABLE":
            if not args:
                status = self.tnc_config.get("WX_ENABLE")
                print_pt(f"WX_ENABLE: {status}")
                return

            value = args[0].upper()
            if value not in ["ON", "OFF"]:
                print_error("Usage: WX_ENABLE <ON|OFF>")
                return

            self.tnc_config.set("WX_ENABLE", value)
            enabled = (value == "ON")
            self.weather_manager.configure(enabled=enabled)

            if enabled and not self.weather_manager._connected:
                # Try to connect
                await self.weather_manager.connect()
            elif not enabled and self.weather_manager._connected:
                # Disconnect
                await self.weather_manager.disconnect()

            print_info(f"WX_ENABLE set to {value}")

        elif cmd == "WX_BACKEND":
            if not args:
                backend = self.tnc_config.get("WX_BACKEND")
                print_pt(f"WX_BACKEND: {backend}")
                print_pt("")
                print_pt("Available backends:")
                from src.weather_manager import WeatherStationManager
                for backend_id, info in WeatherStationManager.list_backends().items():
                    print_pt(f"  {backend_id:12} - {info['description']}")
                return

            backend = args[0].lower()
            if self.weather_manager.configure(backend=backend):
                self.tnc_config.set("WX_BACKEND", backend)
                print_info(f"WX_BACKEND set to {backend}")

        elif cmd == "WX_ADDRESS":
            if not args:
                address = self.tnc_config.get("WX_ADDRESS")
                print_pt(f"WX_ADDRESS: {address or '(not set)'}")
                return

            address = args[0]
            if self.weather_manager.configure(address=address):
                self.tnc_config.set("WX_ADDRESS", address)
                print_info(f"WX_ADDRESS set to {address}")

        elif cmd == "WX_PORT":
            if not args:
                port = self.tnc_config.get("WX_PORT")
                print_pt(f"WX_PORT: {port or '(auto)'}")
                return

            try:
                port = int(args[0])
                if self.weather_manager.configure(port=port):
                    self.tnc_config.set("WX_PORT", str(port))
                    print_info(f"WX_PORT set to {port}")
            except ValueError:
                print_error("WX_PORT must be a number (1-65535)")

        elif cmd == "WX_INTERVAL":
            if not args:
                interval = self.tnc_config.get("WX_INTERVAL")
                print_pt(f"WX_INTERVAL: {interval} seconds")
                return

            try:
                interval = int(args[0])
                if self.weather_manager.configure(update_interval=interval):
                    self.tnc_config.set("WX_INTERVAL", str(interval))
                    print_info(f"WX_INTERVAL set to {interval} seconds")
            except ValueError:
                print_error("WX_INTERVAL must be a number (30-3600)")

        elif cmd == "WX_AVERAGE_WIND":
            if not args:
                status = self.tnc_config.get("WX_AVERAGE_WIND")
                print_pt(f"WX_AVERAGE_WIND: {status}")
                print_pt("")
                print_pt("Wind averaging for beacons:")
                print_pt("  ON  - Average wind speed over beacon interval (recommended)")
                print_pt("  OFF - Use instantaneous wind reading")
                return

            value = args[0].upper()
            if value not in ["ON", "OFF"]:
                print_error("Usage: WX_AVERAGE_WIND <ON|OFF>")
                return

            self.tnc_config.set("WX_AVERAGE_WIND", value)
            self.weather_manager.average_wind = (value == "ON")
            print_info(f"WX_AVERAGE_WIND set to {value}")

        elif cmd == "DIGIPEATER" or cmd == "DIGI":
            # Digipeater mode control
            if args:
                subcmd = args[0].upper()
                if subcmd == "ON":
                    self.radio.digipeater.enabled = True
                    self.tnc_config.set("DIGIPEAT", "ON")
                    print_info("Digipeater: ENABLED")
                    print_info("  Will digipeat packets heard DIRECTLY (hop_count=0)")
                    print_info("  Will NOT digipeat already-digipeated packets")
                    print_info("  Will NOT digipeat other digipeaters")
                elif subcmd == "OFF":
                    self.radio.digipeater.enabled = False
                    self.tnc_config.set("DIGIPEAT", "OFF")
                    print_info("Digipeater: DISABLED")
                elif subcmd == "STATUS":
                    status = "ENABLED" if self.radio.digipeater.enabled else "DISABLED"
                    print_info(f"Digipeater: {status}")
                    print_info(f"  Callsign: {self.radio.digipeater.my_callsign}")
                    print_info(f"  Packets digipeated: {self.radio.digipeater.packets_digipeated}")
                else:
                    print_error("Usage: DIGIPEATER <ON|OFF|STATUS>")
            else:
                # Toggle
                self.radio.digipeater.enabled = not self.radio.digipeater.enabled
                new_value = "ON" if self.radio.digipeater.enabled else "OFF"
                self.tnc_config.set("DIGIPEAT", new_value)
                status = "ENABLED" if self.radio.digipeater.enabled else "DISABLED"
                print_info(f"Digipeater: {status}")

        elif cmd == "DISPLAY":
            self.tnc_config.display()

        elif cmd == "STATUS":
            await self._tnc_status()

        elif cmd == "RESET":
            # Manual TNC reset command - useful if TNC gets stuck
            print_info("Resetting TNC (full reset)...")
            try:
                # Disconnect if connected
                if self.tnc_connected_to:
                    await self._tnc_disconnect()

                # Reset adapter state
                if getattr(self, "ax25", None) is not None:
                    # 1. Stop TX worker task
                    if self.ax25._tx_task:
                        print_debug("Stopping TX worker task...", level=4)
                        self.ax25._tx_task.cancel()
                        try:
                            await self.ax25._tx_task
                        except asyncio.CancelledError:
                            pass
                        self.ax25._tx_task = None

                    # 2. Clear TX queue and reset state
                    async with self.ax25._tx_lock:
                        if len(self.ax25._tx_queue) > 0:
                            print_debug(
                                f"Clearing {len(self.ax25._tx_queue)} queued frames",
                                level=4,
                            )
                        self.ax25._tx_queue.clear()
                    self.ax25._ns = 0
                    self.ax25._nr = 0
                    self.ax25._link_established = False
                    self.ax25._pending_connect = None

                    # 3. Restart TX worker task
                    print_debug("Restarting TX worker task...", level=4)
                    self.ax25._tx_task = asyncio.create_task(
                        self.ax25._tx_worker()
                    )

                # 4. Send KISS reset: 0xC0 0xFF 0xC0 (use response=True to force through immediately)
                kiss_reset = bytes([0xC0, 0xFF, 0xC0])
                await self.radio.write_kiss_frame(kiss_reset, response=True)
                await asyncio.sleep(0.5)

                print_info(
                    "TNC reset complete - TX worker restarted, ready for new connections"
                )
            except Exception as e:
                print_error(f"TNC reset failed: {e}")

        elif cmd == "HARDRESET":
            # Aggressive reset to unstick radio - sends multiple wake-up signals
            print_info("Performing HARD RESET - multiple wake-up signals...")
            try:
                # Disconnect if connected
                if self.tnc_connected_to:
                    await self._tnc_disconnect()

                # Reset adapter state
                if getattr(self, "ax25", None) is not None:
                    # Stop TX worker
                    if self.ax25._tx_task:
                        self.ax25._tx_task.cancel()
                        try:
                            await self.ax25._tx_task
                        except asyncio.CancelledError:
                            pass
                        self.ax25._tx_task = None

                    # Clear state
                    async with self.ax25._tx_lock:
                        self.ax25._tx_queue.clear()
                    self.ax25._ns = 0
                    self.ax25._nr = 0
                    self.ax25._link_established = False
                    self.ax25._pending_connect = None

                # Send KISS exit/reset sequence with pauses
                print_info("  Sending KISS exit command...")
                kiss_reset = bytes([0xC0, 0xFF, 0xC0])
                await self.radio.write_kiss_frame(kiss_reset, response=True)
                await asyncio.sleep(0.3)

                # Send it again to make sure
                print_info("  Sending second KISS exit command...")
                await self.radio.write_kiss_frame(kiss_reset, response=True)
                await asyncio.sleep(0.3)

                # Send a dummy KISS frame to "wake up" the radio's transmit path
                # This is an empty frame that should be ignored but might clear stuck states
                print_info("  Sending wake-up signal...")
                dummy_kiss = bytes(
                    [0xC0, 0x00, 0xC0]
                )  # KISS frame with just port byte
                await self.radio.write_kiss_frame(dummy_kiss, response=True)
                await asyncio.sleep(0.5)

                # Restart TX worker
                if getattr(self, "ax25", None) is not None:
                    self.ax25._tx_task = asyncio.create_task(
                        self.ax25._tx_worker()
                    )

                print_info("HARD RESET complete - radio should be unstuck")
                print_pt(
                    HTML(
                        "<yellow>If still stuck, try sending a UI frame or beacon to trigger transmission</yellow>"
                    )
                )
            except Exception as e:
                print_error(f"HARD RESET failed: {e}")

        elif cmd == "POWERCYCLE":
            # Power cycle the radio's TNC to clear stuck states
            # Uses command 21 (SET_HT_POWER) to turn off then on
            print_info("Power cycling radio TNC...")
            try:
                # Disconnect if connected
                if self.tnc_connected_to:
                    await self._tnc_disconnect()

                # Stop TX worker
                if (
                    getattr(self, "ax25", None) is not None
                    and self.ax25._tx_task
                ):
                    print_debug("Stopping TX worker...", level=1)
                    self.ax25._tx_task.cancel()
                    try:
                        await self.ax25._tx_task
                    except asyncio.CancelledError:
                        pass
                    self.ax25._tx_task = None

                # Power OFF the radio
                print_info("  Turning radio OFF...")
                await self.radio.set_hardware_power(False)
                await asyncio.sleep(
                    2.0
                )  # Wait 2 seconds for radio to fully power down

                # Power ON the radio
                print_info("  Turning radio ON...")
                await self.radio.set_hardware_power(True)
                await asyncio.sleep(
                    2.0
                )  # Wait 2 seconds for radio to fully power up

                # Reset adapter state
                if getattr(self, "ax25", None) is not None:
                    async with self.ax25._tx_lock:
                        self.ax25._tx_queue.clear()
                    self.ax25._ns = 0
                    self.ax25._nr = 0
                    self.ax25._link_established = False
                    self.ax25._pending_connect = None

                    # Restart TX worker
                    print_debug("Restarting TX worker...", level=4)
                    self.ax25._tx_task = asyncio.create_task(
                        self.ax25._tx_worker()
                    )

                print_info("Power cycle complete - TNC should be fully reset")
            except Exception as e:
                print_error(f"Power cycle failed: {e}")

        elif cmd in ("EXIT", "QUIT"):
            # Disconnect gracefully before exiting
            if self.tnc_connected_to:
                print_info("Disconnecting before exit...")
                await self._tnc_disconnect()
            self.tnc_mode = False

        else:
            # Try to handle as generic TNC-2 parameter
            if cmd in self.tnc_config.settings:
                if not args:
                    # Display current value
                    print_pt(f"{cmd}: {self.tnc_config.get(cmd)}")
                else:
                    # Set new value
                    value = " ".join(args)
                    self.tnc_config.set(cmd, value)
                    print_info(f"{cmd} set to {value}")
                    # Apply DEBUGFRAMES immediately if changed
                    try:
                        if cmd == "DEBUGFRAMES":
                            v = value.upper()
                            self.tnc_debug_frames = v in (
                                "ON",
                                "1",
                                "YES",
                                "TRUE",
                            )

                            # Enable/disable DEBUG_LEVEL based on DEBUGFRAMES
                            if self.tnc_debug_frames:
                                constants.DEBUG_LEVEL = 1  # Frame debugging
                            else:
                                constants.DEBUG_LEVEL = 0  # No debugging

                            if getattr(self, "ax25", None) is not None:
                                if self.tnc_debug_frames:
                                    self.ax25.register_frame_debug(
                                        self._tnc_frame_debug_cb
                                    )
                                else:
                                    self.ax25.register_frame_debug(None)
                    except Exception:
                        pass
            else:
                print_error(f"Unknown TNC command: {cmd}")
                print_pt("Type DISPLAY for available parameters")

    async def _tnc_connect(self, callsign, path=None):
        """Connect to a station."""
        if path is None:
            path = []

        if self.tnc_connected_to:
            print_error(f"Already connected to {self.tnc_connected_to}")
            print_pt("DISCONNECT first")
            return

        mycall = self.tnc_config.get("MYCALL")
        if mycall == "NOCALL":
            print_error("Set MYCALL first")
            return

        print_info(f"Connecting to {callsign}...")
        if path:
            print_pt(f"Via: {', '.join(path)}")

        try:
            # If AX25 adapter exists, use it exclusively — no fallback.
            if getattr(self, "ax25", None) is not None:
                # Use 3 second timeout per attempt, 5 retries = 15 seconds total
                ok = await self.ax25.connect(
                    callsign, path or [], timeout=3.0, max_retries=5
                )
                if ok:
                    self.tnc_connected_to = callsign
                    self.tnc_conversation_mode = (
                        True  # Automatically enter conversation mode
                    )
                    self._tnc_text_buffer = (
                        ""  # Clear text buffer for new connection
                    )
                    print_info(f"*** LINK ESTABLISHED with {callsign}")
                    print_pt(
                        HTML(
                            "<gray>Type text to send, type '~~~' for command mode</gray>"
                        )
                    )
                else:
                    print_error("Connect failed: no response after 5 attempts")
                return

            # No AX25 adapter — continue with UI fallback behavior
            from src.ax25_adapter import build_ui_kiss_frame

            frame = build_ui_kiss_frame(
                source=mycall,
                dest=callsign,
                path=path or [],
                info=b"*** CONNECTED\r",
            )

            await self.radio.send_tnc_data(frame)

            self.tnc_connected_to = callsign
            self.tnc_conversation_mode = (
                True  # Automatically enter conversation mode
            )
            print_info(f"*** CONNECTED to {callsign}")
            print_pt(
                HTML(
                    "<gray>Type text to send, type '~~~' for command mode</gray>"
                )
            )

        except Exception as e:
            print_error(f"Connect failed: {e}")

    async def _tnc_disconnect(self):
        """Disconnect from current station."""
        if not self.tnc_connected_to:
            print_warning("Not connected")
            return

        callsign = self.tnc_connected_to
        mycall = self.tnc_config.get("MYCALL")

        try:
            # If AX25 adapter exists, use it exclusively
            if getattr(self, "ax25", None) is not None:
                await self.ax25.disconnect()
                print_info(f"*** LINK TEARDOWN requested for {callsign}")
            else:
                # Send disconnect message as UI fallback
                from src.ax25_adapter import build_ui_kiss_frame

                frame = build_ui_kiss_frame(
                    source=mycall,
                    dest=callsign,
                    path=[],
                    info=b"*** DISCONNECTED\r",
                )
                await self.radio.send_tnc_data(frame)

                print_info(f"*** DISCONNECTED from {callsign}")
            # Clear link-layer state if set
            try:
                self.radio.tnc_link_established = False
                self.radio.tnc_connected_callsign = None
                self.radio.tnc_pending_connect = None
                self.radio.tnc_connect_event.clear()
            except Exception:
                pass
            self.tnc_connected_to = None
            self.tnc_conversation_mode = (
                False  # Exit conversation mode on disconnect
            )
            self._tnc_text_buffer = ""  # Clear text buffer on disconnect

        except Exception as e:
            print_error(f"Disconnect failed: {e}")
            self.tnc_connected_to = None
            self.tnc_conversation_mode = (
                False  # Exit conversation mode on disconnect
            )
            self._tnc_text_buffer = ""  # Clear text buffer on disconnect

    async def _tnc_send_text(self, text):
        """Send text to connected station or as UI frame if disconnected."""
        mycall = self.tnc_config.get("MYCALL")

        # Handle disconnected conversation mode - send UI frames
        if not self.tnc_connected_to:
            # Parse UNPROTO setting: "DEST VIA PATH1,PATH2"
            unproto = self.tnc_config.get("UNPROTO") or "CQ"
            parts = unproto.split()
            dest = parts[0] if parts else "CQ"
            path = []
            if len(parts) > 2 and parts[1].upper() == "VIA":
                path = [p.strip() for p in " ".join(parts[2:]).split(",")]

            try:
                from src.ax25_adapter import build_ui_kiss_frame

                payload = (text + "\r").encode("ascii", errors="replace")
                kiss_frame = build_ui_kiss_frame(mycall, dest, path, payload)
                await self.radio.send_tnc_data(kiss_frame)
                print_pt(HTML(f"<green>&gt;</green> {text}"))
            except Exception as e:
                print_error(f"Send failed: {e}")
            return

        try:
            # Build UI frame with text (append CR to follow TNC-2 line convention)
            payload = (text + "\r").encode("ascii", errors="replace")

            # If AX25Adapter exists, use it exclusively (no fallback)
            if getattr(self, "ax25", None) is not None:
                ok = await self.ax25.send_info(
                    mycall, self.tnc_connected_to, [], payload
                )
                if not ok:
                    print_error(
                        "Send failed: link not established or pyax25 send error"
                    )
                    return
            else:
                # Fallback to existing radio behavior when adapter is not present
                if getattr(self.radio, "tnc_link_established", False):
                    # Use RadioController helper to send linked I-frame (handles queuing)
                    await self.radio.send_linked_info(
                        mycall, self.tnc_connected_to, [], payload
                    )
                else:
                    from src.ax25_adapter import build_ui_kiss_frame

                    kiss_frame = build_ui_kiss_frame(
                        mycall, self.tnc_connected_to, [], payload
                    )
                    await self.radio.send_tnc_data(kiss_frame)

            # Echo locally
            print_pt(HTML(f"<green>&gt;</green> {text}"))

        except Exception as e:
            print_error(f"Send failed: {e}")

    def _tnc_receive_callback(self, parsed_frame):
        """Callback for received AX.25 frames from the adapter.

        Args:
            parsed_frame: Dict with keys: src, dst, path, pid, info
        """
        try:
            # Only display if in TNC mode
            if not self.tnc_mode:
                return

            src = parsed_frame.get("src", "")
            dst = parsed_frame.get("dst", "")
            info = parsed_frame.get("info", b"")

            # If connected and this is from our connected station, display the data
            if self.tnc_connected_to and src == self.tnc_connected_to and info:
                try:
                    # Decode text (keep \r as-is for now to properly handle line continuation)
                    text = info.decode("ascii", errors="replace")

                    # Add to buffer
                    self._tnc_text_buffer += text

                    # Split by \r to find complete lines
                    lines = self._tnc_text_buffer.split("\r")

                    # Last element is incomplete (no \r at end), keep it in buffer
                    self._tnc_text_buffer = lines[-1]

                    # Display all complete lines (all but the last)
                    for line in lines[:-1]:
                        if line:  # Only display non-empty lines
                            print_pt(line)
                except Exception:
                    pass
            # If MONITOR is ON, display all frames
            elif self.tnc_config.get("MONITOR") == "ON":
                try:
                    if info:
                        # Convert \r to \n for proper display
                        text = (
                            info.decode("ascii", errors="replace")
                            .replace("\r", "\n")
                            .rstrip("\n")
                        )
                        if text:
                            header = (
                                f"{src}>{dst}"
                                if src and dst
                                else (src or dst or "")
                            )
                            path = parsed_frame.get("path", [])
                            if path:
                                header += "," + ",".join(path)
                            print_pt(HTML(f"<gray>[{header}] {text}</gray>"))
                except Exception:
                    pass
        except Exception as e:
            if constants.DEBUG:
                print_debug(f"_tnc_receive_callback error: {e}")

    async def _tnc_status(self):
        """Show TNC status."""
        mycall = self.tnc_config.get("MYCALL")
        myalias = self.tnc_config.get("MYALIAS")

        print_pt(HTML(f"<b>MYCALL:</b> {mycall}"))
        if myalias:
            print_pt(HTML(f"<b>MYALIAS:</b> {myalias}"))

        print_pt(HTML(f"<b>UNPROTO:</b> {self.tnc_config.get('UNPROTO')}"))
        print_pt(HTML(f"<b>MONITOR:</b> {self.tnc_config.get('MONITOR')}"))

        if self.tnc_connected_to:
            print_pt(HTML(f"<b>Connected to:</b> {self.tnc_connected_to}"))
        else:
            print_pt(HTML("<gray>Not connected</gray>"))

        # Show internal state for debugging
        if getattr(self, "ax25", None) is not None:
            tx_worker_status = (
                "running"
                if (self.ax25._tx_task and not self.ax25._tx_task.done())
                else "STOPPED"
            )
            tx_queue_len = len(self.ax25._tx_queue)
            print_pt(HTML(f"<b>TX Worker:</b> {tx_worker_status}"))
            print_pt(HTML(f"<b>TX Queue:</b> {tx_queue_len} frame(s)"))
            print_pt(
                HTML(f"<b>N(S)/N(R):</b> {self.ax25._ns}/{self.ax25._nr}")
            )

    def _print_channel_details(self, channel):
        """Print channel details."""
        print_pt(
            HTML(
                f"\n<b>Channel {channel['channel_id']}: {channel['name']}</b>"
            )
        )
        print_pt(f"  TX:        {channel['tx_freq_mhz']:.6f} MHz")
        print_pt(f"  RX:        {channel['rx_freq_mhz']:.6f} MHz")
        print_pt(f"  TX Tone:   {channel['tx_tone']}")
        print_pt(f"  RX Tone:   {channel['rx_tone']}")
        print_pt(f"  Power:     {channel['power']}")
        print_pt(
            f"  Bandwidth: {'Wide' if channel['bandwidth'] else 'Narrow'}"
        )
        print_pt(f"  Scan:      {'Yes' if channel['scan'] else 'No'}")
        print_pt("")

    async def gps_poll_and_beacon_task(self):
        """Background task to poll GPS and send beacons when enabled."""

        while self.radio.running:
            try:
                # Poll GPS every 5 seconds
                await asyncio.sleep(5)

                # Check GPS lock
                gps_locked = await self.radio.check_gps_lock()

                # Try getting position anyway (for debugging - lock check may be inaccurate)
                position = await self.radio.get_gps_position()

                if position:
                    # We got valid position data - update lock status
                    self.gps_position = position
                    self.gps_locked = True  # Override lock check if we got valid data
                    print_debug(f"GPS: {position['latitude']:.6f}, {position['longitude']:.6f} (lock_check={gps_locked})", level=6)

                    # Broadcast position update to web clients
                    if self.aprs_manager._web_broadcast:
                        await self.aprs_manager._web_broadcast('gps_update', {
                            'latitude': position['latitude'],
                            'longitude': position['longitude'],
                            'altitude': position.get('altitude'),
                            'locked': True
                        })

                    # Check if beacon is enabled and due
                    if self.tnc_config.get("BEACON") == "ON":
                        beacon_interval = int(self.tnc_config.get("BEACON_INTERVAL") or "10")

                        # Check if it's time to beacon
                        now = datetime.now()
                        should_beacon = False

                        if self.last_beacon_time is None:
                            should_beacon = True  # First beacon
                        else:
                            elapsed = (now - self.last_beacon_time).total_seconds()
                            if elapsed >= (beacon_interval * 60):
                                should_beacon = True

                        if should_beacon:
                            await self._send_position_beacon(position)
                else:
                    # No GPS position data
                    self.gps_position = None
                    self.gps_locked = False
                    print_debug(f"GPS: No position data (lock_check={gps_locked})", level=6)

                    # Check if beacon is enabled with manual location (MYLOCATION)
                    if self.tnc_config.get("BEACON") == "ON" and self.tnc_config.get("MYLOCATION"):
                        beacon_interval = int(self.tnc_config.get("BEACON_INTERVAL") or "10")

                        # Check if it's time to beacon
                        now = datetime.now()
                        should_beacon = False

                        if self.last_beacon_time is None:
                            should_beacon = True  # First beacon
                        else:
                            elapsed = (now - self.last_beacon_time).total_seconds()
                            if elapsed >= (beacon_interval * 60):
                                should_beacon = True

                        if should_beacon:
                            await self._send_position_beacon(None)  # Use MYLOCATION

            except Exception as e:
                print_error(f"GPS poll task error: {e}")
                await asyncio.sleep(10)  # Back off on error

    async def _send_position_beacon(self, position=None):
        """Send APRS position beacon.

        Args:
            position: GPS position dict with latitude, longitude, altitude, etc.
                     If None, will use MYLOCATION grid square if configured.
        """

        try:
            # Get beacon settings
            mycall = self.tnc_config.get("MYCALL")
            symbol = self.tnc_config.get("BEACON_SYMBOL") or "/["
            comment = self.tnc_config.get("BEACON_COMMENT") or ""
            path_str = self.tnc_config.get("BEACON_PATH") or "WIDE1-1"

            # Parse path
            path = [p.strip() for p in path_str.split(",")]

            # Determine position source
            lat = None
            lon = None
            alt = None
            source = None

            if position:
                # Use GPS position
                lat = position['latitude']
                lon = position['longitude']
                alt = position.get('altitude')
                source = "GPS"
            else:
                # Try manual location (Maidenhead grid square)
                mylocation = self.tnc_config.get("MYLOCATION")
                if mylocation:
                    try:
                        lat, lon = APRSManager.maidenhead_to_latlon(mylocation)
                        source = f"Grid {mylocation.upper()}"
                    except ValueError as e:
                        print_error(f"Invalid MYLOCATION '{mylocation}': {e}")
                        return

            if lat is None or lon is None:
                print_error("No position available (GPS unavailable and MYLOCATION not set)")
                return

            # Convert to APRS lat/lon format (DDMM.HH N/S, DDDMM.HH E/W)
            lat_deg = int(abs(lat))
            lat_min = (abs(lat) - lat_deg) * 60
            lat_dir = 'N' if lat >= 0 else 'S'
            lat_str = f"{lat_deg:02d}{lat_min:05.2f}{lat_dir}"

            lon_deg = int(abs(lon))
            lon_min = (abs(lon) - lon_deg) * 60
            lon_dir = 'E' if lon >= 0 else 'W'
            lon_str = f"{lon_deg:03d}{lon_min:05.2f}{lon_dir}"

            # Symbol table and code
            symbol_table = symbol[0] if len(symbol) >= 1 else '/'
            symbol_code = symbol[1] if len(symbol) >= 2 else '['

            # Check if weather data is available
            wx_string = ""
            wx_source = None
            if hasattr(self, 'weather_manager') and self.weather_manager.enabled:
                # Get beacon interval for wind averaging
                beacon_interval_min = int(self.tnc_config.get("BEACON_INTERVAL") or "10")
                beacon_interval_sec = beacon_interval_min * 60

                # Get weather data with wind averaging over beacon interval
                wx_data = self.weather_manager.get_beacon_weather(beacon_interval_sec)
                if wx_data:
                    # Format weather data in APRS Complete Weather Report format
                    # Format: _DIR/SPDgGUSTtTEMPrRAINpRAIN24PhHUMbPRESSURE
                    # where _ is the weather symbol (underscore)
                    wx_parts = []

                    # Wind direction (3 digits, degrees)
                    if wx_data.wind_direction is not None:
                        wx_parts.append(f"{int(wx_data.wind_direction):03d}")
                    else:
                        wx_parts.append("...")

                    wx_parts.append("/")

                    # Wind speed (3 digits, mph)
                    if wx_data.wind_speed is not None:
                        wx_parts.append(f"{int(wx_data.wind_speed):03d}")
                    else:
                        wx_parts.append("...")

                    # Wind gust (g = gust, 3 digits, mph)
                    if wx_data.wind_gust is not None:
                        wx_parts.append(f"g{int(wx_data.wind_gust):03d}")

                    # Temperature (t = temp, 3 digits, °F, can be negative)
                    if wx_data.temperature_outdoor is not None:
                        temp_int = int(wx_data.temperature_outdoor)
                        if temp_int < 0:
                            # Negative temps: -01 to -99
                            wx_parts.append(f"t{temp_int:03d}")
                        else:
                            wx_parts.append(f"t{temp_int:03d}")

                    # Rain last hour (r = rain 1h, 3 digits, hundredths of inch)
                    if wx_data.rain_hourly is not None:
                        rain_hundredths = int(wx_data.rain_hourly * 100)
                        wx_parts.append(f"r{rain_hundredths:03d}")

                    # Rain last 24h (p = rain 24h, 3 digits, hundredths of inch)
                    if wx_data.rain_daily is not None:
                        rain_hundredths = int(wx_data.rain_daily * 100)
                        wx_parts.append(f"p{rain_hundredths:03d}")

                    # Rain since midnight (P = rain midnight, 3 digits, hundredths of inch)
                    if wx_data.rain_event is not None:
                        rain_hundredths = int(wx_data.rain_event * 100)
                        wx_parts.append(f"P{rain_hundredths:03d}")

                    # Humidity (h = humidity, 2 digits, %, 00 = 100%)
                    if wx_data.humidity_outdoor is not None:
                        humidity = wx_data.humidity_outdoor
                        if humidity == 100:
                            wx_parts.append("h00")
                        else:
                            wx_parts.append(f"h{humidity:02d}")

                    # Barometric pressure (b = pressure, 5 digits, tenths of mbar)
                    if wx_data.pressure_relative is not None:
                        pressure_tenths = int(wx_data.pressure_relative * 10)
                        wx_parts.append(f"b{pressure_tenths:05d}")

                    wx_string = "".join(wx_parts)
                    wx_source = "wx"
                    symbol_code = "_"  # Use weather symbol

            # Build position report (! = position without timestamp)
            # Format: !DDMM.HHN/DDDMM.HHW_WEATHER/A=ALTCOMMENT
            info = f"!{lat_str}/{lon_str}{symbol_code}"

            # Add weather data if available
            if wx_string:
                info += wx_string

            # Add altitude if available (format: /A=XXXXXX feet)
            if alt is not None:
                alt_feet = int(alt * 3.28084)  # Convert meters to feet
                info += f"/A={alt_feet:06d}"

            if comment:
                info += comment

            # Send via APRS
            await self.radio.send_aprs(mycall, info, to_call="APRS", path=path)

            # Update timestamp (both in-memory and persisted to config)
            now = datetime.now()
            self.last_beacon_time = now
            self.tnc_config.set("LAST_BEACON", now.isoformat())

            # Show beacon info
            if wx_source:
                print_info(f"📡 Beacon sent ({source} + weather): {lat:.6f}, {lon:.6f}")
            else:
                print_info(f"📡 Beacon sent ({source}): {lat:.6f}, {lon:.6f}")

        except Exception as e:
            print_error(f"Failed to send position beacon: {e}")


# === Monitor Tasks ===

# Duplicate packet detection cache
# Tracks (packet_hash, timestamp) to suppress digipeater duplicates
# Format: {packet_hash: timestamp}
_duplicate_cache = {}
_DUPLICATE_WINDOW = 30  # seconds to consider packets as duplicates


def is_duplicate_packet(src_call: str, info: str) -> bool:
    """Check if packet is a duplicate based on source and content.

    Packets from the same source with identical content within the
    duplicate window (30 seconds) are considered duplicates.

    This suppresses multiple digipeater copies of the same packet
    while still allowing new packets from the same station.

    Args:
        src_call: Source callsign
        info: Packet information field content

    Returns:
        True if packet is a duplicate, False otherwise
    """
    global _duplicate_cache

    # Create hash of source + content
    packet_key = f"{src_call}:{info}"
    packet_hash = hashlib.md5(packet_key.encode()).hexdigest()

    current_time = time.time()

    # Clean old entries from cache (older than duplicate window)
    expired = [
        h
        for h, ts in _duplicate_cache.items()
        if current_time - ts > _DUPLICATE_WINDOW
    ]
    for h in expired:
        del _duplicate_cache[h]

    # Check if this packet hash exists in cache
    if packet_hash in _duplicate_cache:
        # Duplicate found - update timestamp and return True
        _duplicate_cache[packet_hash] = current_time
        return True

    # Not a duplicate - add to cache
    _duplicate_cache[packet_hash] = current_time
    return False


def parse_and_track_aprs_frame(complete_frame, radio):
    """Parse APRS frame and update database tracking.

    This function runs in ALL modes (TNC and non-TNC) to ensure
    database tracking happens regardless of display mode.

    Args:
        complete_frame: Complete KISS frame bytes
        radio: RadioController instance

    Returns:
        dict with:
            is_aprs: bool - Whether this is an APRS frame
            is_duplicate: bool - Whether this is a duplicate packet
            src_call: str - Source callsign
            dst_call: str - Destination callsign
            info_str: str - Decoded info field
            relay: str - Relay call if third-party packet
            hop_count: int - Number of digipeater hops
            digipeater_path: list - List of digipeater callsigns
            aprs_types: dict - Parsed APRS data:
                mic_e: MICEPosition or None
                object: ObjectPosition or None
                item: ItemPosition or None
                status: StatusReport or None
                telemetry: Telemetry or None
                message: Message or None
                weather: Weather or None
                position: Position or None
    """
    result = {
        'is_aprs': False,
        'is_duplicate': False,
        'src_call': None,
        'dst_call': None,
        'info_str': '',
        'relay': None,
        'hop_count': 999,
        'digipeater_path': [],
        'aprs_types': {
            'mic_e': None,
            'object': None,
            'item': None,
            'status': None,
            'telemetry': None,
            'message': None,
            'weather': None,
            'position': None,
        }
    }

    try:
        # Parse AX.25 frame
        payload = kiss_unwrap(complete_frame)
        addresses, control_byte, offset = parse_ax25_addresses_and_control(payload)

        # Extract PID and info field
        # NOTE: offset points to PID byte (after control byte)
        if offset < len(payload):
            pid = payload[offset]
            info_bytes = payload[offset + 1:]  # Info starts after PID
        else:
            return result  # No PID/info

        # Extract basic frame info
        if len(addresses) >= 2:
            result['src_call'] = addresses[1]
            result['dst_call'] = addresses[0]
            raw_path = addresses[2:] if len(addresses) > 2 else []

            # Filter out Q constructs (APRS-IS server metadata that should never appear in RF)
            # Badly configured iGates sometimes encode these into AX.25 paths when gating from APRS-IS
            # Q constructs are exactly 3 chars: q + two uppercase (qAC, qAO, qAR, qAS, qAX, qAZ, qAU)
            # Reference: http://www.aprs-is.net/q.aspx
            Q_CONSTRUCTS = {'QAC', 'QAO', 'QAR', 'QAS', 'QAX', 'QAZ', 'QAU'}
            result['digipeater_path'] = [
                digi for digi in raw_path
                if digi.upper().rstrip('*') not in Q_CONSTRUCTS
            ]

            # Log when Q constructs are filtered (indicates misbehaving iGate)
            filtered_out = [d for d in raw_path if d.upper().rstrip('*') in Q_CONSTRUCTS]
            if filtered_out and constants.DEBUG:
                print_debug(
                    f"Filtered Q construct(s) from path: {filtered_out} (bad iGate behavior)",
                    level=2
                )

            result['hop_count'] = calculate_hop_count(addresses)
        else:
            return result  # Invalid frame

        # Check if this looks like APRS
        if not info_bytes:
            return result

        first_byte = info_bytes[0]
        first_char = chr(first_byte) if first_byte < 128 else None
        first_two = info_bytes[:2].decode("ascii", errors="ignore") if len(info_bytes) >= 2 else ""

        # APRS marker detection
        aprs_marker = bool(
            first_char and (
                first_char in ("!", "/", "@", ":", "=", "}", "'", "`", ";", ")", ">")
                or first_two.startswith("T#")
                or first_two.startswith("p")
                or first_byte in (0x1C, 0x1D, 0x1E, 0x1F)
            )
        )

        # Only treat as APRS if marker present (reduces false positives)
        if pid == 0xF0 and aprs_marker:
            result['is_aprs'] = True
        elif pid != 0xF0 and aprs_marker:
            result['is_aprs'] = True  # Heuristic match
        else:
            return result  # Not APRS

        # Decode info field
        info_str = info_bytes.decode("ascii", errors="replace")
        result['info_str'] = info_str

        # Check for third-party packet
        third_party = radio.aprs_manager.parse_third_party(result['src_call'], info_str)
        if third_party:
            source_call, relay_call, inner_info = third_party
            parse_call = source_call
            parse_info = inner_info
            result['relay'] = relay_call
        else:
            parse_call = result['src_call']
            parse_info = info_str

        # Check for duplicate packet (suppresses digipeater copies)
        result['is_duplicate'] = radio.aprs_manager.is_duplicate_packet(parse_call, parse_info)

        # Record digipeater paths even for duplicates (improves coverage accuracy)
        if result['is_duplicate'] and result['digipeater_path']:
            radio.aprs_manager.record_digipeater_path(parse_call, result['digipeater_path'])

        # Parse all APRS types (updates database in aprs_manager)
        # This happens even for duplicates to ensure tracking
        if not result['is_duplicate']:
            # MIC-E
            result['aprs_types']['mic_e'] = radio.aprs_manager.parse_aprs_mice(
                parse_call, result['dst_call'], parse_info, result['relay'],
                result['hop_count'], result['digipeater_path']
            )

            # Object
            if not result['aprs_types']['mic_e']:
                result['aprs_types']['object'] = radio.aprs_manager.parse_aprs_object(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path']
                )

            # Item
            if not result['aprs_types']['object']:
                result['aprs_types']['item'] = radio.aprs_manager.parse_aprs_item(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path']
                )

            # Status
            if not result['aprs_types']['item']:
                result['aprs_types']['status'] = radio.aprs_manager.parse_aprs_status(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path']
                )

            # Telemetry
            if not result['aprs_types']['status']:
                result['aprs_types']['telemetry'] = radio.aprs_manager.parse_aprs_telemetry(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path']
                )

            # Message
            if not result['aprs_types']['telemetry']:
                result['aprs_types']['message'] = radio.aprs_manager.parse_aprs_message(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path']
                )

            # Weather and Position (can coexist)
            if not result['aprs_types']['message']:
                result['aprs_types']['weather'] = radio.aprs_manager.parse_aprs_weather(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path']
                )
                result['aprs_types']['position'] = radio.aprs_manager.parse_aprs_position(
                    parse_call, parse_info, result['relay'],
                    result['hop_count'], result['digipeater_path'],
                    result['dst_call']
                )

    except Exception as e:
        if constants.DEBUG:
            print_debug(f"APRS parsing exception: {e}", level=2)

    return result


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
                                parsed_aprs['hop_count'],
                                parsed_aprs['digipeater_path'],
                                is_source_digipeater
                            ):
                                # Create digipeated frame
                                digi_frame = radio.digipeater.digipeat_frame(complete_frame, parsed_aprs)
                                if digi_frame:
                                    # Transmit the digipeated frame via radio
                                    await radio.write_kiss_frame(digi_frame, response=False)
                                    print_info(
                                        f"🔁 Digipeated {parsed_aprs['src_call']} "
                                        f"({radio.digipeater.packets_digipeated} total)"
                                    )
                        except Exception as e:
                            if constants.DEBUG_LEVEL >= 2:
                                print_debug(f"Digipeater error: {e}", level=2)
                                import traceback
                                print_debug(traceback.format_exc(), level=3)

                    # Display ASCII-decoded frame at debug level 1 (all modes)
                    if constants.DEBUG_LEVEL >= 1 and not radio.tnc_mode_active:
                        try:
                            from src.protocol import parse_ax25_addresses_and_control
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
                            cleaned_comment = radio.aprs_manager.clean_position_comment(mice_pos.comment)
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
                            cleaned_comment = radio.aprs_manager.clean_position_comment(obj_pos.comment)
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
                            cleaned_comment = radio.aprs_manager.clean_position_comment(item_pos.comment)
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
                                cleaned_comment = radio.aprs_manager.clean_position_comment(pos.comment)
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
            import traceback

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
    """Periodic auto-save of APRS database every 5 minutes."""
    AUTOSAVE_INTERVAL = 300  # 5 minutes in seconds

    while radio.running:
        try:
            await asyncio.sleep(AUTOSAVE_INTERVAL)

            if not radio.running:
                break

            # Save the APRS database
            if hasattr(radio, 'aprs_manager') and radio.aprs_manager:
                count = radio.aprs_manager.save_database()
                if count > 0:
                    print_debug(
                        f"Auto-saved APRS database ({count} stations)",
                        level=3
                    )
                else:
                    print_debug("Auto-save failed", level=3)

        except Exception as e:
            print_error(f"Auto-save monitor error: {e}")


async def gps_monitor(radio):
    """Monitor GPS and send beacons when enabled."""
    # Wait for command processor to be initialized
    while radio.running:
        if hasattr(radio, "cmd_processor") and radio.cmd_processor:
            break
        await asyncio.sleep(1)

    if not radio.running:
        return

    # Run GPS polling and beacon task
    await radio.cmd_processor.gps_poll_and_beacon_task()


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
                    aprs_mgr.my_callsign, aprs_message, to_call="APRS"
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


async def command_loop(radio, auto_tnc=False, auto_connect=None, serial_mode=False):
    """Command input loop with pinned prompt."""
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
               serial_port=None, serial_baud=9600, tcp_host=None, tcp_port=8001):
    # Enable debug mode if requested via command line
    if auto_debug:
        constants.DEBUG_LEVEL = 2
        constants.DEBUG = True
        print_info("Debug mode enabled at startup")

    print_header("FSY Packet Console")

    rx_queue = asyncio.Queue()
    tnc_queue = asyncio.Queue()

    # Transport and client setup
    transport = None
    client = None
    is_shutting_down = False

    # Serial mode
    if serial_port:
        print_pt(HTML(f"<gray>Serial KISS Mode: {serial_port} @ {serial_baud} baud...</gray>\n"))

        try:
            from src.transport import SerialTransport

            transport = SerialTransport(serial_port, serial_baud, tnc_queue)
            await transport.connect()
            print_info(f"Serial port connected: {serial_port}")

        except Exception as e:
            print_error(f"Failed to open serial port: {e}")
            return

    # TCP KISS TNC Client Mode
    elif tcp_host:
        print_pt(HTML(f"<gray>TCP KISS Mode: {tcp_host}:{tcp_port}...</gray>\n"))

        try:
            from src.transport import TCPTransport

            transport = TCPTransport(
                host=tcp_host,
                port=tcp_port,
                tnc_queue=tnc_queue
            )

            if not await transport.connect():
                print_error("Failed to connect to KISS TNC server")
                print_error("Verify Direwolf or remote TNC is running")
                return

            print_info(f"TCP KISS client ready")

        except Exception as e:
            print_error(f"Failed to connect to TCP TNC: {e}")
            return

    # BLE mode
    else:
        print_pt(HTML("<gray>Connecting to " + RADIO_MAC_ADDRESS + "...</gray>\n"))

        device = await BleakScanner.find_device_by_address(
            RADIO_MAC_ADDRESS, timeout=10.0
        )
        if not device:
            print_error("Device not found")
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

            await client.start_notify(RADIO_INDICATE_UUID, handle_indication)
            await client.start_notify(TNC_RX_UUID, handle_tnc)

            print_info("Notifications enabled")

            await asyncio.sleep(0.5)
            while not rx_queue.empty():
                await rx_queue.get()

            # Create BLE transport
            from src.transport import BLETransport
            transport = BLETransport(client, rx_queue, tnc_queue)

        except Exception as e:
            print_error(f"BLE connection failed: {e}")
            return

    # Create radio controller with transport
    try:
        radio = RadioController(transport, rx_queue, tnc_queue)

        # Create shared AX25Adapter that will be used by both CommandProcessor and AGWPE
        # This prevents the two-adapter conflict where one sets _pending_connect
        # but the other receives the UA frames

        tnc_config = TNCConfig()
        shared_ax25 = AX25Adapter(
            radio,
            get_mycall=lambda: tnc_config.get("MYCALL"),
            get_txdelay=lambda: tnc_config.get("TXDELAY"),
        )
        # Store on radio object so CommandProcessor can access it
        radio.shared_ax25 = shared_ax25

        # Create APRS manager early so web server can use it
        mycall = tnc_config.get("MYCALL") or "NOCALL"
        retry_count = int(tnc_config.get("RETRY") or "3")
        retry_fast = int(tnc_config.get("RETRY_FAST") or "20")
        retry_slow = int(tnc_config.get("RETRY_SLOW") or "600")
        radio.aprs_manager = APRSManager(mycall, max_retries=retry_count,
                                        retry_fast=retry_fast, retry_slow=retry_slow)

        # Create digipeater (read state from TNC config)
        digipeat_value = tnc_config.get("DIGIPEAT") or ""
        digipeat_enabled = digipeat_value.upper() == "ON"
        radio.digipeater = Digipeater(mycall, enabled=digipeat_enabled)

        # Only start TNC/AGWPE bridges if NOT in TCP client mode
        # (TCP mode acts as client to external TNC, not a server for other apps)
        if not tcp_host:
            # Start TNC TCP bridge (with error handling to survive bind failures)
            try:
                tnc_host = tnc_config.get("TNC_HOST") or "0.0.0.0"
                tnc_port = int(tnc_config.get("TNC_PORT") or "8001")
                radio.tnc_bridge = TNCBridge(radio, port=tnc_port)
                await radio.tnc_bridge.start(host=tnc_host)
            except OSError as e:
                print_error(f"TNC bridge failed to bind to {tnc_host}:{tnc_port} - {e}")
                print_error(f"  Port may be in use or address unavailable. Fix with: TNC_HOST/TNC_PORT")
                radio.tnc_bridge = None
            except Exception as e:
                print_error(f"Failed to start TNC bridge: {e}")
                radio.tnc_bridge = None

            # Start AGWPE-compatible bridge (with error handling to survive bind failures)
            try:
                from src.agwpe_bridge import AGWPEBridge

                agwpe_host = tnc_config.get("AGWPE_HOST") or "0.0.0.0"
                agwpe_port = int(tnc_config.get("AGWPE_PORT") or "8000")
                radio.agwpe_bridge = AGWPEBridge(
                    radio,
                    get_mycall=lambda: tnc_config.get("MYCALL"),
                    get_txdelay=lambda: tnc_config.get("TXDELAY"),
                    ax25_adapter=shared_ax25,  # Use the shared adapter
                )
                started = await radio.agwpe_bridge.start(host=agwpe_host, port=agwpe_port)
                if not started:
                    print_error("AGWPE bridge failed to start")
                    radio.agwpe_bridge = None
            except OSError as e:
                print_error(f"AGWPE bridge failed to bind to {agwpe_host}:{agwpe_port} - {e}")
                print_error(f"  Port may be in use or address unavailable. Fix with: AGWPE_HOST/AGWPE_PORT")
                radio.agwpe_bridge = None
            except Exception as e:
                print_error(f"Failed to start AGWPE bridge: {e}")
                radio.agwpe_bridge = None
        else:
            print_info("TNC/AGWPE bridges disabled (TCP client mode)")

        # Start Web UI Server (with error handling to survive bind failures)
        try:
            webui_host = tnc_config.get("WEBUI_HOST") or "0.0.0.0"
            webui_port = int(tnc_config.get("WEBUI_PORT") or "8002")
            print_info(f"Starting Web UI server on port {webui_port}...")

            radio.web_server = WebServer(
                radio=radio,
                aprs_manager=radio.aprs_manager,
                get_mycall=lambda: tnc_config.get("MYCALL"),
                get_mylocation=lambda: tnc_config.get("MYLOCATION")
            )

            started = await radio.web_server.start(host=webui_host, port=webui_port)
            if started:
                print_info(f"Web UI started on http://{webui_host}:{webui_port}")
            else:
                print_error("Web UI failed to start")
                radio.web_server = None
        except OSError as e:
            print_error(f"Web UI failed to bind to {webui_host}:{webui_port} - {e}")
            print_error(f"  Port may be in use or address unavailable. Fix with: WEBUI_HOST/WEBUI_PORT")
            radio.web_server = None
        except Exception as e:
            print_error(f"Failed to start Web UI: {e}")
            radio.web_server = None

        # Auto-connect to weather station if enabled
        # Wait until CommandProcessor is created (happens in processor creation below)
        # We'll connect after command_loop starts

        print_info("Monitoring TNC traffic...")

        # Create background task list
        tasks = [
            asyncio.create_task(tnc_monitor(tnc_queue, radio)),
            asyncio.create_task(message_retry_monitor(radio)),
            asyncio.create_task(autosave_monitor(radio)),
            asyncio.create_task(gps_monitor(radio)),  # Runs in both BLE and serial modes
        ]

        # Add BLE-only monitors
        if not serial_port and not tcp_host:
            tasks.extend([
                asyncio.create_task(connection_watcher(radio)),
                asyncio.create_task(heartbeat_monitor(radio)),
            ])

        # Add command loop
        tasks.append(
            asyncio.create_task(
                command_loop(
                    radio, auto_tnc=auto_tnc, auto_connect=auto_connect,
                    serial_mode=(serial_port is not None or tcp_host is not None)
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

        # Save frame buffer to disk
        if hasattr(radio, 'cmd_processor') and radio.cmd_processor:
            print_info("Saving frame buffer...")
            radio.cmd_processor.frame_history.save_to_disk()

        print_info("Disconnecting...")

        # Close transport
        if transport:
            await transport.close()

    except Exception as e:
        print_error(f"{type(e).__name__}: {e}")

        traceback.print_exc()


def run(auto_tnc=False, auto_connect=None, auto_debug=False,
        serial_port=None, serial_baud=9600, tcp_host=None, tcp_port=8001):
    """Entry point for the console application."""
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
            )
        )
    except KeyboardInterrupt:
        print_pt(HTML("\n<yellow>Interrupted by user</yellow>"))

    print_pt(HTML("<gray>Goodbye!</gray>"))
