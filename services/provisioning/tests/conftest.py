"""Shared fixtures. No Bluetooth stack and no NetworkManager required."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# Provisioning optionally uses the engine's registry/cert helpers.
sys.path.insert(0, str(ROOT.parent / "engine" / "src"))

from openpc_provisioning.ble import SimulatedBleTransport  # noqa: E402
from openpc_provisioning.service import ApplianceIdentity, ProvisioningService  # noqa: E402
from openpc_provisioning.state import ProvisioningStateMachine  # noqa: E402


class FakeWifi:
    """Deterministic stand-in for NetworkManager."""

    def __init__(self, online: bool = False, join_ok: bool = True, ip: str = "10.42.0.42") -> None:
        self.online = online
        self.join_ok = join_ok
        self.ip = ip
        self.joined: list[tuple[str, str]] = []
        self.error = "Error: 802-11-wireless-security.psk: invalid"

    async def is_online(self) -> bool:
        return self.online

    async def current_ip(self) -> str | None:
        return self.ip if self.online else None

    async def join(self, credentials):
        self.joined.append((credentials.ssid, credentials.password))
        if not self.join_ok:
            from openpc_provisioning.wifi import JoinResult

            return JoinResult(ok=False, error=self.error)
        self.online = True
        from openpc_provisioning.wifi import JoinResult

        return JoinResult(ok=True, ip=self.ip)


class FakeMdns:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, instance: str, appliance_id: str, engine_port: int | None = None) -> bool:
        self.published.append((instance, appliance_id))
        return True


@pytest.fixture
def identity() -> ApplianceIdentity:
    return ApplianceIdentity(
        appliance_id="a1b2c3d4e5f6a7b8",
        local_name="OpenPC-1234",
        cert_fingerprint="ab" * 32,
    )


@pytest.fixture
def transport(identity) -> SimulatedBleTransport:
    return SimulatedBleTransport(identity.local_name)


@pytest.fixture
def states() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def machine(states) -> ProvisioningStateMachine:
    def record(state, message):
        states.append((state.value, message))

    return ProvisioningStateMachine(on_change=record)


@pytest.fixture
def wifi() -> FakeWifi:
    return FakeWifi()


@pytest.fixture
def service(identity, machine, wifi, transport) -> ProvisioningService:
    return ProvisioningService(
        identity=identity,
        machine=machine,
        wifi=wifi,
        transport=transport,
        registry=None,
        mdns=FakeMdns(),
    )
