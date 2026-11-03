"""Identity, pairing and challenge/response."""

from __future__ import annotations

import base64
import os
import stat
import time

import pytest
from openpc_engine.auth import (
    AuthManager,
    DeviceRegistry,
    PairingCodeStore,
    RegistryError,
    certificate_fingerprint,
    ensure_tls_identity,
    format_fingerprint,
    new_device_keypair,
    sign_with,
)


def test_registry_persists_and_is_root_only(tmp_path):
    path = tmp_path / "devices.json"
    registry = DeviceRegistry(path)
    key, public_b64 = new_device_keypair()
    registry.add("phone-1", "Pixel", public_b64, kind="mobile")

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    reloaded = DeviceRegistry(path)
    record = reloaded.get("phone-1")
    assert record is not None
    assert record.public_key == public_b64
    assert record.name == "Pixel"

    assert reloaded.remove("phone-1") is True
    assert reloaded.remove("phone-1") is False
    assert len(DeviceRegistry(path)) == 0


def test_registry_rejects_non_ed25519_keys(tmp_path):
    registry = DeviceRegistry(tmp_path / "devices.json")
    with pytest.raises(RegistryError):
        registry.add("d", "n", base64.b64encode(b"short").decode())
    with pytest.raises(RegistryError):
        registry.add("d", "n", "not-base64!!")


def test_corrupt_registry_is_reported_not_ignored(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text("{not json")
    with pytest.raises(RegistryError):
        DeviceRegistry(path)


def test_pairing_code_is_single_use_and_expires(tmp_path):
    store = PairingCodeStore(ttl=60, path=tmp_path / "pairing.json")
    code, _expires = store.mint()
    assert len(code) == 8
    # No ambiguous glyphs, so a screen-read code cannot be mistyped.
    assert not set(code) & set("O0I1L")
    assert store.redeem(code) is True
    assert store.redeem(code) is False
    assert store.redeem("WRONGCODE") is False

    short = PairingCodeStore(ttl=0.01, path=tmp_path / "pairing.json")
    expired, _ = short.mint()
    time.sleep(0.03)
    assert short.redeem(expired) is False


def test_pairing_code_survives_process_boundary_and_is_deleted_on_redeem(tmp_path):
    path = tmp_path / "pairing.json"
    minted, _ = PairingCodeStore(ttl=60, path=path).mint()
    # A separate instance stands in for the running server reading the file.
    server_side = PairingCodeStore(ttl=60, path=path)
    assert server_side.redeem(minted) is True
    assert minted not in path.read_text()


def test_pairing_uses_constant_time_case_insensitive_match(tmp_path):
    store = PairingCodeStore(ttl=60, path=tmp_path / "p.json")
    code, _ = store.mint()
    assert store.redeem(code.lower()) is True


def test_challenge_response_round_trip(registry, device_key):
    key, public_b64 = device_key
    registry.add("phone-1", "Pixel", public_b64)
    auth = AuthManager(registry, ttl=30)
    challenge = auth.new_challenge()
    signature = sign_with(key, f"openpc-auth-v1:{challenge.nonce}".encode())
    record = auth.verify("phone-1", challenge.nonce, signature)
    assert record is not None
    assert record.device_id == "phone-1"
    assert record.last_seen_at is not None


def test_nonce_is_single_use(registry, device_key):
    key, public_b64 = device_key
    registry.add("phone-1", "Pixel", public_b64)
    auth = AuthManager(registry, ttl=30)
    challenge = auth.new_challenge()
    signature = sign_with(key, f"openpc-auth-v1:{challenge.nonce}".encode())
    assert auth.verify("phone-1", challenge.nonce, signature) is not None
    assert auth.verify("phone-1", challenge.nonce, signature) is None


def test_wrong_key_unknown_device_and_expired_nonce_are_rejected(registry, device_key):
    key, public_b64 = device_key
    other_key, other_public = new_device_keypair()
    registry.add("phone-1", "Pixel", public_b64)
    registry.add("phone-2", "Other", other_public)
    auth = AuthManager(registry, ttl=0.01)

    challenge = auth.new_challenge()
    forged = sign_with(other_key, f"openpc-auth-v1:{challenge.nonce}".encode())
    assert auth.verify("phone-1", challenge.nonce, forged) is None

    challenge = auth.new_challenge()
    good = sign_with(key, f"openpc-auth-v1:{challenge.nonce}".encode())
    assert auth.verify("ghost", challenge.nonce, good) is None

    challenge = auth.new_challenge()
    good = sign_with(key, f"openpc-auth-v1:{challenge.nonce}".encode())
    time.sleep(0.03)
    assert auth.verify("phone-1", challenge.nonce, good) is None


def test_tls_identity_is_stable_and_private(tmp_path):
    cert = tmp_path / "tls" / "engine.crt"
    key = tmp_path / "tls" / "engine.key"
    first = ensure_tls_identity(cert, key)
    second = ensure_tls_identity(cert, key)
    assert first == second
    assert certificate_fingerprint(cert) == first
    assert len(first) == 64
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert ":" in format_fingerprint(first)
    assert format_fingerprint(first).replace(":", "").lower() == first
