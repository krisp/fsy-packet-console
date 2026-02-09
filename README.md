# FSY Packet Console

**Professional packet radio and APRS platform with universal TNC support**

A comprehensive, production-ready APRS client and packet radio toolkit for serious amateur radio operators. Features reliable APRS messaging, station tracking, digipeater functionality, and professional-grade packet analysis - whether you're running Direwolf on a Raspberry Pi, using a hardware TNC, or connecting to a UV-50PRO handheld via Bluetooth LE.

[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-sa/4.0/)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)

---

## 🎯 What Is This?

This is a **complete APRS and packet radio platform** that combines:

- **🌐 World-Class APRS Client** - Full-featured APRS with messaging, tracking, weather, and telemetry
- **📡 Universal TNC Support** - Works with Direwolf (TCP), serial KISS TNCs, and BLE radios
- **🗺️ Real-Time Web Dashboard** - Beautiful web UI with live maps, station tracking, and coverage visualization
- **📻 Radio Control** - Direct VFO/frequency/power control (BLE mode with UV-50PRO)
- **🔌 Network Bridges** - AGWPE and KISS servers for integration with YAAC, Outpost PM, UIView32, etc.
- **🔍 Integrated Protocol Analysis** - Built-in Wireshark-style frame decoder with full layer-by-layer decode
- **🌟 APRS Digipeater** - Full WIDEn-N digipeating with configurable paths
- **💬 AX.25 BBS/Node Support** - Connected mode for packet BBS systems and DX clusters

Whether you're monitoring APRS traffic, running a digipeater, connecting to a packet BBS, or analyzing RF protocols - this suite has you covered.

---

## 🚀 Key Features

### 🌐 APRS (Automatic Packet Reporting System)

- **Two-Way APRS Messaging** with automatic ACK and intelligent retry
  - **Auto-ACK Transmission** - RFC-compliant acknowledgment of received messages
  - **Two-Tier Retry** - Fast retries (20s) for RF, slow retries (10min) for digipeater paths
  - **Three-State Tracking** - Visual indicators: ⋯ (pending), → (digipeated), ✓ (acknowledged)
  - **Fuzzy Duplicate Detection** - Intelligent filtering of corrupted iGate packets

- **Comprehensive Station Tracking**
  - Event-sourced reception history (up to 200 receptions per station)
  - Position tracking with history (6000+ positions stored)
  - Weather station monitoring with historical charts
  - Telemetry parsing and filtering
  - Device identification (2000+ radio/software database)
  - Hop count and path analysis
  - First/last heard timestamps with proper timezone handling (UTC)

- **Advanced Position Formats**
  - **MIC-E Decoding** - Full support for Kenwood/Yaesu compressed positions
  - **Compressed Positions** - Base-91 decoding for OpenTracker/TinyTrak devices
  - **Maidenhead Grid** - Manual position via grid square (FN42, DM79, etc.)
  - **GPS Beaconing** - Automatic position transmission with configurable intervals

- **Coverage Visualization**
  - Digipeater coverage mapping with convex hull polygons
  - Local coverage circles showing heard stations
  - Mobile station path tracking with movement history
  - Time-bound filtering (last 1h, 6h, 24h, all time)
  - Interactive map with clickable stations

- **Full APRS Digipeater** (NEW!)
  - WIDEn-N path processing with hop decrementing
  - Direct-only repeating mode (no WIDE paths)
  - Configurable via `DIGIPEAT ON|OFF` command
  - Automatic duplicate suppression
  - TNC-style control interface

### 📡 Universal TNC Support

**Three Transport Modes - One Codebase:**

#### 1️⃣ **KISS-over-TCP** (Direwolf, QtSoundModem, etc.)
```bash
python main.py -k localhost:8001          # Direwolf on same machine
python main.py -k 192.168.1.50:8001       # Remote TNC on LAN
python main.py -k direwolf.local          # mDNS hostname (default port 8001)
```
- Perfect for **Direwolf** software TNC users
- Connects to any KISS-over-TCP server
- No TNC/AGWPE bridge servers (console is the client)
- Web UI remains available for visualization

#### 2️⃣ **Serial KISS** (Hardware TNCs)
```bash
python main.py --serial /dev/ttyUSB0 --baud 9600
python main.py -s /dev/ttyUSB0 -b 19200
```
- Works with **Kantronics KPC-3**, **MFJ-1270C**, **TNC-X**, etc.
- Standard baud rates: 1200, 9600, 19200, 38400, 57600, 115200
- Auto-detects serial ports on Linux/macOS/Windows
- Provides TNC/AGWPE bridges for other applications

#### 3️⃣ **Bluetooth LE** (UV-50PRO and Clones)
```bash
python main.py  # Default mode - connects to UV-50PRO
```
- **Primary Target:** UV-50PRO dual-band handheld
- **Potential Support:** UV-50PRO clones/variants (untested but likely compatible)
- Built-in TNC with KISS protocol via BLE
- Includes radio control features (frequency, power, GPS, etc.)
- Provides TNC/AGWPE bridges for other applications

**All modes support:**
- ✅ Full APRS functionality (messaging, tracking, weather, digipeating)
- ✅ AX.25 connected mode (packet BBS, nodes, DX clusters)
- ✅ KISS frame transmission/reception
- ✅ Real-time Web UI visualization

