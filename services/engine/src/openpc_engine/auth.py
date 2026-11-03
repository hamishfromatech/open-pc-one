"""Device identity: TLS pinning material, device key registry, pairing codes.

Threat model
------------
The appliance listens on the LAN only (no inbound WAN ports; remote access is
Tailscale in v1). Two things must be true before a client may `prompt`:

1. **The client knows it is talking to the real box.** The TLS certificate is
   generated on first boot and its SHA-256 fingerprint is delivered to the phone
   during BLE provisioning - the same GATT session that hands over the WiFi
   passphrase. The phone pins that fingerprint, so a hostile LAN peer cannot
   impersonate the appliance. (Certificates are self-signed: there is no CA on
   an appliance, and a public CA cannot issue for `openpc.local`.)

2. **The appliance knows the client.** Each phone/desktop generates an Ed25519
   keypair on its own device; the private key never leaves it. During
   provisioning (mobile) or with a one-time pairing code (desktop) it registers
   the public half. Every WebSocket connection then proves possession of the
   private key by signing a fresh single-use nonce.

No shared secrets are stored on either side, and nothing in this module is
logged: pairing codes and credentials are registered with the redactor.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import string
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from .logging_setup import register_secret

log = logging.getLogger("openpc.auth")

AUTH_PREFIX = "openpc-auth-v1:"
#: Ambiguous glyphs (0/O, 1/I/L) are excluded so a code read off a phone screen
#: and typed on a keyboard cannot be misread.
CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1L")


# ---------------------------------------------------------------------------
# TLS identity
# ---------------------------------------------------------------------------


def _generate_self_signed(key_path: Path, cert_path: Path) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    hostname = socket.gethostname().split(".")[0] or "openpc"
    san = [
        x509.DNSName("openpc.local"),
        x509.DNSName(f"{hostname}.local"),
        x509.DNSName(hostname),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "The A-Tech Corporation PTY LTD"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Open-PC One"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, algorithm=None)  # Ed25519 signs without a separate digest
    )

    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.touch(mode=0o600, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(key_path, 0o600)
    os.chmod(cert_path, 0o644)


def ensure_tls_identity(cert_path: Path, key_path: Path) -> str:
    """Create the appliance keypair+certificate if missing; return the pin.

    The returned value is the lowercase hex SHA-256 of the DER certificate.
    It is stable for the life of the installation and is what clients pin.
    """
    if not cert_path.exists() or not key_path.exists():
        log.info("generating appliance TLS identity", extra={"detail": str(cert_path)})
        _generate_self_signed(key_path, cert_path)
    return certificate_fingerprint(cert_path)


def certificate_fingerprint(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def format_fingerprint(fingerprint: str) -> str:
    """Group a hex fingerprint for display: ``AB:CD:...``."""
    upper = fingerprint.upper()
    return ":".join(upper[i : i + 2] for i in range(0, len(upper), 2))


# ---------------------------------------------------------------------------
# Device registry
# ---------------------------------------------------------------------------


class RegistryError(RuntimeError):
    pass


@dataclass(slots=True)
class DeviceRecord:
    device_id: str
    name: str
    public_key: str  # base64 raw Ed25519 (32 bytes)
    kind: str = "mobile"  # "mobile" | "desktop"
    added_at: float = field(default_factory=time.time)
    last_seen_at: float | None = None

    def public_key_object(self) -> ed25519.Ed25519PublicKey:
        try:
            raw = base64.b64decode(self.public_key, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RegistryError(f"device {self.device_id} has an unreadable key") from exc
        if len(raw) != 32:
            raise RegistryError(f"device {self.device_id} key is not 32 bytes")
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


class DeviceRegistry:
    """Registered client public keys, persisted as root-only JSON."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._devices: dict[str, DeviceRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise RegistryError(f"cannot read device registry: {exc}") from exc
        for item in raw.get("devices", []):
            try:
                record = DeviceRecord(**item)
            except TypeError:
                log.warning("skipping malformed device record")
                continue
            self._devices[record.device_id] = record

    def _save(self) -> None:
        payload = json.dumps(
            {"version": 1, "devices": [asdict(d) for d in self._devices.values()]},
            indent=2,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # Create with restrictive permissions before any bytes are written.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    # -- operations --------------------------------------------------------

    def add(self, device_id: str, name: str, public_key_b64: str, kind: str = "mobile") -> DeviceRecord:
        try:
            raw = base64.b64decode(public_key_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RegistryError("public_key must be base64") from exc
        if len(raw) != 32:
            raise RegistryError("public_key must be a raw 32-byte Ed25519 key")
        if kind not in ("mobile", "desktop"):
            raise RegistryError("kind must be 'mobile' or 'desktop'")
        record = DeviceRecord(
            device_id=device_id.strip() or f"dev-{secrets.token_hex(4)}",
            name=name.strip() or "Unnamed device",
            public_key=public_key_b64,
            kind=kind,
        )
        self._devices[record.device_id] = record
        self._save()
        return record

    def remove(self, device_id: str) -> bool:
        if self._devices.pop(device_id, None) is None:
            return False
        self._save()
        return True

    def get(self, device_id: str) -> DeviceRecord | None:
        return self._devices.get(device_id)

    def list(self) -> list[DeviceRecord]:
        return list(self._devices.values())

    def touch(self, device_id: str) -> None:
        record = self._devices.get(device_id)
        if record is None:
            return
        record.last_seen_at = time.time()
        try:
            self._save()
        except OSError:  # read-only root: last_seen is best-effort
            log.debug("could not persist last_seen_at")

    def __len__(self) -> int:
        return len(self._devices)


# ---------------------------------------------------------------------------
# Pairing codes (desktop clients; phones pair during BLE provisioning)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PairingCode:
    code: str
    created_at: float
    expires_at: float
    used: bool = False


class PairingCodeStore:
    """Short-lived, single-use pairing codes.

    With ``path`` the codes survive a service restart and can be minted by the
    ``openpc-engine pair-code`` CLI; without it they are memory-only. Only a
    hash-free plaintext code is stored, at mode 0600, and it is deleted the
    moment it is redeemed - that is the whole point of a one-time code.
    """

    def __init__(self, ttl: float = 600.0, length: int = 8, path: Path | None = None) -> None:
        self.ttl = ttl
        self.length = length
        self.path = Path(path) if path is not None else None
        self._codes: dict[str, _PairingCode] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("ignoring unreadable pairing store")
            return
        for code, item in raw.get("codes", {}).items():
            try:
                self._codes[code] = _PairingCode(**item)
                register_secret(code)
            except TypeError:
                continue

    def _save(self) -> None:
        if self.path is None:
            return
        payload = json.dumps({"version": 1, "codes": {c: asdict(e) for c, e in self._codes.items()}})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    # -- operations --------------------------------------------------------

    def mint(self) -> tuple[str, float]:
        self._prune()
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(self.length))
        now = time.time()
        entry = _PairingCode(code=code, created_at=now, expires_at=now + self.ttl)
        self._codes[code] = entry
        register_secret(code)
        self._save()
        return code, entry.expires_at

    def redeem(self, code: str) -> bool:
        """Burn a code. Comparison is constant-time and case-insensitive."""
        self._prune()
        candidate = (code or "").strip().upper()
        if not candidate:
            return False
        matched: str | None = None
        for stored in self._codes:
            if hmac.compare_digest(stored, candidate):
                matched = stored
        if matched is None:
            return False
        entry = self._codes[matched]
        if entry.used or time.time() > entry.expires_at:
            return False
        del self._codes[matched]
        self._save()
        return True

    def _prune(self) -> None:
        now = time.time()
        expired = [c for c, e in self._codes.items() if e.used or now > e.expires_at]
        for code in expired:
            self._codes.pop(code, None)
        if expired:
            self._save()

    def __len__(self) -> int:
        self._prune()
        return len(self._codes)


# ---------------------------------------------------------------------------
# Challenge / response
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Challenge:
    nonce: str
    expires_at: float


class AuthManager:
    """Single-use nonce challenge verifier."""

    def __init__(self, registry: DeviceRegistry, ttl: float = 30.0) -> None:
        self.registry = registry
        self.ttl = ttl
        self._challenges: dict[str, Challenge] = {}

    def new_challenge(self) -> Challenge:
        self._prune()
        nonce = secrets.token_urlsafe(32)
        challenge = Challenge(nonce=nonce, expires_at=time.time() + self.ttl)
        self._challenges[nonce] = challenge
        return challenge

    @staticmethod
    def signing_payload(nonce: str) -> bytes:
        return f"{AUTH_PREFIX}{nonce}".encode()

    def verify(self, device_id: str, nonce: str, signature_b64: str) -> DeviceRecord | None:
        """Verify a signature over a live nonce. Nonces are single use."""
        challenge = self._challenges.pop(nonce, None)
        if challenge is None:
            return None
        if time.time() > challenge.expires_at:
            return None
        record = self.registry.get(device_id)
        if record is None:
            return None
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (binascii.Error, ValueError):
            return None
        try:
            record.public_key_object().verify(signature, self.signing_payload(nonce))
        except Exception:
            # cryptography raises InvalidSignature; treat any failure as "no".
            return None
        self.registry.touch(device_id)
        return record

    def _prune(self) -> None:
        now = time.time()
        for nonce in [n for n, c in self._challenges.items() if now > c.expires_at]:
            self._challenges.pop(nonce, None)

    def __len__(self) -> int:
        self._prune()
        return len(self._challenges)


def new_device_keypair() -> tuple[ed25519.Ed25519PrivateKey, str]:
    """Test/tooling helper: a fresh client keypair and its base64 public half."""
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return key, base64.b64encode(raw).decode("ascii")


def sign_with(key: ed25519.Ed25519PrivateKey, payload: bytes) -> str:
    return base64.b64encode(key.sign(payload)).decode("ascii")


__all__ = [
    "AUTH_PREFIX",
    "AuthManager",
    "Challenge",
    "DeviceRecord",
    "DeviceRegistry",
    "PairingCodeStore",
    "RegistryError",
    "certificate_fingerprint",
    "ensure_tls_identity",
    "format_fingerprint",
    "new_device_keypair",
    "sign_with",
]
