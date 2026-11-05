"""BlueZ GATT peripheral over D-Bus (the real radio path on the appliance).

This is the only module that needs ``dbus-next`` and a running ``bluetoothd``;
it is imported by the service entry point on Linux and never by the logic tests.
It is **not exercised on macOS** - verify on the reference box with
``bluetoothctl`` and ``nRF Connect`` before trusting it.

BlueZ exposes a peripheral as a set of exported objects:

    /org/openpc/gatt/service0        org.bluez.GattService1
    /org/openpc/gatt/service0/chr0   org.bluez.GattCharacteristic1
    /org/openpc/advertisement0       org.bluez.LEAdvertisement1

Registered through ``org.bluez.GattManager1.RegisterApplication`` and
``org.bluez.LEAdvertisingManager1.RegisterAdvertisement`` on ``/org/bluez/hci0``.
"""

# NOTE: do NOT re-add "from __future__ import annotations" here.
# dbus-next 0.2.3 reads method annotations at class-creation time, and the
# future import turns them into source text: "-> None" arrives as the string
# "None" and parse_annotation raises
#   ValueError: service annotations must be a string constant (got None)
# which killed openpc-provision on every boot of the pilot image. The dbus
# type signatures below are already string constants ("s", "ay", "a{sv}"),
# which is exactly what dbus-next wants.

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .ble import (
    CHAR_DEVICE_INFO_UUID,
    CHAR_STATUS_UUID,
    CHAR_WIFI_UUID,
    SERVICE_UUID,
    DeviceInfoFn,
    ProvisioningRequest,
    WifiWriteFn,
    encode_status,
)

log = logging.getLogger("openpc.provisioning.bluez")

BLUEZ = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"
GATT_PATH = "/org/openpc/gatt/service0"
ADV_PATH = "/org/openpc/advertisement0"

#: Writes are marked ``encrypt-authenticated-write`` so BlueZ requires a bonded
#: (LE Secure Connections) link before it will hand over a passphrase.
FLAGS_READ = ["read"]
FLAGS_WRITE = ["write", "encrypt-authenticated-write"]
FLAGS_NOTIFY = ["read", "notify"]