### 🗺️ Real-Time Web Dashboard (Port 8002)

**Modern, responsive web interface for APRS monitoring:**

- **📍 Interactive Map** - Leaflet.js map with live station updates
  - Custom APRS symbol rendering (850+ symbols)
  - Click stations for detailed info
  - Position history paths for mobile stations
  - Coverage polygons for digipeaters
  - Multi-station popups for co-located stations
  - Map state persistence (remembers zoom/pan)

- **📊 Stations Table** - Sortable, searchable station list
  - 9 columns: Callsign, Last Heard, Position, Distance, Packets, Hops, Device, Path, Comment
  - Real-time updates via Server-Sent Events (SSE)
  - Click callsign to navigate to station detail page
  - Device identification (shows radio/software used)

- **🌤️ Weather Dashboard** - Weather station visualization
  - Temperature, humidity, pressure charts (Chart.js)
  - Historical data with configurable retention
  - Weather station map locations

- **💬 Activity Feed** - Live console-style activity monitor
  - Real-time packet reception log
  - APRS message notifications
  - Station position updates
  - Console-style output with timestamps

- **📱 LAN Access** - Binds to `0.0.0.0` for network access
  - Access from phones, tablets, laptops
  - No internet required (self-contained)
  - Auto-downloads dependencies on first run

### 📻 Radio Control (UV-50PRO BLE Mode Only)

**When connected via Bluetooth LE to a UV-50PRO, you get full radio control in addition to packet/APRS features:**

#### Frequency & VFO Control
```bash
radio> freq 146.520                # Set frequency (MHz)
radio> vfo a                       # Select VFO A
radio> vfo b                       # Select VFO B
radio> vfo swap                    # Swap VFO A/B frequencies
```

#### Power & Audio
```bash
radio> power high                  # TX power: high/medium/low
radio> volume 8                    # Speaker volume (0-15)
radio> squelch 5                   # Squelch level (0-9)
```

#### GPS & Status
```bash
radio> gps                         # Get current GPS position
radio> status                      # Battery, RSSI, channel, power levels
```

#### Monitoring Features
```bash
radio> dualwatch on                # Enable dual watch (monitor 2 frequencies)
radio> dualwatch off               # Disable dual watch
```

**Note:** Radio control features are **only available in BLE mode** (UV-50PRO). Serial KISS and TCP modes connect to external TNCs which don't provide radio control access. All packet/APRS functionality works identically across all three modes.

### 🔌 Network Bridges

#### **AGWPE Bridge** (Port 8000)
Full AGWPE TCP/IP API v1.0 compliance for integration with popular ham radio software:

- **Compatible Applications:**
  - YAAC (Yet Another APRS Client)
  - Outpost PM (Packet Message Manager)
  - APRSISCE/32
  - UIView32
  - Xastir
  - WinLink Express (with AGWPE mode)

- **Supported Features:**
  - UI frame transmission with digipeater paths
  - Connected mode (C/v/c/D/d commands)
  - Monitor mode for packet sniffing
  - Port selection and radio control
  - Multiple client support

#### **TNC Bridge** (Port 8001)
Raw KISS protocol access for direct TNC control:

- Compatible with **KISSet**, **APRX**, and custom KISS applications
- Bidirectional KISS frame exchange
- Multiple simultaneous clients
- Suitable for general packet applications

**Note:** Bridges are only available in BLE and Serial modes. In TCP mode, the console acts as a client to an external TNC (no bridge servers).

### 📦 AX.25 Packet Radio (Full v2.0/v2.2 Implementation)

- **Connected Mode** - Establish reliable links with remote stations
  - SABM/DISC handshake with automatic retry (5 attempts, exponential backoff)
  - I-frame exchange with automatic ACK and sequence tracking
  - REJ/RNR/FRMR handling for error recovery
  - Duplicate detection and out-of-sequence recovery
  - TXDELAY support for half-duplex timing

- **Conversation Mode** - User-friendly text interface
  - Type messages naturally, press Enter to send
  - Escape with `~~~` on new line
  - Frame logging to `~/tnc.log`
  - Compatible with packet BBS systems, nodes, gateways

- **Carrier Sense** - CSMA/CA for collision avoidance
  - Monitors channel before transmission
  - Configurable PERSIST and SLOTTIME
  - Reduces packet collisions on busy frequencies

### 🔍 Integrated Protocol Debugging & Analysis

**Built-in Wireshark-style packet decoder with comprehensive frame analysis - no external tools needed!**

#### **Multi-Level Debug System**

```bash
aprs> debug 0                          # Disabled
aprs> debug 1                          # TNC monitor only (RX/TX frames)
aprs> debug 2                          # Protocol events (connections, ACKs, retries)
aprs> debug 5                          # Verbose (all APRS parsing details)
aprs> debug 6                          # Full trace (every byte, every decision)
```

#### **Frame History with Full Protocol Decode**

Every received frame is buffered in memory for instant analysis:

```bash
# Brief mode - compact hex output with metadata
aprs> debug dump 20
[1] RX 11:49:44.979 (42b): c00092884040e0a6...
[2] RX 11:49:51.123 (58b): c000a4a68886...
[3] TX 11:50:15.456 (35b): c000a8b2a4...

# Detail mode - full layer-by-layer protocol decode
aprs> debug dump 50 detail
```

