# Bluetooth Audio Test Scripts

This directory contains diagnostic and test scripts for UV-50PRO Bluetooth audio support.

**See:** [CHECKPOINT_2026-01-31_AUDIO_SUPPORT.md](../doc/CHECKPOINT_2026-01-31_AUDIO_SUPPORT.md) for complete documentation.

---

## Scripts

### 1. `test_bluez_audio.py`
BlueZ D-Bus interface for Classic Bluetooth operations.

**Features:**
- Device discovery with profile detection
- Pairing and trust management
- Profile connection (SPP, HFP)
- Status monitoring

**Usage:**
```bash
python3 test_bluez_audio.py discover
python3 test_bluez_audio.py status <MAC>
python3 test_bluez_audio.py connect <MAC>
python3 test_bluez_audio.py pair <MAC>
```

**Dependencies:** `pydbus`

---

### 2. `check_audio_profile.py`
Detailed inspection of Bluetooth audio capabilities.

**Features:**
- MediaControl interface status
- RFCOMM device presence
- D-Bus interface enumeration
- Device properties and UUIDs

**Usage:**
```bash
python3 check_audio_profile.py <MAC>
```

**Dependencies:** `pydbus`

---

### 3. `connect_hfp.py`
Attempts explicit HFP and SPP profile connections.

**Features:**
- ConnectProfile() D-Bus calls
- RFCOMM channel detection
- PulseAudio/PipeWire sink checking

**Usage:**
```bash
python3 connect_hfp.py <MAC>
```

**Dependencies:** `pydbus`

---

### 4. `test_concurrent_connections.py`
Validates BLE + Classic BT simultaneous operation.

**Test Sequence:**
1. Establish BLE connection (TNC/GPS)
2. Attempt RFCOMM connection (audio)
3. Verify BLE remains stable

**Usage:**
```bash
python3 test_concurrent_connections.py
```

**Dependencies:** `bleak`, `pydbus`

---

### 5. `register_hfp_dbus.py` ⭐ **Primary Tool**
Registers HFP Handsfree profile with BlueZ to advertise audio capabilities.

**Features:**
- Implements org.bluez.Profile1 D-Bus interface
- Advertises audio support via SDP
- Handles NewConnection callbacks
- Provides RFCOMM file descriptor for audio I/O

**Usage:**
```bash
# Run BEFORE pairing to advertise audio support
python3 register_hfp_dbus.py

# Keep running - will show when audio connections established
```

**Dependencies:** `python3-dbus` (system package)

**Installation:**
```bash
sudo apt install python3-dbus python3-gi
```

**Note:** Must run BEFORE pairing device for radio to detect audio capability.

---

### 6. `register_hfp_profile.py`
Initial attempt using pydbus (deprecated - use `register_hfp_dbus.py` instead).

**Status:** Not functional - pydbus doesn't support D-Bus service publishing well.

---

## Quick Start

### Enable Audio Support on UV-50PRO

```bash
# 1. Install dependencies
sudo apt install python3-dbus python3-gi

# 2. Start HFP registration
python3 register_hfp_dbus.py

# 3. In another terminal, remove old pairing
bluetoothctl
> remove 38:D2:00:01:62:C2

# 4. Re-pair with HFP active
> scan on
> pair 38:D2:00:01:62:C2
> trust 38:D2:00:01:62:C2
> connect 38:D2:00:01:62:C2

# Terminal 1 should show:
# [HFP] ✓ New audio connection from ...
```

---

## Test Results

All scripts validated on:
- **Hardware:** Raspberry Pi 4 Model B
- **OS:** Raspberry Pi OS (Debian 12 Bookworm)
- **Radio:** UV-50PRO (MAC: 38:D2:00:01:62:C2)
- **BlueZ:** 5.66+

**Key Findings:**
- ✅ BLE and Classic BT can coexist
- ✅ HFP profile registration works
- ✅ RFCOMM audio connection established
- ✅ File descriptor received for audio I/O
- ⚠️ Radio's handset menu may need re-pairing to show audio toggle

---

Last Updated: 2026-01-31