def _classes() -> dict[str, Any]:
    """Build the D-Bus interface classes.

    Imported lazily so the rest of the package stays importable on a machine
    with no ``dbus-next``.
    """
    from dbus_next.constants import PropertyAccess
    from dbus_next.service import ServiceInterface, dbus_property, method

    class Characteristic(ServiceInterface):
        def __init__(self, uuid: str, flags: list[str], value: bytes = b"") -> None:
            super().__init__("org.bluez.GattCharacteristic1")
            self._uuid = uuid
            self._flags = flags
            self._value = list(value)
            self._notifying = False
            self.on_read: Callable[[], bytes] | None = None
            self.on_write: Callable[[bytes], None] | None = None

        # -- properties
        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":
            return self._uuid

        @dbus_property(access=PropertyAccess.READ)
        def Service(self) -> "o":
            return GATT_PATH

        @dbus_property(access=PropertyAccess.READ)
        def Flags(self) -> "as":
            return self._flags

        @dbus_property(access=PropertyAccess.READ)
        def Value(self) -> "ay":
            return self._value

        # -- methods
        @method()
        def ReadValue(self, options: "a{sv}") -> "ay":
            if self.on_read is not None:
                self._value = list(self.on_read())
            return self._value

        @method()
        def WriteValue(self, value: "ay", options: "a{sv}") -> None:
            if self.on_write is not None:
                self.on_write(bytes(value))

        @method()
        def StartNotify(self) -> None:
            self._notifying = True

        @method()
        def StopNotify(self) -> None:
            self._notifying = False

        # -- notify
        def notify(self, value: bytes) -> None:
            self._value = list(value)
            if self._notifying:
                self.emit_properties_changed({"Value": self._value})

    class GattService(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.GattService1")
            self._characteristics: list[Any] = []

        def add(self, characteristic: Any) -> None:
            self._characteristics.append(characteristic)

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":
            return SERVICE_UUID

        @dbus_property(access=PropertyAccess.READ)
        def Primary(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def Characteristics(self) -> "ao":
            return [f"{GATT_PATH}/chr{i}" for i in range(len(self._characteristics))]

    class Advertisement(ServiceInterface):
        def __init__(self, local_name: str) -> None:
            super().__init__("org.bluez.LEAdvertisement1")
            self._local_name = local_name

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> "s":
            return "peripheral"

        @dbus_property(access=PropertyAccess.READ)
        def ServiceUUIDs(self) -> "as":
            return [SERVICE_UUID]

        @dbus_property(access=PropertyAccess.READ)
        def LocalName(self) -> "s":
            return self._local_name

        @dbus_property(access=PropertyAccess.READ)
        def Includes(self) -> "as":
            return ["tx-power"]

        @method()
        def Release(self) -> None:
            log.info("advertisement released")

    return {
        "Characteristic": Characteristic,
        "GattService": GattService,
        "Advertisement": Advertisement,
    }


class DbusBleTransport:
    """BlueZ peripheral transport."""

    def __init__(self, local_name: str) -> None:
        self._local_name = local_name
        self._bus: Any = None
        self._device_info: DeviceInfoFn | None = None
        self._wifi_write: WifiWriteFn | None = None
        self._chars: list[Any] = []
        self._service: Any = None
        self._adv: Any = None
        self._write_task: asyncio.Task[None] | None = None

    @property
    def local_name(self) -> str:
        return self._local_name

    def set_handlers(self, device_info: DeviceInfoFn, wifi_write: WifiWriteFn) -> None:
        self._device_info = device_info
        self._wifi_write = wifi_write

    async def start(self) -> None:
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType

        classes = _classes()
        Characteristic = classes["Characteristic"]

        # BlueZ lives on the SYSTEM bus. MessageBus() defaults to the *session*
        # bus, which does not exist on an appliance with no desktop login - the
        # unit died with:
        #   InvalidAddressError: DBUS_SESSION_BUS_ADDRESS not set and could not
        #   get DISPLAY environment variable to get bus address
        # and then crash-looped. Be explicit.
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        bus = self._bus

        self._service = classes["GattService"]()
        info = Characteristic(CHAR_DEVICE_INFO_UUID, FLAGS_READ)
        wifi = Characteristic(CHAR_WIFI_UUID, FLAGS_WRITE)
        status = Characteristic(CHAR_STATUS_UUID, FLAGS_NOTIFY)

        info.on_read = lambda: encode_status(self._device_info() if self._device_info else {})
        wifi.on_write = self._on_write
        self._chars = [info, wifi, status]

        for index, characteristic in enumerate(self._chars):
            bus.export(f"{GATT_PATH}/chr{index}", characteristic)
            self._service.add(characteristic)
        bus.export(GATT_PATH, self._service)

        self._adv = classes["Advertisement"](self._local_name)
        bus.export(ADV_PATH, self._adv)

        adapter = await self._adapter_proxy(bus)
        props = adapter.get_interface("org.freedesktop.DBus.Properties")
        from dbus_next import Variant

        await props.call_set("org.bluez.Adapter1", "Alias", Variant("s", self._local_name))
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        await props.call_set("org.bluez.Adapter1", "Discoverable", Variant("b", True))

        gatt = adapter.get_interface("org.bluez.GattManager1")
        await gatt.call_register_application(GATT_PATH, {})
        advertising = adapter.get_interface("org.bluez.LEAdvertisingManager1")
        await advertising.call_register_advertisement(ADV_PATH, {})
        log.info("ble advertising", extra={"detail": self._local_name})

    async def _adapter_proxy(self, bus: Any) -> Any:
        introspection = await bus.introspect(BLUEZ, ADAPTER_PATH)
        return bus.get_proxy_object(BLUEZ, ADAPTER_PATH, introspection)

    def _on_write(self, value: bytes) -> None:
        # BlueZ calls this from the D-Bus message loop. Provisioning is async
        # (NetworkManager), so hand off to the main loop immediately.
        self._write_task = asyncio.get_event_loop().create_task(self._handle_write(value))

    async def _handle_write(self, value: bytes) -> None:
        import json

        try:
            payload = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("rejected malformed provisioning payload")
            await self.notify_status({"state": "failed", "message": "malformed payload"})
            return
        try:
            request = ProvisioningRequest.from_payload(payload)
        except Exception as exc:
            # CredentialsError text is safe: it never echoes the passphrase.
            log.warning("rejected provisioning payload", extra={"detail": str(exc)})
            await self.notify_status({"state": "failed", "message": str(exc)})
            return
        if self._wifi_write is not None:
            await self._wifi_write(request)

    async def notify_status(self, status: dict[str, Any]) -> None:
        if self._chars:
            # chr2 is the status characteristic (see start()).
            self._chars[2].notify(encode_status(status))

    async def stop(self) -> None:
        if self._bus is None:
            return
        try:
            adapter = await self._adapter_proxy(self._bus)
            advertising = adapter.get_interface("org.bluez.LEAdvertisingManager1")
            await advertising.call_unregister_advertisement(ADV_PATH)
            gatt = adapter.get_interface("org.bluez.GattManager1")
            await gatt.call_unregister_application(GATT_PATH)
        except Exception:
            log.exception("failed to unregister BLE objects")
        finally:
            for index in range(len(self._chars)):
                try:
                    self._bus.unexport(f"{GATT_PATH}/chr{index}")
                except Exception:
                    pass
            for path in (GATT_PATH, ADV_PATH):
                try:
                    self._bus.unexport(path)
                except Exception:
                    pass
            self._bus.disconnect()
            self._bus = None
        log.info("ble advertising stopped")
