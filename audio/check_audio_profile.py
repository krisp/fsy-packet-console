#!/usr/bin/env python3
"""Check if Bluetooth audio profiles are active on a device."""

import sys
from pydbus import SystemBus

def check_audio_profiles(mac_address):
    """Check active audio profiles on device.

    Args:
        mac_address: Device MAC address
    """
    bus = SystemBus()
    adapter_path = "/org/bluez/hci0"
    device_path = f"{adapter_path}/dev_{mac_address.replace(':', '_')}"

    try:
        # Get device object
        device = bus.get("org.bluez", device_path)

        print(f"Checking audio profiles for {mac_address}...")
        print("=" * 60)

        # Check for MediaControl interface (audio playback control)
        try:
            media_path = device_path
            media = bus.get("org.bluez", media_path)
            if hasattr(media, 'Connected'):
                print(f"[AUDIO] MediaControl Connected: {media.Connected}")
        except Exception:
            print("[INFO] No MediaControl interface (expected for HFP)")

        # Check for RFCOMM channels (HFP uses RFCOMM)
        import os
        import glob

        rfcomm_devices = glob.glob("/dev/rfcomm*")
        if rfcomm_devices:
            print(f"\n[HFP] RFCOMM devices found: {rfcomm_devices}")
        else:
            print("\n[INFO] No RFCOMM devices (HFP not connected)")

        # Get all device properties
        print("\n[DEVICE] All Properties:")
        print("-" * 60)
        for attr in dir(device):
            if not attr.startswith('_'):
                try:
                    value = getattr(device, attr)
                    if not callable(value):
                        print(f"  {attr}: {value}")
                except Exception:
                    pass

        # List all D-Bus interfaces on this device object
        print("\n[D-BUS] Available Interfaces:")
        print("-" * 60)

        introspection_xml = device._Introspect()
        if 'org.bluez.MediaControl1' in introspection_xml:
            print("  ✓ org.bluez.MediaControl1 (Audio control)")
        if 'org.bluez.MediaPlayer1' in introspection_xml:
            print("  ✓ org.bluez.MediaPlayer1 (Media player)")
        if 'org.bluez.Device1' in introspection_xml:
            print("  ✓ org.bluez.Device1 (Base device)")
        if 'org.bluez.GattService1' in introspection_xml:
            print("  ✓ GATT Services (BLE)")

        # Show raw introspection for debugging
        print("\n[DEBUG] Raw Introspection:")
        print("-" * 60)
        print(introspection_xml[:500] + "..." if len(introspection_xml) > 500 else introspection_xml)

    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_audio_profile.py <MAC_ADDRESS>")
        sys.exit(1)

    check_audio_profiles(sys.argv[1])
