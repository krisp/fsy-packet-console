#!/usr/bin/env python3
"""Explicitly connect to HFP Audio Gateway profile."""

import sys
from pydbus import SystemBus

def connect_hfp(mac_address):
    """Connect to HFP Audio Gateway profile.

    Args:
        mac_address: Device MAC address
    """
    bus = SystemBus()
    adapter_path = "/org/bluez/hci0"
    device_path = f"{adapter_path}/dev_{mac_address.replace(':', '_')}"

    # HFP Audio Gateway UUID
    HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
    # Serial Port Profile UUID (alternative)
    SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

    try:
        device = bus.get("org.bluez", device_path)

        print(f"Attempting to connect HFP profile for {mac_address}...")
        print("=" * 60)

        # Try to connect to HFP Audio Gateway profile
        print(f"[1] Trying HFP Audio Gateway (111f)...")
        try:
            device.ConnectProfile(HFP_AG_UUID)
            print("    ✓ HFP profile connection initiated")
        except Exception as e:
            print(f"    ✗ HFP failed: {e}")

        # Also try SPP (Serial Port Profile) - might be used for audio
        print(f"\n[2] Trying Serial Port Profile (1101)...")
        try:
            device.ConnectProfile(SPP_UUID)
            print("    ✓ SPP profile connection initiated")
        except Exception as e:
            print(f"    ✗ SPP failed: {e}")

        # Check status after attempts
        print("\n[3] Checking connection status...")
        import time
        time.sleep(2)

        connected = device.Connected
        print(f"    Device connected: {connected}")

        # Check for RFCOMM devices again
        import glob
        rfcomm = glob.glob("/dev/rfcomm*")
        if rfcomm:
            print(f"    ✓ RFCOMM devices: {rfcomm}")
        else:
            print(f"    - No RFCOMM devices")

        # Check for audio sinks (if PipeWire/PulseAudio running)
        import subprocess
        try:
            result = subprocess.run(
                ['pactl', 'list', 'sinks', 'short'],
                capture_output=True,
                text=True,
                timeout=2
            )
            if 'bluez' in result.stdout.lower():
                print(f"    ✓ Bluetooth audio sink detected!")
                print(result.stdout)
            else:
                print(f"    - No Bluetooth audio sink (PulseAudio may not be running)")
        except Exception:
            print(f"    - Can't check audio sinks (pactl not available)")

        print("=" * 60)

    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: connect_hfp.py <MAC_ADDRESS>")
        sys.exit(1)

    connect_hfp(sys.argv[1])
