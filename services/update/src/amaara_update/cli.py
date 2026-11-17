"""The update client: check, verify, download, hand to ota-update.

Order matters and is the whole design:

1. read the feed config and the public key — no key, no update, ever;
2. fetch the manifest, verify the signature *before* looking at the version;
3. compare versions; nothing newer means nothing downloaded;
4. download to a staging directory, hashing while writing;
5. verify the hash against the signed manifest;
6. hand the file to ``ota-update``, which writes the inactive slot and arms the
   three-try boot.

Every failure exits non-zero with one line on stderr, because this runs from a
timer as well as from a person.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .manifest import Manifest, ManifestError, is_newer, load, read_document, sign, verify

DEFAULT_CONFIG = Path("/etc/amaara/update.conf")
DEFAULT_PUBKEY = Path("/etc/amaara/update.pub")
DEFAULT_STATE = Path("/var/lib/amaara/update")
DEFAULT_STAGING = Path("/var/lib/amaara/update/staging")
OTA_UPDATE = "/opt/openpc/bin/ota-update"
OS_RELEASE = Path("/etc/os-release")

EXIT_OK = 0
EXIT_UP_TO_DATE = 0
EXIT_REFUSED = 2  # unsafe or unverifiable feed: do not retry a human away
EXIT_TRANSIENT = 3  # network trouble: a later attempt may succeed


@dataclass(slots=True)
class Config:
    feed_url: str
    channel: str = "pilot"
    auto_apply: bool = False

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> Config:
        values: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip('"')
        except OSError:
            pass
        return cls(
            feed_url=values.get("FEED_URL", ""),
            channel=values.get("CHANNEL", "pilot"),
            auto_apply=values.get("AUTO_APPLY", "no").lower() in ("yes", "true", "1"),
        )


def current_version() -> str:
    """The AmaaraOS version, or "" when this root is not ours.

    An update client that reads any os-release it finds can be fooled: Debian's
    ``VERSION="12 (bookworm)"`` compares as 12 and "wins" against every real
    AmaaraOS release, so the box would sit there believing it is up to date. The
    ID is checked first, and a foreign root is refused rather than compared.
    """
    fields: dict[str, str] = {}
    try:
        for line in OS_RELEASE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                fields[key.strip()] = value.strip().strip('"')
    except OSError:
        return ""
    if fields.get("ID") != "amaaraos":
        return ""
    return fields.get("VERSION", "")


def fetch(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"amaara-update/{__version__}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed scheme by config
        return response.read()


def resolve_manifest(config: Config, key_path: Path) -> Manifest:
    if not config.feed_url:
        raise ManifestError("no FEED_URL configured in /etc/amaara/update.conf")
    if not key_path.exists():
        raise ManifestError(f"no update public key at {key_path}; refusing to trust the feed")
    try:
        document = json.loads(fetch(f"{config.feed_url.rstrip('/')}/manifest.json").decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ConnectionError(f"cannot read the feed: {exc}") from exc
    manifest, signature = load(document)
    verify(manifest, signature, key_path.read_bytes())
    if manifest.channel != config.channel:
        raise ManifestError(
            f"feed is channel {manifest.channel!r}, this box tracks {config.channel!r}"
        )
    return manifest


def download(manifest: Manifest, staging: Path) -> Path:
    """Fetch the image, hashing as it arrives, and refuse a mismatch."""
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / f"amaaraos-{manifest.version}.raw"
    digest = hashlib.sha256()
    written = 0
    with urllib.request.urlopen(manifest.image.url, timeout=60) as response:  # noqa: S310
        with open(target, "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                handle.write(chunk)
                written += len(chunk)
    if digest.hexdigest() != manifest.image.sha256:
        target.unlink(missing_ok=True)
        raise ManifestError("downloaded image does not match the signed hash")
    if manifest.image.size and written != manifest.image.size:
        target.unlink(missing_ok=True)
        raise ManifestError(f"downloaded {written} bytes, manifest says {manifest.image.size}")
    return target


def record(state: Path, manifest: Manifest | None, status: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    payload = {
        "checkedAt": _now(),
        "status": status,
        "version": manifest.version if manifest else None,
        "channel": manifest.channel if manifest else None,
    }
    (state / "last-check.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()


# --- commands --------------------------------------------------------------- #
def cmd_check(config: Config, key_path: Path, state: Path) -> int:
    try:
        manifest = resolve_manifest(config, key_path)
    except ConnectionError as exc:
        print(f"amaara-update: {exc}", file=sys.stderr)
        return EXIT_TRANSIENT
    except ManifestError as exc:
        print(f"amaara-update: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    current = current_version()
    if not current:
        print("amaara-update: this root is not AmaaraOS (no ID=amaaraos); refusing to compare",
              file=sys.stderr)
        return EXIT_REFUSED
    newer = is_newer(manifest.version, current)
    record(state, manifest, "update-available" if newer else "up-to-date")
    if not newer:
        print(f"amaara-update: {current} is current (feed offers {manifest.version})")
        return EXIT_UP_TO_DATE
    print(f"amaara-update: {manifest.version} is available (this box runs {current})")
    if manifest.notes:
        print(f"  {manifest.notes}")
    return EXIT_OK


def cmd_apply(config: Config, key_path: Path, state: Path, staging: Path, dry_run: bool) -> int:
    try:
        manifest = resolve_manifest(config, key_path)
    except ConnectionError as exc:
        print(f"amaara-update: {exc}", file=sys.stderr)
        return EXIT_TRANSIENT
    except ManifestError as exc:
        print(f"amaara-update: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    current = current_version()
    if not current:
        print("amaara-update: this root is not AmaaraOS (no ID=amaaraos); refusing to update",
              file=sys.stderr)
        return EXIT_REFUSED
    if not is_newer(manifest.version, current):
        print(f"amaara-update: nothing to do, {current} is current")
        return EXIT_UP_TO_DATE

    try:
        image = download(manifest, staging)
    except (ConnectionError, urllib.error.URLError, TimeoutError) as exc:
        print(f"amaara-update: download failed: {exc}", file=sys.stderr)
        return EXIT_TRANSIENT
    except ManifestError as exc:
        print(f"amaara-update: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"amaara-update: verified {image} ({image.stat().st_size // (1024 * 1024)} MiB)")
    if dry_run:
        print("amaara-update: dry run, not writing a slot")
        return EXIT_OK

    ota = shutil.which(OTA_UPDATE) or OTA_UPDATE
    if not Path(ota).exists():
        print(f"amaara-update: {ota} is missing, cannot write the inactive slot", file=sys.stderr)
        return EXIT_REFUSED
    completed = subprocess.run([ota, str(image)], check=False)
    record(state, manifest, "applied" if completed.returncode == 0 else "failed")
    return EXIT_OK if completed.returncode == 0 else EXIT_REFUSED


def cmd_status(state: Path) -> int:
    print(f"version   {current_version()}")
    last = state / "last-check.json"
    if last.exists():
        print(f"last check {last.read_text(encoding='utf-8').strip()}")
    else:
        print("last check  never")
    return EXIT_OK


def cmd_sign(document: dict, private_key_path: Path) -> int:
    """Signing-host only: wrap a manifest body in a signature."""
    from .manifest import Image as I
    from .manifest import Manifest as M

    key = Path(private_key_path).read_bytes()
    manifest = M(
        version=str(document["version"]),
        channel=str(document.get("channel", "pilot")),
        released=str(document["released"]),
        image=I(**document["image"]),
        notes=str(document.get("notes", "")),
    )
    print(json.dumps(sign(manifest, key), indent=2))
    return EXIT_OK


def cmd_keygen(out: Path) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    out.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    (out / "update.private.pem").write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(out / "update.private.pem", 0o600)
    (out / "update.pub").write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    print(f"wrote {out / 'update.private.pem'} (0600, signing host only)")
    print(f"wrote {out / 'update.pub'} (bake into the image)")
    return EXIT_OK


def cmd_verify(manifest_path: Path, key_path: Path) -> int:
    try:
        manifest, signature = load(read_document(manifest_path))
        verify(manifest, signature, key_path.read_bytes())
    except ManifestError as exc:
        print(f"amaara-update: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"ok: {manifest.version} ({manifest.channel}) {manifest.image.sha256[:16]}…")
    return EXIT_OK


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="amaara-update",
        description="Check a signed update feed and write a new root slot.",
    )
    parser.add_argument("--version", action="version", version=f"amaara-update {__version__}")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--pubkey", default=str(DEFAULT_PUBKEY))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE))
    parser.add_argument("--staging", default=str(DEFAULT_STAGING))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="is there a newer signed image?")
    apply_p = sub.add_parser("apply", help="download, verify and write the inactive slot")
    apply_p.add_argument("--dry-run", action="store_true", help="verify and stop before writing")
    sub.add_parser("status", help="current version and the last check")

    sign_p = sub.add_parser("sign", help="signing host: sign a manifest body")
    sign_p.add_argument("--private-key", required=True)
    sign_p.add_argument("--manifest", required=True, help="unsigned manifest body (JSON)")

    keygen_p = sub.add_parser("keygen", help="signing host: mint the update keypair")
    keygen_p.add_argument("--out", required=True)

    verify_p = sub.add_parser("verify", help="check a signed manifest document")
    verify_p.add_argument("--manifest", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.load(Path(args.config))
    key = Path(args.pubkey)
    state = Path(args.state_dir)

    if args.command == "check":
        return cmd_check(config, key, state)
    if args.command == "apply":
        return cmd_apply(config, key, state, Path(args.staging), args.dry_run)
    if args.command == "status":
        return cmd_status(state)
    if args.command == "sign":
        document = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        return cmd_sign(document, Path(args.private_key))
    if args.command == "keygen":
        return cmd_keygen(Path(args.out))
    if args.command == "verify":
        return cmd_verify(Path(args.manifest), key)
    return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
