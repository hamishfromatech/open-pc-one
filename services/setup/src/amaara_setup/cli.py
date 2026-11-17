"""``amaara-setup`` — the first-boot UI, plus the tools for our side of the desk.

The appliance only ever *verifies* licenses. ``generate-license`` and ``keygen``
exist here so the whole flow can be exercised on one machine, but they are the
signing host's tools: the private key must never be on a device we ship.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import __version__, catalog
from .apply import apply_profiles
from .license import LicenseError, generate_manifest, load_manifest, redeem, verify_manifest
from .state import SetupState
from .web import SetupApp, serve

DEFAULT_STATE_DIR = Path("/var/lib/amaara/setup") if os.geteuid() == 0 else Path.home() / ".amaara" / "setup"
DEFAULT_MANIFEST = Path("/etc/amaara/license.json")
DEFAULT_PUBKEY = Path("/etc/amaara/license.pub")


def _state_path(args: argparse.Namespace) -> Path:
    return Path(args.state_dir) / "state.json"


def cmd_serve(args: argparse.Namespace) -> int:
    host, _, port = args.bind.rpartition(":")
    app = SetupApp(
        _state_path(args),
        Path(args.license_manifest) if args.license_manifest else None,
        Path(args.license_pubkey) if args.license_pubkey else None,
        dry_run=args.dry_run,
        allow_poweroff=not args.no_poweroff,
    )
    state = app.state
    print(
        f"amaara-setup {__version__} on {host or '127.0.0.1'}:{port or 8099} "
        f"phase={state.phase} device={state.device_id}",
        flush=True,
    )
    serve(app, host or "127.0.0.1", int(port or 8099))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = SetupState.load(_state_path(args))
    print(json.dumps(state.public(), indent=2))
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    print(json.dumps([p.public() for p in catalog.CATALOG], indent=2))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    """Development escape hatch: do not leave a box stuck on the setup screen."""
    state = SetupState.load(_state_path(args))
    if state.phase != "complete":
        if state.phase != "installing":
            state.phase = "installing"
        state.move("complete")
    state.save(_state_path(args))
    print(json.dumps(state.public(), indent=2))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    profiles = catalog.resolve(args.profile or catalog.defaults())
    result = apply_profiles(profiles, dry_run=args.dry_run)
    print(json.dumps(result.public(), indent=2))
    return 0 if result.ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Check a key against a manifest, exactly as the appliance will."""
    manifest = load_manifest(Path(args.manifest))
    verify_manifest(manifest, Path(args.public_key).read_bytes())
    payload = redeem(args.key, manifest, Path(args.public_key).read_bytes())
    print(f"valid: batch={payload.batch} tier={payload.tier} seats={payload.seats}")
    return 0


def cmd_keygen(args: argparse.Namespace) -> int:
    private = Ed25519PrivateKey.generate()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "license.private.pem").write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(out / "license.private.pem", 0o600)
    (out / "license.pub").write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    print(f"wrote {out / 'license.private.pem'} (0600, signing host only)")
    print(f"wrote {out / 'license.pub'} (bake this into the image)")
    return 0


def cmd_generate_license(args: argparse.Namespace) -> int:
    manifest, keys = generate_manifest(
        Path(args.private_key).read_bytes(),
        batch=args.batch,
        tier=args.tier,
        key_count=args.count,
        seats=args.seats,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"manifest-{args.batch}.json").write_text(json.dumps(manifest.to_json(), indent=2))
    keys_file = out / f"keys-{args.batch}.txt"
    keys_file.write_text("\n".join(keys) + "\n")
    os.chmod(keys_file, 0o600)
    print(f"wrote {out / f'manifest-{args.batch}.json'}")
    print(f"wrote {keys_file} ({len(keys)} keys, 0600)")
    print(f"first key: {keys[0]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amaara-setup", description=__doc__)
    parser.add_argument("--version", action="version", version=f"amaara-setup {__version__}")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="run the first-boot UI")
    serve_p.add_argument("--bind", default="127.0.0.1:8099")
    serve_p.add_argument("--license-manifest", default=str(DEFAULT_MANIFEST))
    serve_p.add_argument("--license-pubkey", default=str(DEFAULT_PUBKEY))
    serve_p.add_argument("--dry-run", action="store_true", help="show what would be installed")
    serve_p.add_argument(
        "--no-poweroff",
        action="store_true",
        help="never call systemctl poweroff (containers, CI)",
    )
    serve_p.set_defaults(func=cmd_serve)

    sub.add_parser("status", help="print the setup state").set_defaults(func=cmd_status)
    sub.add_parser("catalog", help="print the installable profiles").set_defaults(func=cmd_catalog)
    sub.add_parser("complete", help="mark setup done (development)").set_defaults(func=cmd_complete)

    apply_p = sub.add_parser("apply", help="apply profiles without the UI")
    apply_p.add_argument("--profile", action="append")
    apply_p.add_argument("--dry-run", action="store_true")
    apply_p.set_defaults(func=cmd_apply)

    keygen_p = sub.add_parser("keygen", help="mint a license signing keypair (our side)")
    keygen_p.add_argument("--out", required=True)
    keygen_p.set_defaults(func=cmd_keygen)

    gen_p = sub.add_parser("generate-license", help="mint keys + a signed manifest (our side)")
    gen_p.add_argument("--private-key", required=True)
    gen_p.add_argument("--batch", required=True)
    gen_p.add_argument("--tier", default="standard")
    gen_p.add_argument("--count", type=int, default=10)
    gen_p.add_argument("--seats", type=int, default=1)
    gen_p.add_argument("--out", required=True)
    gen_p.set_defaults(func=cmd_generate_license)

    verify_p = sub.add_parser("verify", help="check a key against a manifest")
    verify_p.add_argument("--key", required=True)
    verify_p.add_argument("--manifest", required=True)
    verify_p.add_argument("--public-key", required=True)
    verify_p.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LicenseError as exc:
        print(f"amaara-setup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
