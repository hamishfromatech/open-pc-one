"""Signed update manifests.

An appliance that updates itself is an appliance that can be told to install
anything, so the feed is signed and the client fails closed: no public key, no
signature, or a hash that does not match means *nothing happens* and the reason is
printed. The signature covers the whole manifest except itself, in a canonical
form (sorted keys, no whitespace), so a manifest cannot be edited between signing
and use.

The private key lives on the signing host. `amaara-update sign` exists for that
host only; it is not installed in the image.
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class ManifestError(ValueError):
    """The feed is not acceptable. Never recoverable by retrying."""


@dataclass(slots=True)
class Image:
    url: str
    sha256: str
    size: int = 0


@dataclass(slots=True)
class Manifest:
    version: str
    channel: str
    released: str
    image: Image
    notes: str = ""
    #: Anything the client does not understand is dropped, not trusted.
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("extra", None)
        # Notes are advisory: signed, but never load-bearing.
        body["image"] = asdict(self.image)
        return body

    def canonical(self) -> bytes:
        return json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(manifest: Manifest, private_key_pem: bytes) -> dict[str, Any]:
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ManifestError("update manifests must be signed with an Ed25519 key")
    signature = base64.b64encode(key.sign(manifest.canonical())).decode("ascii")
    document = {"manifest": manifest.payload(), "signature": signature}
    return document


def load(document: dict[str, Any]) -> tuple[Manifest, str]:
    """Parse a manifest document, keeping the signature alongside it."""
    try:
        body = document["manifest"]
        signature = document["signature"]
        image = Image(**body["image"])
        manifest = Manifest(
            version=str(body["version"]),
            channel=str(body["channel"]),
            released=str(body["released"]),
            image=image,
            notes=str(body.get("notes", "")),
        )
    except (KeyError, TypeError) as exc:
        raise ManifestError(f"manifest is malformed: {exc}") from exc
    return manifest, str(signature)


def verify(manifest: Manifest, signature: str, public_key_pem: bytes) -> None:
    key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ManifestError("the feed key is not Ed25519")
    try:
        key.verify(base64.b64decode(signature, validate=True), manifest.canonical())
    except (InvalidSignature, ValueError) as exc:
        raise ManifestError("manifest signature is not ours") from exc


def read_document(path: Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc


def version_tuple(text: str) -> tuple[int, ...]:
    """Versions are X.Y.Z; anything else sorts as 0.0.0 rather than raising.

    The comparison decides whether to offer an update, not whether a box boots, so
    a weird version string must not be able to break the client.
    """
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)