**Detail Mode Output** provides complete Wireshark-style analysis:

```
======================================================================
Frame #1 - RX at 11:49:44.979 (42 bytes)
======================================================================

KISS LAYER:
  Command: 0x00 (Data Frame)
  Port: 0

AX.25 HEADER:
  Destination: APRS   (generic APRS address)
  Source:      K1FSY-9 (SSID: 9)
  Path:        WIDE1-1,WIDE2-1
    [1] WIDE1-1* (digipeated, h-bit set)
    [2] WIDE2-1  (not yet digipeated)
  Control:     0x03 (UI frame - unnumbered information)
  PID:         0xF0 (No layer 3 protocol)

APRS DATA:
  Data Type:   ! (Position without timestamp)
  Latitude:    42.3456N
  Longitude:   71.1234W
  Symbol:      /[ (Jogger)
  Comment:     "Testing APRS console"

PARSED POSITION:
  Grid Square: FN42hi
  Maidenhead:  FN42hi23
  Altitude:    Not specified
  Course:      Not specified
  Speed:       Not specified

DIGIPEATER ANALYSIS:
  Heard via:   WIDE1-1 (1 hop)
  Total hops:  1
  Path valid:  Yes (standard WIDEn-N)
  Q construct: None (direct RF)
======================================================================
```

**For APRS Messages:**
```
APRS DATA:
  Data Type:   : (Message)
  Recipient:   W1ABC-5
  Message:     "Hello from the console"
  Message ID:  123

MESSAGE TRACKING:
  Delivery:    ✓ Acknowledged
  Retry count: 0
  First TX:    11:48:30
  ACK RX:      11:48:35 (5 second round-trip)
```

**For Weather Reports:**
```
APRS DATA:
  Data Type:   _ (Weather report)
  Temperature: 72°F (22.2°C)
  Humidity:    65%
  Pressure:    1013.2 mbar
  Wind:        12 mph from 270° (W)
  Rain 1h:     0.00 in
  Rain 24h:    0.25 in
  Rain today:  0.35 in
```

**For MIC-E Compressed Format:**
```
APRS DATA:
  Data Type:   ` (MIC-E encoded)
  Device:      Kenwood TH-D74 (from tocall APK004)

MIC-E DECODE:
  Destination: TW2S4V (encodes lat/lon/msg)
  Latitude:    42.3456N (from destination)
  Longitude:   71.1234W (from info field)
  Message:     "In Service" (standard message code 1)
  Symbol:      /> (Car)
  Course:      245°
  Speed:       35 mph
  Altitude:    150 ft
```

#### **Range Queries & Filtering**

```bash
# Specific frame range
aprs> debug dump 10-30 detail          # Frames 10 through 30
aprs> debug dump 100-150               # Range in brief mode

# All frames in buffer
aprs> debug dump all detail            # Everything with full decode

# Most recent frames
aprs> debug dump 5 detail              # Last 5 frames only
```

#### **Per-Station Debug Filtering**

Focus analysis on specific stations:

```bash
# Filter to only show K1FSY and W1ABC packets
aprs> debug filter K1FSY W1ABC
[INFO] Debug filter active: K1FSY, W1ABC (matches any SSID)

# Now debug output only shows these stations:
[DEBUG] RX from K1FSY-9: Position update
[DEBUG] RX from W1ABC-5: APRS message

# View active filters
aprs> debug filter --show
Active filters: K1FSY, W1ABC

# Clear all filters
aprs> debug filter --clear
```

**Smart Callsign Matching:**
- `K1FSY` matches `K1FSY-9`, `K1FSY-5`, `K1FSY` (any SSID)
- `W1ABC-5` matches exactly `W1ABC-5` only
- Filters apply to ALL debug output (frame dumps, protocol traces, etc.)

#### **Frame Buffer Configuration**

```bash
# Large buffer for extended monitoring sessions
aprs> debug buffer 50                  # 50MB (~50,000 frames)

# Default buffer for normal use
aprs> debug buffer 10                  # 10MB (~10,000 frames)

# Low-memory mode (keeps only last 10 frames)
aprs> debug buffer off                 # Simple ring buffer

# Check current buffer status
aprs> debug buffer
Buffer: 10MB, 1,247 frames stored (8.3MB used)
```

#### **Real-Time Protocol Tracing**

Enable detailed protocol logging to watch packets flow:

```bash
aprs> debug 5

