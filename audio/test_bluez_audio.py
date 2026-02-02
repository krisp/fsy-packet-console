#!/usr/bin/env python3
"""
Proof-of-Concept: BlueZ D-Bus Classic Bluetooth Audio Support

This module demonstrates how to use BlueZ's D-Bus API to:
1. Discover Classic Bluetooth devices
2. Pair and trust a device
3. Connect to audio profiles (A2DP for music, HSP/HFP for voice)
4. Monitor connection status

Requirements:
    - bluez (system Bluetooth stack)
    - pulseaudio-module-bluetooth OR pipewire
    - python3-dbus (apt) OR pydbus (pip)

Usage:
    # Test discovery
    python test_bluez_audio.py discover

    # Connect to UV-50PRO audio (replace MAC)
    python test_bluez_audio.py connect AA:BB:CC:DD:EE:FF

    # Disconnect audio
    python test_bluez_audio.py disconnect AA:BB:CC:DD:EE:FF

    # Check status
    python test_bluez_audio.py status AA:BB:CC:DD:EE:FF
"""

import sys
import time
from typing import Optional, Dict, Any

# Try pydbus first (cleaner API), fall back to dbus-python
try:
    from pydbus import SystemBus
    USING_PYDBUS = True
    print("[INFO] Using pydbus library")
except ImportError:
    try:
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
        USING_PYDBUS = False
        print("[INFO] Using dbus-python library")
    except ImportError:
        print("[ERROR] Neither pydbus nor dbus-python available")
        print("Install: sudo apt install python3-dbus python3-gi")
        print("   OR:   pip install pydbus")
        sys.exit(1)


