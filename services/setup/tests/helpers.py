"""Shared test setup for the setup service.

Deliberately NOT a conftest.py: pytest imports conftest modules by basename, and a
second ``conftest`` in this repository collides with the provisioning suite's
(which imports ``FakeMdns`` from it). A plain module with functions cannot.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from amaara_setup.license import generate_manifest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def signing_keys() -> tuple[bytes, bytes]:
    """A throwaway keypair: the real private half never leaves the signing host."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def issued(signing: tuple[bytes, bytes]):
    """Three keys plus the manifest that signs them."""
    private_pem, _ = signing
    return generate_manifest(private_pem, batch="BATCH-TEST", tier="standard", key_count=3)