# Now see complete parsing in real-time:
[DEBUG] TNC RX (42 bytes): c00092884040e0a6...
[DEBUG] KISS: Command=0x00 (Data), Port=0
[DEBUG] AX.25: K1FSY-9 > APRS via WIDE1-1*,WIDE2-1
[DEBUG] APRS: Position without timestamp
[DEBUG] Position: 42.3456N, 71.1234W (FN42hi)
[DEBUG] Symbol: /[ (Jogger)
[DEBUG] Comment: "Testing APRS console"
[DEBUG] Heard via WIDE1-1 (1 hop)
[DEBUG] Added to station database
```

#### **Export Frame History**

Copy frames from console for external analysis:

```bash
# Copy frames to clipboard/file
aprs> debug dump 100 > frames.txt

# Use with external tools
cat frames.txt | ./analyze_frames.py   # Optional external analyzer
```

**Benefits of Integrated Analysis:**
- ✅ **No context switching** - analyze frames without leaving console
- ✅ **Instant access** - all frames buffered in memory
- ✅ **Correlated data** - see station info alongside protocol decode
- ✅ **Time-ordered** - frames numbered sequentially
- ✅ **Filterable** - focus on specific stations or time ranges
- ✅ **Complete decode** - KISS → AX.25 → APRS in one view

#### **Offline Frame Analysis with analyze_frames.py**

The `analyze_frames.py` tool provides powerful offline forensic analysis of captured frames from the frame buffer database:

```bash
# List all captured frames
./analyze_frames.py --buffer --list

# Analyze specific frames that had errors
./analyze_frames.py --buffer --frames 1234 1235 1236

# Analyze a range of frames
./analyze_frames.py --buffer --range 1000-1100

# Analyze all frames in buffer
./analyze_frames.py --buffer

# Use custom buffer file
./analyze_frames.py --buffer-file /path/to/buffer.json.gz --frames 42
```

**Features:**
- ✅ **Load from frame buffer database** (`~/.console_frame_buffer.json.gz`)
- ✅ **Selective frame analysis** by number or range
- ✅ **Wireshark-style output** with full layer decode
- ✅ **Shows RX/TX direction** for each frame
- ✅ **Preserves timestamps** from live capture
- ✅ **Backward compatible** with stdin input

**Use Cases:**
- **Post-mortem debugging** - Analyze frames from earlier sessions
- **Intermittent issue diagnosis** - Examine specific problem frames
- **Protocol learning** - Study real APRS/AX.25 traffic
- **ACK tracking** - Find missing acknowledgments
- **Path analysis** - Understand digipeater routing

The frame buffer is automatically saved every 100 frames and persists across console restarts, giving you a complete capture history for offline analysis.

### 🎛️ TNC-Style Configuration

**TNC-2 compatible command set** for familiar operation. **These configuration commands are ONLY available in TNC mode (`tnc>` prompt)**:

```bash
# Switch to TNC mode first to access configuration commands
aprs> tnc

# Now you can configure TNC-2 parameters
tnc> mycall K1FSY-9
tnc> unproto CQ WIDE1-1
tnc> beacon every 10
tnc> txdelay 40
tnc> digipeat on
```

All settings saved to `~/.tnc_config.json` and persist across sessions.

**Key Parameters:**
- `MYCALL` - Your callsign (with SSID)
- `MYLOCATION` - Maidenhead grid square (for beaconing without GPS)
- `TXDELAY` - Transmit delay in 10ms units (default: 30)
- `RETRY` - Number of retry attempts (default: 3)
- `RETRY_FAST` - Fast retry timeout in seconds (default: 20)
- `RETRY_SLOW` - Slow retry timeout in seconds (default: 600)
- `DIGIPEAT` - Enable/disable digipeater (ON/OFF)
- `AUTO_ACK` - Automatic APRS message ACK (ON/OFF)
- `BEACON` - Enable position beaconing (ON/OFF)
- `BEACON_INTERVAL` - Beacon interval in minutes
- `DEBUG_BUFFER` - Frame buffer size in MB (or "OFF")

---

## 📋 System Requirements

### Software
- **Python 3.7+** (3.11+ recommended)
- **Linux, macOS, or Windows**
- **Terminal** with color support (recommended)

### Hardware (Choose One Transport Mode)

#### Option 1: KISS-over-TCP (Direwolf)
- **Direwolf Software TNC** running on any machine
  - Raspberry Pi (most common)
  - Linux desktop/server
  - macOS (via Homebrew)
- **Network connection** to Direwolf host
- **Radio** connected to Direwolf (sound card or USB)

#### Option 2: Serial KISS TNC
- **Hardware TNC** with serial interface:
  - Kantronics KPC-3/KPC-3+
  - MFJ-1270C/1270X
  - TNC-X, TNC-Pi
  - Any KISS-compatible TNC
- **Serial port** (USB-to-serial or native)
- **Radio** connected to TNC

#### Option 3: Bluetooth LE (UV-50PRO)
- **UV-50PRO** dual-band handheld radio (or compatible clone)
- **Bluetooth LE 4.0+** adapter on computer
- No additional TNC hardware required!

---

## 🔧 Installation

### 1. Clone Repository
```bash
git clone https://github.com/krisp/fsy-packet-console.git
cd fsy-packet-console
```

### 2. Create Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

**Or install manually:**
```bash
pip install bleak prompt-toolkit aiohttp pyserial-asyncio
```

**Required Packages:**
- `bleak` - Bluetooth LE support (BLE mode only)
- `prompt_toolkit` - Terminal UI with tab completion
- `aiohttp` - Async HTTP server for web UI
- `pyserial-asyncio` - Serial port support (Serial mode only)

**Note:** APRS parsing is implemented natively - no external APRS library needed!

### 4. Configure Bluetooth Radio (BLE Mode Only)

Find your UV-50PRO's Bluetooth MAC address:

**Linux:**
```bash
bluetoothctl
scan on
# Look for "UV-50PRO" - note the MAC address (e.g., 38:D2:00:01:62:C2)
```

**macOS:**
```bash
system_profiler SPBluetoothDataType | grep -A 5 "UV-50PRO"
```

**Configure the MAC address** (choose one method):

**Option 1: Command line** (temporary, for testing):
```bash
python main.py -r 38:D2:00:01:62:C2
# or
python main.py --radio-mac 38:D2:00:01:62:C2
```

**Option 2: TNC configuration** (permanent):
```bash
python main.py
aprs> tnc                        # Enter TNC mode
tnc> RADIO_MAC 38:D2:00:01:62:C2 # Set your radio's MAC address
tnc> aprs                        # Return to APRS mode
```

The MAC address is saved to `~/.tnc_config.json` and will be used on subsequent runs.

---

## 🚀 Quick Start

### KISS-over-TCP Mode (Direwolf - Recommended)

**1. Start Direwolf** (on Raspberry Pi or local machine):
```bash
# direwolf.conf should have:
# KISSPORT 8001
direwolf -c direwolf.conf
```

**2. Connect console:**
```bash
python main.py -k localhost:8001          # Direwolf on same machine
python main.py -k 192.168.1.50:8001       # Direwolf on Pi at 192.168.1.50
python main.py -k direwolf.local          # mDNS hostname
```

**3. Access Web UI:**
```
http://localhost:8002
```

You'll see:
- Real-time APRS map with all heard stations
- Station list with sortable columns
- Live activity feed
- No TNC/AGWPE bridges (console is client mode)

### Serial KISS Mode (Hardware TNC)

```bash
# Default baud rate (9600)
python main.py --serial /dev/ttyUSB0

# Custom baud rate (19200)
python main.py --serial /dev/ttyUSB0 --baud 19200

# Auto-start in TNC mode
python main.py -s /dev/ttyUSB0 -t
```

Available serial ports:
- **Linux:** `/dev/ttyUSB0`, `/dev/ttyACM0`, `/dev/ttyS0`
- **macOS:** `/dev/cu.usbserial-*`, `/dev/cu.usbmodem*`
- **Windows:** `COM3`, `COM4`, etc.

### BLE Mode (UV-50PRO Radio)

**First time setup** - configure your radio's MAC address:
```bash
python main.py
aprs> tnc
tnc> RADIO_MAC 38:D2:00:01:62:C2   # Use your radio's actual MAC
tnc> aprs
```

**Subsequent runs:**
```bash
python main.py              # Uses saved MAC from config
# or
python main.py -r 38:D2:00:01:62:C2   # Override with command line
```

The console will:
1. Connect to UV-50PRO via Bluetooth LE
2. Start AGWPE bridge on port 8000
3. Start TNC bridge on port 8001
4. Start Web UI on port 8002
5. Begin monitoring APRS traffic

---

## 📖 Usage Guide

### First Run - Set Your Callsign

**Configuration commands are only available in TNC mode.** Switch to TNC mode first:

```bash
aprs> tnc                        # Switch to TNC mode to access configuration
tnc> mycall K1FSY-9              # Set your callsign
tnc> mylocation FN42             # Set grid square (for beaconing)
tnc> beacon on                   # Enable position beaconing
tnc> beacon every 10             # Beacon every 10 minutes
tnc> aprs                        # Return to APRS mode
```

### Send APRS Messages

```bash
aprs> msg W1ABC Hello from the console!
aprs> msg W1ABC-5 Testing APRS messaging
```

Messages show delivery status:
- `⋯` - Queued/pending RF delivery
- `→` - Digipeated (heard by digipeater)
- `✓` - Acknowledged by recipient

### View Heard Stations

```bash
aprs> stations                         # List all stations
aprs> stations last 1h                 # Stations heard in last hour
aprs> station W1ABC                    # Details for specific station
aprs> weather KB1ABC                   # Weather data for station
```

### Console Modes & Mode Switching

The console has multiple modes depending on your hardware:

- **`aprs>`** - APRS mode for messaging, tracking, beaconing (always available)
- **`tnc>`** - TNC terminal mode for AX.25 connected mode (packet BBS/nodes)
- **`radio>`** - Radio control mode for frequency/VFO/power settings (BLE mode only)

**NEW:** Commands can now be prefixed to work from any mode:
- `aprs <command>` - APRS operations (messaging, stations, weather)
- `radio <command>` - Radio control (vfo, channels, power)
- `tnc <command>` - TNC-2 configuration (mycall, monitor, digipeater)

**Examples:**
```bash
aprs> tnc display           # View TNC parameters from APRS mode
aprs> tnc mycall N0CALL     # Set callsign from APRS mode
radio> aprs message read    # Read messages from radio mode
```

### Connect to Packet BBS

```bash
aprs> tnc                             # Switch to TNC mode
tnc> connect W1XM                     # Connect to BBS
Connected to W1XM

# Type messages normally
# Use ~~~ on new line to disconnect
~~~
tnc> aprs                             # Return to APRS mode (or 'radio' for radio mode)
```

### Enable Digipeater

```bash
aprs> tnc digipeater on               # Enable WIDEn-N digipeating
aprs> tnc myalias WIDE1               # Set digipeater alias
aprs> tnc digipeater off              # Disable digipeating
```

**Note:** MYALIAS allows your digipeater to respond to generic aliases (WIDE1, GATE, RELAY) in addition to your callsign. Supports both exact match and SSID matching (WIDE1 matches WIDE1-1, WIDE1-2, etc.)

### Personal Weather Station

Connect local weather hardware (Ecowitt, Davis, etc.) for APRS weather beaconing:

```bash
aprs> pws                             # Show PWS status
aprs> pws show                        # Display current weather
aprs> pws fetch                       # Fetch fresh data
aprs> pws connect                     # Connect to station

# Configuration (in TNC mode or with prefix)
tnc> wx_enable on                     # Enable weather integration
tnc> wx_backend ecowitt               # Set backend type
tnc> wx_address 192.168.1.100         # Weather station IP
tnc> wx_interval 300                  # Update interval (seconds)
```

**Note:** `pws` controls YOUR local weather station. Use `aprs wx list` to view remote weather stations heard over RF.

### Debugging

```bash
aprs> debug 2                         # Enable protocol debugging
aprs> debug filter K1FSY W1ABC        # Only debug these stations
aprs> debug dump 20                   # Show last 20 frames
aprs> debug dump 50 detail            # Show last 50 (with full decode)
```

### View Help

```bash
aprs> help                            # Show all commands
aprs> help msg                        # Help for specific command
aprs> ?                               # Quick help
```

---

## 🌐 Web UI Features

**Access:** http://localhost:8002 (or your Pi's IP address)

### Map View
- Click **any station callsign** to see details
- **Position history paths** for mobile stations
- **Coverage polygons** for digipeaters
- **Local coverage circles** showing 1-hop stations
- **Multi-station popups** for co-located stations
- **Custom APRS symbols** (850+ symbols rendered)
- **Time filtering:** Last 1h, 6h, 24h, or all time

### Stations Table
- **Sortable columns:** Last Heard, Callsign, Packets, Distance, Hops
- **Device identification:** Shows radio/software (Kenwood TH-D74, Direwolf, etc.)
- **Real-time updates** via Server-Sent Events
- **Clickable callsigns** navigate to station detail pages

### Weather Dashboard
- **Interactive charts** showing temperature, humidity, pressure over time
- **Weather station list** with latest readings
- **Historical data** with configurable retention

### Activity Feed
- **Live console output** showing all received packets
- **Message notifications** with sender/recipient
- **Position updates** from mobile stations
- **Auto-scroll** to latest activity

### HTTP POST API (NEW in v0.9.0)
- **Authenticated endpoint** for remote beacon comment updates
- **POST /api/beacon/comment** - Update beacon comment via HTTP
- **Password-based security** (configurable via `WEBUI_PASSWORD`)
- **Optional immediate transmission** - Send beacon after update
- **IoT/automation friendly** - Perfect for remote monitoring and control
- **Example:** Update comment from GPS tracker, weather station, or monitoring system
- **Documentation:** See `doc/WEBUI_POST_API.md` for complete API reference

```bash
# Set password
WEBUI_PASSWORD your_secure_password

# Update comment via cURL
curl -X POST http://localhost:8002/api/beacon/comment \
  -H "Content-Type: application/json" \
  -d '{"password":"your_secure_password","comment":"Updated from API!"}'
```

---

## 🔌 Integration with Other Software

### YAAC (Yet Another APRS Client)

1. **Configure YAAC:**
   - TNC Type: `AGWPE`
   - Host: `localhost`
   - Port: `8000`

2. **Start this console** (any mode)

3. **YAAC connects** and uses this console as TNC

### Outpost PM (Packet Message Manager)

1. **Configure Outpost:**
   - TNC Type: `AGWPE over TCP/IP`
   - Host: `localhost`
   - Port: `8000`

2. **Start this console**

3. **Outpost** uses this for packet messaging

### UIView32 / APRSISCE/32 / Xastir

All support AGWPE mode - configure with:
- **Host:** `localhost`
- **Port:** `8000`

### KISSet (KISS Terminal)

For raw KISS access:
- **Host:** `localhost`
- **Port:** `8001`

---

## 🎓 Architecture & Design

### Modular Design (~10,500+ lines across 15 modules)

```
src/
├── aprs_manager.py       # APRS protocol parsing and station tracking
├── ax25_adapter.py       # AX.25 connected mode implementation
├── radio.py              # Radio controller (UV-50PRO BLE interface)
├── transport.py          # Transport abstraction (BLE, Serial, TCP)
├── tnc_bridge.py         # KISS TCP server (port 8001)
├── agwpe_bridge.py       # AGWPE TCP server (port 8000)
├── web_server.py         # HTTP server for Web UI
├── web_api.py            # REST API and SSE endpoints
├── digipeater.py         # APRS digipeater logic
├── coverage.py           # Coverage polygon generation
├── console.py            # Main console UI and command processor
└── constants.py          # Configuration and UUIDs
```

### Transport Abstraction

Clean separation allows identical functionality across all modes:

```python
class TransportBase(ABC):
    async def write_kiss_frame(data: bytes) -> bool
    async def send_tnc_data(data: bytes) -> None
    async def close() -> None
```

**Implementations:**
- `BLETransport` - UV-50PRO via Bluetooth LE
- `SerialTransport` - Hardware TNCs via serial
- `TCPTransport` - Direwolf via KISS-over-TCP

**Result:** Switch transports with a command-line flag - all features work identically!

### Asyncio-Native

Built from the ground up with Python's `asyncio`:
- Non-blocking I/O for all network operations
- Concurrent handling of TNC, AGWPE, and Web UI
- Efficient event-driven architecture
- No threading complexity

### Database Persistence

All data stored in user's home directory:
- **APRS Database:** `~/.aprs_stations.json.gz` (GZIP compressed)
- **TNC Config:** `~/.tnc_config.json`
- **Frame History:** In-memory with configurable buffer (default: 10MB)

**Auto-migration** from legacy locations on first run.

---

## 🛠️ Advanced Configuration

### Port Configuration

All network ports are configurable via TNC commands:

```bash
aprs> agwpe_port 8000              # AGWPE bridge port
aprs> tnc_port 8001                # TNC KISS bridge port
aprs> webui_port 8002              # Web UI HTTP port
```

**Note:** Restart required for port changes to take effect.

### Beacon Configuration

```bash
aprs> beacon on                    # Enable beaconing
aprs> beacon every 10              # Beacon every 10 minutes
aprs> beacon path WIDE1-1,WIDE2-1  # Set digipeater path
aprs> beacon symbol /[             # Jogger symbol
aprs> beacon comment FSY Packet Console  # Beacon comment
```

### Message Retry Configuration

```bash
aprs> retry 3                      # Maximum retry attempts
aprs> retry_fast 20                # Fast retry (RF delivery) in seconds
aprs> retry_slow 600               # Slow retry (waiting for ACK) in seconds
```

### Debug Buffer Configuration

```bash
aprs> debug buffer 50              # 50MB buffer (stores ~50,000 frames)
aprs> debug buffer 10              # 10MB buffer (default)
aprs> debug buffer off             # Simple 10-frame mode (low memory)
```

---

## 📊 Performance & Scalability

### Tested Loads

- ✅ **661 stations tracked** simultaneously
- ✅ **1380 messages** in database
- ✅ **6028 position records** with full history
- ✅ **725 weather reports** with historical charts
- ✅ **Database load time:** 0.57s (GZIP decompression + JSON parse)
- ✅ **Startup time:** ~2 seconds (Raspberry Pi 4)
- ✅ **Memory usage:** ~45MB base, ~100MB with full database
- ✅ **CPU usage:** <5% idle, <15% during busy traffic

### Optimizations

- **GZIP compression** reduces database size by 70%
- **Lazy imports** speed up startup
- **Asyncio** enables concurrent operations without threads
- **Intelligent caching** for coverage polygon generation
- **Frame buffering** prevents memory bloat

---

## 🧪 Protocol Debugging Workflow

### Quick Debugging Session Example

**Scenario:** You're not receiving ACKs for your APRS messages. Here's how to debug:

```bash
# 1. Enable protocol debugging
aprs> debug 5

# 2. Send a test message
aprs> msg W1ABC-5 Testing ACK

# 3. Watch the real-time protocol trace
[DEBUG] TNC TX (38 bytes): c000a8b2a4...
[DEBUG] KISS: Command=0x00 (Data), Port=0
[DEBUG] AX.25: K1FSY-9 > W1ABC-5 via WIDE1-1
[DEBUG] APRS: Message to W1ABC-5, ID=123
[DEBUG] Message queued for retry (fast: 20s)

# 4. Wait for response... see digipeater activity
[DEBUG] TNC RX (42 bytes): c00092884040...
[DEBUG] Heard via WIDE1-1* (digipeated)
[DEBUG] Message state: DIGIPEATED (→)

# 5. No ACK received after 20s - check frame details
aprs> debug dump 10 detail

# Shows full decode of last 10 frames including:
# - Your outgoing message
# - Digipeater repeats
# - Any ACK attempts (or lack thereof)
```

### Analyzing Problematic Frames

```bash
# Enable filtering to focus on problem station
aprs> debug filter W1ABC

# Set large buffer to capture extended session
aprs> debug buffer 50

# Watch activity
aprs> debug 2

# After issue occurs, dump with full analysis
aprs> debug dump 50 detail

# Look for:
# - Path issues (bad digipeater routing)
# - Corrupted frames (checksum errors)
# - Missing ACKs (message IDs don't match)
# - Timing issues (duplicate packets)
```

### Common Debug Scenarios

**Finding why a station isn't appearing on map:**
```bash
aprs> debug filter K1MAL
aprs> debug 5
# Wait for packets...
# Check if position is valid (not 0.0, 0.0)
# Verify symbol table and symbol code
# Check for "Null Island" filtering
```

**Diagnosing message delivery:**
```bash
aprs> messages K1FSY
# Shows all messages with delivery status
# ⋯ = pending, → = digipeated, ✓ = ACKed

aprs> debug dump all detail | grep "Message"
# Shows all message frames with full decode
```

**Tracking digipeater coverage:**
```bash
aprs> debug filter W1ABC W2XYZ N1DEF
aprs> debug 2
# Watch which digipeaters repeat which packets
# Build mental map of coverage areas
```

### Buffer Management

```bash
# Check buffer status
aprs> debug buffer
Buffer: 10MB, 1,247 frames stored (8.3MB used)

# For extended monitoring (24hr+ sessions)
aprs> debug buffer 50            # 50MB = ~50,000 frames

# For low-memory systems (Raspberry Pi Zero)
aprs> debug buffer off           # Keep only last 10 frames

# Export frames for later analysis
aprs> debug dump all > /tmp/frames.txt
```

### Real-World Examples

**Example 1: Diagnosing "Heard but not displayed" issue**
```bash
aprs> debug 6                    # Full trace
# Received packet from KB1XYZ
[DEBUG] Position: 0.000000N, 0.000000W
[DEBUG] Invalid position (Null Island) - not adding to database
# Solution: Station has no GPS fix
```

**Example 2: Understanding message retry behavior**
```bash
aprs> debug 5
aprs> msg W1ABC Testing

[DEBUG] Message TX #1 at 12:00:00 (ID: 001)
[DEBUG] No digipeater response after 20s
[DEBUG] Message TX #2 at 12:00:20 (fast retry)
[DEBUG] Heard via WIDE1-1* at 12:00:22
[DEBUG] Message state: DIGIPEATED (→)
[DEBUG] Waiting 600s for ACK (slow retry)
[DEBUG] ACK received at 12:02:15
[DEBUG] Message state: ACKNOWLEDGED (✓)
# Total round-trip: 2min 15sec
```

**Example 3: Identifying duplicate iGate packets**
```bash
aprs> debug 2
[DEBUG] RX from W1ABC-5 via qAR,IGATE1
[DEBUG] Fuzzy duplicate (80% match) - suppressed
# Same message corrupted by iGate, correctly ignored
```

---

## 🗺️ Coverage Visualization

### Digipeater Coverage

The Web UI automatically generates **convex hull polygons** showing digipeater coverage:

1. **Algorithm:** Graham scan for convex hull
2. **Input:** All stations heard via a specific digipeater
3. **Output:** Polygon showing digipeater's effective range
4. **Color-coded** by digipeater callsign

### Local Coverage Circles

Click any station on the map to see:
- **Green circle** - Stations heard directly (1-hop)
- **Coverage radius** calculated from furthest 1-hop station
- **Station count** within local coverage area

### Time-Based Filtering

Filter coverage by time window:
- **Last 1 hour** - Recent activity only
- **Last 6 hours** - Half-day coverage
- **Last 24 hours** - Full day (default)
- **All time** - Complete historical coverage

### Mobile Station Paths

Stations with position history show:
- **Blue line** connecting all position reports
- **Oldest → Newest** direction
- **Timestamps** on hover
- **Path length** in station details

---

## 🤝 Contributing

Contributions welcome! Areas of interest:

- **Additional TNC support** (Mobilinkd, BTECH, etc.)
- **Improved MIC-E decoding** for edge cases
- **Web UI enhancements** (dark mode, mobile optimization)
- **Database migration tools** (import from APRS.fi, etc.)
- **Testing on UV-50PRO clones** (compatibility reports)
- **Documentation improvements**

Please open an issue before major changes to discuss approach.

---

## 📜 License

This work is licensed under a [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-nc-sa/4.0/).

**You are free to:**
- Share — copy and redistribute the material in any medium or format
- Adapt — remix, transform, and build upon the material

**Under the following terms:**
- **Attribution** — You must give appropriate credit
- **NonCommercial** — You may not use the material for commercial purposes
- **ShareAlike** — If you remix, transform, or build upon the material, you must distribute your contributions under the same license

See [LICENSE](LICENSE) file for full legal text.

---

## 🙏 Acknowledgments

- **Direwolf** - Excellent software TNC by WB2OSZ
- **APRS.org** - Bob Bruninga WB4APR for creating APRS
- **UV-50PRO** - Affordable dual-band with Bluetooth TNC
- **Amateur Radio Community** - For protocols, documentation, and testing

---

## 🐛 Troubleshooting

### "Device not found" (BLE mode)
- Ensure UV-50PRO is powered on and in range
- Check Bluetooth is enabled on computer
- Verify MAC address is configured: `tnc> RADIO_MAC 38:D2:00:01:62:C2`
- Or use command line: `python main.py -r 38:D2:00:01:62:C2`
- Try `bluetoothctl scan on` to find your radio's MAC address

### "Connection refused" (TCP mode)
- Verify Direwolf is running: `ps aux | grep direwolf`
- Check Direwolf config: `KISSPORT 8001`
- Test connectivity: `telnet localhost 8001`
- Check firewall rules

### "Permission denied" (Serial mode)
- Add user to `dialout` group: `sudo usermod -a -G dialout $USER`
- Log out and log back in
- Check port permissions: `ls -l /dev/ttyUSB0`

### No stations appearing on map
- Verify radio is receiving packets (check console for `RX` messages)
- Check antenna connection
- Ensure on correct frequency (144.390 MHz in North America)
- Try higher debug level: `debug 2`

### Web UI not loading
- Check port 8002 is not in use: `netstat -an | grep 8002`
- Try different port: `webui_port 8003` (restart required)
- Check browser console for errors
- Verify network connectivity to server

---

## 💬 Support

- **Issues:** [GitHub Issues](https://github.com/krisp/fsy-packet-console/issues)
- **Discussions:** [GitHub Discussions](https://github.com/krisp/fsy-packet-console/discussions)
- **Email:** k1fsy@vhfwiki.com

---

**73 de K1FSY** 📡

---

*FSY Packet Console - Professional packet radio for serious operators.*
