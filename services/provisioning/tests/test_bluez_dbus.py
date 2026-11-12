"""The BlueZ D-Bus object model, without a radio or a bus.

The pilot image logged a SignatureBodyMismatchError for every
org.bluez.GattCharacteristic1 property because the ``ay`` (array of byte)
properties were stored as Python lists. dbus-next only serialises a *bytes*
object for ``ay``, so the Value property was dropped from the InterfacesAdded
signal. These tests pin the wire type; they need dbus-next but no Bluetooth
stack, no D-Bus daemon and no NetworkManager.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dbus_next")

from openpc_provisioning.bluez_dbus import _classes  # noqa: E402


def characteristic(value: bytes = b"\x01\x02"):
    return _classes()["Characteristic"]("2a00", ["read"], value)


def test_byte_array_property_holds_bytes():
    char = characteristic()
    assert isinstance(char._value, bytes)
    assert char.Value == b"\x01\x02"


def test_read_value_returns_bytes():
    char = characteristic()
    char.on_read = lambda: b"\x03\x04"
    # @dbus_property/@method wrap the function for dbus-next; __wrapped__ is the
    # implementation BlueZ actually calls.
    result = char.ReadValue.__wrapped__(char, {})
    assert result == b"\x03\x04"
    assert isinstance(char.Value, bytes)


def test_notify_keeps_bytes():
    char = characteristic()
    char.notify(b"\x05")
    assert isinstance(char.Value, bytes)
    assert char.Value == b"\x05"
