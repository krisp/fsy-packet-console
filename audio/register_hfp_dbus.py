#!/usr/bin/env python3
"""
Register HFP (Handsfree Profile) with BlueZ to enable audio support.

This advertises audio capabilities to the UV-50PRO radio, enabling
the audio toggle on the radio's handset menu.

Uses system python3-dbus (not pydbus).
"""

import sys
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib


# HFP Handsfree UUID
HFP_HF_UUID = "0000111e-0000-1000-8000-00805f9b34fb"

# RFCOMM channel to use
RFCOMM_CHANNEL = 7


class HFPProfile(dbus.service.Object):
    """HFP Handsfree Profile implementation."""

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="", out_signature="")
    def Release(self):
        """Called when profile is unregistered."""
        print("[HFP] Profile released")

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, properties):
        """Called when new connection is made."""
        print(f"[HFP] ✓ New audio connection from {device}")
        print(f"[HFP] File descriptor: {fd}")
        print(f"[HFP] Properties: {properties}")
        print("[HFP] ")
        print("[HFP] 🎧 Check radio handset - audio toggle should now be available!")

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        """Called when disconnection is requested."""
        print(f"[HFP] Disconnection requested for {device}")


def register_hfp_profile():
    """Register HFP Handsfree profile with BlueZ."""

    print("=" * 60)
    print("HFP Handsfree Profile Registration")
    print("=" * 60)

    # Setup D-Bus main loop
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    # Get system bus
    bus = dbus.SystemBus()

    # Create profile object
    profile_path = "/org/bluez/hfp_handsfree"
    profile = HFPProfile(bus, profile_path)
    print(f"[HFP] Profile object created at {profile_path}")

    # Get ProfileManager
    manager = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"),
        "org.bluez.ProfileManager1"
    )

    # Profile options
    opts = {
        "Name": "Handsfree",
        "Role": "client",
        "Channel": dbus.UInt16(RFCOMM_CHANNEL),
        "AutoConnect": dbus.Boolean(True),
    }

    try:
        print(f"\n[1] Registering HFP profile with BlueZ...")
        print(f"    UUID: {HFP_HF_UUID}")
        print(f"    Role: client (handsfree)")
        print(f"    Channel: {RFCOMM_CHANNEL}")

        manager.RegisterProfile(profile_path, HFP_HF_UUID, opts)

        print(f"[2] ✓ Profile registered successfully!")
        print(f"\n[3] Profile is advertising audio capabilities")
        print(f"\n[4] Next steps:")
        print(f"    a) Disconnect/reconnect UV-50PRO:")
        print(f"       bluetoothctl> disconnect 38:D2:00:01:62:C2")
        print(f"       bluetoothctl> connect 38:D2:00:01:62:C2")
        print(f"    b) Check radio handset menu for audio toggle")
        print(f"\n[5] Waiting for audio connections... (Press Ctrl+C to stop)")
        print("=" * 60)

        # Run main loop
        mainloop = GLib.MainLoop()
        mainloop.run()

    except dbus.exceptions.DBusException as e:
        print(f"\n[ERROR] Failed to register profile: {e}")

        if "Already Exists" in str(e):
            print("\n[FIX] Profile already registered.")
            print("      Run: sudo systemctl restart bluetooth")
            print("      Then try again")

        sys.exit(1)

    except KeyboardInterrupt:
        print("\n\n[CLEANUP] Unregistering profile...")
        try:
            manager.UnregisterProfile(profile_path)
            print("[CLEANUP] ✓ Profile unregistered")
        except Exception:
            pass


if __name__ == "__main__":
    try:
        import dbus
    except ImportError:
        print("[ERROR] dbus-python not available")
        print("[FIX] Install: sudo apt install python3-dbus")
        sys.exit(1)

    register_hfp_profile()
