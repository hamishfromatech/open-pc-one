"""``openpc-engine`` command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from pathlib import Path

from . import __version__
from .auth import DeviceRegistry, PairingCodeStore, ensure_tls_identity, format_fingerprint
from .config import EngineConfig
from .logging_setup import configure
from .models import ManifestError, load_default, load_manifest
from .rpc_process import EngineError, EngineProcess


def _config_from_args(args: argparse.Namespace) -> EngineConfig:
    config = EngineConfig()
    if getattr(args, "state_dir", None):
        root = Path(args.state_dir)
        config = EngineConfig(
            state_dir=root,
            session_root=root / "sessions",
            devices_file=root / "devices.json",
            tls_cert=root / "tls" / "engine.crt",
            tls_key=root / "tls" / "engine.key",
        )
    if getattr(args, "bind", None):
        host, _, port = args.bind.rpartition(":")
        if host:
            config.bind_host = host
        if port:
            config.bind_port = int(port)
    if getattr(args, "engine_command", None):
        config.engine_command = tuple(shlex.split(args.engine_command))
    elif getattr(args, "engine_bin", None):
        config.engine_command = (args.engine_bin,)
    if getattr(args, "agent_cwd", None):
        config.agent_cwd = Path(args.agent_cwd)
    if getattr(args, "idle_timeout", None):
        config.idle_timeout = float(args.idle_timeout)
    if getattr(args, "model_manifest", None):
        config.model_manifest = Path(args.model_manifest)
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openpc-engine", description=__doc__)
    parser.add_argument("--version", action="version", version=f"openpc-engine {__version__}")
    parser.add_argument("--state-dir", help="state root (default /var/lib/openpc, ~/.openpc as non-root)")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-format", choices=["json", "text"], default="json")
    parser.add_argument(
        "--model-manifest",
        default=None,
        help="local model manifest (default: $OPENPC_MODEL_MANIFEST, /etc/openpc/models.json, ./config/models.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="run the WSS edge")
    serve_p.add_argument("--bind", default=None, help="host:port (default 0.0.0.0:8095)")
    serve_p.add_argument("--engine-bin", default=None, help="path to a-coder-cli")
    serve_p.add_argument("--engine-command", default=None, help="full argv prefix, e.g. 'node /opt/x/dist/cli.js'")
    serve_p.add_argument("--agent-cwd", default=None, help="cwd the agent drives (default /home/openpc)")
    serve_p.add_argument("--idle-timeout", default=None, help="seconds before an unwatched session is reaped")

    doctor_p = sub.add_parser("doctor", help="check identity, engine handshake and devices")
    doctor_p.add_argument("--engine-bin", default=None)
    doctor_p.add_argument("--engine-command", default=None)

    fp_p = sub.add_parser("fingerprint", help="print the TLS pin clients must trust")
    fp_p.add_argument("--formatted", action="store_true", help="print as AA:BB:...")

    pair_p = sub.add_parser("pair-code", help="mint a one-time pairing code for a desktop client")
    pair_p.add_argument("--ttl", type=float, default=600.0)

    devices_p = sub.add_parser("devices", help="inspect or revoke registered devices")
    devices_sub = devices_p.add_subparsers(dest="devices_command", required=True)
    devices_sub.add_parser("list")
    rm = devices_sub.add_parser("remove")
    rm.add_argument("device_id")

    return parser


async def _doctor(config: EngineConfig) -> int:
    config.ensure_dirs()
    try:
        manifest = (
            load_manifest(config.model_manifest)
            if config.model_manifest is not None
            else load_default()
        )
    except ManifestError as exc:
        print(f"model          FAIL {exc}", file=sys.stderr)
        return 1
    if manifest is None:
        print("local model    -   (no manifest; set OPENPC_MODEL_MANIFEST)")
    else:
        spec = manifest.active
        print(f"local model    {spec.display_name} {spec.quant} ctx={spec.context_length} {spec.repo_id}")
        print(f"licence        {spec.licence.status.upper()} ({spec.licence.id})")
        if spec.licence.status != "verified":
            print("               retail image builds will refuse this model")
        print(f"manifest       {manifest.path}")

    pin = ensure_tls_identity(config.tls_cert, config.tls_key)
    print(f"tls            ok  {config.tls_cert}")
    print(f"pin            {format_fingerprint(pin)}")
    registry = DeviceRegistry(config.devices_file)
    print(f"devices        {len(registry)} registered")
    proc = EngineProcess(config, session_id="probe")
    try:
        info = await proc.start(timeout=config.engine_start_timeout)
        print(f"engine         ok  {info.get('version')} (contract {info.get('contract')})")
        print(f"capabilities   {', '.join(info.get('capabilities', [])) or '-'}")
        state = await proc.command({"type": "get_state"}, timeout=30.0)
        model = state.get("model") or {}
        print(f"session        {state.get('sessionId')} cwd={state.get('cwd')}")
        print(f"session model  {model.get('provider')}/{model.get('id')}")
        models = await proc.command({"type": "get_available_models"}, timeout=30.0)
        print(f"models         {len(models.get('models', []))} available")
        return 0
    except EngineError as exc:
        print(f"engine         FAIL {exc}", file=sys.stderr)
        return 1
    finally:
        await proc.stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure(args.log_level, json_output=args.log_format == "json")
    config = _config_from_args(args)

    if args.command == "serve":
        from .ws_server import serve_forever

        try:
            asyncio.run(serve_forever(config))
        except KeyboardInterrupt:
            return 0
        return 0

    if args.command == "doctor":
        return asyncio.run(_doctor(config))

    if args.command == "fingerprint":
        config.ensure_dirs()
        pin = ensure_tls_identity(config.tls_cert, config.tls_key)
        print(format_fingerprint(pin) if args.formatted else pin)
        return 0

    if args.command == "pair-code":
        # Shared with the running server through pairing.json, so a code minted
        # over SSH/Tailscale works against the live edge without a restart.
        store = PairingCodeStore(ttl=args.ttl, path=config.pairing_file)
        code, expires_at = store.mint()
        print(json.dumps({"code": code, "expiresAt": expires_at, "ttl": args.ttl}))
        return 0

    if args.command == "devices":
        registry = DeviceRegistry(config.devices_file)
        if args.devices_command == "list":
            for record in registry.list():
                print(
                    json.dumps(
                        {
                            "deviceId": record.device_id,
                            "name": record.name,
                            "kind": record.kind,
                            "addedAt": record.added_at,
                            "lastSeenAt": record.last_seen_at,
                        }
                    )
                )
            return 0
        if args.devices_command == "remove":
            removed = registry.remove(args.device_id)
            print("removed" if removed else "not found", file=sys.stdout if removed else sys.stderr)
            return 0 if removed else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