class BlueZAudioManager:
    """Manager for Classic Bluetooth audio via BlueZ D-Bus API."""

    # D-Bus service and interface names
    BLUEZ_SERVICE = "org.bluez"
    ADAPTER_INTERFACE = "org.bluez.Adapter1"
    DEVICE_INTERFACE = "org.bluez.Device1"
    MEDIA_INTERFACE = "org.bluez.Media1"

    def __init__(self, adapter_name: str = "hci0"):
        """Initialize BlueZ audio manager.

        Args:
            adapter_name: Bluetooth adapter name (default: hci0)
        """
        self.adapter_name = adapter_name
        self.adapter_path = f"/org/bluez/{adapter_name}"

        if USING_PYDBUS:
            self.bus = SystemBus()
        else:
            DBusGMainLoop(set_as_default=True)
            self.bus = dbus.SystemBus()

    def _get_adapter(self):
        """Get Bluetooth adapter object."""
        if USING_PYDBUS:
            return self.bus.get(self.BLUEZ_SERVICE, self.adapter_path)
        else:
            return self.bus.get_object(self.BLUEZ_SERVICE, self.adapter_path)

    def _get_device(self, mac_address: str):
        """Get device object by MAC address.

        Args:
            mac_address: Device MAC address (e.g., 'AA:BB:CC:DD:EE:FF')

        Returns:
            Device D-Bus object or None if not found
        """
        # Convert MAC to D-Bus path format
        device_path = f"{self.adapter_path}/dev_{mac_address.replace(':', '_')}"

        try:
            if USING_PYDBUS:
                return self.bus.get(self.BLUEZ_SERVICE, device_path)
            else:
                return self.bus.get_object(self.BLUEZ_SERVICE, device_path)
        except Exception as e:
            print(f"[ERROR] Device not found: {e}")
            return None

    def _get_device_property(self, device, prop_name: str) -> Any:
        """Get device property value."""
        if USING_PYDBUS:
            return getattr(device, prop_name)
        else:
            props_iface = dbus.Interface(device, "org.freedesktop.DBus.Properties")
            return props_iface.Get(self.DEVICE_INTERFACE, prop_name)

    def _set_device_property(self, device, prop_name: str, value: Any):
        """Set device property value."""
        if USING_PYDBUS:
            setattr(device, prop_name, value)
        else:
            props_iface = dbus.Interface(device, "org.freedesktop.DBus.Properties")
            props_iface.Set(self.DEVICE_INTERFACE, prop_name, value)

    def discover_devices(self, timeout: int = 10) -> list:
        """Discover nearby Bluetooth devices.

        Args:
            timeout: Discovery timeout in seconds

        Returns:
            List of discovered devices with info
        """
        print(f"[INFO] Scanning for Bluetooth devices ({timeout}s)...")

        adapter = self._get_adapter()

        try:
            # Start discovery
            if USING_PYDBUS:
                adapter.StartDiscovery()
            else:
                adapter_iface = dbus.Interface(adapter, self.ADAPTER_INTERFACE)
                adapter_iface.StartDiscovery()

            # Wait for discovery
            time.sleep(timeout)

            # Stop discovery
            if USING_PYDBUS:
                adapter.StopDiscovery()
            else:
                adapter_iface.StopDiscovery()

            # Get managed objects to list devices
            if USING_PYDBUS:
                obj_manager = self.bus.get(self.BLUEZ_SERVICE, "/")
                managed_objects = obj_manager.GetManagedObjects()
            else:
                obj_manager = dbus.Interface(
                    self.bus.get_object(self.BLUEZ_SERVICE, "/"),
                    "org.freedesktop.DBus.ObjectManager"
                )
                managed_objects = obj_manager.GetManagedObjects()

            devices = []
            for path, interfaces in managed_objects.items():
                if self.DEVICE_INTERFACE in interfaces:
                    props = interfaces[self.DEVICE_INTERFACE]
                    devices.append({
                        'path': path,
                        'address': props.get('Address', 'Unknown'),
                        'name': props.get('Name', props.get('Alias', 'Unknown')),
                        'paired': props.get('Paired', False),
                        'connected': props.get('Connected', False),
                        'trusted': props.get('Trusted', False),
                    })

            return devices

        except Exception as e:
            print(f"[ERROR] Discovery failed: {e}")
            return []

    def pair_device(self, mac_address: str) -> bool:
        """Pair with a Bluetooth device.

        Args:
            mac_address: Device MAC address

        Returns:
            True if paired successfully
        """
        device = self._get_device(mac_address)
        if not device:
            return False

        try:
            paired = self._get_device_property(device, "Paired")
            if paired:
                print(f"[INFO] Device {mac_address} already paired")
                return True

            print(f"[INFO] Pairing with {mac_address}...")
            if USING_PYDBUS:
                device.Pair()
            else:
                device_iface = dbus.Interface(device, self.DEVICE_INTERFACE)
                device_iface.Pair()

            print(f"[INFO] Successfully paired with {mac_address}")

            # Auto-trust the device
            self._set_device_property(device, "Trusted", True)
            print(f"[INFO] Marked {mac_address} as trusted")

            return True

        except Exception as e:
            print(f"[ERROR] Pairing failed: {e}")
            return False

    def connect_audio(self, mac_address: str) -> bool:
        """Connect to device audio profiles.

        Args:
            mac_address: Device MAC address

        Returns:
            True if connected successfully
        """
        device = self._get_device(mac_address)
        if not device:
            return False

        try:
            # Check if already connected
            connected = self._get_device_property(device, "Connected")
            if connected:
                print(f"[INFO] Device {mac_address} already connected")
                return True

            # Ensure paired first
            paired = self._get_device_property(device, "Paired")
            if not paired:
                print("[INFO] Device not paired, pairing first...")
                if not self.pair_device(mac_address):
                    return False

            print(f"[INFO] Connecting to {mac_address}...")
            if USING_PYDBUS:
                device.Connect()
            else:
                device_iface = dbus.Interface(device, self.DEVICE_INTERFACE)
                device_iface.Connect()

            # Wait a bit for connection to establish
            time.sleep(2)

            connected = self._get_device_property(device, "Connected")
            if connected:
                print(f"[SUCCESS] Connected to {mac_address}")
                self.print_device_status(mac_address)
                return True
            else:
                print(f"[ERROR] Connection failed")
                return False

        except Exception as e:
            print(f"[ERROR] Connection failed: {e}")
            return False

    def disconnect_audio(self, mac_address: str) -> bool:
        """Disconnect from device audio.

        Args:
            mac_address: Device MAC address

        Returns:
            True if disconnected successfully
        """
        device = self._get_device(mac_address)
        if not device:
            return False

        try:
            print(f"[INFO] Disconnecting from {mac_address}...")
            if USING_PYDBUS:
                device.Disconnect()
            else:
                device_iface = dbus.Interface(device, self.DEVICE_INTERFACE)
                device_iface.Disconnect()

            print(f"[SUCCESS] Disconnected from {mac_address}")
            return True

        except Exception as e:
            print(f"[ERROR] Disconnect failed: {e}")
            return False

    def print_device_status(self, mac_address: str):
        """Print detailed device status.

        Args:
            mac_address: Device MAC address
        """
        device = self._get_device(mac_address)
        if not device:
            return

        try:
            name = self._get_device_property(device, "Name")
            alias = self._get_device_property(device, "Alias")
            paired = self._get_device_property(device, "Paired")
            trusted = self._get_device_property(device, "Trusted")
            connected = self._get_device_property(device, "Connected")

            # Try to get UUIDs (service profiles)
            try:
                uuids = self._get_device_property(device, "UUIDs")
            except:
                uuids = []

            print(f"\n{'='*60}")
            print(f"Device Status: {mac_address}")
            print(f"{'='*60}")
            print(f"  Name:      {name}")
            print(f"  Alias:     {alias}")
            print(f"  Paired:    {paired}")
            print(f"  Trusted:   {trusted}")
            print(f"  Connected: {connected}")

            if uuids:
                print(f"\n  Supported Profiles:")
                # Common Bluetooth profile UUIDs
                profile_names = {
                    "0000110b-0000-1000-8000-00805f9b34fb": "Audio Sink (A2DP)",
                    "0000110a-0000-1000-8000-00805f9b34fb": "Audio Source",
                    "0000111e-0000-1000-8000-00805f9b34fb": "Handsfree (HFP)",
                    "00001108-0000-1000-8000-00805f9b34fb": "Headset (HSP)",
                    "0000110c-0000-1000-8000-00805f9b34fb": "Audio/Video Remote Control",
                }
                for uuid in uuids:
                    profile = profile_names.get(uuid.lower(), uuid)
                    print(f"    - {profile}")

            print(f"{'='*60}\n")

        except Exception as e:
            print(f"[ERROR] Failed to get device status: {e}")


