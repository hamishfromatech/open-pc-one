"""State machine, credential validation and the NetworkManager keyfile."""

from __future__ import annotations

import pytest
from openpc_provisioning.ble import ProvisioningRequest, advertising_name
from openpc_provisioning.state import (
    CredentialsError,
    ProvisioningStateMachine,
    WifiCredentials,
)
from openpc_provisioning.state import (
    ProvisioningState as S,
)
from openpc_provisioning.wifi import NmcliBackend, render_keyfile

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_happy_path_no_network_to_connected(machine, states):
    machine.begin_advertising()
    machine.credentials_received("phone-1")
    machine.connecting("HomeNet")
    machine.connected("10.42.0.42")
    assert machine.state is S.CONNECTED
    assert [s for s, _ in states] == ["advertising", "receiving_credentials", "connecting", "connected"]


def test_illegal_transitions_are_refused(machine):
    with pytest.raises(ValueError):
        machine.transition(S.CONNECTED)  # cannot skip advertising
    machine.begin_advertising()
    with pytest.raises(ValueError):
        machine.transition(S.CONNECTING)  # must receive credentials first


def test_ethernet_skips_provisioning(machine, states):
    machine.network_present()
    assert machine.state is S.CONNECTED
    assert "network present" in states[-1][1]


def test_failure_is_retryable_without_a_factory_reset(machine):
    machine.begin_advertising()
    machine.credentials_received("phone-1")
    machine.failed("wrong password")
    assert machine.state is S.FAILED
    machine.begin_advertising()
    assert machine.state is S.ADVERTISING
    # The phone can try again immediately.
    machine.credentials_received("phone-1")
    assert machine.state is S.RECEIVING_CREDENTIALS


def test_connected_can_fall_back_to_no_network(machine):
    machine.network_present()
    machine.network_lost()
    assert machine.state is S.NO_NETWORK


def test_history_is_recorded_with_monotonic_clock(machine):
    machine.begin_advertising()
    machine.connected("10.0.0.2")
    assert len(machine.history) == 2
    assert machine.history[1][2].startswith("online")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_valid_credentials():
    creds = WifiCredentials.create("HomeNet", "correct horse battery")
    assert creds.ssid == "HomeNet"
    assert creds.is_open is False


def test_open_network_needs_no_passphrase():
    creds = WifiCredentials.create("CafeFreeWiFi", "")
    assert creds.is_open is True


@pytest.mark.parametrize(
    "ssid,password",
    [
        ("", "password123"),
        ("x" * 33, "password123"),  # 32 bytes is the 802.11 limit
        ("bad\nssid", "password123"),
        ("bad\x00ssid", "password123"),
        ("Home", "short"),  # WPA minimum
        ("Home", "p" * 64),  # WPA maximum is 63
        ("Home", "pass\x01word"),
    ],
)
def test_invalid_credentials_are_refused(ssid, password):
    with pytest.raises(CredentialsError):
        WifiCredentials.create(ssid, password)


def test_ssid_limit_is_bytes_not_characters():
    # 16 four-byte emoji = 64 bytes: too long even though it is 16 characters.
    with pytest.raises(CredentialsError):
        WifiCredentials.create("😀" * 16, "password123")
    # 8 emoji = 32 bytes: exactly at the limit and legal.
    assert WifiCredentials.create("😀" * 8, "password123").ssid


def test_repr_never_contains_the_passphrase():
    creds = WifiCredentials.create("HomeNet", "hunter2hunter2")
    assert "hunter2" not in repr(creds)
    assert "HomeNet" in repr(creds)


# ---------------------------------------------------------------------------
# Keyfile
# ---------------------------------------------------------------------------


def test_keyfile_contains_ssid_and_psk():
    xml = render_keyfile(WifiCredentials.create("HomeNet", "s3cret-pass"), interface="wlan0")
    assert "ssid=HomeNet" in xml
    assert "psk=s3cret-pass" in xml
    assert "key-mgmt=wpa-psk" in xml
    assert "interface-name=wlan0" in xml
    assert "hidden=false" in xml


def test_keyfile_hidden_flag_and_missing_security_for_open_network():
    xml = render_keyfile(WifiCredentials.create("Camp", "", hidden=True))
    assert "hidden=true" in xml
    assert "wifi-security" not in xml


def test_keyfile_uuid_is_stable_so_reprovisioning_replaces():
    a = render_keyfile(WifiCredentials.create("A", "password123"))
    b = render_keyfile(WifiCredentials.create("B", "password456"))
    uuid_a = [line for line in a.splitlines() if line.startswith("uuid=")][0]
    uuid_b = [line for line in b.splitlines() if line.startswith("uuid=")][0]
    assert uuid_a == uuid_b


