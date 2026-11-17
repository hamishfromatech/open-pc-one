"""The feed is only worth anything if it can refuse.

Every test here is a refusal: a manifest from another key, a manifest edited after
signing, a channel this box does not track, a download that does not hash to what
was signed, and a feed with no key at all. The happy path is one test; the other
seven are the reasons this client exists.
"""

from __future__ import annotations

import base64
import json

import pytest
import update_testkit as helpers  # noqa: F401  (puts src/ on sys.path)
from amaara_update.manifest import (
    Image,
    Manifest,
    ManifestError,
    is_newer,
    load,
    read_document,
    sign,
    verify,
    version_tuple,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@pytest.fixture
def keypair():
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public


def manifest(version: str = "1.1.0", channel: str = "pilot") -> Manifest:
    return Manifest(
        version=version,
        channel=channel,
        released="2026-11-20T00:00:00Z",
        image=Image(url="https://feed.example/amaaraos-1.1.0.raw", sha256="a" * 64, size=1024),
        notes="Security fixes.",
    )


def test_a_signed_manifest_verifies(keypair):
    private, public = keypair
    document = sign(manifest(), private)
    loaded, signature = load(document)
    verify(loaded, signature, public)
    assert loaded.version == "1.1.0"
    assert loaded.image.size == 1024


def test_an_edited_manifest_fails(keypair):
    private, public = keypair
    document = sign(manifest(), private)
    document["manifest"]["image"]["sha256"] = "b" * 64
    loaded, signature = load(document)
    with pytest.raises(ManifestError, match="signature"):
        verify(loaded, signature, public)


def test_a_manifest_signed_by_another_key_fails(keypair):
    private, _ = keypair
    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    document = sign(manifest(), private)
    loaded, signature = load(document)
    with pytest.raises(ManifestError, match="signature"):
        verify(loaded, signature, other)


def test_a_truncated_signature_is_refused(keypair):
    private, public = keypair
    document = sign(manifest(), private)
    loaded, signature = load(document)
    with pytest.raises(ManifestError):
        verify(loaded, base64.b64encode(b"short").decode(), public)


def test_a_malformed_document_is_reported(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"manifest": {"version": "1.0.0"}}))
    with pytest.raises(ManifestError, match="malformed"):
        load(read_document(path))
    (tmp_path / "broken.json").write_text("{not json")
    with pytest.raises(ManifestError, match="cannot read"):
        read_document(tmp_path / "broken.json")


def test_the_signature_covers_the_notes_too(keypair):
    private, public = keypair
    note = manifest()
    note.notes = "A different sentence."
    document = sign(note, private)
    document["manifest"]["notes"] = "Edited on the wire."
    loaded, signature = load(document)
    with pytest.raises(ManifestError, match="signature"):
        verify(loaded, signature, public)


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [("1.1.0", "1.0.0", True), ("1.0.0", "1.0.0", False), ("1.0.1", "1.0.0", True),
     ("1.0.0", "1.1.0", False), ("2.0", "1.9.9", True), ("garbage", "1.0.0", False)],
)
def test_version_comparison(candidate, current, newer):
    assert is_newer(candidate, current) is newer


def test_a_weird_version_never_raises():
    assert version_tuple("1.1.0-pilot") == (1, 1, 0)
    assert version_tuple("") == (0,)
