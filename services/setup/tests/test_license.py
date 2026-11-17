"""Licenses verify offline, and a key is not a secret we keep.

The interesting failures are the ones a customer would hit: a mistyped key with
an O instead of a zero, a manifest from another product, a manifest someone
edited after signing, and a key that was never issued.
"""

from __future__ import annotations

import json

import pytest
from amaara_setup.license import (
    KEY_LENGTH,
    LicenseError,
    format_key,
    generate_manifest,
    key_hash,
    load_manifest,
    new_key,
    normalise,
    redeem,
    verify_manifest,
)
from helpers import issued as make_issued  # noqa: E402
from helpers import signing_keys as make_keys


def test_a_key_is_five_groups_of_five_and_survives_being_retyped():
    key = new_key()
    assert key.startswith("AMAARA-")
    assert len(key) == len("AMAARA-") + KEY_LENGTH + 4
    # What a human does: lower case, no dashes, and O/0, I/1, L/1 confusion.
    sloppy = key.lower().replace("-", " ").replace("0", "O").replace("1", "I")
    assert normalise(sloppy) == normalise(key)
    assert format_key(sloppy) == key


def test_keys_are_unique(signing_keys):
    keys = {new_key() for _ in range(200)}
    assert len(keys) == 200


def test_issued_key_verifies_against_its_manifest(issued, signing_keys):
    manifest, keys = issued
    _, public_pem = signing_keys
    payload = redeem(keys[0], manifest, public_pem)
    assert payload.batch == "BATCH-TEST"
    assert payload.tier == "standard"


def test_only_the_hash_is_kept(issued, signing_keys):
    manifest, keys = issued
    _, public_pem = signing_keys
    verify_manifest(manifest, public_pem)
    blob = json.dumps(manifest.to_json())
    assert keys[0] not in blob, "the manifest must never contain a usable key"
    assert key_hash(keys[0]) in blob


def test_a_key_from_another_batch_is_rejected(issued, signing_keys):
    manifest, _ = issued
    _, public_pem = signing_keys
    with pytest.raises(LicenseError, match="not issued"):
        redeem(new_key(), manifest, public_pem)


def test_an_edited_manifest_fails_signature_check(issued, signing_keys):
    manifest, keys = issued
    _, public_pem = signing_keys
    manifest.payload.key_hashes.append(key_hash(new_key()))
    with pytest.raises(LicenseError, match="signature"):
        redeem(new_key(), manifest, public_pem)


def test_a_manifest_signed_by_someone_else_is_rejected(issued, signing_keys):
    manifest, keys = issued
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with pytest.raises(LicenseError, match="signature"):
        redeem(keys[0], manifest, other)


def test_manifest_round_trips_through_a_file(tmp_path, issued, signing_keys):
    manifest, keys = issued
    _, public_pem = signing_keys
    path = tmp_path / "license.json"
    path.write_text(json.dumps(manifest.to_json()))

    loaded = load_manifest(path)
    assert redeem(keys[1], loaded, public_pem).batch == "BATCH-TEST"


def test_manifest_signature_does_not_depend_on_hash_order(signing_keys):
    private_pem, public_pem = signing_keys
    manifest, keys = generate_manifest(private_pem, batch="B", tier="t", key_count=3)
    manifest.payload.key_hashes.reverse()  # same set, opposite order
    verify_manifest(manifest, public_pem)
    assert redeem(keys[0], manifest, public_pem).batch == "B"


def test_a_malformed_manifest_is_reported_not_ignored(tmp_path):
    path = tmp_path / "license.json"
    path.write_text('{"payload": {"batch": "x"}, "signature": "nope"}')
    with pytest.raises(LicenseError, match="malformed"):
        load_manifest(path)


@pytest.fixture
def signing_keys():
    return make_keys()


@pytest.fixture
def issued(signing_keys):
    return make_issued(signing_keys)
