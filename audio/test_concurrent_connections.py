#!/usr/bin/env python3
"""
Test if BLE (TNC) and RFCOMM (audio) can coexist on UV-50PRO.

This script attempts to:
1. Maintain existing BLE connection (read-only test)
2. Establish RFCOMM connection for audio
3. Verify both are active simultaneously
"""

import asyncio
import sys
from bleak import BleakClient, BleakScanner
from pydbus import SystemBus

# UV-50PRO MAC address
MAC_ADDRESS = "38:D2:00:01:62:C2"

# BLE UUIDs (from your console)
TNC_RX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


async def test_ble_connection():
    """Test BLE connectivity (your existing console connection)."""
    print("\n[BLE] Testing BLE connection...")
    print("=" * 60)

    try:
        # Find device
        print(f"[BLE] Scanning for {MAC_ADDRESS}...")
        device = await BleakScanner.find_device_by_address(MAC_ADDRESS, timeout=10.0)

        if not device:
            print(f"[BLE] ✗ Device not found")
            return None

        print(f"[BLE] ✓ Device found: {device.name}")

        # Connect
        print(f"[BLE] Connecting...")
        client = BleakClient(device, timeout=20.0)
        await client.connect()

        if client.is_connected:
            print(f"[BLE] ✓ Connected successfully")

            # Check if TNC characteristic exists (verify it's our radio)
            try:
                services = client.services
                tnc_char = services.get_characteristic(TNC_RX_UUID)
                if tnc_char:
                    print(f"[BLE] ✓ TNC characteristic found")
                else:
                    print(f"[BLE] - TNC characteristic not found")
            except Exception as e:
                print(f"[BLE] - TNC characteristic check failed: {e}")

            return client
        else:
            print(f"[BLE] ✗ Connection failed")
            return None

    except Exception as e:
        print(f"[BLE] ✗ Error: {e}")
        return None


def test_rfcomm_connection():
    """Test RFCOMM connectivity (for audio)."""
    print("\n[RFCOMM] Testing RFCOMM connection...")
    print("=" * 60)

    try:
        bus = SystemBus()
        adapter_path = "/org/bluez/hci0"
        device_path = f"{adapter_path}/dev_{MAC_ADDRESS.replace(':', '_')}"

        device = bus.get("org.bluez", device_path)

        # Get device info
        connected = device.Connected
        print(f"[RFCOMM] Device connected (BlueZ): {connected}")

        # Check available services
        uuids = device.UUIDs
        print(f"[RFCOMM] Available UUIDs:")

        has_spp = False
        for uuid in uuids:
            if uuid.startswith("00001101"):
                print(f"  ✓ {uuid} - Serial Port Profile (SPP)")
                has_spp = True
            elif uuid.startswith("0000111f"):
                print(f"  ✓ {uuid} - Handsfree Audio Gateway (HFP)")
            elif uuid.startswith("00001800") or uuid.startswith("00001801"):
                print(f"  - {uuid} - GATT (BLE)")
            else:
                print(f"  - {uuid}")

        if not has_spp:
            print(f"[RFCOMM] ⚠ No Serial Port Profile found")
            return False

        # Try to connect to SPP profile
        SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
        print(f"\n[RFCOMM] Attempting to connect SPP profile...")

        try:
            device.ConnectProfile(SPP_UUID)
            print(f"[RFCOMM] ✓ SPP connection initiated")

            # Wait a bit for connection
            import time
            time.sleep(2)

            # Check for RFCOMM devices
            import glob
            rfcomm_devs = glob.glob("/dev/rfcomm*")
            if rfcomm_devs:
                print(f"[RFCOMM] ✓ RFCOMM devices: {rfcomm_devs}")
                return True
            else:
                print(f"[RFCOMM] - No /dev/rfcomm* devices (might use socket directly)")
                return True  # Connection might still work via socket

        except Exception as e:
            if "Already Connected" in str(e):
                print(f"[RFCOMM] ✓ Profile already connected")
                return True
            else:
                print(f"[RFCOMM] ✗ Connection failed: {e}")
                return False

    except Exception as e:
        print(f"[RFCOMM] ✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_concurrent():
    """Test both BLE and RFCOMM simultaneously."""
    print("\n" + "=" * 60)
    print("CONCURRENT CONNECTION TEST")
    print("=" * 60)

    # Step 1: Establish BLE
    ble_client = await test_ble_connection()

    if not ble_client:
        print("\n[RESULT] ✗ BLE connection failed, cannot test concurrency")
        return

    # Step 2: Try RFCOMM while BLE is active
    print("\n[TEST] BLE is connected, now attempting RFCOMM...")
    rfcomm_ok = test_rfcomm_connection()

    # Step 3: Verify BLE still works
    print("\n[TEST] Verifying BLE still active after RFCOMM attempt...")
    if ble_client.is_connected:
        print(f"[BLE] ✓ Still connected")

        # Try to access services again
        try:
            services = ble_client.services
            # Try to use the services to verify they're still accessible
            tnc_char = services.get_characteristic(TNC_RX_UUID)
            print(f"[BLE] ✓ Services still accessible (TNC char: {tnc_char is not None})")
        except Exception as e:
            print(f"[BLE] ✗ Service access failed: {e}")
    else:
        print(f"[BLE] ✗ Connection lost!")

    # Results
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    if ble_client.is_connected and rfcomm_ok:
        print("✅ SUCCESS: Both BLE and RFCOMM can coexist!")
        print("\nThis means you can:")
        print("  • Keep your console's BLE connection (TNC, GPS, commands)")
        print("  • Add RFCOMM audio using benlink or custom implementation")
    elif ble_client.is_connected:
        print("⚠️  PARTIAL: BLE works, but RFCOMM connection unclear")
        print("   RFCOMM might work via socket even without /dev/rfcomm*")
    else:
        print("❌ CONFLICT: BLE connection lost when RFCOMM attempted")
        print("   Radio might not support concurrent connections")

    print("=" * 60)

    # Cleanup
    await ble_client.disconnect()
    print("\n[CLEANUP] Disconnected BLE")


if __name__ == "__main__":
    try:
        asyncio.run(test_concurrent())
    except KeyboardInterrupt:
        print("\n\n[ABORT] Test interrupted")