def test_keyfile_escapes_backslashes():
    xml = render_keyfile(WifiCredentials.create("Back\\slash", "password123"))
    assert "ssid=Back\\\\slash" in xml


# ---------------------------------------------------------------------------
# nmcli backend
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, outputs: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.outputs = outputs or {}

    async def __call__(self, *argv: str) -> tuple[int, str]:
        self.calls.append(argv)
        for key, value in self.outputs.items():
            if argv[: len(key)] == key:
                return value
        if argv[:2] == ("ip", "-4") and argv[2] == "route":
            return 0, ("default via 10.42.0.1 dev wlan0\n" if self._online else "")
        return 0, ""

    _online = True


@pytest.fixture
def online_runner():
    runner = FakeRunner()
    runner._online = True
    return runner


async def test_is_online_reads_the_default_route(online_runner, tmp_path):
    backend = NmcliBackend(runner=online_runner, keyfile_dir=tmp_path)
    assert await backend.is_online() is True
    online_runner._online = False
    assert await backend.is_online() is False


async def test_join_success_reports_ip(tmp_path):
    runner = FakeRunner(
        {
            ("ip", "-4", "-o", "addr"): (0, "2: wlan0    inet 10.42.0.42/24 brd 10.42.0.255 scope global\n"),
        }
    )
    backend = NmcliBackend(runner=runner, keyfile_dir=tmp_path, timeout=2, poll_interval=0.01)
    result = await backend.join(WifiCredentials.create("HomeNet", "s3cret-pass"))
    assert result.ok is True
    assert result.ip == "10.42.0.42"
    assert ("nmcli", "connection", "reload") in runner.calls
    assert ("nmcli", "connection", "up", "openpc-wifi") in runner.calls


async def test_join_never_puts_the_passphrase_on_a_command_line(tmp_path):
    runner = FakeRunner()
    backend = NmcliBackend(runner=runner, keyfile_dir=tmp_path, timeout=2, poll_interval=0.01)
    secret = "s3cret-pass"
    await backend.join(WifiCredentials.create("HomeNet", secret))
    for argv in runner.calls:
        assert all(secret not in part for part in argv)
    # But the profile on disk does carry it, at mode 0600.
    keyfile = tmp_path / "openpc-wifi.nmconnection"
    assert secret in keyfile.read_text()
    import os
    import stat

    assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600


async def test_join_failure_surfaces_a_safe_reason(tmp_path):
    runner = FakeRunner({("nmcli", "connection", "up"): (4, "Error: 802-11-wireless-security.psk: invalid\n")})
    backend = NmcliBackend(runner=runner, keyfile_dir=tmp_path, timeout=1, poll_interval=0.01)
    result = await backend.join(WifiCredentials.create("HomeNet", "s3cret-pass"))
    assert result.ok is False
    assert "psk: invalid" in (result.error or "")
    assert "s3cret-pass" not in (result.error or "")


async def test_join_times_out_without_a_lease(tmp_path):
    runner = FakeRunner({("ip", "-4", "route"): (0, "")})
    backend = NmcliBackend(runner=runner, keyfile_dir=tmp_path, timeout=0.05, poll_interval=0.01)
    result = await backend.join(WifiCredentials.create("HomeNet", "s3cret-pass"))
    assert result.ok is False
    assert "timed out" in (result.error or "")


# ---------------------------------------------------------------------------
# BLE payloads
# ---------------------------------------------------------------------------


def test_advertising_name_uses_last_four_hex_digits():
    assert advertising_name("deadbeef") == "OpenPC-BEEF"
    assert advertising_name("serial-0a1f") == "OpenPC-0A1F"
    assert advertising_name("") == "OpenPC-0000"


def test_request_parsing_accepts_a_full_payload():
    import base64

    key = base64.b64encode(b"k" * 32).decode()
    request = ProvisioningRequest.from_payload(
        {"ssid": "HomeNet", "password": "password123", "deviceId": "phone-1", "publicKey": key}
    )
    assert request.ssid == "HomeNet"
    assert request.device_public_key == key


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"ssid": 5},
        {"ssid": "x", "password": "y", "deviceId": "bad id with spaces"},
        {"ssid": "x", "password": "y", "publicKey": "!!!"},
        {"ssid": "x", "password": "y", "publicKey": "AAAA"},
        {"ssid": "x", "password": "y", "kind": "tablet"},
    ],
)
def test_request_parsing_rejects_bad_payloads(payload):
    with pytest.raises(CredentialsError):
        ProvisioningRequest.from_payload(payload)


def test_request_repr_hides_the_passphrase():
    request = ProvisioningRequest.from_payload({"ssid": "Home", "password": "s3cret-pass"})
    assert "s3cret-pass" not in repr(request)
