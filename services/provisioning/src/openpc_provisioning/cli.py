"""``openpc-provision`` command line.

On the appliance this runs as ``openpc-provision.service`` and exits once the box
is online. ``--simulate`` runs the same logic with an in-process transport, which
is how the flow is exercised on a laptop.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from .ble import SimulatedBleTransport, advertising_name
from .mdns import MdnsPublisher
from .service import ApplianceIdentity, ProvisioningService
from .state import ProvisioningState, ProvisioningStateMachine
from .wifi import NmcliBackend


def _machine_id() -> str:
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        if path.exists():
            return path.read_text().strip()
    return hashlib.sha256(str(Path.home()).encode()).hexdigest()


def _local_name() -> str:
    return advertising_name(hashlib.sha256(_machine_id().encode()).hexdigest())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openpc-provision", description=__doc__)
    parser.add_argument("--interface", default="wlan0")
    parser.add_argument("--cert", default="/var/lib/openpc/tls/engine.crt")
    parser.add_argument("--devices", default="/var/lib/openpc/devices.json")
    parser.add_argument("--firmware", default="0.1.0")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use the in-process transport instead of BlueZ (no radio needed)",
    )
    parser.add_argument(
        "--simulate-credentials",
        default=None,
        help="with --simulate: 'SSID:PASSWORD' to provision immediately",
    )
    return parser


def _identity(args: argparse.Namespace) -> ApplianceIdentity:
    fingerprint = ""
    cert = Path(args.cert)
    if cert.exists():
        from openpc_engine.auth import certificate_fingerprint

        fingerprint = certificate_fingerprint(cert)
    return ApplianceIdentity(
        appliance_id=_machine_id()[:16],
        local_name=_local_name(),
        firmware=args.firmware,
        cert_fingerprint=fingerprint,
    )


def _registry(args: argparse.Namespace):
    try:
        from openpc_engine.auth import DeviceRegistry

        return DeviceRegistry(Path(args.devices))
    except Exception:  # engine package not installed (standalone dev run)
        return None


async def _run(args: argparse.Namespace) -> int:
    identity = _identity(args)

    def on_state(state: ProvisioningState, message: str) -> None:
        # The LED service watches state.json; journald keeps the history.
        payload = {"state": state.value, "message": message, "at": time.time()}
        target = Path("/run/openpc/provisioning.json")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload))
        except OSError:
            pass
        logging.getLogger("openpc.provisioning").info(
            "state", extra={"event": state.value, "detail": message}
        )

    machine = ProvisioningStateMachine(on_change=on_state)
    transport = (
        SimulatedBleTransport(identity.local_name)
        if args.simulate
        else _dbus_transport(identity.local_name)
    )
    service = ProvisioningService(
        identity=identity,
        machine=machine,
        wifi=NmcliBackend(interface=args.interface),
        transport=transport,
        registry=_registry(args),
        mdns=MdnsPublisher(port=8095),
        on_state=on_state,
    )

    # Starting the peripheral can fail for reasons that are not faults: a VM has
    # no Bluetooth adapter, bluetoothd may be absent, or the system bus may not
    # be reachable yet. The unit is non-blocking by design ("nothing gates on
    # it"), so log and carry on instead of crash-looping - the box may still
    # come up on the wired network.
    log = logging.getLogger("openpc.provisioning")
    try:
        online = await service.run()
    except Exception as exc:
        log.warning(
            "Bluetooth provisioning unavailable (%s: %s); relying on the wired network",
            type(exc).__name__,
            exc,
        )
        online = False
    if online:
        return 0

    if args.simulate_credentials:
        ssid, _, password = args.simulate_credentials.partition(":")
        from .ble import ProvisioningRequest

        ok = await service.handle_wifi(ProvisioningRequest(ssid=ssid, password=password))
        print(json.dumps(service.status_payload()))
        return 0 if ok else 1

    # Real appliance: stay up until the phone provisions us.
    while True:
        await asyncio.sleep(3600)


def _dbus_transport(local_name: str):
    try:
        from .bluez_dbus import DbusBleTransport
    except ImportError as exc:  # pragma: no cover - Linux-only path
        raise SystemExit(f"BlueZ transport unavailable ({exc}); install python3-dbus-next") from exc
    return DbusBleTransport(local_name)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
