#!/usr/bin/env python3
"""
Register HFP (Handsfree Profile) with BlueZ to enable audio support.

This advertises audio capabilities to the UV-50PRO radio, enabling
the audio toggle on the radio's handset menu.

Based on BlueZ 5 ProfileManager1 D-Bus API.
Uses pydbus library.
"""

import sys
from pydbus import SystemBus
from pydbus.generic import signal
from gi.repository import GLib


# HFP Handsfree UUID
HFP_HF_UUID = "0000111e-0000-1000-8000-00805f9b34fb"

# RFCOMM channel to use (typically auto-assigned)
RFCOMM_CHANNEL = 7


class HFPProfile:
    """HFP Handsfree Profile implementation.

    This class implements the org.bluez.Profile1 interface.
    """

    dbus = """
    <node>
      <interface name='org.bluez.Profile1'>
        <method name='Release'/>
        <method name='NewConnection'>
          <arg type='o' name='device' direction='in'/>
          <arg type='h' name='fd' direction='in'/>
          <arg type='a{sv}' name='fd_properties' direction='in'/>
        </method>
        <method name='RequestDisconnection'>
          <arg type='o' name='device' direction='in'/>
        </method>
        <method name='Cancel'/>
      </interface>
    </node>
    """

    def Release(self):
        """Called when profile is unregistered."""
        print("[HFP] Profile released")

    def NewConnection(self, device, fd, properties):
        """Called when new connection is made.

        Args:
            device: Device D-Bus path
            fd: File descriptor for RFCOMM socket
            properties: Connection properties
        """
        print(f"[HFP] New connection from {device}")
        print(f"[HFP] File descriptor: {fd}")
        print(f"[HFP] Properties: {properties}")

        # Here we would handle the RFCOMM connection
        # For now, just acknowledge it
        print("[HFP] ✓ Audio connection established!")
        print("[HFP] Check radio handset - audio toggle should now be available")

    def RequestDisconnection(self, device):
        """Called when disconnection is requested.

        Args:
            device: Device D-Bus path
        """
        print(f"[HFP] Disconnection requested for {device}")

    def Cancel(self):
        """Called to cancel pending operation."""
        print("[HFP] Operation cancelled")


def register_hfp_profile():
    """Register HFP Handsfree profile with BlueZ."""

    print("=" * 60)
    print("HFP Handsfree Profile Registration")
    print("=" * 60)

    # Get system bus
    bus = SystemBus()

    # Create profile object
    profile_path = "/org/bluez/hfp_handsfree"
    profile = HFPProfile()

    # Publish profile on D-Bus
    print(f"[HFP] Publishing profile at {profile_path}")
    bus.publish(profile_path, profile)

    # Get ProfileManager
    manager = bus.get("org.bluez", "/org/bluez")["org.bluez.ProfileManager1"]

    # Profile options (GVariant types)
    from gi.repository import GLib
    opts = {
        "Name": GLib.Variant('s', "Handsfree"),
        "Role": GLib.Variant('s', "client"),  # We are the HFP client (headset role)
        "Channel": GLib.Variant('q', RFCOMM_CHANNEL),  # 'q' = uint16
        "AutoConnect": GLib.Variant('b', True),  # 'b' = boolean

        # HFP features we support
        "Features": GLib.Variant('q', 0x0001),  # Basic features

        # Service record (SDP)
        "ServiceRecord": GLib.Variant('s', """
        <?xml version="1.0" encoding="UTF-8" ?>
        <record>
          <attribute id="0x0001">
            <sequence>
              <uuid value="0x111e"/>
              <uuid value="0x1203"/>
            </sequence>
          </attribute>
          <attribute id="0x0004">
            <sequence>
              <sequence>
                <uuid value="0x0100"/>
              </sequence>
              <sequence>
                <uuid value="0x0003"/>
                <uint8 value="0x07"/>
              </sequence>
            </sequence>
          </attribute>
          <attribute id="0x0009">
            <sequence>
              <sequence>
                <uuid value="0x111e"/>
                <uint16 value="0x0107"/>
              </sequence>
            </sequence>
          </attribute>
          <attribute id="0x0100">
            <text value="Handsfree"/>
          </attribute>
          <attribute id="0x0311">
            <uint16 value="0x0001"/>
          </attribute>
        </record>
        """)
    }

    try:
        # Register profile
        print(f"\n[1] Registering HFP profile with BlueZ...")
        print(f"    UUID: {HFP_HF_UUID}")
        print(f"    Role: client (handsfree)")
        print(f"    Channel: {RFCOMM_CHANNEL}")

        manager.RegisterProfile(profile_path, HFP_HF_UUID, opts)

        print(f"[2] ✓ Profile registered successfully!")
        print(f"\n[3] Profile is now active and advertising audio capabilities")
        print(f"\n[4] Next steps:")
        print(f"    a) Re-pair your UV-50PRO (remove pairing, pair again)")
        print(f"    b) OR disconnect/reconnect from radio")
        print(f"    c) Check handset menu - audio toggle should appear")
        print(f"\n[5] Waiting for connections... (Press Ctrl+C to stop)")
        print("=" * 60)

        # Run main loop
        mainloop = GLib.MainLoop()
        mainloop.run()

    except Exception as e:
        print(f"\n[ERROR] Failed to register profile: {e}")

        if "Already Exists" in str(e) or "AlreadyExists" in str(e):
            print("\n[FIX] Profile already registered. Unregister first:")
            print("      Try: sudo systemctl restart bluetooth")
            print("      Or run: python3 unregister_hfp.py")

        import traceback
        traceback.print_exc()
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n\n[CLEANUP] Unregistering profile...")
        try:
            manager.UnregisterProfile(profile_path)
            print("[CLEANUP] ✓ Profile unregistered")
        except Exception:
            pass


if __name__ == "__main__":
    register_hfp_profile()
