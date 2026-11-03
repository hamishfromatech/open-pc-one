"""The provisioning service: boot with no keyboard, end up on WiFi.

Sequence on a cold boot with no network:

1. ``NO_NETWORK``    - no default route. Ethernet is tried first and wins
                       silently if a cable is plugged in.
2. ``ADVERTISING``   - the radio advertises ``OpenPC-XXXX`` with the three GATT
                       characteristics. The LED is solid amber.
3. ``RECEIVING...``  - the phone writes SSID + passphrase *and its own public
                       key*, which registers it as a trusted client.
4. ``CONNECTING``    - NetworkManager joins; the passphrase never touches argv.
5. ``CONNECTED``     - advertising stops, ``_openpc._tcp`` is published, the
                       engine is reachable and the phone can open WSS :8095.

A failed attempt returns to advertising so a mistyped passphrase is retryable
without a factory reset - there is no keyboard on the box.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .ble import BleTransport, ProvisioningRequest
from .mdns import MdnsPublisher
from .state import CredentialsError, ProvisioningState, ProvisioningStateMachine, WifiCredentials
from .wifi import JoinResult

log = logging.getLogger("openpc.provisioning")

PROTOCOL_VERSION = 1


@dataclass(slots=True)
class ApplianceIdentity:
    appliance_id: str
    local_name: str
    firmware: str = "0.1.0"
    model: str = "Open-PC One"
    cert_fingerprint: str = ""

    def device_info(self) -> dict[str, Any]:
        """Content of the device-info characteristic.

        ``certFingerprint`` is the whole point of this characteristic: the phone
        pins it, so a later hostile-LAN impersonation of ``openpc.local`` fails
        TLS validation.
        """
        return {
            "applianceId": self.appliance_id,
            "name": self.local_name,
            "model": self.model,
            "firmware": self.firmware,
            "protocol": PROTOCOL_VERSION,
            "certFingerprint": self.cert_fingerprint,
        }


StateCallback = Callable[[ProvisioningState, str], None]


@dataclass(slots=True)
class ProvisioningService:
    identity: ApplianceIdentity
    machine: ProvisioningStateMachine
    wifi: Any  # WifiBackend; typed loosely to keep the protocol import-free
    transport: BleTransport
    registry: Any | None = None  # DeviceRegistry-ish: .add(device_id, name, key, kind)
    mdns: MdnsPublisher | None = None
    on_state: StateCallback | None = None
    last_error: str | None = field(default=None)
    last_ip: str | None = field(default=None)

    def __post_init__(self) -> None:
        set_handlers = getattr(self.transport, "set_handlers", None)
        if set_handlers is not None:
            set_handlers(self.device_info, self.handle_wifi)

    # -- GATT surfaces -----------------------------------------------------

    def device_info(self) -> dict[str, Any]:
        return self.identity.device_info()

    async def handle_wifi(self, request: ProvisioningRequest) -> bool:
        """Handle one credential write. Returns True once the box is online."""
        self.machine.credentials_received(request.device_id or "unknown-phone")
        self._publish_state()

        try:
            credentials = WifiCredentials.create(request.ssid, request.password, hidden=request.hidden)
        except CredentialsError as exc:
            self.last_error = str(exc)
            self._fail(str(exc), retryable=True)
            return False

        # Register the phone before joining WiFi. If the join succeeds but the
        # key was lost, the phone would have credentials it cannot use.
        if self.registry is not None and request.device_public_key and request.device_id:
            try:
                self.registry.add(
                    request.device_id,
                    request.device_name or "Phone",
                    request.device_public_key,
                    kind=request.device_kind,
                )
                log.info("device registered over BLE", extra={"client_id": request.device_id})
            except Exception as exc:
                self.last_error = f"could not register device: {exc}"
                self._fail(self.last_error, retryable=True)
                return False

        self.machine.connecting(credentials.ssid)
        self._publish_state()

        result: JoinResult = await self.wifi.join(credentials)
        if not result.ok:
            self.last_error = result.error or "could not join network"
            log.warning("wifi join failed", extra={"detail": self.last_error})
            self._fail(self.last_error, retryable=True)
            return False

        self.last_error = None
        self.last_ip = result.ip
        self.machine.connected(result.ip)
        await self._stop_advertising()
        if self.mdns is not None:
            await self.mdns.publish(self.identity.local_name, self.identity.appliance_id)
        self._publish_state()
        log.info("provisioned", extra={"detail": f"ip={result.ip}"})
        return True

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> bool:
        """One provisioning pass.

        Returns True when the box is online (already, or after provisioning).
        """
        if await self.wifi.is_online():
            self.machine.network_present()
            if self.mdns is not None:
                await self.mdns.publish(self.identity.local_name, self.identity.appliance_id)
            self._publish_state()
            log.info("network already present; skipping provisioning")
            return True

        self.machine.begin_advertising()
        await self.transport.start()
        self._publish_state()
        log.info("advertising", extra={"detail": self.transport.local_name})
        return False

    async def _stop_advertising(self) -> None:
        try:
            await self.transport.stop()
        except Exception:  # pragma: no cover - radio teardown is best-effort
            log.exception("failed to stop advertising")

    def _fail(self, message: str, *, retryable: bool) -> None:
        self.machine.failed(message)
        if retryable:
            # Stay discoverable so the phone can retry immediately.
            self.machine.begin_advertising()
        self._publish_state()

    def _publish_state(self) -> None:
        if self.on_state is not None:
            self.on_state(self.machine.state, self.machine.message)

    def status_payload(self) -> dict[str, Any]:
        return {
            "state": self.machine.state.value,
            "message": self.machine.message,
            "lastError": self.last_error,
            "ip": self.last_ip,
            "advertising": self.machine.state
            in (ProvisioningState.ADVERTISING, ProvisioningState.RECEIVING_CREDENTIALS),
            "applianceId": self.identity.appliance_id,
        }


def wire_simulated(identity: ApplianceIdentity, service: ProvisioningService) -> None:
    """Connect a SimulatedBleTransport to a service (test/CLI convenience)."""
    transport = service.transport
    if hasattr(transport, "set_handlers"):
        transport.set_handlers(service.device_info, service.handle_wifi)


__all__ = ["ApplianceIdentity", "ProvisioningService", "PROTOCOL_VERSION", "StateCallback"]
