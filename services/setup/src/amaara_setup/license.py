"""License keys that verify with no network.

A supplier ships a box to a customer who may have no internet on the first boot,
so activation cannot depend on a call home. The design is deliberately boring:

* a **key** is 25 characters of Crockford base32 in five groups of five. That is
  125 bits of randomness, typed by a human once, and it carries nothing else.
* a **manifest** is the list of key hashes issued for a batch, plus tier and
  seat count, signed with A-Tech's Ed25519 private key. The appliance ships with
  the public half baked in, so it can verify the manifest and the key offline.
* a **key is never stored**, only its SHA-256 hash, in the manifest and on the
  device.

The private key never leaves our signing host. ``generate-manifest`` here is the
tool that runs there; the appliance only ever verifies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford: no I, L, O, U
GROUPS = 5
GROUP_SIZE = 5
KEY_LENGTH = GROUPS * GROUP_SIZE
PREFIX = "AMAARA"
KEY_BYTES = 16  # 128 random bits, rendered as 25 base32 characters


class LicenseError(ValueError):
    """The key or the manifest is not acceptable."""


def normalise(text: str) -> str:
    """Accept what a human types: lower case, spaces, dashes, I/L/O typos."""
    cleaned = "".join(ch for ch in text.upper() if ch not in " -\t\r\n")
    for wrong, right in (("I", "1"), ("L", "1"), ("O", "0"), ("U", "V")):
        cleaned = cleaned.replace(wrong, right)
    return cleaned


def format_key(raw: str) -> str:
    body = normalise(raw)
    if body.startswith(PREFIX):
        body = body[len(PREFIX):]
    if len(body) != KEY_LENGTH:
        raise LicenseError(f"a key is {KEY_LENGTH} characters, got {len(body)}")
    groups = [body[i:i + GROUP_SIZE] for i in range(0, len(body), GROUP_SIZE)]
    return "-".join([PREFIX, *groups])


def new_key() -> str:
    """A fresh key. 125 bits, rendered the only way a human can retype it."""
    value = int.from_bytes(secrets.token_bytes(KEY_BYTES), "big")
    chars = []
    for _ in range(KEY_LENGTH):
        chars.append(ALPHABET[value & 31])
        value >>= 5
    return format_key("".join(reversed(chars)))


def key_hash(key: str) -> str:
    """What goes in the manifest and on the device. The key itself is not stored."""
    return hashlib.sha256(normalise(key).encode("ascii")).hexdigest()


@dataclass(slots=True)
class License:
    batch: str
    tier: str
    seats: int
    issued_at: float
    key_hashes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Manifest:
    """A signed list of issued keys. Shipped by the OEM installer or a repack."""

    payload: License
    signature: str

    def to_json(self) -> dict[str, Any]:
        return {"version": 1, "payload": asdict(self.payload), "signature": self.signature}


def _signing_bytes(payload: License) -> bytes:
    # Sorted, separator-stable JSON: the signature must not depend on dict order
    # or on which machine serialised it.
    body = asdict(payload)
    body["keyHashes"] = sorted(body.pop("key_hashes"))
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate_manifest(
    private_key_pem: bytes, batch: str, tier: str, key_count: int, seats: int = 1
) -> tuple[Manifest, list[str]]:
    """Our side of the desk: mint a batch of keys and the manifest that signs them."""
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise LicenseError("manifest signing needs an Ed25519 key")
    keys = [new_key() for _ in range(key_count)]
    payload = License(
        batch=batch,
        tier=tier,
        seats=seats,
        issued_at=time.time(),
        key_hashes=[key_hash(k) for k in keys],
    )
    signature = base64.b64encode(private_key.sign(_signing_bytes(payload))).decode("ascii")
    return Manifest(payload=payload, signature=signature), keys


def load_manifest(path: Path) -> Manifest:
    raw = json.loads(Path(path).read_text())
    try:
        payload = License(**{k: v for k, v in raw["payload"].items()})
        return Manifest(payload=payload, signature=raw["signature"])
    except (KeyError, TypeError) as exc:
        raise LicenseError(f"manifest is malformed: {exc}") from exc


def verify_manifest(manifest: Manifest, public_key_pem: bytes) -> None:
    """Fail unless the manifest was signed by us."""
    public_key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(public_key, Ed25519PublicKey):
        raise LicenseError("verification needs an Ed25519 public key")
    try:
        signature = base64.b64decode(manifest.signature, validate=True)
        public_key.verify(signature, _signing_bytes(manifest.payload))
    except (InvalidSignature, ValueError) as exc:
        raise LicenseError("manifest signature is not ours") from exc


def redeem(key: str, manifest: Manifest, public_key_pem: bytes) -> License:
    """Verify the manifest, then the key. Constant-time on the hash compare.

    Returns the payload so the caller can record tier/seats; the key is not kept.
    """
    verify_manifest(manifest, public_key_pem)
    candidate = key_hash(key)
    for issued in manifest.payload.key_hashes:
        if hmac.compare_digest(issued, candidate):
            return manifest.payload
    raise LicenseError("this key was not issued for this manifest")
