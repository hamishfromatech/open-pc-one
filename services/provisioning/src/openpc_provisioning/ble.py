"""BLE transport contract, GATT identifiers and payload parsing.

A phone talks to the appliance over three GATT characteristics. The transport is
an interface so the provisioning state machine, credential validation and device
registration can be tested on a laptop with no Bluetooth stack; the real BlueZ
D-Bus implementation lives in :mod:`openpc_provisioning.bluez_dbus`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .state import CredentialsError

log = logging.getLogger("openpc.provisioning.ble")

#: Custom 128-bit base. Every UUID below is stable for the product lifetime -
#: phones persist them, so changing one is a breaking protocol change.
SERVICE_UUID = "5f3a0001-6f70-6e63-0000-6f70656e7063"
CHAR_DEVICE_INFO_UUID = "5f3a0002-6f70-6e63-0000-6f70656e7063"
CHAR_WIFI_UUID = "5f3a0003-6f70-6e63-0000-6f70656e7063"
CHAR_STATUS_UUID = "5f3a0004-6f70-6e63-0000-6f70656e7063"

ADVERTISING_PREFIX = "OpenPC-"
PROTOCOL_VERSION = 1

_DEVICE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def advertising_name(serial_suffix: str) -> str:
    """``OpenPC-XXXX`` from the last four hex characters of the serial.

    The suffix is what a person compares against the sticker on the base of the
    unit, so it must be stable and unambiguous.
    """
    suffix = re.sub(r"[^0-9A-Fa-f]", "", serial_suffix).upper()[-4:]
    return f"{ADVERTISING_PREFIX}{suffix or '0000'}"


@dataclass(slots=True)
class ProvisioningRequest:
    """One BLE write: WiFi credentials plus the phone's device identity.

    Registering the phone's public key in the same session that hands over the
    passphrase means there is no second, weaker pairing step: possession of the
    BLE link at first boot *is* the trust anchor.
    """

    ssid: str
    password: str = field(repr=False)
    hidden: bool = False
    device_id: str = ""
    device_name: str = ""
    device_public_key: str = ""
    device_kind: str = "mobile"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProvisioningRequest:
        if not isinstance(payload, dict):
            raise CredentialsError("payload must be an object")
        ssid = payload.get("ssid")
        password = payload.get("password", "")
        if not isinstance(ssid, str) or not isinstance(password, str):
            raise CredentialsError("ssid and password must be strings")
        device_id = str(payload.get("deviceId") or "").strip()
        if device_id and not _DEVICE_ID.match(device_id):
            raise CredentialsError("deviceId is not a valid identifier")
        public_key = str(payload.get("publicKey") or "").strip()
        if public_key:
            try:
                raw = base64.b64decode(public_key, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise CredentialsError("publicKey must be base64") from exc
            if len(raw) != 32:
                raise CredentialsError("publicKey must be a raw 32-byte Ed25519 key")
        kind = str(payload.get("kind") or "mobile")
        if kind not in ("mobile", "desktop"):
            raise CredentialsError("kind must be 'mobile' or 'desktop'")
        return cls(
            ssid=ssid,
            password=password,
            hidden=bool(payload.get("hidden", False)),
            device_id=device_id,
            device_name=str(payload.get("name") or "")[:64],
            device_public_key=public_key,
            device_kind=kind,
        )

    def __repr__(self) -> str:
        return (
            f"ProvisioningRequest(ssid={self.ssid!r}, device_id={self.device_id!r}, "
            f"kind={self.device_kind}, key={'yes' if self.device_public_key else 'no'})"
        )


DeviceInfoFn = Callable[[], dict[str, Any]]
WifiWriteFn = Callable[[ProvisioningRequest], Awaitable[bool]]


class BleTransport(Protocol):
    """A peripheral that exposes the three characteristics above."""

    @property
    def local_name(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def notify_status(self, status: dict[str, Any]) -> None: ...


class SimulatedBleTransport:
    """In-process transport used by tests and the ``--simulate`` CLI mode.

    It records what the service asked the radio to do, and lets a test drive the
    same callbacks a real GATT write would trigger.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self.started = False
        self.notifications: list[dict[str, Any]] = []
        self._device_info: DeviceInfoFn | None = None
        self._wifi_write: WifiWriteFn | None = None

    @property
    def local_name(self) -> str:
        return self._name

    def set_handlers(self, device_info: DeviceInfoFn, wifi_write: WifiWriteFn) -> None:
        self._device_info = device_info
        self._wifi_write = wifi_write

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def notify_status(self, status: dict[str, Any]) -> None:
        self.notifications.append(status)

    # -- test/GUI-driven inputs -------------------------------------------

    def read_device_info(self) -> dict[str, Any]:
        assert self._device_info is not None, "transport not wired"
        return self._device_info()

    async def write_wifi(self, payload: dict[str, Any]) -> bool:
        assert self._wifi_write is not None, "transport not wired"
        request = ProvisioningRequest.from_payload(payload)
        return await self._wifi_write(request)


def encode_status(status: dict[str, Any]) -> bytes:
    """Status characteristic payload: compact JSON, ASCII-only."""
    return json.dumps(status, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
