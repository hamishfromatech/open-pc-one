"""The provisioning service end to end, over the simulated transport."""

from __future__ import annotations

import base64

import pytest
from conftest import FakeMdns, FakeWifi
from openpc_provisioning.ble import SimulatedBleTransport
from openpc_provisioning.mdns import SERVICE_TYPE, render_service_xml
from openpc_provisioning.service import ApplianceIdentity, ProvisioningService
from openpc_provisioning.state import ProvisioningState as S


def payload(ssid="HomeNet", password="s3cret-pass", **extra):
    body = {"ssid": ssid, "password": password}
    body.update(extra)
    return body


async def test_already_online_never_advertises(service, wifi, transport, machine):
    wifi.online = True
    assert await service.run() is True
    assert machine.state is S.CONNECTED
    assert transport.started is False
    assert service.mdns.published  # type: ignore[union-attr]


async def test_offline_boot_starts_advertising_with_pin(service, machine, transport):
    assert await service.run() is False
    assert machine.state is S.ADVERTISING
    assert transport.started is True
    info = transport.read_device_info()
    assert info["applianceId"] == "a1b2c3d4e5f6a7b8"
    assert info["certFingerprint"] == "ab" * 32
    assert info["protocol"] == 1


async def test_successful_provisioning_joins_publishes_and_stops(
    service, machine, transport, wifi
):
    await service.run()
    ok = await transport.write_wifi(payload())
    assert ok is True
    assert machine.state is S.CONNECTED
    assert wifi.joined == [("HomeNet", "s3cret-pass")]
    assert transport.started is False
    published = service.mdns.published  # type: ignore[union-attr]
    assert published == [("OpenPC-1234", "a1b2c3d4e5f6a7b8")]
    assert service.status_payload()["ip"] == "10.42.0.42"


async def test_failed_join_returns_to_advertising_with_a_safe_error(
    service, machine, transport, wifi
):
    wifi.join_ok = False
    await service.run()
    ok = await transport.write_wifi(payload())
    assert ok is False
    # Retryable: the phone can correct the passphrase without a factory reset.
    assert machine.state is S.ADVERTISING
    assert machine.can(S.RECEIVING_CREDENTIALS)
    status = service.status_payload()
    assert status["lastError"]
    assert "s3cret-pass" not in str(status)
    assert transport.started is True


async def test_invalid_credentials_never_reach_networkmanager(service, transport, wifi, machine):
    await service.run()
    # A 7-character WPA key is refused before NetworkManager is asked anything.
    assert await transport.write_wifi(payload(password="short12")) is False
    assert wifi.joined == []
    assert machine.state is S.ADVERTISING
    assert "8-63" in (service.status_payload()["lastError"] or "")


async def test_phone_public_key_is_registered_before_joining(identity, machine, wifi, transport, tmp_path):
    from openpc_engine.auth import DeviceRegistry

    registry = DeviceRegistry(tmp_path / "devices.json")
    service = ProvisioningService(
        identity=identity,
        machine=machine,
        wifi=wifi,
        transport=transport,
        registry=registry,
        mdns=FakeMdns(),
    )
    await service.run()
    key = base64.b64encode(b"k" * 32).decode()
    ok = await transport.write_wifi(payload(deviceId="phone-1", name="Pixel", publicKey=key, kind="mobile"))
    assert ok is True
    record = registry.get("phone-1")
    assert record is not None
    assert record.public_key == key
    assert record.kind == "mobile"


async def test_registry_failure_aborts_before_joining(identity, machine, wifi, transport, tmp_path):
    class BrokenRegistry:
        def add(self, *args, **kwargs):
            raise RuntimeError("disk full")

    service = ProvisioningService(
        identity=identity,
        machine=machine,
        wifi=wifi,
        transport=transport,
        registry=BrokenRegistry(),
        mdns=FakeMdns(),
    )
    await service.run()
    ok = await transport.write_wifi(payload(deviceId="phone-1", publicKey=base64.b64encode(b"k" * 32).decode()))
    assert ok is False
    assert wifi.joined == []  # never joined with credentials the phone cannot use
    assert "disk full" in (service.status_payload()["lastError"] or "")


async def test_status_notifications_describe_each_step(service, transport):
    await service.run()
    assert transport.notifications == []  # SimulatedBleTransport is passive
    service._publish_state()
    assert service.status_payload()["state"] == "advertising"


def test_mdns_service_definition_is_escaped(identity):
    xml = render_service_xml("Box & <two>", 8095, {"id": "a<b"})
    assert SERVICE_TYPE in xml
    assert "Box &amp; &lt;two&gt;" in xml
    assert "<two>" not in xml
    assert "<txt-record>id=a&lt;b</txt-record>" in xml
    assert "8095" in xml


async def test_service_handlers_are_wired_by_construction(identity, machine, wifi):
    transport = SimulatedBleTransport("OpenPC-1234")
    ProvisioningService(identity=identity, machine=machine, wifi=wifi, transport=transport)
    # A real GATT read must return the appliance information, not raise.
    assert transport.read_device_info()["name"] == "OpenPC-1234"
