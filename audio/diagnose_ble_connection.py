#!/usr/bin/env python3
"""
Diagnose BLE connection issues after HFP profile registration.

The error "br-connection-profile-unavailable" suggests BlueZ
is trying to connect via Classic BT instead of BLE.
"""

import asyncio
from bleak import BleakScanner, BleakClient
from pydbus import SystemBus


MAC_ADDRESS = "38:D2:00:01:62:C2"


async def check_ble_advertisement():
    """Check if device is advertising via BLE."""
    print("=" * 60)
    print("BLE Advertisement Check")
    print("=" * 60)

    print("\n[1] Scanning for BLE advertisements...")
    devices = await BleakScanner.discover(timeout=10.0)

    found_ble = False
    for device in devices:
        if device.address.upper() == MAC_ADDRESS.upper():
            found_ble = True
            print(f"\n✅ Found BLE advertisement!")
            print(f"   Address: {device.address}")
            print(f"   Name: {device.name}")
            print(f"   RSSI: {device.rssi}")
            print(f"   Metadata: {device.metadata}")
            break

    if not found_ble:
        print(f"\n❌ No BLE advertisement from {MAC_ADDRESS}")
        print("   Radio may not be in BLE mode or is out of range")

    return found_ble


def check_bluez_device_state():
    """Check device state in BlueZ."""
    print("\n" + "=" * 60)
    print("BlueZ Device State Check")
    print("=" * 60)

    try:
        bus = SystemBus()
        adapter_path = "/org/bluez/hci0"
        device_path = f"{adapter_path}/dev_{MAC_ADDRESS.replace(':', '_')}"

        device = bus.get("org.bluez", device_path)

        print(f"\n[BlueZ] Device Information:")
        print(f"  Address: {device.Address}")
        print(f"  Name: {device.Name}")
        print(f"  AddressType: {device.AddressType}")
        print(f"  Paired: {device.Paired}")
        print(f"  Connected: {device.Connected}")
        print(f"  Trusted: {device.Trusted}")
        print(f"  Bonded: {device.Bonded}")

        # Check what's connected
        print(f"\n[BlueZ] Connection Details:")
        print(f"  ServicesResolved: {device.ServicesResolved}")

        # Check UUIDs
        print(f"\n[BlueZ] Available UUIDs:")
        for uuid in device.UUIDs:
            if uuid.startswith("00001800") or uuid.startswith("00001801"):
                print(f"  ✓ {uuid} - GATT (BLE)")
            elif uuid.startswith("00001101"):
                print(f"  • {uuid} - Serial Port (Classic BT)")
            elif uuid.startswith("0000111f"):
                print(f"  • {uuid} - HFP AG (Classic BT)")
            elif uuid.startswith("0000111e"):
                print(f"  ✓ {uuid} - HFP HF (Registered by us)")
            else:
                print(f"  • {uuid}")

        # Check adapter settings
        adapter = bus.get("org.bluez", adapter_path)
        print(f"\n[BlueZ] Adapter Settings:")
        print(f"  Powered: {adapter.Powered}")
        print(f"  Discoverable: {adapter.Discoverable}")
        print(f"  Pairable: {adapter.Pairable}")

        return True

    except Exception as e:
        print(f"\n❌ BlueZ error: {e}")
        return False


async def attempt_ble_connection():
    """Attempt BLE connection with detailed error reporting."""
    print("\n" + "=" * 60)
    print("BLE Connection Attempt")
    print("=" * 60)

    try:
        print(f"\n[1] Finding device by address...")
        device = await BleakScanner.find_device_by_address(
            MAC_ADDRESS, timeout=10.0
        )

        if not device:
            print(f"❌ Device not found via BLE scan")
            print(f"\nPossible causes:")
            print(f"  1. Radio not in range")
            print(f"  2. Radio BLE disabled (check radio settings)")
            print(f"  3. Radio already connected via Classic BT")
            return False

        print(f"✓ Device found: {device.name}")

        print(f"\n[2] Creating BLE client...")
        client = BleakClient(device, timeout=20.0)

        print(f"\n[3] Attempting connection...")
        await client.connect()

        if client.is_connected:
            print(f"✅ BLE connection successful!")

            # Check services
            services = client.services
            print(f"\n[4] Checking GATT services...")
            for service in services:
                print(f"   Service: {service.uuid}")
                for char in service.characteristics:
                    print(f"     • {char.uuid}")

            await client.disconnect()
            return True
        else:
            print(f"❌ Connection failed (no error but not connected)")
            return False

    except Exception as e:
        print(f"\n❌ BLE connection error: {e}")
        print(f"\nError type: {type(e).__name__}")

        if "br-connection-profile-unavailable" in str(e):
            print(f"\n⚠️  DIAGNOSIS:")
            print(f"   BlueZ is trying to connect via Classic Bluetooth")
            print(f"   instead of BLE. This happens when:")
            print(f"   1. Device is cached as Classic BT device")
            print(f"   2. HFP profile registration changed connection preference")
            print(f"\n💡 FIX:")
            print(f"   Option 1: Remove device from BlueZ cache")
            print(f"      bluetoothctl> remove {MAC_ADDRESS}")
            print(f"      bluetoothctl> scan on")
            print(f"      bluetoothctl> (wait for device)")
            print(f"   Option 2: Stop HFP profile registration")
            print(f"      killall register_hfp_dbus.py")
            print(f"      sudo systemctl restart bluetooth")
            print(f"   Option 3: Connect to BLE explicitly")
            print(f"      bluetoothctl> menu scan")
            print(f"      bluetoothctl> transport le")
            print(f"      bluetoothctl> back")
            print(f"      bluetoothctl> scan on")

        return False


async def main():
    """Run all diagnostics."""
    print("\n" + "=" * 60)
    print("UV-50PRO BLE Connection Diagnostic")
    print("=" * 60)

    # 1. Check BLE advertisement
    ble_adv = await check_ble_advertisement()

    # 2. Check BlueZ state
    bluez_ok = check_bluez_device_state()

    # 3. Attempt connection
    if ble_adv:
        conn_ok = await attempt_ble_connection()
    else:
        conn_ok = False

    # Summary
    print("\n" + "=" * 60)
    print("DIAGNOSIS SUMMARY")
    print("=" * 60)
    print(f"  BLE Advertisement: {'✅' if ble_adv else '❌'}")
    print(f"  BlueZ State: {'✅' if bluez_ok else '❌'}")
    print(f"  BLE Connection: {'✅' if conn_ok else '❌'}")
    print("=" * 60)

    if not conn_ok:
        print("\n💡 RECOMMENDED ACTIONS:")
        print("   1. Stop any running HFP registration:")
        print("      pkill -f register_hfp")
        print("   2. Remove device from BlueZ:")
        print("      bluetoothctl> remove 38:D2:00:01:62:C2")
        print("   3. Restart Bluetooth:")
        print("      sudo systemctl restart bluetooth")
        print("   4. Re-scan and connect:")
        print("      bluetoothctl> scan on")
        print("      bluetoothctl> connect 38:D2:00:01:62:C2")
        print("   5. Start console normally")


if __name__ == "__main__":
    asyncio.run(main())