def main():
    """Main entry point for CLI usage."""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()
    manager = BlueZAudioManager()

    if command == "discover":
        print("[INFO] Discovering Bluetooth devices...")
        devices = manager.discover_devices(timeout=10)

        if devices:
            print(f"\nFound {len(devices)} device(s):")
            print(f"{'='*80}")
            for dev in devices:
                status = []
                if dev['paired']:
                    status.append("PAIRED")
                if dev['trusted']:
                    status.append("TRUSTED")
                if dev['connected']:
                    status.append("CONNECTED")
                status_str = ", ".join(status) if status else "NEW"
                print(f"{dev['address']:20} {dev['name']:30} [{status_str}]")
            print(f"{'='*80}")
        else:
            print("[INFO] No devices found")

    elif command == "connect":
        if len(sys.argv) < 3:
            print("Usage: test_bluez_audio.py connect <MAC_ADDRESS>")
            sys.exit(1)

        mac = sys.argv[2]
        manager.connect_audio(mac)

    elif command == "disconnect":
        if len(sys.argv) < 3:
            print("Usage: test_bluez_audio.py disconnect <MAC_ADDRESS>")
            sys.exit(1)

        mac = sys.argv[2]
        manager.disconnect_audio(mac)

    elif command == "status":
        if len(sys.argv) < 3:
            print("Usage: test_bluez_audio.py status <MAC_ADDRESS>")
            sys.exit(1)

        mac = sys.argv[2]
        manager.print_device_status(mac)

    elif command == "pair":
        if len(sys.argv) < 3:
            print("Usage: test_bluez_audio.py pair <MAC_ADDRESS>")
            sys.exit(1)

        mac = sys.argv[2]
        manager.pair_device(mac)

    else:
        print(f"[ERROR] Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
